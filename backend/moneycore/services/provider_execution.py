"""Executing a prepared transfer against an external rail, and reading it.

M6 adds the boundary M5 deliberately stopped short of: actually asking someone
to move the money, and interpreting the immediate answer truthfully.

The shape of execution
----------------------
Three phases, and the middle one holds **no database transaction and no row
lock**::

    PRE-CALL   (atomic)   lock wallet -> lock transaction -> validate ->
                          start_processing -> persist the attempt -> COMMIT
    CALL       (no txn)   provider.submit_transfer(request)
    POST-CALL  (atomic)   lock wallet -> lock transaction -> lock attempt ->
                          apply the outcome -> finalise the attempt -> COMMIT

The canonical lock order, in both database phases and everywhere else in the
money core, is::

    Wallet rows (ascending PK) -> FinancialTransaction -> ProviderExecutionAttempt
    -> hold / transfer / ledger work

Both phases take it in full and explicitly. Leaving the transaction lock to a
nested M4/M5 call would invert it, because the attempt row would already be
held by then.

Why the call is outside a transaction
-------------------------------------
A network call inside ``transaction.atomic`` holds every row lock it has taken
for the duration of somebody else's outage. On the wallet row — which M3 made
the serialisation gate for *all* spendability — that would stall every other
operation on that customer's money behind a hung socket. So the call happens
between two committed transactions, and a test proves at call time that
``connection.in_atomic_block`` is ``False`` rather than merely inspecting the
source.

The cost of that choice is a window where an attempt is claimed but no outcome
is recorded, which is discussed on
:class:`~moneycore.models.ProviderExecutionAttempt`. It is the right trade: the
window is recoverable, a lock held across a provider outage is not.

Why nothing is ever retried here
--------------------------------
There is no loop, no backoff and no second submission anywhere in this module.
Once a request has left, resending it can double-spend a customer's money,
because an ambiguous answer means *maybe it worked*. Establishing what actually
happened — status query, webhook, reconciliation — is M7's, and is the reason
M7 exists. M6's job is to be honest about not knowing.

What this module does not do
----------------------------
No webhook, no status polling, no reconciliation, no settlement, no retry
scheduler, no queue. No vendor is named and no credential is read.
"""

from __future__ import annotations

import hashlib

from django.db import IntegrityError, transaction
from django.utils import timezone

from moneycore.domain.errors import (
    AccountResolutionInvalidError,
    AccountResolutionNotFoundError,
    AccountResolutionUnavailableError,
    ProviderExecutionAlreadyStartedError,
    ProviderResultInvalidError,
    TransferTransactionMismatchError,
)
from moneycore.domain.providers import (
    AccountResolutionFailureReason,
    ProviderAccountResolution,
    ProviderAccountResolutionFailed,
    ProviderAttemptStatus,
    ProviderOperation,
    ProviderTransferFailed,
    ProviderTransferRequest,
    ProviderTransferSucceeded,
    ProviderTransferUnknown,
    normalise_provider_reference,
    require_transfer_result,
)
from moneycore.domain.transactions import TransactionStatus
from moneycore.domain.transfers import VerifiedBankAccount
from moneycore.models import (
    FinancialTransaction,
    ProviderExecutionAttempt,
    Transfer,
)
from moneycore.services.ledger import _lock_wallets
from moneycore.services.transactions import start_processing
from moneycore.services.transfers import (
    fail_transfer,
    mark_transfer_unknown,
    succeed_transfer,
)

#: Prefix for our own outbound reference. Ours, not any provider's.
CLIENT_REFERENCE_PREFIX = 'SW-'
_CLIENT_REFERENCE_DIGEST_LENGTH = 24


# --------------------------------------------------------------------------
# Account resolution
# --------------------------------------------------------------------------


