"""Resolving a transfer whose immediate outcome was never established.

M6 leaves two kinds of unresolved truth behind:

* ``STARTED`` — the attempt was claimed and **may or may not** have reached the
  rail (M6's crash window).
* ``UNKNOWN`` — the request went out and the answer never came back.

M7 resolves both, from evidence, and never by sending anything again.

The one thing this module must never do
---------------------------------------
There is **no path from here to** ``submit_transfer``. Not a retry, not a
fallback, not a "the provider says it never arrived, so resend". An unresolved
operation may already have moved a customer's money; resending is the single
action that turns an ambiguity into a double debit. This module is typed
against a Protocol that has no submission method, imports nothing that submits,
and is asserted by AST walk to call no such thing.

Evidence is appended, never applied over the top of history
-----------------------------------------------------------
M6's ``ProviderExecutionAttempt`` records what *one interaction observed*, and
M7 does not revise it. An attempt that returned ``unknown`` stays ``unknown``
even after a webhook proves the transfer succeeded, because that is what was
observed at the time. What resolves is the **financial transaction**, through
M4/M5's existing services, justified by an appended
``ProviderRecoveryEvidence`` row.

Keeping those apart is what makes the audit trail truthful: "we did not know,
and then were told" reads differently from "it succeeded all along", and only
the first is what happened.

Duplicate deliveries are idempotent, whenever they arrive
---------------------------------------------------------
Rails redeliver: once, twice, ten times, sometimes simultaneously. An identical
event — same provider identity, same normalised contents — resolves money at
most once and every caller is told it was accepted, **whether the deliveries
were sequential or concurrent**. Timing is not part of what an event means.

A conflict is something else entirely: the same event identity carrying
*different* contents. That is not redelivery, and the first receipt is never
overwritten.

Contradiction is escalated, never resolved
------------------------------------------
If definitive evidence disagrees with a settled outcome, this raises. Choosing
a winner would mean either fabricating a reversal or discarding evidence. The
evidence is kept, the terminal truth is untouched, no journal is reversed, and
a human decides. That is an operations and reconciliation concern (M8), not
something to guess at here.

Shape and lock order
--------------------
Provider I/O happens outside every database transaction, exactly as in M6::

    (no txn)   provider.get_transfer_status(...)   /   verify_and_parse_webhook(...)
    (atomic)   lock wallet -> lock transaction -> lock attempt ->
               append evidence -> resolve through M4/M5 -> COMMIT

The canonical order is unchanged and taken in full:

    Wallet rows (ascending PK) -> FinancialTransaction -> ProviderExecutionAttempt
    -> recovery evidence / webhook receipt -> hold / transfer / ledger work

Financial rows are always locked **before** evidence rows, so no path here can
invert against M6's execution path.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone

from moneycore.domain.errors import (
    ProviderRecoveryConflictError,
    ProviderRecoveryNotAllowedError,
    ProviderRecoveryReferenceMismatchError,
    ProviderWebhookConflictError,
    ProviderWebhookUnmatchedError,
)
from moneycore.domain.providers import ProviderAttemptStatus
from moneycore.domain.recovery import (
    EvidenceSource,
    NormalisedWebhookEvent,
    RecoveryOutcome,
    require_status_result,
)
from moneycore.domain.transactions import TransactionStatus
from moneycore.models import (
    FinancialTransaction,
    ProviderExecutionAttempt,
    ProviderRecoveryEvidence,
    ProviderWebhookEvent,
    Transfer,
)
from moneycore.domain.recovery import WebhookProcessingStatus
from moneycore.services.ledger import _lock_wallets
from moneycore.services.transfers import fail_transfer, succeed_transfer

#: Attempt states whose truth is not yet established.
RECOVERABLE_ATTEMPT_STATES = frozenset({
    ProviderAttemptStatus.STARTED,
    ProviderAttemptStatus.UNKNOWN,
})

#: Transaction states a recovery may still resolve.
RESOLVABLE_TRANSACTION_STATES = frozenset({
    TransactionStatus.PROCESSING,
    TransactionStatus.UNKNOWN,
})


# --------------------------------------------------------------------------
# Locking, in the canonical order
# --------------------------------------------------------------------------


def _lock_transaction(transaction_id: int) -> FinancialTransaction:
    return FinancialTransaction.objects.select_for_update().get(pk=transaction_id)


def _lock_financial_rows(attempt_id: int) -> tuple[
    ProviderExecutionAttempt, FinancialTransaction, Transfer
]:
    """Wallet -> FinancialTransaction -> ProviderExecutionAttempt, in that order.

    Taken here explicitly rather than left to a nested M4/M5 call, for the same
    reason M6 takes it explicitly: by the time those services run the attempt
    row would already be held and the order would be inverted.
    """
    unlocked = ProviderExecutionAttempt.objects.select_related(
        'financial_transaction'
    ).get(pk=attempt_id)

    _lock_wallets([unlocked.financial_transaction.wallet_id])
    txn = _lock_transaction(unlocked.financial_transaction_id)
    attempt = ProviderExecutionAttempt.objects.select_for_update().get(
        pk=attempt_id
    )
    transfer = Transfer.objects.select_related('financial_transaction').get(
        financial_transaction=txn
    )
    return attempt, txn, transfer


# --------------------------------------------------------------------------
# Applying evidence
# --------------------------------------------------------------------------


def _record_evidence(
    attempt: ProviderExecutionAttempt,
    *,
    source: str,
    outcome: str,
    provider_reference: str = '',
    failure_code: str = '',
    provider_event_id: str = '',
    observed_at=None,
) -> ProviderRecoveryEvidence:
    """Append one observation. Never updates an existing row."""
    return ProviderRecoveryEvidence.objects.create(
        provider_attempt=attempt,
        source=source,
        outcome=outcome,
        provider_reference=provider_reference,
        failure_code=failure_code if outcome == RecoveryOutcome.FAILED else '',
        provider_event_id=provider_event_id,
        observed_at=observed_at or timezone.now(),
    )


def _require_no_contradiction(txn: FinancialTransaction, outcome: str) -> bool:
    """Compare definitive evidence with what is already settled.

    Returns ``True`` when the transaction is already resolved and the evidence
    agrees — a redelivery or a repeated query, which is a safe no-op. Raises
    when it disagrees, because reversing posted money on the strength of a
    later message is not a decision this milestone may take.
    """
    if txn.status == TransactionStatus.SUCCEEDED:
        if outcome == RecoveryOutcome.SUCCEEDED:
            return True
        raise ProviderRecoveryConflictError(
            'This transfer is already settled as succeeded, and this evidence '
            'says otherwise.',
            details={
                'transaction': txn.pk,
                'settled_as': txn.status,
                'evidence': outcome,
            },
        )

    if txn.status == TransactionStatus.FAILED:
        if outcome == RecoveryOutcome.FAILED:
            return True
        raise ProviderRecoveryConflictError(
            'This transfer is already settled as failed, and this evidence '
            'says otherwise.',
            details={
                'transaction': txn.pk,
                'settled_as': txn.status,
                'evidence': outcome,
            },
        )

    return False


def _apply_definitive_outcome(
    transfer: Transfer,
    txn: FinancialTransaction,
    *,
    outcome: str,
    failure_code: str,
    counterpart_account,
) -> None:
    """Resolve the transaction through M4/M5, or do nothing if already settled.

    Nothing here assigns a status, releases a hold or creates a journal. The
    reservation release, the posting and the state change all happen inside
    M4's single atomic block, exactly as they do for an immediate success.
    """
    if _require_no_contradiction(txn, outcome):
        # Already settled the same way. Redelivery and repeated queries are
        # expected, so this is a no-op rather than an error.
        return

    if outcome == RecoveryOutcome.SUCCEEDED:
        succeed_transfer(transfer, counterpart_account=counterpart_account)
    else:
        fail_transfer(transfer, failure_code=failure_code)


# --------------------------------------------------------------------------
# Status query recovery
# --------------------------------------------------------------------------


def _require_recoverable(attempt: ProviderExecutionAttempt) -> None:
    if attempt.status not in RECOVERABLE_ATTEMPT_STATES:
        raise ProviderRecoveryNotAllowedError(
            'That provider attempt already recorded a definitive outcome.',
            details={'attempt': attempt.pk, 'status': attempt.status},
        )


def recover_provider_attempt(
    attempt: ProviderExecutionAttempt,
    *,
    provider,
    counterpart_account,
) -> ProviderRecoveryEvidence:
    """Ask the rail what became of a request already made, and act on it.

    Observational: safe to call repeatedly, because asking moves no money. It
    is **not** safe to resubmit, and this never does.

    The lookup happens outside any database transaction and holding no row
    lock, for the reason M6 established: a wallet lock held across a provider
    outage stalls every other operation on that customer's money.

    Returns the evidence row that was appended, whatever it says. An
    ``unresolved`` answer still produces a row — that SpendWise asked and could
    not be told is itself worth recording — but changes nothing financial.
    """
    _require_recoverable(attempt)

    client_reference = attempt.client_reference
    provider_reference = attempt.provider_reference

    # ---- outside any database transaction, holding no row lock ----
    result = require_status_result(
        provider.get_transfer_status(
            client_reference=client_reference,
            provider_reference=provider_reference,
        )
    )
    # ---------------------------------------------------------------

    evidence, conflict = _apply_status_result(
        attempt.pk, result, counterpart_account=counterpart_account
    )
    if conflict is not None:
        # Raised only after the evidence has committed. Discarding a
        # contradiction is exactly what must not happen: it is the thing
        # operations needs to see.
        raise conflict
    return evidence


@transaction.atomic
def _apply_status_result(
    attempt_id: int, result, *, counterpart_account
):
    """Record what the lookup said, and resolve if it was definitive.

    A contradiction is **returned rather than raised** so this transaction can
    commit the evidence first. Raising from inside would roll the observation
    back, destroying the record of the disagreement.
    """
    attempt, txn, transfer = _lock_financial_rows(attempt_id)
    observed_at = timezone.now()

    evidence = _record_evidence(
        attempt,
        source=EvidenceSource.STATUS_QUERY,
        outcome=result.outcome,
        provider_reference=result.provider_reference,
        failure_code=getattr(result, 'failure_code', ''),
        observed_at=observed_at,
    )

    if result.outcome == RecoveryOutcome.UNRESOLVED:
        # Nothing moves. The reservation stays exactly where it was, and the
        # attempt keeps its original observation — a lookup that could not tell
        # us does not turn STARTED into UNKNOWN, because those record different
        # historical facts.
        return evidence, None

    try:
        _apply_definitive_outcome(
            transfer,
            txn,
            outcome=result.outcome,
            failure_code=getattr(result, 'failure_code', ''),
            counterpart_account=counterpart_account,
        )
    except ProviderRecoveryConflictError as conflict:
        # No financial change was made — the contradiction is detected before
        # anything moves — so committing here commits only the evidence.
        return evidence, conflict

    return evidence, None


# --------------------------------------------------------------------------
# Webhook recovery
# --------------------------------------------------------------------------


def _match_attempt(event: NormalisedWebhookEvent) -> ProviderExecutionAttempt:
    """Find the one execution this event is about.

    ``client_reference`` is preferred because SpendWise owns it and M7 makes it
    unique. A provider reference corroborates. If both are present and name
    different attempts, that is a mismatch and is refused — guessing which to
    believe is how evidence gets attached to the wrong customer's money.

    Matching never falls back to amount, account number or recipient. A
    plausible-looking match is not a match.
    """
    by_client = None
    if event.client_reference:
        by_client = ProviderExecutionAttempt.objects.filter(
            client_reference=event.client_reference
        ).first()

    by_provider = None
    if event.provider_reference:
        candidates = list(
            ProviderExecutionAttempt.objects.filter(
                provider_key=event.provider_key,
                provider_reference=event.provider_reference,
            )[:2]
        )
        if len(candidates) == 1:
            by_provider = candidates[0]
        elif len(candidates) > 1:
            raise ProviderRecoveryReferenceMismatchError(
                'That provider reference names more than one execution.',
                details={'provider_reference': event.provider_reference},
            )

    if by_client is not None and by_provider is not None:
        if by_client.pk != by_provider.pk:
            raise ProviderRecoveryReferenceMismatchError(
                'The client and provider references in that webhook name '
                'different executions.',
                details={'client_reference': event.client_reference},
            )

    matched = by_client or by_provider
    if matched is None:
        raise ProviderWebhookUnmatchedError(
            'That webhook refers to a request this system has no record of.',
            details={
                'provider_key': event.provider_key,
                'provider_event_id': event.provider_event_id,
            },
        )
    return matched


def _existing_receipt(event: NormalisedWebhookEvent) -> ProviderWebhookEvent | None:
    return ProviderWebhookEvent.objects.filter(
        provider_key=event.provider_key,
        provider_event_id=event.provider_event_id,
    ).first()


def _require_same_delivery(
    receipt: ProviderWebhookEvent, event: NormalisedWebhookEvent
) -> None:
    """Redelivery must say the same thing it said the first time.

    The same event id carrying different contents is not a redelivery; it is a
    contradiction, and the original receipt is not overwritten.
    """
    differences = [
        field
        for field, incoming in (
            ('outcome', event.outcome),
            ('client_reference', event.client_reference),
            ('provider_reference', event.provider_reference),
            ('failure_code', event.failure_code),
        )
        if getattr(receipt, field) != incoming
    ]
    if differences:
        raise ProviderWebhookConflictError(
            'That webhook event id was already received with different '
            'contents.',
            details={
                'provider_event_id': event.provider_event_id,
                'conflicting_fields': sorted(differences),
            },
        )


def ingest_webhook(
    *, provider, body: bytes, headers, counterpart_account
) -> ProviderWebhookEvent:
    """Authenticate an inbound delivery, then act on what it says.

    Verification and parsing happen **first, and outside any database
    transaction** — no wallet or transaction lock is held while request bytes
    are being checked. A delivery that does not authenticate raises before
    anything is read from the database, so it can change nothing and cannot
    fill this table.
    """
    # ---- outside any database transaction, holding no row lock ----
    event = provider.verify_and_parse_webhook(body=body, headers=headers)
    # ---------------------------------------------------------------

    if not isinstance(event, NormalisedWebhookEvent):
        from moneycore.domain.errors import ProviderWebhookInvalidError

        raise ProviderWebhookInvalidError(
            'A provider adapter must return a normalised webhook event.',
            details={'returned': type(event).__name__},
        )

    return _apply_webhook_event(event, counterpart_account=counterpart_account)


@transaction.atomic
def _record_unmatched(event: NormalisedWebhookEvent) -> ProviderWebhookEvent:
    """Keep an authentic delivery we cannot place, for operations to see.

    Never creates a transaction, and never attaches itself to the nearest
    plausible one. An authenticated rail telling us about money we have no
    record of is exactly what someone needs to look at.
    """
    return ProviderWebhookEvent.objects.create(
        provider_key=event.provider_key,
        provider_event_id=event.provider_event_id,
        outcome=event.outcome,
        client_reference=event.client_reference,
        provider_reference=event.provider_reference,
        failure_code=event.failure_code,
        processing_status=WebhookProcessingStatus.UNMATCHED,
    )


def _apply_webhook_event(
    event: NormalisedWebhookEvent, *, counterpart_account
) -> ProviderWebhookEvent:
    """Resolve one authenticated event, at most once."""
    existing = _existing_receipt(event)
    if existing is not None:
        # Redelivery. Confirm it says the same thing, then do nothing further.
        _require_same_delivery(existing, event)
        return existing

    try:
        attempt = _match_attempt(event)
    except ProviderWebhookUnmatchedError:
        _record_unmatched(event)
        raise

    receipt, conflict = _apply_matched_webhook(
        event, attempt.pk, counterpart_account=counterpart_account
    )
    if conflict is not None:
        # Raised after the receipt and evidence have committed, so the
        # contradiction survives for operations to act on.
        raise conflict
    return receipt


@transaction.atomic
def _apply_matched_webhook(
    event: NormalisedWebhookEvent, attempt_id: int, *, counterpart_account
):
    """Canonical order: wallet, transaction, attempt, then the evidence rows."""
    attempt, txn, transfer = _lock_financial_rows(attempt_id)

    try:
        # Isolated in its own savepoint. A unique violation marks the enclosing
        # block as needing rollback, so without this the outer transaction
        # could not be used to go and read what the winner wrote.
        with transaction.atomic():
            receipt = ProviderWebhookEvent.objects.create(
                provider_key=event.provider_key,
                provider_event_id=event.provider_event_id,
                outcome=event.outcome,
                client_reference=event.client_reference,
                provider_reference=event.provider_reference,
                failure_code=event.failure_code,
                processing_status=WebhookProcessingStatus.RECEIVED,
                provider_attempt=attempt,
            )
    except IntegrityError:
        # Another delivery of this identity committed first. That is the
        # ordinary case — rails redeliver, sometimes simultaneously — so the
        # question is what it *said*, not when it arrived. **Timing must not
        # change the meaning of an identical event**: if the winner reported
        # the same thing, this is a duplicate and is idempotent, exactly as it
        # would be sequentially. Only identity reuse with different contents is
        # a conflict.
        existing = _existing_receipt(event)
        if existing is None:
            # Not the uniqueness we expected. Never swallow it as a conflict.
            raise
        _require_same_delivery(existing, event)
        return existing, None

    if event.outcome == RecoveryOutcome.UNRESOLVED:
        _record_evidence(
            attempt,
            source=EvidenceSource.WEBHOOK,
            outcome=event.outcome,
            provider_reference=event.provider_reference,
            provider_event_id=event.provider_event_id,
        )
        receipt.processing_status = WebhookProcessingStatus.PROCESSED
        receipt.processed_at = timezone.now()
        receipt.save(update_fields=['processing_status', 'processed_at'])
        return receipt, None

    _record_evidence(
        attempt,
        source=EvidenceSource.WEBHOOK,
        outcome=event.outcome,
        provider_reference=event.provider_reference,
        failure_code=event.failure_code,
        provider_event_id=event.provider_event_id,
    )

    try:
        _apply_definitive_outcome(
            transfer,
            txn,
            outcome=event.outcome,
            failure_code=event.failure_code,
            counterpart_account=counterpart_account,
        )
    except ProviderRecoveryConflictError as conflict:
        # The evidence stands and is marked, the settled outcome is untouched,
        # and nothing is reversed. Returned rather than raised so this
        # transaction commits the record; the caller raises afterwards.
        receipt.processing_status = WebhookProcessingStatus.CONFLICTED
        receipt.processed_at = timezone.now()
        receipt.save(update_fields=['processing_status', 'processed_at'])
        return receipt, conflict

    receipt.processing_status = WebhookProcessingStatus.PROCESSED
    receipt.processed_at = timezone.now()
    receipt.save(update_fields=['processing_status', 'processed_at'])
    return receipt, None


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def recovery_evidence_for(attempt: ProviderExecutionAttempt):
    """Every observation about one execution, oldest first."""
    return ProviderRecoveryEvidence.objects.filter(
        provider_attempt=attempt
    ).order_by('observed_at', 'pk')


def attempts_awaiting_recovery():
    """Executions whose truth is not yet established.

    The sweep M7 makes possible and deliberately does not schedule: how often
    to look, and by what trigger, is a production decision nobody has made.
    """
    return ProviderExecutionAttempt.objects.filter(
        status__in=sorted(RECOVERABLE_ATTEMPT_STATES)
    ).order_by('started_at')
