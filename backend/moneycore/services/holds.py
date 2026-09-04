"""Holds and balance projection — the authoritative service boundary.

Every hold is created, released and expired here. Nothing else writes a hold,
and no hold operation ever writes to the ledger.

The central problem this module solves
--------------------------------------
Two requests reserving funds from the same wallet at the same moment must not
both succeed if only one of them fits. A naive "read the available balance, then
insert" is exactly the check-then-act race that lets both readers see the same
headroom and oversubscribe the wallet.

The fix is to serialise competing reservations for a wallet by taking a row lock
on the ``Wallet`` row itself before deriving anything::

    with transaction.atomic():
        SELECT ... FROM wallet WHERE id = %s FOR UPDATE   # gate
        posted  = <derived from immutable ledger entries>
        held    = <derived from effectively-active holds>
        validate posted - held >= amount
        INSERT hold

The Wallet row is a natural gate: it already exists, it is stable, it is exactly
one per wallet, and locking it costs nothing because nothing else contends for
it. It is emphatically **not** a balance row — it holds no balance, and none was
invented to have something to lock. Holding the lock for the whole read-validate-
insert window is what makes the availability check meaningful, because no other
reservation for that wallet can interleave.

This is PostgreSQL's semantics. SQLite's ``SELECT ... FOR UPDATE`` is a no-op, so
the concurrency guarantee is a PostgreSQL guarantee and the threaded tests say so
rather than pretending otherwise.

Concurrency between hold creation and the other money operations
----------------------------------------------------------------
*Ledger posting.* Posting does not take the wallet lock, so a journal may commit
while a hold is being evaluated. That is safe and deliberate: posting only ever
adds immutable entries, so a hold evaluated against a slightly older posted
balance is evaluated against a **smaller or equal** figure than the one that
lands. A hold can therefore be refused a moment before funds appear, but it can
never be granted against funds that were not there. Being conservative in that
direction is the correct bias, and making posting contend for a wallet lock would
push M2's ledger design around for no gain.

*Release and expiry.* Both take the same wallet lock, so they serialise against
creation. A release that commits first simply means the next creation sees the
freed headroom.

What this module does not do
----------------------------
No capture, no settlement, no hold-to-journal conversion — a hold never becomes
an accounting entry here, and that orchestration belongs to M4/M5. No scheduling:
expiry correctness is a read-time predicate, so no worker is required and none is
introduced.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from moneycore.domain.errors import (
    BalanceProjectionInvalidError,
    HoldCurrencyMismatchError,
    HoldNotActiveError,
    HoldNotDueForExpiryError,
    InsufficientAvailableBalanceError,
    InvalidHoldAmountError,
    InvalidHoldTransitionError,
    WalletNotHoldableError,
)
from moneycore.domain.holds import (
    BalanceProjection,
    HoldStatus,
    can_transition_hold,
    is_effectively_active,
)
from moneycore.domain.ledger import LedgerAccountStatus, is_valid_amount_minor
from moneycore.domain.lifecycle import WalletStatus
from moneycore.domain.money import Money
from moneycore.models import FundsHold, LedgerAccount, Wallet
from moneycore.services.ledger import wallet_posted_balance


# --------------------------------------------------------------------------
# Derivation
# --------------------------------------------------------------------------


def held_amount(wallet: Wallet, at=None) -> Money:
    """The total reserved by effectively-active holds at ``at`` (default now).

    Integer aggregation over rows, with no cached column and no mutable total.
    A hold past its ``expires_at`` is excluded whether or not anything has
    written ``status=EXPIRED``, so this figure is correct without a cleanup job.
    """
    moment = at if at is not None else timezone.now()
    total = (
        FundsHold.objects.filter(wallet=wallet)
        .effectively_active(moment)
        .aggregate(total=Sum('amount_minor'))['total']
        or 0
    )
    return Money(total, wallet.currency)


def wallet_balance_projection(wallet: Wallet, at=None) -> BalanceProjection:
    """The wallet's posted / held / available picture at ``at`` (default now).

    Raises :class:`~moneycore.domain.errors.WalletLedgerAccountNotFoundError`
    when the wallet has no ledger account — an unmapped wallet has no
    authoritative balance, and reporting 0/0/0 for it would fabricate one. That
    M2 invariant is inherited here unchanged, not re-decided.

    Raises :class:`~moneycore.domain.errors.BalanceProjectionInvalidError` if the
    result is not financially coherent — held exceeding posted, or a negative
    posted balance on a customer wallet. Both mean something upstream is wrong,
    and both are surfaced rather than clamped: reporting a tidy zero would hide
    the fault behind a plausible number.

    Performs no writes.
    """
    posted = wallet_posted_balance(wallet)  # raises if unmapped
    held = held_amount(wallet, at)
    _require_coherent(wallet, posted, held)
    return BalanceProjection.build(posted=posted, held=held)


def _require_coherent(wallet: Wallet, posted: Money, held: Money) -> None:
    """Reject an incoherent balance picture rather than tidying it away."""
    if posted.minor_units < 0:
        # M3 invents no overdraft. A negative posted balance on a customer
        # wallet is not a supported state, so it is reported, not absorbed.
        raise BalanceProjectionInvalidError(
            'This wallet has a negative posted balance, which is not a '
            'supported state.',
            details={
                'wallet': wallet.pk,
                'posted_minor': posted.minor_units,
                'currency': posted.currency,
            },
        )
    if held.minor_units > posted.minor_units:
        raise BalanceProjectionInvalidError(
            'Held funds exceed posted funds for this wallet.',
            details={
                'wallet': wallet.pk,
                'posted_minor': posted.minor_units,
                'held_minor': held.minor_units,
                'currency': posted.currency,
            },
        )


# --------------------------------------------------------------------------
# Creation
# --------------------------------------------------------------------------


def _require_holdable(wallet: Wallet) -> LedgerAccount:
    """The wallet must be usable and mapped to an open ledger account."""
    if wallet.status != WalletStatus.ACTIVE:
        raise WalletNotHoldableError(
            'This wallet is not active.',
            details={'wallet': wallet.pk, 'status': wallet.status},
        )

    account = LedgerAccount.objects.filter(wallet=wallet).first()
    if account is None:
        # Deliberately the same M2 error as reading a balance: the wallet has no
        # ledger relationship, which is not the same as having no funds.
        from moneycore.domain.errors import WalletLedgerAccountNotFoundError

        raise WalletLedgerAccountNotFoundError(
            'This wallet has no ledger account, so funds cannot be held.',
            details={'wallet': wallet.pk, 'currency': wallet.currency},
        )
    if account.status != LedgerAccountStatus.ACTIVE:
        raise WalletNotHoldableError(
            'The ledger account for this wallet is closed.',
            details={'wallet': wallet.pk, 'ledger_account': account.code},
        )
    return account


@transaction.atomic
def create_hold(
    wallet: Wallet,
    amount_minor: int,
    *,
    expires_at=None,
    reason: str = '',
) -> FundsHold:
    """Reserve ``amount_minor`` of the wallet's available funds.

    Locks the wallet row for the whole read-validate-insert window, so competing
    reservations for the same wallet serialise and cannot oversubscribe it (see
    the module docstring).

    Creates **no** journal and no ledger entry: reserving funds is not moving
    them.
    """
    if not is_valid_amount_minor(amount_minor):
        # Covers zero, negatives, float, Decimal, bool and out-of-range.
        raise InvalidHoldAmountError(
            'A hold amount must be a positive whole number of minor units '
            'within the supported 64-bit range.',
            details={'amount_type': type(amount_minor).__name__},
        )

    # The gate. Everything below reads and writes under this lock.
    locked_wallet = Wallet.objects.select_for_update().get(pk=wallet.pk)

    _require_holdable(locked_wallet)

    posted = wallet_posted_balance(locked_wallet)
    held = held_amount(locked_wallet)
    _require_coherent(locked_wallet, posted, held)
    projection = BalanceProjection.build(posted=posted, held=held)

    requested = Money(amount_minor, locked_wallet.currency)
    if requested > projection.available:
        raise InsufficientAvailableBalanceError(
            'There is not enough available balance to place this hold.',
            details={
                'wallet': locked_wallet.pk,
                'requested_minor': amount_minor,
                'available_minor': projection.available.minor_units,
                'currency': locked_wallet.currency,
            },
        )

    return FundsHold.objects.create(
        wallet=locked_wallet,
        currency=locked_wallet.currency,
        amount_minor=amount_minor,
        status=HoldStatus.ACTIVE,
        expires_at=expires_at,
        reason=reason,
    )


# --------------------------------------------------------------------------
# Release and expiry
# --------------------------------------------------------------------------


@transaction.atomic
def release_hold(hold: FundsHold) -> FundsHold:
    """Give the reserved funds back.

    Contract: **not idempotent.** Releasing an already-terminal hold raises
    rather than quietly succeeding, so a duplicate release is surfaced as the
    caller mistake it is instead of being absorbed. M4's idempotency-key
    infrastructure is where safe retry semantics will live; inventing a private
    version of it here would pre-empt that decision.

    Creates no journal — the ledger never recorded the reservation, so there is
    nothing there to undo.
    """
    # Same gate as creation, so a release cannot interleave with a reservation
    # that is mid-evaluation for this wallet.
    Wallet.objects.select_for_update().get(pk=hold.wallet_id)
    locked = FundsHold.objects.select_for_update().get(pk=hold.pk)

    if locked.status != HoldStatus.ACTIVE:
        raise HoldNotActiveError(
            'Only an active hold can be released.',
            details={'hold': locked.pk, 'status': locked.status},
        )
    if not can_transition_hold(locked.status, HoldStatus.RELEASED):
        raise InvalidHoldTransitionError(
            f'A {locked.status} hold cannot be released.',
            details={'current': locked.status, 'requested': HoldStatus.RELEASED},
        )

    locked.status = HoldStatus.RELEASED
    locked.released_at = timezone.now()
    locked.save(update_fields=['status', 'released_at', 'updated_at'])
    return locked


@transaction.atomic
def expire_hold(hold: FundsHold, at=None) -> FundsHold:
    """Record that a hold's time ran out.

    Only legal once the hold is actually due. Ending a reservation early is a
    *release*, not an expiry — they record different operational facts, and
    allowing early expiry would blur that.

    This writes down a state the projection already reflects: an overdue hold
    stops reserving funds the moment it passes ``expires_at``, so this changes
    no balance. Creates no journal.
    """
    moment = at if at is not None else timezone.now()

    Wallet.objects.select_for_update().get(pk=hold.wallet_id)
    locked = FundsHold.objects.select_for_update().get(pk=hold.pk)

    if locked.status != HoldStatus.ACTIVE:
        raise HoldNotActiveError(
            'Only an active hold can expire.',
            details={'hold': locked.pk, 'status': locked.status},
        )
    if locked.expires_at is None or locked.expires_at > moment:
        raise HoldNotDueForExpiryError(
            'That hold is not due to expire. Release it instead.',
            details={
                'hold': locked.pk,
                'expires_at': locked.expires_at.isoformat() if locked.expires_at else None,
            },
        )

    locked.status = HoldStatus.EXPIRED
    locked.expired_at = moment
    locked.save(update_fields=['status', 'expired_at', 'updated_at'])
    return locked


def expire_due_holds(*, wallet: Wallet | None = None, at=None) -> list[FundsHold]:
    """Write ``EXPIRED`` onto holds whose time has passed.

    Housekeeping only. The projection is already correct without it — an overdue
    hold stops reserving funds at its expiry regardless — so this exists to keep
    stored status honest, not to make balances right. Nothing schedules it, and
    M3 adds no worker to run it.
    """
    moment = at if at is not None else timezone.now()
    candidates = FundsHold.objects.filter(
        status=HoldStatus.ACTIVE,
        expires_at__isnull=False,
        expires_at__lte=moment,
    )
    if wallet is not None:
        candidates = candidates.filter(wallet=wallet)

    return [expire_hold(hold, at=moment) for hold in list(candidates)]


def is_hold_effective(hold: FundsHold, at=None) -> bool:
    """Whether this hold reserves funds at ``at`` (default now)."""
    moment = at if at is not None else timezone.now()
    return is_effectively_active(hold.status, hold.expires_at, moment)
