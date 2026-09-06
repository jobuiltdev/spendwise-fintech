"""Comparing what SpendWise believes with what the provider says happened.

Pure domain. No I/O, no database, no vendor.

Reconciliation is observational
-------------------------------
A provider record is **external evidence, not an instruction**. Discovering
that a rail disagrees with our ledger is a reason to raise an alarm, never a
licence to move money: a system that silently rewrote its own books to agree
with an external file would have no books worth keeping. So M8 detects and
classifies, and every corrective action stays a human decision made with the
evidence in front of them.

That means nothing here — and nothing in the service built on it — posts a
journal, releases a hold, changes a transaction status, or calls any M4/M5/M7
mutator. The strongest thing reconciliation can do is write down what it saw.

Discrepancy is not the same as corruption
-----------------------------------------
Two very different things can go wrong, and collapsing them would be the
easiest way to hide the serious one:

* **A discrepancy** is SpendWise and the provider disagreeing. Expected,
  survivable, classified, recorded.
* **An internal integrity failure** is SpendWise disagreeing with *itself* — a
  transaction claiming success with no journal behind it. M2–M5 make that
  impossible through supported paths, and if it ever appears it is a defect in
  our own system, not a difference of opinion with a rail. It raises rather
  than being filed as one discrepancy among many.

Classification shape
--------------------
One `overall_status` plus independent mismatch flags, rather than a single
twenty-way enum. A record whose amount *and* currency both differ is one item
with two flags set; forcing it into one mutually exclusive category would throw
away half of what was found. Booleans rather than JSON or a child table: they
are queryable and constrainable identically on both engines, and there are only
four of them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol, runtime_checkable

from moneycore.domain.currency import is_valid_currency_code
from moneycore.domain.errors import (
    ReconciliationRecordInvalidError,
    ReconciliationWindowInvalidError,
)
from moneycore.domain.ledger import is_valid_amount_minor

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------


class ReconciliationOutcome:
    """What either side says became of a transfer.

    Deliberately the same three-way shape M6 and M7 use, for the same reason:
    "we cannot currently say" is a real answer and is not a failure. A provider
    export listing a transfer as still in flight tells us nothing about whether
    money moved.
    """

    SUCCEEDED: Final = 'succeeded'
    FAILED: Final = 'failed'
    UNRESOLVED: Final = 'unresolved'

    CHOICES: Final = [
        (SUCCEEDED, 'Succeeded'),
        (FAILED, 'Failed'),
        (UNRESOLVED, 'Unresolved'),
    ]

    ALL: Final = frozenset({SUCCEEDED, FAILED, UNRESOLVED})


class ReconciliationRunStatus:
    """Operational history of one comparison. Not financial truth."""

    STARTED: Final = 'started'
    COMPLETED: Final = 'completed'
    #: The run could not be carried out — a provider fetch failed, or the data
    #: it returned was unusable. No items are recorded, and nothing financial
    #: is touched. A new run is created to try again; this one is history.
    FAILED_INTERNAL: Final = 'failed_internal'

    CHOICES: Final = [
        (STARTED, 'Started'),
        (COMPLETED, 'Completed'),
        (FAILED_INTERNAL, 'Failed internal'),
    ]

    ALL: Final = frozenset({STARTED, COMPLETED, FAILED_INTERNAL})
    TERMINAL: Final = frozenset({COMPLETED, FAILED_INTERNAL})


class ReconciliationStatus:
    """The headline finding for one compared item."""

    #: Both sides agree on identity, amount, currency and outcome.
    MATCHED: Final = 'matched'
    #: Both sides know about it and disagree about something. The specific
    #: disagreements are carried as flags, because there can be more than one.
    DISCREPANCY: Final = 'discrepancy'
    #: The provider has a record SpendWise cannot account for.
    PROVIDER_ONLY: Final = 'provider_only'
    #: SpendWise attempted something the provider did not report.
    INTERNAL_ONLY: Final = 'internal_only'

    CHOICES: Final = [
        (MATCHED, 'Matched'),
        (DISCREPANCY, 'Discrepancy'),
        (PROVIDER_ONLY, 'Provider only'),
        (INTERNAL_ONLY, 'Internal only'),
    ]

    ALL: Final = frozenset({MATCHED, DISCREPANCY, PROVIDER_ONLY, INTERNAL_ONLY})

    #: Statuses where both sides were present and could be compared.
    COMPARED: Final = frozenset({MATCHED, DISCREPANCY})


#: The independent things two sides can disagree about. Kept as separate flags
#: so an item can report every difference it found rather than only the first.
DISCREPANCY_FLAGS: Final = (
    'outcome_mismatch',
    'amount_mismatch',
    'currency_mismatch',
    'reference_mismatch',
)

PROVIDER_RECORD_ID_MAX_LENGTH: Final = 128

_CONTROL_CHARACTERS: Final = re.compile(r'[\x00-\x1f\x7f]')


def _clean_identifier(
    value: object, *, field: str, max_length: int, required: bool = True
) -> str:
    if value is None:
        value = ''
    if not isinstance(value, str):
        raise ReconciliationRecordInvalidError(
            f'{field} must be text.',
            details={'field': field, 'type': type(value).__name__},
        )

    cleaned = value.strip()
    if required and not cleaned:
        raise ReconciliationRecordInvalidError(
            f'{field} is required on a provider record.',
            details={'field': field},
        )
    if len(cleaned) > max_length:
        raise ReconciliationRecordInvalidError(
            f'{field} is longer than {max_length} characters.',
            details={'field': field, 'length': len(cleaned)},
        )
    if _CONTROL_CHARACTERS.search(cleaned):
        raise ReconciliationRecordInvalidError(
            f'{field} must not contain control characters.',
            details={'field': field},
        )
    return cleaned


# --------------------------------------------------------------------------
# The provider record
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderTransferRecord:
    """One transfer as the provider reports it.

    Provider-neutral and already normalised: no vendor status string, no raw
    payload, no JSON. What cannot be normalised is not evidence a comparison
    can act on.

    Identity is ``(provider_key, provider_record_id)`` and nothing else.
    Deriving it from amount, recipient or timestamp would make reconciliation
    guess, and a reconciliation that guesses is worse than none.
    """

    provider_key: str
    provider_record_id: str
    outcome: str
    amount_minor: int
    currency: str
    observed_at: datetime
    client_reference: str = ''
    provider_reference: str = ''
    failure_code: str = ''

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            'provider_key',
            _clean_identifier(
                self.provider_key, field='provider_key', max_length=64
            ),
        )
        object.__setattr__(
            self,
            'provider_record_id',
            _clean_identifier(
                self.provider_record_id,
                field='provider_record_id',
                max_length=PROVIDER_RECORD_ID_MAX_LENGTH,
            ),
        )

        if self.outcome not in ReconciliationOutcome.ALL:
            raise ReconciliationRecordInvalidError(
                'A provider record needs a known outcome.',
                details={'outcome': str(self.outcome)},
            )

        # Integer minor units, exactly as everywhere else in the money core.
        # A float here would be a second, lossier representation of money.
        if not is_valid_amount_minor(self.amount_minor):
            raise ReconciliationRecordInvalidError(
                'A provider record amount must be a positive whole number of '
                'minor units.',
                details={'amount_type': type(self.amount_minor).__name__},
            )
        if not is_valid_currency_code(self.currency):
            raise ReconciliationRecordInvalidError(
                'A provider record needs a valid ISO-4217 currency.',
                details={'currency': str(self.currency)},
            )

        if not isinstance(self.observed_at, datetime):
            raise ReconciliationRecordInvalidError(
                'A provider record needs an observation time.',
            )
        if self.observed_at.tzinfo is None or (
            self.observed_at.utcoffset() is None
        ):
            # A naive timestamp would be silently reinterpreted by whichever
            # machine read it. Reconciliation windows must not depend on that.
            raise ReconciliationRecordInvalidError(
                'A provider record observation time must be timezone-aware.',
            )

        object.__setattr__(
            self,
            'client_reference',
            _clean_identifier(
                self.client_reference,
                field='client_reference',
                max_length=64,
                required=False,
            ),
        )
        object.__setattr__(
            self,
            'provider_reference',
            _clean_identifier(
                self.provider_reference,
                field='provider_reference',
                max_length=128,
                required=False,
            ),
        )
        object.__setattr__(
            self,
            'failure_code',
            _clean_identifier(
                self.failure_code,
                field='failure_code',
                max_length=64,
                required=False,
            ),
        )

        if not self.client_reference and not self.provider_reference:
            raise ReconciliationRecordInvalidError(
                'A provider record must name the transfer it is about.',
                details={'provider_record_id': self.provider_record_id},
            )

    @property
    def identity(self) -> tuple[str, str]:
        return (self.provider_key, self.provider_record_id)

    @property
    def comparable(self) -> tuple:
        """Everything two deliveries of the same record must agree on."""
        return (
            self.outcome,
            self.amount_minor,
            self.currency,
            self.client_reference,
            self.provider_reference,
            self.failure_code,
            self.observed_at,
        )


@dataclass(frozen=True)
class ProviderTransferRecordPage:
    """One page of provider records, with an opaque cursor for the next."""

    records: tuple[ProviderTransferRecord, ...]
    next_cursor: str = ''

    def __post_init__(self) -> None:
        if not isinstance(self.records, tuple):
            object.__setattr__(self, 'records', tuple(self.records))
        for record in self.records:
            if not isinstance(record, ProviderTransferRecord):
                raise ReconciliationRecordInvalidError(
                    'A reconciliation page must contain provider records.',
                    details={'returned': type(record).__name__},
                )


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconciliationWindow:
    """A half-open interval of time: ``[start, end)``.

    Half-open so consecutive windows tile without overlapping and without
    gaps — a record on the boundary belongs to exactly one run, which is what
    makes repeated reconciliation add up.

    Both ends must be timezone-aware. Reconciliation that depended on a
    server's local zone would produce different answers in different places.
    """

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        for label, value in (('start', self.start), ('end', self.end)):
            if not isinstance(value, datetime):
                raise ReconciliationWindowInvalidError(
                    f'The reconciliation window {label} must be a datetime.',
                    details={'field': label},
                )
            if value.tzinfo is None or value.utcoffset() is None:
                raise ReconciliationWindowInvalidError(
                    f'The reconciliation window {label} must be '
                    'timezone-aware.',
                    details={'field': label},
                )

        if self.end <= self.start:
            raise ReconciliationWindowInvalidError(
                'A reconciliation window must end after it starts.',
                details={'start': str(self.start), 'end': str(self.end)},
            )

    def contains(self, moment: datetime) -> bool:
        """Half-open: start is included, end is not."""
        return self.start <= moment < self.end


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class InternalTransferView:
    """What SpendWise currently believes about one execution attempt.

    A read-only projection assembled for comparison. It carries no model
    instance, so nothing downstream can mutate financial state through it —
    which is the whole point of building it.
    """

    attempt_id: int
    client_reference: str
    provider_reference: str
    outcome: str
    amount_minor: int
    currency: str
    has_recovery_conflict: bool = False


@dataclass(frozen=True)
class Comparison:
    """The result of comparing one provider record with internal truth."""

    overall_status: str
    outcome_mismatch: bool = False
    amount_mismatch: bool = False
    currency_mismatch: bool = False
    reference_mismatch: bool = False

    @property
    def flags(self) -> tuple[bool, ...]:
        return (
            self.outcome_mismatch,
            self.amount_mismatch,
            self.currency_mismatch,
            self.reference_mismatch,
        )

    @property
    def has_discrepancy(self) -> bool:
        return any(self.flags)


def compare(
    record: ProviderTransferRecord, internal: InternalTransferView
) -> Comparison:
    """Compare one provider record with what SpendWise believes.

    Deterministic: the same inputs always give the same answer, independent of
    insertion order, row ids or which side was read first. Every difference is
    reported — an item whose amount and currency both differ sets both flags
    rather than reporting whichever was checked first.

    A reference mismatch means the record names a provider reference that
    contradicts the one recorded against this attempt. It is a *flag* rather
    than a refusal to compare, because the rest of the comparison is still
    informative and hiding it would be worse.
    """
    reference_mismatch = bool(
        record.provider_reference
        and internal.provider_reference
        and record.provider_reference != internal.provider_reference
    )

    comparison = Comparison(
        overall_status=ReconciliationStatus.MATCHED,
        outcome_mismatch=record.outcome != internal.outcome,
        amount_mismatch=record.amount_minor != internal.amount_minor,
        currency_mismatch=record.currency != internal.currency,
        reference_mismatch=reference_mismatch,
    )

    if comparison.has_discrepancy:
        return Comparison(
            overall_status=ReconciliationStatus.DISCREPANCY,
            outcome_mismatch=comparison.outcome_mismatch,
            amount_mismatch=comparison.amount_mismatch,
            currency_mismatch=comparison.currency_mismatch,
            reference_mismatch=comparison.reference_mismatch,
        )
    return comparison


# --------------------------------------------------------------------------
# The provider interface
# --------------------------------------------------------------------------


@runtime_checkable
class TransferReconciliationProvider(Protocol):
    """What SpendWise needs from a rail in order to reconcile.

    One read operation, and it is the only one. There is deliberately no
    submission, no status mutation and no correction method on this Protocol:
    a reconciliation service typed against it cannot reach any of them.
    """

    provider_key: str

    def list_transfer_records(
        self, *, window: ReconciliationWindow, cursor: str = ''
    ) -> ProviderTransferRecordPage:
        """List the transfers the provider believes it handled in a window.

        Read-only and safe to repeat: listing moves no money. Paged through an
        opaque cursor, so a large export does not have to arrive at once.
        """
        ...
