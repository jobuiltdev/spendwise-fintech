"""Reconciling SpendWise's records against a provider's, and only that.

This module **detects**. It does not repair.

A provider export is external evidence. Finding that a rail disagrees with our
ledger is a reason to raise an alarm and hand a human the details; it is never
authority to move money. A system that quietly rewrote its own books to agree
with someone else's file would have no books worth keeping — and the file is
just as likely to be the thing that is wrong.

So there is no path from here to any financial mutator. This module does not
import and never calls ``succeed_transfer``, ``fail_transfer``,
``mark_transfer_unknown``, ``recover_provider_attempt``, ``ingest_webhook``,
``post_journal``, ``reverse_journal`` or ``release_hold``; it assigns no
transaction, hold or journal status; and it is asserted by AST walk to contain
no such call. The strongest thing it can do is write down what it saw.

Discrepancy versus corruption
-----------------------------
Two different failures, kept apart because collapsing them would bury the
serious one:

* SpendWise and the provider disagreeing is a **discrepancy** — expected,
  classified, recorded, survivable.
* SpendWise disagreeing with *itself* — a succeeded transaction with no journal
  behind it — is an **internal integrity failure**. M2-M5 make it impossible
  through supported paths, so if it ever appears it is our defect, and it
  raises rather than being filed alongside ordinary differences.

Shape of a run
--------------
::

    (atomic)   validate window -> create STARTED run -> COMMIT
    (no txn)   provider.list_transfer_records(...)      <- paged, read-only
    (atomic)   read internal state -> classify -> write every item -> COMPLETE

The fetch sits between two committed transactions, holding nothing, for the
reason M6 established: a provider outage must not stall anything else.

Locking
-------
Reconciliation takes **observation locks, and nothing else**.

Under ``READ COMMITTED`` — PostgreSQL's default, and the one this project
runs — each statement inside a transaction sees its own fresh snapshot. So
reading a transaction's status in one statement and its hold in the next can
straddle a concurrent M6/M7 resolution and produce a state SpendWise was never
actually in: ``UNKNOWN`` with a released hold, or ``SUCCEEDED`` with no journal.
That is not a discrepancy to file; it is a phantom, and it would be reported as
an internal integrity failure against a system that is perfectly consistent.

So before reading any dependent state, the classification transaction takes
``select_for_update`` on the ``FinancialTransaction`` row, then on the
``ProviderExecutionAttempt`` row, and only then reads the hold, journal,
transfer and recovery evidence. Every read of a transfer's internal state is
therefore taken from one settled moment. A run observes a concurrent
resolution as **entirely before** or **entirely after**, never as a mixture.

What is deliberately *not* locked, and why:

* **No wallet lock.** Reconciliation moves no money, so it has no business in
  the wallet ordering that M3-M7 mutators contend on. Taking one would make an
  audit able to stall a customer's payment.
* **No hold, journal or ledger-account lock.** Nothing may change those without
  first holding the transaction row, so the transaction lock already settles
  them. Locking them again would add ordering edges for nothing.
* **Attempt is never locked before its transaction**, which keeps the canonical
  order — wallet, transaction, attempt — intact with reconciliation simply
  starting one step in. Across a run the transaction rows are locked in
  ascending primary-key order, and the attempts likewise; provider record
  order never reaches the locking, so two concurrent runs over overlapping
  windows cannot deadlock against each other.

The locks are held only for the classify-and-record transaction. The provider
fetch still holds nothing. And a lock is not a licence: this module still
writes to no financial table, which is what makes "observation" the accurate
word.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from moneycore.domain.errors import (
    ReconciliationInputConflictError,
    ReconciliationInternalIntegrityError,
    ReconciliationProviderError,
    ReconciliationRecordInvalidError,
    ReconciliationReferenceConflictError,
)
from moneycore.domain.reconciliation import (
    Comparison,
    InternalTransferView,
    ProviderTransferRecord,
    ProviderTransferRecordPage,
    ReconciliationOutcome,
    ReconciliationRunStatus,
    ReconciliationStatus,
    ReconciliationWindow,
    compare,
)
from moneycore.domain.recovery import RecoveryOutcome
from moneycore.domain.transactions import TransactionStatus
from moneycore.models import (
    FinancialTransaction,
    ProviderExecutionAttempt,
    ProviderReconciliationItem,
    ProviderReconciliationRun,
    ProviderRecoveryEvidence,
)

#: How an internal transaction state reads as a reconciliation outcome.
#:
#: ``PROCESSING`` and ``UNKNOWN`` both map to ``unresolved``, never to failure.
#: An operation still in flight, or one whose answer never came back, has not
#: been established as not having happened — and treating either as a failure
#: is how a reconciliation report starts recommending that reserved funds be
#: released on money that may already have gone.
INTERNAL_OUTCOME_BY_TRANSACTION_STATUS = {
    TransactionStatus.SUCCEEDED: ReconciliationOutcome.SUCCEEDED,
    TransactionStatus.FAILED: ReconciliationOutcome.FAILED,
    TransactionStatus.UNKNOWN: ReconciliationOutcome.UNRESOLVED,
    TransactionStatus.PROCESSING: ReconciliationOutcome.UNRESOLVED,
}

#: The page limit is the provider's; this only stops a broken adapter that
#: returns the same cursor for ever from looping without end.
MAX_RECORD_PAGES = 1_000


# --------------------------------------------------------------------------
# Fetching provider records
# --------------------------------------------------------------------------


def _fetch_provider_records(
    provider, window: ReconciliationWindow
) -> list[ProviderTransferRecord]:
    """Page through the provider's records for a window.

    Called with **no database transaction open and no row lock held**. A
    provider that hangs must not be able to stall anything else, and this is
    read-only besides.
    """
    records: list[ProviderTransferRecord] = []
    cursor = ''

    for _ in range(MAX_RECORD_PAGES):
        page = provider.list_transfer_records(window=window, cursor=cursor)
        if not isinstance(page, ProviderTransferRecordPage):
            raise ReconciliationRecordInvalidError(
                'A provider must return a page of reconciliation records.',
                details={'returned': type(page).__name__},
            )
        records.extend(page.records)
        cursor = page.next_cursor
        if not cursor:
            return records

    raise ReconciliationProviderError(
        'The provider did not finish listing its records.',
        details={'pages': MAX_RECORD_PAGES},
    )


def _deduplicate(records) -> list[ProviderTransferRecord]:
    """Collapse repeated identities, and refuse contradictory ones.

    An export listing the same record twice is ordinary. The same identity
    saying two different things means the export contradicts itself, and there
    is no correct way to choose between them — so the run fails rather than
    reconciling against a guess.
    """
    seen: dict[tuple[str, str], ProviderTransferRecord] = {}
    for record in records:
        existing = seen.get(record.identity)
        if existing is None:
            seen[record.identity] = record
            continue
        if existing.comparable != record.comparable:
            raise ReconciliationInputConflictError(
                'The provider reported one record identity with different '
                'contents.',
                details={
                    'provider_key': record.provider_key,
                    'provider_record_id': record.provider_record_id,
                },
            )
    # Deterministic order, independent of how the provider paged them.
    return sorted(seen.values(), key=lambda record: record.provider_record_id)


# --------------------------------------------------------------------------
# Reading internal truth
# --------------------------------------------------------------------------


def _require_internal_integrity(attempt, txn, transfer) -> None:
    """Check SpendWise against itself before comparing it with anyone else.

    These are not discrepancies. A succeeded transfer with no journal, a failed
    one still holding the customer's funds, or an unresolved one that has
    somehow posted, are all states M2-M5 make unreachable through supported
    paths. Reporting one as an ordinary difference of opinion with a provider
    would file a defect in our own ledger among the routine noise.
    """
    status = txn.status

    if status == TransactionStatus.CREATED:
        # An attempt exists, so execution was claimed; M6 transitions the
        # transaction before writing the attempt row.
        raise ReconciliationInternalIntegrityError(
            'A provider attempt exists for a transaction that never started.',
            details={'attempt': attempt.pk, 'transaction': txn.pk},
        )

    if status == TransactionStatus.SUCCEEDED:
        if txn.journal_id is None:
            raise ReconciliationInternalIntegrityError(
                'A succeeded transaction has no posted journal.',
                details={'transaction': txn.pk},
            )
        if txn.hold_id is not None and txn.hold.is_effectively_active():
            raise ReconciliationInternalIntegrityError(
                'A succeeded transaction still reserves the customer funds.',
                details={'transaction': txn.pk, 'hold': txn.hold_id},
            )
        return

    if status == TransactionStatus.FAILED:
        if txn.journal_id is not None:
            raise ReconciliationInternalIntegrityError(
                'A failed transaction has a posted journal.',
                details={'transaction': txn.pk, 'journal': txn.journal_id},
            )
        if txn.hold_id is not None and txn.hold.is_effectively_active():
            raise ReconciliationInternalIntegrityError(
                'A failed transaction still reserves the customer funds.',
                details={'transaction': txn.pk, 'hold': txn.hold_id},
            )
        return

    # PROCESSING or UNKNOWN: unresolved, so nothing may have posted and the
    # reservation must still be standing.
    if txn.journal_id is not None:
        raise ReconciliationInternalIntegrityError(
            'An unresolved transaction has a posted journal.',
            details={'transaction': txn.pk, 'journal': txn.journal_id},
        )
    if txn.hold_id is not None and not txn.hold.is_effectively_active():
        raise ReconciliationInternalIntegrityError(
            'An unresolved transaction no longer reserves the customer funds.',
            details={'transaction': txn.pk, 'hold': txn.hold_id},
        )


def _has_recovery_conflict(attempt_id: int) -> bool:
    """Whether M7 already recorded definitive evidence that disagrees.

    Surfaced rather than re-derived, and never resolved here. An investigator
    reading a reconciliation finding needs to know the rail has already
    contradicted itself once.
    """
    outcomes = set(
        ProviderRecoveryEvidence.objects.filter(
            provider_attempt_id=attempt_id,
            outcome__in=[RecoveryOutcome.SUCCEEDED, RecoveryOutcome.FAILED],
        ).values_list('outcome', flat=True)
    )
    return len(outcomes) > 1


def _internal_view(attempt) -> InternalTransferView:
    """Project one attempt into the read-only shape comparison works on.

    Deliberately a value object holding no model instance: nothing downstream
    can reach a financial row through it, which is what keeps "reconciliation
    cannot mutate" a structural property rather than a promise.

    Called only on an attempt whose transaction and attempt rows are already
    locked by :func:`_locked_internal_candidates`. Every dependent read below —
    hold, journal, transfer, recovery evidence — therefore comes from one
    settled moment rather than from four successive ``READ COMMITTED``
    snapshots that a concurrent resolution could fall between.
    """
    txn = attempt.financial_transaction
    transfer = getattr(txn, 'transfer', None)

    _require_internal_integrity(attempt, txn, transfer)

    outcome = INTERNAL_OUTCOME_BY_TRANSACTION_STATUS.get(txn.status)
    if outcome is None:
        raise ReconciliationInternalIntegrityError(
            'A transaction is in a state reconciliation cannot read.',
            details={'transaction': txn.pk, 'status': txn.status},
        )

    return InternalTransferView(
        attempt_id=attempt.pk,
        client_reference=attempt.client_reference,
        provider_reference=attempt.provider_reference,
        outcome=outcome,
        amount_minor=txn.amount_minor,
        currency=txn.currency,
        has_recovery_conflict=_has_recovery_conflict(attempt.pk),
    )


def _locked_internal_candidates(provider_key: str, window: ReconciliationWindow):
    """The attempts a run compares, each pinned under an observation lock.

    Inclusion is by ``ProviderExecutionAttempt.started_at`` — the moment
    SpendWise claimed execution — because it is ours, immutable and always
    present. A provider's export is keyed on *its* observation time, so in
    production the two views of a window can disagree at the edges and a
    transfer near a boundary may surface as internal-only in one run and
    matched in the next. That is a property of comparing two clocks, not a
    defect, and the production alignment rule stays open (O-49).

    The first query reads identities only. The rows are then locked in a fixed
    order — every ``FinancialTransaction`` ascending by primary key, then every
    ``ProviderExecutionAttempt`` ascending by primary key — so that the order
    is a property of the data and never of the provider's export. Only after
    the locks are held is any dependent state read, which is what makes each
    transfer's internal state a single consistent observation under
    ``READ COMMITTED``.

    Note the absence of ``select_related`` here: the joins are gone precisely
    so that ``FOR UPDATE`` names one table. Hold, journal and transfer load
    lazily, after their transaction row is pinned.
    """
    identities = list(
        ProviderExecutionAttempt.objects.filter(
            provider_key=provider_key,
            started_at__gte=window.start,
            started_at__lt=window.end,
        )
        .order_by('pk')
        .values_list('pk', 'financial_transaction_id')
    )
    if not identities:
        return []

    transactions = {
        txn.pk: txn
        for txn in FinancialTransaction.objects.select_for_update()
        .filter(pk__in=sorted({txn_id for _, txn_id in identities}))
        .order_by('pk')
    }

    attempts = list(
        ProviderExecutionAttempt.objects.select_for_update()
        .filter(pk__in=sorted(pk for pk, _ in identities))
        .order_by('pk')
    )
    for attempt in attempts:
        # Hand each attempt the locked row, so no later access re-reads an
        # unpinned transaction through the descriptor.
        attempt.financial_transaction = transactions[
            attempt.financial_transaction_id
        ]
    return attempts


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------


def _match(record: ProviderTransferRecord, by_client, by_provider):
    """Find the one execution a record is about, or nothing.

    Correlation is by identity only. ``client_reference`` comes first because
    SpendWise owns it and M7 made it unique; ``provider_reference`` corroborates
    and can stand alone when it identifies exactly one attempt.

    **There is no fallback to amount, time, currency or recipient.** A record
    that merely looks like a transfer is not that transfer, and a
    reconciliation willing to guess would quietly attach a rail's evidence to
    the wrong customer's money.
    """
    client_match = (
        by_client.get(record.client_reference)
        if record.client_reference else None
    )

    provider_match = None
    if record.provider_reference:
        candidates = by_provider.get(record.provider_reference, ())
        if len(candidates) == 1:
            provider_match = candidates[0]
        elif len(candidates) > 1:
            raise ReconciliationReferenceConflictError(
                'That provider reference names more than one execution.',
                details={
                    'provider_record_id': record.provider_record_id,
                    'provider_reference': record.provider_reference,
                },
            )

    if client_match is not None and provider_match is not None:
        if client_match.attempt_id != provider_match.attempt_id:
            # The two references disagree about which execution this is.
            # Believing one and ignoring the other is how evidence lands on
            # the wrong transfer.
            raise ReconciliationReferenceConflictError(
                'The client and provider references in that record name '
                'different executions.',
                details={'provider_record_id': record.provider_record_id},
            )

    return client_match or provider_match


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


@transaction.atomic
def _open_run(provider_key: str, window: ReconciliationWindow):
    return ProviderReconciliationRun.objects.create(
        provider_key=provider_key,
        window_start=window.start,
        window_end=window.end,
        status=ReconciliationRunStatus.STARTED,
    )


@transaction.atomic
def _fail_run(run_id: int) -> None:
    """Record that a comparison was attempted and could not be carried out.

    Written in its own transaction so it survives whatever went wrong. No item
    is recorded and nothing financial is touched — there was nothing to touch.
    """
    run = ProviderReconciliationRun.objects.get(pk=run_id)
    run.status = ReconciliationRunStatus.FAILED_INTERNAL
    run.completed_at = timezone.now()
    run.save(update_fields=['status', 'completed_at'])


@transaction.atomic
def _classify_and_record(run_id: int, records) -> ProviderReconciliationRun:
    """Compare, then write every finding, or write none at all.

    One transaction for the whole run: a reconciliation report that recorded
    half its findings would look complete while having compared less than it
    claims, which is worse than reporting nothing.
    """
    run = ProviderReconciliationRun.objects.get(pk=run_id)
    window = ReconciliationWindow(start=run.window_start, end=run.window_end)

    views = [
        _internal_view(attempt)
        for attempt in _locked_internal_candidates(run.provider_key, window)
    ]

    by_client = {}
    by_provider: dict[str, list] = {}
    for view in views:
        if view.client_reference:
            if view.client_reference in by_client:
                # M7 made this unique, so reaching here means our own data is
                # broken — not something a provider did.
                raise ReconciliationInternalIntegrityError(
                    'Two execution attempts share one client reference.',
                    details={'client_reference': view.client_reference},
                )
            by_client[view.client_reference] = view
        if view.provider_reference:
            by_provider.setdefault(view.provider_reference, []).append(view)

    matched_attempt_ids = set()
    items = []

    for record in records:
        view = _match(record, by_client, by_provider)

        if view is None:
            # The provider knows about something we have no record of. It is
            # never turned into a transaction, and never attached to whichever
            # transfer looks closest.
            items.append(
                ProviderReconciliationItem(
                    reconciliation_run=run,
                    provider_attempt=None,
                    provider_key=record.provider_key,
                    provider_record_id=record.provider_record_id,
                    client_reference=record.client_reference,
                    provider_reference=record.provider_reference,
                    overall_status=ReconciliationStatus.PROVIDER_ONLY,
                    provider_outcome=record.outcome,
                    provider_amount_minor=record.amount_minor,
                    provider_currency=record.currency,
                    observed_at=record.observed_at,
                )
            )
            continue

        matched_attempt_ids.add(view.attempt_id)
        comparison: Comparison = compare(record, view)

        items.append(
            ProviderReconciliationItem(
                reconciliation_run=run,
                provider_attempt_id=view.attempt_id,
                provider_key=record.provider_key,
                provider_record_id=record.provider_record_id,
                client_reference=view.client_reference,
                provider_reference=record.provider_reference,
                overall_status=comparison.overall_status,
                provider_outcome=record.outcome,
                internal_outcome=view.outcome,
                provider_amount_minor=record.amount_minor,
                internal_amount_minor=view.amount_minor,
                provider_currency=record.currency,
                internal_currency=view.currency,
                outcome_mismatch=comparison.outcome_mismatch,
                amount_mismatch=comparison.amount_mismatch,
                currency_mismatch=comparison.currency_mismatch,
                reference_mismatch=comparison.reference_mismatch,
                has_recovery_conflict=view.has_recovery_conflict,
                observed_at=record.observed_at,
            )
        )

    for view in views:
        if view.attempt_id in matched_attempt_ids:
            continue
        # We attempted something the provider did not report. **Not a
        # failure**: an export delay, a window edge or an omission look
        # identical to "it never arrived", and concluding failure would
        # release a customer's reservation on that guess.
        items.append(
            ProviderReconciliationItem(
                reconciliation_run=run,
                provider_attempt_id=view.attempt_id,
                provider_key=run.provider_key,
                provider_record_id='',
                client_reference=view.client_reference,
                provider_reference=view.provider_reference,
                overall_status=ReconciliationStatus.INTERNAL_ONLY,
                internal_outcome=view.outcome,
                internal_amount_minor=view.amount_minor,
                internal_currency=view.currency,
                has_recovery_conflict=view.has_recovery_conflict,
            )
        )

    ProviderReconciliationItem.objects.bulk_create(items)

    run.status = ReconciliationRunStatus.COMPLETED
    run.completed_at = timezone.now()
    run.save(update_fields=['status', 'completed_at'])
    return run


def reconcile_transfers(
    *, provider, window: ReconciliationWindow
) -> ProviderReconciliationRun:
    """Compare a provider's records with internal truth, and record what differs.

    Observational throughout: safe to repeat, and it changes no financial
    state whatever it finds. Each invocation is a **new run** — a separate
    observation made at a separate moment — because a rerun that overwrote the
    first would destroy the record that the answer had once been different.

    Raises rather than reporting a clean result when the comparison cannot be
    carried out honestly: unreadable provider data, an export that contradicts
    itself, references that name two executions, or our own records disagreeing
    with themselves. In every one of those cases the run is marked
    ``FAILED_INTERNAL``, no findings are written, and nothing financial moves.
    """
    if not isinstance(window, ReconciliationWindow):
        window = ReconciliationWindow(start=window.start, end=window.end)

    run = _open_run(provider.provider_key, window)

    try:
        # ---- outside any database transaction, holding no row lock ----
        fetched = _fetch_provider_records(provider, window)
        # ---------------------------------------------------------------
        records = _deduplicate(fetched)
    except Exception:
        # Includes a provider that raised: the run records that a comparison
        # was attempted, and no finding is invented from data we never got.
        _fail_run(run.pk)
        raise

    try:
        return _classify_and_record(run.pk, records)
    except Exception:
        _fail_run(run.pk)
        raise


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def run_summary(run: ProviderReconciliationRun) -> dict:
    """Deterministic counts for one run. Derived, never stored.

    A persisted counter is one more thing that can disagree with the rows it
    counts, and these are cheap to compute from the findings themselves.
    """
    items = ProviderReconciliationItem.objects.filter(reconciliation_run=run)

    return {
        'total_items': items.count(),
        'provider_records': items.exclude(provider_record_id='').count(),
        'matched': items.filter(
            overall_status=ReconciliationStatus.MATCHED
        ).count(),
        'discrepancies': items.filter(
            overall_status=ReconciliationStatus.DISCREPANCY
        ).count(),
        'provider_only': items.filter(
            overall_status=ReconciliationStatus.PROVIDER_ONLY
        ).count(),
        'internal_only': items.filter(
            overall_status=ReconciliationStatus.INTERNAL_ONLY
        ).count(),
        'outcome_mismatches': items.filter(outcome_mismatch=True).count(),
        'amount_mismatches': items.filter(amount_mismatch=True).count(),
        'currency_mismatches': items.filter(currency_mismatch=True).count(),
        'reference_mismatches': items.filter(reference_mismatch=True).count(),
        'recovery_conflicts': items.filter(has_recovery_conflict=True).count(),
    }


def items_for(run: ProviderReconciliationRun):
    """Every finding in a run, in a stable order."""
    return ProviderReconciliationItem.objects.filter(
        reconciliation_run=run
    ).order_by('pk')
