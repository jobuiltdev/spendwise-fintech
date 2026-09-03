"""An authoritative money value type for the financial core.

Money is held as **integer minor units** plus an ISO-4217 alpha-3 currency code.
There is no float path anywhere in this module: floats are rejected at
construction rather than silently rounded, because a value that reached this
type through a float has already lost the precision a ledger depends on.

This type is for the financial core only. It is deliberately *not* wired into
the existing expense tracker, whose ``Expense.amount`` stays a ``Decimal`` field
and whose display formatting is unchanged.

Scope is intentionally narrow: construction, equality, addition, subtraction,
negation and ordering. Formatting, rounding policy, allocation/remainder rules,
persistence and any notion of exchange rates are explicitly out of scope and
belong to later milestones.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

# ISO 4217 alpha-3: exactly three uppercase letters. Codes are not normalised —
# a lowercase code is a caller bug, and quietly upper-casing it would hide that.
_CURRENCY_PATTERN: Final = re.compile(r'^[A-Z]{3}$')


class CurrencyMismatchError(ValueError):
    """Raised when an operation combines two different currencies.

    A programming error rather than a customer-facing condition, so it is a
    ValueError rather than one of the domain errors in ``moneycore.domain.errors``.
    """

    def __init__(self, left: str, right: str) -> None:
        super().__init__(f'Cannot combine {left} and {right}: currencies must match.')
        self.left = left
        self.right = right


@dataclass(frozen=True, slots=True)
class Money:
    """An exact amount of a single currency.

    ``minor_units`` is the amount in the currency's smallest unit — kobo for
    NGN, cents for USD. The type carries no opinion about which currency it
    holds; nothing here is specific to the NGN money-core V1.
    """

    minor_units: int
    currency: str

    def __post_init__(self) -> None:
        # bool is a subclass of int; True would otherwise become 1 minor unit.
        if isinstance(self.minor_units, bool) or not isinstance(self.minor_units, int):
            raise TypeError(
                'minor_units must be an int (a whole number of the currency\'s '
                f'smallest unit), got {type(self.minor_units).__name__}. '
                'Money never accepts float or Decimal input.'
            )
        if not isinstance(self.currency, str) or not _CURRENCY_PATTERN.match(self.currency):
            raise ValueError(
                'currency must be an ISO-4217 alpha-3 code in uppercase, '
                f'got {self.currency!r}.'
            )

    @classmethod
    def zero(cls, currency: str) -> Money:
        """A zero amount in the given currency."""
        return cls(0, currency)

    def _require_same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(self.currency, other.currency)

    def __add__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return Money(self.minor_units + other.minor_units, self.currency)

    def __sub__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return Money(self.minor_units - other.minor_units, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.minor_units, self.currency)

    def __lt__(self, other: Money) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return self.minor_units < other.minor_units

    def __le__(self, other: Money) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return self.minor_units <= other.minor_units

    def __gt__(self, other: Money) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return self.minor_units > other.minor_units

    def __ge__(self, other: Money) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return self.minor_units >= other.minor_units

    def __repr__(self) -> str:
        return f'Money({self.minor_units}, {self.currency!r})'