def resolve_transfer_destination(
    provider, *, bank_code: str, account_number: str
) -> VerifiedBankAccount:
    """Resolve a destination through a provider, into M5's verified value.

    This is the trusted producer M5 deliberately left absent: M5 modelled the
    *fact* of a verified destination and refused to fake one, and this is where
    that fact now legitimately comes from.

    **No database transaction is opened here at all**, and nothing is
    persisted. Resolving a destination moves no money and creates no intent; it
    is a question, and the answer is handed straight back to the caller.

    Failure is reported precisely. "No such account" and "we could not check"
    are different errors, because telling a customer their recipient does not
    exist when our own rail was unreachable would state a fact nobody
    established.
    """
    result = provider.resolve_bank_account(
        bank_code=bank_code, account_number=account_number
    )

    if isinstance(result, ProviderAccountResolutionFailed):
        if result.reason == AccountResolutionFailureReason.NOT_FOUND:
            raise AccountResolutionNotFoundError(
                'That account could not be found at that bank.',
                details={'bank_code': bank_code},
            )
        if result.reason == AccountResolutionFailureReason.UNAVAILABLE:
            raise AccountResolutionUnavailableError(
                'That account could not be verified right now. Nothing was '
                'sent.',
                details={'bank_code': bank_code},
            )
        raise AccountResolutionInvalidError(
            'That account could not be looked up as given.',
            details={'bank_code': bank_code},
        )

    if not isinstance(result, ProviderAccountResolution):
        raise ProviderResultInvalidError(
            'A provider must return a resolution or an explicit failure.',
            details={'returned': type(result).__name__},
        )

    # The conversion that keeps provider shapes out of the money core: from
    # here on it is an M5 value object, and a Transfer never sees anything else.
    return VerifiedBankAccount(
        account_number=result.account_number,
        bank_code=result.bank_code,
        bank_name=result.bank_name,
        account_name=result.account_name,
    )


# --------------------------------------------------------------------------
# Outbound reference
# --------------------------------------------------------------------------


def client_reference_for(txn: FinancialTransaction) -> str:
    """Our stable outbound reference for one transaction's execution.

    Deterministic, so reading it twice gives the same answer and a replayed
    read never invents a new one. Derived from immutable transaction identity
    through a digest, so the database sequence is not published to a third
    party.

    This is ours. It is not the transfer id, not M4's idempotency key, and
    emphatically not the provider's reference — using a provider's own value as
    our key would make our correctness depend on their numbering.
    """
    seed = f'{txn.pk}:{txn.wallet_id}:{txn.idempotency_key}'
    digest = hashlib.sha256(seed.encode('utf-8')).hexdigest()
    return f'{CLIENT_REFERENCE_PREFIX}{digest[:_CLIENT_REFERENCE_DIGEST_LENGTH]}'


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


def _lock_transaction(transaction_id: int) -> FinancialTransaction:
    """Take the FinancialTransaction row lock.

    Always called after the wallet lock and **before** the attempt lock. That
    order is the canonical one across M2-M6, and it matters here beyond this
    module: a future recovery sweep may legitimately want to lock a
    transaction and its attempt *without* touching the wallet at all, and if
    this path took the attempt first the two would form a cycle.
    """
    return FinancialTransaction.objects.select_for_update().get(pk=transaction_id)


def _require_executable(transfer: Transfer) -> FinancialTransaction:
    txn = transfer.financial_transaction

    if txn.status != TransactionStatus.CREATED:
        raise ProviderExecutionAlreadyStartedError(
            'That transfer is no longer awaiting execution.',
            details={'transfer': transfer.pk, 'status': txn.status},
        )
    if txn.hold_id is None:
        raise TransferTransactionMismatchError(
            'That transfer has no reservation and cannot be executed.',
            details={'transfer': transfer.pk},
        )
    return txn


@transaction.atomic
def _claim_execution(transfer: Transfer, provider_key: str) -> tuple[int, str]:
    """Phase one: take the execution claim, durably, then commit.

    Locks in the canonical order — **wallet, then financial transaction, then
    the attempt** — transitions the transaction to PROCESSING through M4 rather
    than by assignment, and writes the attempt row. The unique constraint on
    that row is what makes a second submission impossible even if two callers
    arrive together.

    Returns the ids the caller needs *after* this transaction has committed —
    deliberately not model instances, so nothing tempts the caller into holding
    a stale row across the provider call.
    """
    _lock_wallets([transfer.financial_transaction.wallet_id])
    locked_transfer = Transfer.objects.select_related(
        'financial_transaction'
    ).get(pk=transfer.pk)
    # Wallet, then transaction, then the attempt. Taken explicitly rather than
    # left to a nested service, so the order is visible where it is relied on.
    _lock_transaction(locked_transfer.financial_transaction_id)
    txn = _require_executable(locked_transfer)

    if ProviderExecutionAttempt.objects.filter(financial_transaction=txn).exists():
        raise ProviderExecutionAlreadyStartedError(
            'That transfer has already been submitted to a provider.',
            details={'transfer': transfer.pk},
        )

    # Through M4, never by writing status directly.
    start_processing(txn)

    reference = client_reference_for(txn)
    try:
        attempt = ProviderExecutionAttempt.objects.create(
            financial_transaction=txn,
            provider_key=provider_key,
            operation=ProviderOperation.SUBMIT_TRANSFER,
            status=ProviderAttemptStatus.STARTED,
            client_reference=reference,
        )
    except IntegrityError:
        # Another caller claimed this transaction first. Never resubmit.
        raise ProviderExecutionAlreadyStartedError(
            'That transfer has already been submitted to a provider.',
            details={'transfer': transfer.pk},
        ) from None

    return attempt.pk, reference


