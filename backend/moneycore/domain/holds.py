"""Holds, and the posted / held / available projection.

Pure domain: the hold lifecycle, what makes a hold *effective*, and the value
object describing a wallet's balance picture. Nothing here touches the database.

What a hold is — and is not
---------------------------
A hold says **"some already-posted funds are temporarily not spendable"**. It
does not say money moved. That distinction is the whole point: the ledger is
untouched by a hold's entire life, so creating, releasing and expiring a hold
each post exactly zero journal entries. Money only moves when a later domain
operation explicitly posts accounting entries, and that orchestration belongs to
M4/M5, not here.

A hold is also not a transaction. There is no capture, no settlement, no
provider state and no customer-facing wording. A hold may legitimately stay
ACTIVE for a long time — which is what lets a later milestone keep funds
reserved while an outcome is UNKNOWN — but the vocabulary for that outcome lives
in the transaction layer, not on this model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from moneycore.domain.money import Money

# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------
# Three states, each with a real distinction:
#
#   ACTIVE     reserving funds now
#   RELEASED   deliberately given back (a cancellation, by any name)
#   EXPIRED    its time ran out
#
# Released and expired are both terminal and both stop reserving funds, but they
# are kept apart because *why* the reservation ended is operational history worth
# preserving. Deliberately absent: CAPTURED, SETTLED, COMPLETED, FAILED, PENDING,
# PROCESSING, CONFIRMING — every one of those describes a transaction or a
# provider outcome, and a hold knows about neither.


class HoldStatus:
    ACTIVE: Final = 'active'
    RELEASED: Final = 'released'
    EXPIRED: Final = 'expired'

    CHOICES: Final = [
        (ACTIVE, 'Active'),
        (RELEASED, 'Released'),
        (EXPIRED, 'Expired'),
    ]

    ALL: Final = frozenset({ACTIVE, RELEASED, EXPIRED})
    TERMINAL: Final = frozenset({RELEASED, EXPIRED})


HOLD_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    HoldStatus.ACTIVE: frozenset({HoldStatus.RELEASED, HoldStatus.EXPIRED}),
    HoldStatus.RELEASED: frozenset(),
    HoldStatus.EXPIRED: frozenset(),
}


def can_transition_hold(current: str, target: str) -> bool:
    """Whether a hold may move from ``current`` to ``target``."""
    return target in HOLD_TRANSITIONS.get(current, frozenset())


def hold_is_terminal(status: str) -> bool:
    return status in HoldStatus.TERMINAL


# --------------------------------------------------------------------------
# Effective activity
# --------------------------------------------------------------------------


def is_effectively_active(status: str, expires_at, at) -> bool:
    """Whether a hold actually reserves funds at time ``at``.

    A hold reserves funds when it is ACTIVE **and** has not passed its expiry.
    The second clause is what keeps balance correctness independent of any
    cleanup process: a hold whose ``expires_at`` has passed stops reserving the
    moment it does, whether or not a worker has yet written ``status=EXPIRED``.

    Making this a read-time predicate rather than a stored flag means the
    available balance can never be wrong merely because a scheduler was late,
    was never deployed, or does not exist. No background job is required for the
    projection to be correct, which is why M3 introduces none.

    Expiry is exclusive at the boundary: a hold expiring exactly at ``at`` has
    expired.
    """
    if status != HoldStatus.ACTIVE:
        return False
    if expires_at is None:
        return True
    return expires_at > at


# --------------------------------------------------------------------------
# Balance projection
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BalanceProjection:
    """A wallet's balance picture at one moment.

    Three figures, one invariant: ``available = posted - held``.

    Every figure is a :class:`Money` in the same currency, so there is no float
    anywhere and no bare number that could be misread. The object is frozen: it
    is a snapshot of a derivation, not a record anyone can adjust.

    Nothing here is stored. ``posted`` comes from the ledger, ``held`` from
    effectively-active holds, and ``available`` is computed — so none of the
    three can drift out of step with the rows they came from.
    """

    posted: Money
    held: Money
    available: Money

    def __post_init__(self) -> None:
        currencies = {self.posted.currency, self.held.currency, self.available.currency}
        if len(currencies) != 1:
            raise ValueError(
                f'A balance projection must be single-currency, got {sorted(currencies)}.'
            )
        if self.available != self.posted - self.held:
            raise ValueError(
                'A balance projection must satisfy available = posted - held.'
            )

    @classmethod
    def build(cls, posted: Money, held: Money) -> BalanceProjection:
        """Derive a projection from a posted balance and a held total."""
        return cls(posted=posted, held=held, available=posted - held)

    @property
    def currency(self) -> str:
        return self.posted.currency
