"""The transaction engine — the authoritative state machine.

Every status change goes through here. Nothing assigns ``transaction.status``
directly, no serializer orchestrates a transition, and no signal is involved.

Lock ordering
-------------
M3 established the ``Wallet`` row as the serialisation gate for everything that
affects spendability, and M2's posting service takes the same gate. M4 follows
that order exactly rather than inventing a second one::

    1. every relevant Wallet row, ascending primary key   (reuses M2's helper)
    2. the FinancialTransaction row
    3. the FundsHold row, via the M3 service
    4. ledger posting                                     (re-enters the same
                                                           wallet locks, already
                                                           held by this
                                                           transaction)
    5. the transaction update

Locking the wallet first everywhere is what keeps M4 from introducing a new
deadlock cycle: no path in the codebase takes a transaction or hold lock before
a wallet lock. Multiple wallets are locked in ascending primary-key order, which
is M2's rule, reused here rather than reimplemented.

Why success has to be one transaction
-------------------------------------
A successful outgoing operation must release the reservation *and* post the
ledger entries together. It cannot be two commits: while the hold is active,
M3's posting guard correctly refuses a journal that would reduce the wallet
below its reserved funds, and if the release were committed first and the
posting then failed, the customer's money would be unreserved and unspent —
briefly spendable twice. So both happen inside one ``atomic`` block, and either
both land or neither does.

What this module does not know
------------------------------
It does not know what a transfer is, which ledger accounts one uses, or that
providers exist. Success orchestration therefore *accepts* prepared entries
from the calling domain: M4 posts what it is given, and M5 will supply the
transfer-specific accounting. Inventing those accounts here would put transfer
knowledge in the wrong milestone.

It also has no cancellation, and no abandonment transition of any name. A
customer backing out before execution is accepted produces no transaction at
all, so there is nothing here to cancel (locked architecture §8.5).
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone

from moneycore.domain.currency import is_valid_currency_code
from moneycore.domain.errors import (
    InvalidTransactionAmountError,
    InvalidTransactionDirectionError,
    InvalidTransactionTransitionError,
    TransactionAlreadyResolvedError,
    TransactionCurrencyMismatchError,
    TransactionHoldInvalidError,
    TransactionHoldRequiredError,
    TransactionIdempotencyConflictError,
    TransactionSuccessRequiresJournalError,
)
from moneycore.domain.holds import HoldStatus
from moneycore.domain.ledger import JournalStatus, is_valid_amount_minor
from moneycore.domain.transactions import (
    TransactionDirection,
    TransactionStatus,
    can_transition,
    is_terminal,
    requires_reservation,
)
from moneycore.models import FinancialTransaction, FundsHold, Wallet
from moneycore.services.holds import is_hold_effective, release_hold
from moneycore.services.ledger import _lock_wallets, _wallet_balance_deltas, post_journal


# --------------------------------------------------------------------------
# Creation
# --------------------------------------------------------------------------


def _validate_intent(wallet: Wallet, direction: str, amount_minor: int) -> None:
    if direction not in TransactionDirection.ALL:
        raise InvalidTransactionDirectionError(
            f'{direction!r} is not a transaction direction.',
            details={'direction': str(direction)},
        )
    if not is_valid_amount_minor(amount_minor):
        # Covers zero, negatives, float, Decimal, str, bool and out-of-range.
        raise InvalidTransactionAmountError(
            'A transaction amount must be a positive whole number of minor '
            'units within the supported 64-bit range.',
            details={'amount_type': type(amount_minor).__name__},
        )
    if not is_valid_currency_code(wallet.currency):
        raise TransactionCurrencyMismatchError(
            'The wallet currency is not a valid ISO-4217 code.',
            details={'currency': str(wallet.currency)},
        )


def _same_intent(existing: FinancialTransaction, direction, amount_minor, currency) -> bool:
    return (
        existing.direction == direction
        and existing.amount_minor == amount_minor
        and existing.currency == currency
    )


@transaction.atomic
def create_transaction(
    wallet: Wallet,
    direction: str,
    amount_minor: int,
    *,
    idempotency_key: str,
    currency: str | None = None,
) -> FinancialTransaction:
    """Record the intent to perform one financial operation.

    Idempotent on ``(wallet, idempotency_key)``: the same key with the same
    intent returns the transaction that already exists, and the same key with a
    *different* intent raises rather than quietly ignoring what was asked for.

    Concurrency rests on the unique constraint, not on a read-then-write check:
    two simultaneous callers both attempt the insert, one wins, and the loser
    catches the integrity error and returns the winner's row. Exactly one
    transaction exists either way.

    This is transaction-intent idempotency only. It is not a general framework,
    and it says nothing about provider or webhook idempotency, which belong to
    M6 and M7.
    """
    currency = currency or wallet.currency
    _validate_intent(wallet, direction, amount_minor)

    if currency != wallet.currency:
        raise TransactionCurrencyMismatchError(
            'A transaction must use the wallet currency.',
            details={'wallet_currency': wallet.currency, 'requested': currency},
        )

    existing = FinancialTransaction.objects.filter(
        wallet=wallet, idempotency_key=idempotency_key
    ).first()
    if existing is not None:
        if not _same_intent(existing, direction, amount_minor, currency):
            raise TransactionIdempotencyConflictError(
                'That idempotency key was already used for a different '
                'transaction.',
                details={
                    'idempotency_key': idempotency_key,
                    'existing': {
                        'direction': existing.direction,
                        'amount_minor': existing.amount_minor,
                        'currency': existing.currency,
                    },
                    'requested': {
                        'direction': direction,
                        'amount_minor': amount_minor,
                        'currency': currency,
                    },
                },
            )
        return existing

    try:
        with transaction.atomic():
            return FinancialTransaction.objects.create(
                wallet=wallet,
                direction=direction,
                amount_minor=amount_minor,
                currency=currency,
                status=TransactionStatus.CREATED,
                idempotency_key=idempotency_key,
            )
    except IntegrityError:
        # Another caller inserted the same key first. Return theirs, or raise if
        # what they asked for differs from what we did.
        winner = FinancialTransaction.objects.get(
            wallet=wallet, idempotency_key=idempotency_key
        )
        if not _same_intent(winner, direction, amount_minor, currency):
            raise TransactionIdempotencyConflictError(
                'That idempotency key was already used for a different '
                'transaction.',
                details={'idempotency_key': idempotency_key},
            ) from None
        return winner


# --------------------------------------------------------------------------
# Shared transition machinery
# --------------------------------------------------------------------------


def _lock_for_transition(txn: FinancialTransaction, extra_wallet_ids=()) -> FinancialTransaction:
    """Take the wallet locks, then the transaction row lock, in that order."""
    wallet_ids = {txn.wallet_id, *extra_wallet_ids}
    _lock_wallets(wallet_ids)
    return FinancialTransaction.objects.select_for_update().get(pk=txn.pk)


def _require_transition(txn: FinancialTransaction, target: str) -> None:
    """Refuse an illegal move, distinguishing 'already settled' from 'illegal'."""
    if is_terminal(txn.status):
        raise TransactionAlreadyResolvedError(
            f'That transaction is already {txn.status}.',
            details={'current': txn.status, 'requested': target},
        )
    if not can_transition(txn.status, target):
        raise InvalidTransactionTransitionError(
            f'A {txn.status} transaction cannot become {target}.',
            details={'current': txn.status, 'requested': target},
        )


def _validate_hold_for(txn: FinancialTransaction, hold: FundsHold) -> None:
    if hold.wallet_id != txn.wallet_id:
        raise TransactionHoldInvalidError(
            'That reservation belongs to a different wallet.',
            details={'hold': hold.pk},
        )
    if hold.currency != txn.currency:
        raise TransactionHoldInvalidError(
            'That reservation is in a different currency.',
            details={'hold': hold.pk, 'hold_currency': hold.currency},
        )
    owner = FinancialTransaction.objects.filter(hold=hold).exclude(pk=txn.pk).first()
    if owner is not None:
        raise TransactionHoldInvalidError(
            'That reservation already funds another transaction.',
            details={'hold': hold.pk, 'owning_transaction': owner.pk},
        )


@transaction.atomic
def attach_hold(txn: FinancialTransaction, hold: FundsHold) -> FinancialTransaction:
    """Bind a reservation to a transaction before execution starts.

    Only while the transaction is still ``CREATED``: once execution may have
    begun, changing which funds back it would rewrite what was attempted.

    The hold's amount is deliberately **not** required to equal the
    transaction's. A reservation may legitimately cover more than the principal
    — fees are the obvious future case — and M4 has no business inventing that
    arithmetic.
    """
    locked = _lock_for_transition(txn)

    if locked.status != TransactionStatus.CREATED:
        raise InvalidTransactionTransitionError(
            'A reservation can only be attached before execution starts.',
            details={'current': locked.status},
        )

    locked_hold = FundsHold.objects.select_for_update().get(pk=hold.pk)
    _validate_hold_for(locked, locked_hold)

    locked.hold = locked_hold
    locked.save(update_fields=['hold', 'updated_at'])
    return locked


@transaction.atomic
def start_processing(txn: FinancialTransaction) -> FinancialTransaction:
    """Begin execution.

    An outgoing transaction must already have an **effectively active**
    reservation: an expired one no longer reserves anything, so starting against
    it would let the same funds be spent twice. M4 neither renews nor extends a
    reservation — how long one should live is still open (O-17) — so an expired
    hold is a refusal, not something to quietly repair.
    """
    locked = _lock_for_transition(txn)
    _require_transition(locked, TransactionStatus.PROCESSING)

    if requires_reservation(locked.direction):
        if locked.hold_id is None:
            raise TransactionHoldRequiredError(
                'An outgoing transaction needs a reservation before it can '
                'start.',
                details={'transaction': locked.pk},
            )
        hold = FundsHold.objects.select_for_update().get(pk=locked.hold_id)
        if not is_hold_effective(hold):
            raise TransactionHoldInvalidError(
                'The reservation for this transaction is no longer active.',
                details={
                    'transaction': locked.pk,
                    'hold': hold.pk,
                    'hold_status': hold.status,
                },
            )

    locked.status = TransactionStatus.PROCESSING
    locked.processing_at = timezone.now()
    locked.save(update_fields=['status', 'processing_at', 'updated_at'])
    return locked


@transaction.atomic
def mark_unknown(txn: FinancialTransaction) -> FinancialTransaction:
    """Record that the outcome cannot yet be established.

    **This is not a failure.** An operation that may already have executed and
    whose result is unavailable is unresolved, not unsuccessful, and saying
    otherwise would tell a customer their money did not move when it may well
    have.

    So this deliberately does nothing except record the state: no journal is
    posted, no reservation is released, no balance changes. **The hold stays
    active**, which is exactly what keeps the funds reserved while SpendWise
    works out what happened. The state is persisted, so it survives any process
    or worker restart.
    """
    locked = _lock_for_transition(txn)
    _require_transition(locked, TransactionStatus.UNKNOWN)

    locked.status = TransactionStatus.UNKNOWN
    locked.unknown_at = timezone.now()
    locked.save(update_fields=['status', 'unknown_at', 'updated_at'])
    return locked


@transaction.atomic
def fail_transaction(
    txn: FinancialTransaction, *, failure_code: str = ''
) -> FinancialTransaction:
    """Record an authoritative failure: the money is known not to have moved.

    Only call this when that is actually known. An absent, timed-out or
    ambiguous answer is :func:`mark_unknown`, never this — there is no path in
    the state machine that turns "we did not hear back" into a failure.

    Because nothing moved, any reservation is released in the same transaction:
    the funds were never spent, so they must not stay reserved. Either both the
    status change and the release commit, or neither does — a transaction cannot
    end up FAILED with the customer's money still locked away.
    """
    locked = _lock_for_transition(txn)
    _require_transition(locked, TransactionStatus.FAILED)

    if locked.hold_id is not None:
        hold = FundsHold.objects.select_for_update().get(pk=locked.hold_id)
        if hold.status == HoldStatus.ACTIVE:
            release_hold(hold)

    locked.status = TransactionStatus.FAILED
    locked.failure_code = failure_code
    locked.resolved_at = timezone.now()
    locked.save(
        update_fields=['status', 'failure_code', 'resolved_at', 'updated_at']
    )
    return locked


@transaction.atomic
def succeed_transaction(
    txn: FinancialTransaction,
    *,
    entries,
    description: str = '',
    reference: str = '',
) -> FinancialTransaction:
    """Record authoritative success, with the financial truth to back it.

    ``entries`` are prepared :class:`~moneycore.services.ledger.EntryDraft`
    values supplied by the calling domain. M4 posts them; it does not decide
    which ledger accounts a transfer uses, because it does not know what a
    transfer is. M5 will supply that.

    Everything happens in one database transaction, in this order:

    1. lock every affected wallet (this transaction's, plus any wallet-backed
       account named in the entries), ascending primary key
    2. lock the transaction row
    3. release the reservation
    4. post the balanced journal
    5. link it and mark the transaction succeeded

    The release must precede the posting, and both must share a transaction:
    M3's posting guard refuses a journal that would take a wallet below its
    reserved funds, so an active hold would block the very posting it exists to
    enable. Releasing in a separate committed step instead would leave a window
    where the funds are neither reserved nor spent.

    If the posting is rejected for any reason, the release rolls back with it —
    the reservation is still there and the transaction has not moved.
    """
    # Wallets named by the entries as well as the transaction's own, so the
    # locks taken here cover everything post_journal will touch.
    entry_wallet_ids = set(_wallet_balance_deltas(list(entries)).keys())
    locked = _lock_for_transition(txn, extra_wallet_ids=entry_wallet_ids)
    _require_transition(locked, TransactionStatus.SUCCEEDED)

    if locked.hold_id is not None:
        hold = FundsHold.objects.select_for_update().get(pk=locked.hold_id)
        if hold.status == HoldStatus.ACTIVE:
            release_hold(hold)

    journal = post_journal(
        currency=locked.currency,
        entries=list(entries),
        description=description or f'Transaction {locked.pk}',
        reference=reference,
    )

    if journal.status != JournalStatus.POSTED:
        # Defensive: post_journal only ever returns posted journals, but success
        # must never be certified by a draft.
        raise TransactionSuccessRequiresJournalError(
            'A successful transaction must have a posted journal.',
            details={'transaction': locked.pk, 'journal_status': journal.status},
        )
    if journal.currency != locked.currency:
        raise TransactionSuccessRequiresJournalError(
            'The success journal must use the transaction currency.',
            details={
                'transaction': locked.pk,
                'transaction_currency': locked.currency,
                'journal_currency': journal.currency,
            },
        )

    locked.status = TransactionStatus.SUCCEEDED
    locked.journal = journal
    locked.resolved_at = timezone.now()
    locked.save(update_fields=['status', 'journal', 'resolved_at', 'updated_at'])
    return locked