def _build_request(
    transfer: Transfer, client_reference: str
) -> ProviderTransferRequest:
    """Business values only. No model instance crosses the boundary."""
    return ProviderTransferRequest(
        amount_minor=transfer.amount_minor,
        currency=transfer.currency,
        destination_account_number=transfer.destination_account_number,
        destination_bank_code=transfer.destination_bank_code,
        client_reference=client_reference,
        narration=transfer.narration,
    )


@transaction.atomic
def _apply_outcome(
    transfer_id: int, attempt_id: int, result, counterpart_account
) -> Transfer:
    """Phase three: record what the rail said, coherently and atomically.

    Locks in the established order — **wallet, then financial transaction, then
    provider attempt** — so no path here can form a cycle with any earlier
    milestone's ordering, nor with a future recovery path that locks a
    transaction and its attempt without needing the wallet.

    Every state change goes through M4/M5's existing boundaries. This module
    owns no state machine of its own and never assigns a transaction status.
    """
    transfer = Transfer.objects.select_related('financial_transaction').get(
        pk=transfer_id
    )
    # Canonical order, taken explicitly and in full before anything is read for
    # update: wallet -> financial transaction -> provider attempt. The
    # transaction lock is acquired here rather than being left to the nested
    # M4/M5 service further down, because by then the attempt row would already
    # be held and the order would be inverted.
    _lock_wallets([transfer.financial_transaction.wallet_id])
    _lock_transaction(transfer.financial_transaction_id)
    attempt = ProviderExecutionAttempt.objects.select_for_update().get(
        pk=attempt_id
    )

    if attempt.is_finished:
        raise ProviderExecutionAlreadyStartedError(
            'That provider attempt has already been resolved.',
            details={'attempt': attempt.pk, 'status': attempt.status},
        )

    finished_at = timezone.now()

    if isinstance(result, ProviderTransferSucceeded):
        # Provider success alone is not enough: the money is only really moved
        # once M5's accounting posts and M4 releases the reservation, in one
        # atomic step. If that is rejected, this whole phase rolls back and the
        # attempt stays unfinished rather than claiming a success that has no
        # posted truth behind it.
        succeed_transfer(transfer, counterpart_account=counterpart_account)
        attempt.status = ProviderAttemptStatus.SUCCEEDED
        attempt.provider_reference = result.provider_reference

    elif isinstance(result, ProviderTransferFailed):
        fail_transfer(transfer, failure_code=result.failure_code)
        attempt.status = ProviderAttemptStatus.FAILED
        attempt.failure_code = result.failure_code
        attempt.provider_reference = result.provider_reference

    else:
        # Ambiguous. Nothing is posted, the reservation stays active, and the
        # transaction becomes UNKNOWN — waiting for the truth, not concluding.
        mark_transfer_unknown(transfer)
        attempt.status = ProviderAttemptStatus.UNKNOWN
        attempt.ambiguity_reason = result.ambiguity_reason[:64]
        attempt.provider_reference = result.provider_reference

    attempt.finished_at = finished_at
    attempt.save(
        update_fields=[
            'status', 'provider_reference', 'failure_code',
            'ambiguity_reason', 'finished_at', 'updated_at',
        ]
    )

    transfer.refresh_from_db()
    return transfer


def execute_transfer(
    transfer: Transfer, *, provider, counterpart_account
) -> Transfer:
    """Submit one prepared transfer to a rail, and record what came back.

    The whole of M6's orchestration, and deliberately **not** one database
    transaction: see the module docstring. The provider call happens between
    two committed transactions, with no row lock held.

    ``counterpart_account`` is passed through to M5's success accounting. M6
    invents no settlement account — which internal account a real transfer
    faces follows from a custody design that is still open (O-29).

    Called twice on the same transfer, the second call is refused: the durable
    attempt row is the execution claim, and resubmitting a transfer whose
    outcome may be unresolved is how a customer gets debited twice.
    """
    attempt_id, client_reference = _claim_execution(transfer, provider.provider_key)
    request = _build_request(transfer, client_reference)

    # ---- outside any database transaction, holding no row lock ----
    result = require_transfer_result(provider.submit_transfer(request))
    # ---------------------------------------------------------------

    return _apply_outcome(
        transfer.pk, attempt_id, result, counterpart_account
    )


def provider_attempt_for(transfer: Transfer) -> ProviderExecutionAttempt | None:
    """The execution attempt for a transfer, if one has been claimed."""
    return ProviderExecutionAttempt.objects.filter(
        financial_transaction=transfer.financial_transaction_id
    ).first()
