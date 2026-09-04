"""Double-entry bookkeeping vocabulary and rules.

Pure domain: the account taxonomy, entry directions, normal-balance convention
and the balancing invariant. Nothing here touches the database.

A deliberate limit on what this module claims
--------------------------------------------
The account **types** below are ordinary bookkeeping classifications. They say
how a balance is computed, and nothing more. In particular, classifying a
customer wallet's ledger account as a liability is a statement about SpendWise's
own books — funds recorded as attributable to that customer — and is **not** a
claim about custody: which legal entity holds the money, whether SpendWise is a
deposit-holder or a record-keeper for a partner, and what the counterpart asset
actually is all depend on a banking partner that has not been chosen. That
remains DEFERRED, which is why this module names no bank, settlement, custody or
provider account, and why the counterpart side of a customer posting is supplied
by the caller rather than assumed here.
"""

from __future__ import annotations

from typing import Final

# --------------------------------------------------------------------------
# Account taxonomy
# --------------------------------------------------------------------------
# The five conventional classes. Product concepts — "Food", "Transport",
# spending categories — are expense-intelligence, not bookkeeping, and never
# appear here.


class LedgerAccountType:
    ASSET: Final = 'asset'
    LIABILITY: Final = 'liability'
    EQUITY: Final = 'equity'
    REVENUE: Final = 'revenue'
    EXPENSE: Final = 'expense'

    CHOICES: Final = [
        (ASSET, 'Asset'),
        (LIABILITY, 'Liability'),
        (EQUITY, 'Equity'),
        (REVENUE, 'Revenue'),
        (EXPENSE, 'Expense'),
    ]

    ALL: Final = frozenset({ASSET, LIABILITY, EQUITY, REVENUE, EXPENSE})

    # Accounts whose balance increases on the debit side. The rest are
    # credit-normal. This is the only place the convention is defined.
    DEBIT_NORMAL: Final = frozenset({ASSET, EXPENSE})
    CREDIT_NORMAL: Final = frozenset({LIABILITY, EQUITY, REVENUE})


def is_debit_normal(account_type: str) -> bool:
    """Whether this account type increases on the debit side."""
    return account_type in LedgerAccountType.DEBIT_NORMAL


class LedgerAccountStatus:
    ACTIVE: Final = 'active'
    CLOSED: Final = 'closed'

    CHOICES: Final = [
        (ACTIVE, 'Active'),
        (CLOSED, 'Closed'),
    ]

    ALL: Final = frozenset({ACTIVE, CLOSED})


# --------------------------------------------------------------------------
# Entry direction
# --------------------------------------------------------------------------
# Direction is held separately from magnitude. An entry's amount is always a
# positive integer; a debit is never stored as a negative credit.


class EntryDirection:
    DEBIT: Final = 'debit'
    CREDIT: Final = 'credit'

    CHOICES: Final = [
        (DEBIT, 'Debit'),
        (CREDIT, 'Credit'),
    ]

    ALL: Final = frozenset({DEBIT, CREDIT})

    OPPOSITE: Final = {DEBIT: CREDIT, CREDIT: DEBIT}


def opposite_direction(direction: str) -> str:
    """The direction that reverses ``direction``."""
    return EntryDirection.OPPOSITE[direction]


# --------------------------------------------------------------------------
# Journal lifecycle
# --------------------------------------------------------------------------
# A journal is created and posted inside a single transaction, so a DRAFT never
# survives a completed call to the posting service. The state still exists
# explicitly so that "posted" is a property the balance query can filter on
# rather than an assumption, and so that a half-built journal is representable
# and provably excluded.


class JournalStatus:
    DRAFT: Final = 'draft'
    POSTED: Final = 'posted'

    CHOICES: Final = [
        (DRAFT, 'Draft'),
        (POSTED, 'Posted'),
    ]

    ALL: Final = frozenset({DRAFT, POSTED})


# --------------------------------------------------------------------------
# Amount range
# --------------------------------------------------------------------------
# Entries persist as a 64-bit signed integer (BigIntegerField → PostgreSQL
# bigint, SQLite INTEGER). The bound below is that storage limit, not a product
# limit: what a customer may actually move is a policy decision that belongs to
# later milestones and to a provider, and nothing here should be read as one.

MAX_AMOUNT_MINOR: Final = 2**63 - 1
MIN_AMOUNT_MINOR: Final = 1


def is_valid_amount_minor(value: object) -> bool:
    """Whether ``value`` is a usable positive integer minor-unit magnitude.

    ``bool`` is rejected explicitly: it subclasses ``int``, so ``True`` would
    otherwise post as one minor unit.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return MIN_AMOUNT_MINOR <= value <= MAX_AMOUNT_MINOR


# --------------------------------------------------------------------------
# The balancing invariant
# --------------------------------------------------------------------------


def totals_by_direction(entries) -> tuple[int, int]:
    """Sum ``(debits, credits)`` over an iterable of ``(direction, amount)``."""
    debits = sum(a for d, a in entries if d == EntryDirection.DEBIT)
    credits = sum(a for d, a in entries if d == EntryDirection.CREDIT)
    return debits, credits


def is_balanced(entries) -> bool:
    """Whether debits equal credits exactly.

    Integer arithmetic throughout — there is no tolerance, no epsilon and no
    rounding. An imbalance is a rejection, never something to absorb.
    """
    debits, credits = totals_by_direction(entries)
    return debits == credits
