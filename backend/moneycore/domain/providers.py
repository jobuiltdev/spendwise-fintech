"""The provider boundary: asking an external rail to execute, and reading it.

Pure domain. Nothing here touches the database, performs I/O, or knows which
provider SpendWise will use — none has been chosen (D-1), and no vendor name,
credential or protocol appears anywhere in this package.

Five concepts, kept apart
-------------------------
* **Journal** — what money has posted (M2).
* **Hold** — what posted money is reserved (M3).
* **FinancialTransaction** — what operation is happening, and where it has got
  to (M4). Still the only authoritative lifecycle.
* **Transfer** — which bank transfer is intended (M5).
* **ProviderExecutionAttempt** — *one interaction with an external rail*, and
  what was observed at that interaction (M6).

An attempt is an **observation**, not a second business lifecycle. It records
what a single interaction told us; the transaction records what SpendWise
believes. They are related but not the same thing, which is why the attempt has
its own small vocabulary rather than borrowing the transaction's.

Three outcomes, and the third is the point
------------------------------------------
A provider interaction has exactly three honest readings:

* **succeeded** — the rail definitively accepted and executed it
* **failed** — the rail definitively did not, and nothing moved
* **unknown** — the request may have been accepted or executed, and SpendWise
  cannot currently know which

The third is not a degenerate case of the second. A timeout, a dropped
connection or a lost response after submission tells us nothing about whether
money moved, and recording that as failure is how a system tells a customer
their money did not move when it did. So the result vocabulary makes ambiguity
a **first-class value with its own type**, not a boolean that lost information:
a plain ``success: bool`` cannot distinguish "definitely not" from "we do not
know", which is precisely the distinction that matters.

Impossible states are unrepresentable here. A success carries no failure code
because :class:`ProviderTransferSucceeded` has no such field; an ambiguous
outcome cannot claim success because it is a different type.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Protocol, Union, runtime_checkable

from moneycore.domain.currency import is_valid_currency_code
from moneycore.domain.errors import ProviderResultInvalidError
from moneycore.domain.ledger import is_valid_amount_minor

# --------------------------------------------------------------------------
# Attempt vocabulary
# --------------------------------------------------------------------------


class ProviderAttemptStatus:
    """What one interaction with an external rail told us.

    ``STARTED`` means the attempt was durably claimed **and may or may not have
    reached the provider** — see the crash-window note on
    :class:`ProviderExecutionAttempt`. It is emphatically *not* a synonym for
    "not submitted", and nothing may treat it as a failure.

    The other three are finished observations. Note that ``UNKNOWN`` **is**
    terminal for an *attempt* while it is deliberately not terminal for a
    *transaction*: the interaction is over and told us nothing conclusive, but
    the operation itself remains unresolved and is still waiting for the truth.
    """

    STARTED: Final = 'started'
    SUCCEEDED: Final = 'succeeded'
    FAILED: Final = 'failed'
    UNKNOWN: Final = 'unknown'

    CHOICES: Final = [
        (STARTED, 'Started'),
        (SUCCEEDED, 'Succeeded'),
        (FAILED, 'Failed'),
        (UNKNOWN, 'Unknown'),
    ]

    ALL: Final = frozenset({STARTED, SUCCEEDED, FAILED, UNKNOWN})

    #: The interaction is over and its observation is fixed.
    FINISHED: Final = frozenset({SUCCEEDED, FAILED, UNKNOWN})


class ProviderOperation:
    """Which provider interaction an attempt records."""

    SUBMIT_TRANSFER: Final = 'submit_transfer'

    CHOICES: Final = [(SUBMIT_TRANSFER, 'Submit transfer')]
    ALL: Final = frozenset({SUBMIT_TRANSFER})


# --------------------------------------------------------------------------
# Normalised failure codes
# --------------------------------------------------------------------------
# A deliberately tiny, provider-neutral vocabulary. Each value is a distinction
# SpendWise can actually justify today; anything finer would be inventing
# categories no real provider has yet told us about.
#
# NOTHING AMBIGUOUS BELONGS HERE. A timeout, a dropped connection or a lost
# response is not a failure code — it is ProviderTransferUnknown. There is
# deliberately no `TIMEOUT` member, so it is not possible to record one.


class ProviderFailureCode:
    #: The rail rejected the destination itself.
    DESTINATION_REJECTED: Final = 'destination_rejected'
    #: The rail rejected the request on its own terms (validation, policy).
    REQUEST_REJECTED: Final = 'request_rejected'
    #: The rail could not be reached, and nothing was submitted.
    PROVIDER_UNAVAILABLE: Final = 'provider_unavailable'
    #: A definite rejection whose reason does not map to anything above.
    UNKNOWN_PROVIDER_FAILURE: Final = 'unknown_provider_failure'

    CHOICES: Final = [
        (DESTINATION_REJECTED, 'Destination rejected'),
        (REQUEST_REJECTED, 'Request rejected'),
        (PROVIDER_UNAVAILABLE, 'Provider unavailable'),
        (UNKNOWN_PROVIDER_FAILURE, 'Unknown provider failure'),
    ]

    ALL: Final = frozenset({
        DESTINATION_REJECTED,
        REQUEST_REJECTED,
        PROVIDER_UNAVAILABLE,
        UNKNOWN_PROVIDER_FAILURE,
    })


class AccountResolutionFailureReason:
    """Why a destination could not be resolved.

    ``NOT_FOUND`` and ``UNAVAILABLE`` are kept strictly apart: telling a
    customer their recipient's account does not exist because *our* provider
    was unreachable would be stating a fact we have not established.
    """

    NOT_FOUND: Final = 'not_found'
    UNAVAILABLE: Final = 'unavailable'
    INVALID_REQUEST: Final = 'invalid_request'

    ALL: Final = frozenset({NOT_FOUND, UNAVAILABLE, INVALID_REQUEST})


# --------------------------------------------------------------------------
# Opaque references
# --------------------------------------------------------------------------

#: A provider reference is opaque: no assumption that it is numeric, a UUID, or
#: any fixed length. Only bounded, printable and free of control characters.
PROVIDER_REFERENCE_MAX_LENGTH: Final = 128
CLIENT_REFERENCE_MAX_LENGTH: Final = 64

_CONTROL_CHARACTERS: Final = re.compile(r'[\x00-\x1f\x7f]')


def normalise_provider_reference(value: object) -> str:
    """Validate an optional provider reference, returning the stored form.

    Outer whitespace is trimmed conservatively; control characters are refused
    outright, because a reference is echoed into logs and operational tooling.
    """
    if value is None:
        return ''
    if not isinstance(value, str):
        raise ProviderResultInvalidError(
            'A provider reference must be text.',
            details={'type': type(value).__name__},
        )

    cleaned = value.strip()
    if len(cleaned) > PROVIDER_REFERENCE_MAX_LENGTH:
        raise ProviderResultInvalidError(
            'That provider reference is longer than '
            f'{PROVIDER_REFERENCE_MAX_LENGTH} characters.',
            details={'length': len(cleaned)},
        )
    if _CONTROL_CHARACTERS.search(cleaned):
        raise ProviderResultInvalidError(
            'A provider reference must not contain control characters.',
        )
    return cleaned


# --------------------------------------------------------------------------
# The outbound request
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderTransferRequest:
    """What an adapter is given, and the whole of what it is given.

    Serialisable business values only. **No Django model instance crosses this
    boundary** — no wallet, transfer, transaction, hold, journal or user — so an
    adapter cannot reach back into the money core, and adding a provider cannot
    quietly become a way to mutate financial state.
    """

    amount_minor: int
    currency: str
    destination_account_number: str
    destination_bank_code: str
    client_reference: str
    narration: str = ''

    def __post_init__(self) -> None:
        if not is_valid_amount_minor(self.amount_minor):
            raise ProviderResultInvalidError(
                'A provider request amount must be a positive whole number of '
                'minor units.',
                details={'amount_type': type(self.amount_minor).__name__},
            )
        if not is_valid_currency_code(self.currency):
            raise ProviderResultInvalidError(
                'A provider request needs a valid ISO-4217 currency.',
                details={'currency': str(self.currency)},
            )
        for field in (
            'destination_account_number',
            'destination_bank_code',
            'client_reference',
        ):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ProviderResultInvalidError(
                    f'A provider request needs a {field}.',
                    details={'field': field},
                )
        if len(self.client_reference) > CLIENT_REFERENCE_MAX_LENGTH:
            raise ProviderResultInvalidError(
                'That client reference is too long.',
                details={'length': len(self.client_reference)},
            )


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderTransferSucceeded:
    """The rail definitively accepted and executed the transfer.

    Carries no failure code, because it has no such field: a success that
    claims a failure reason is not a state this vocabulary can express.
    """

    provider_reference: str = ''

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            'provider_reference',
            normalise_provider_reference(self.provider_reference),
        )


@dataclass(frozen=True)
class ProviderTransferFailed:
    """The rail definitively did not execute it, and nothing moved.

    Only ever constructed when that is actually known. An adapter that cannot
    establish it must return :class:`ProviderTransferUnknown` instead.
    """

    failure_code: str
    provider_reference: str = ''

    def __post_init__(self) -> None:
        if self.failure_code not in ProviderFailureCode.ALL:
            raise ProviderResultInvalidError(
                'A provider failure must use a normalised failure code.',
                details={'failure_code': str(self.failure_code)},
            )
        object.__setattr__(
            self,
            'provider_reference',
            normalise_provider_reference(self.provider_reference),
        )


@dataclass(frozen=True)
class ProviderTransferUnknown:
    """The request may have been accepted or executed. We cannot yet say.

    **This is not a failure**, and there is no path from here to one. It is an
    admission, and the correct response is to keep the customer's funds
    reserved and go and find out — which is M7's job, not M6's.

    ``ambiguity_reason`` is a short internal note for operators. It is
    deliberately *not* a failure code and is stored nowhere near one.
    """

    ambiguity_reason: str = ''
    provider_reference: str = ''

    def __post_init__(self) -> None:
        if self.ambiguity_reason and self.ambiguity_reason in (
            ProviderFailureCode.ALL
        ):
            raise ProviderResultInvalidError(
                'An ambiguous outcome must not carry a failure code.',
                details={'ambiguity_reason': self.ambiguity_reason},
            )
        object.__setattr__(
            self,
            'provider_reference',
            normalise_provider_reference(self.provider_reference),
        )


ProviderTransferResult = Union[
    ProviderTransferSucceeded,
    ProviderTransferFailed,
    ProviderTransferUnknown,
]

#: Every legal result type. Anything else is a broken adapter, not an outcome.
PROVIDER_TRANSFER_RESULT_TYPES: Final = (
    ProviderTransferSucceeded,
    ProviderTransferFailed,
    ProviderTransferUnknown,
)


def require_transfer_result(value: object) -> ProviderTransferResult:
    """Refuse anything that is not one of the three honest outcomes.

    An adapter returning ``None``, a bare boolean or a dict has not told us
    what happened, and guessing on its behalf is exactly how ambiguity becomes
    a fabricated failure.
    """
    if not isinstance(value, PROVIDER_TRANSFER_RESULT_TYPES):
        raise ProviderResultInvalidError(
            'A provider adapter must return a definitive success, a definitive '
            'failure, or an explicit unknown.',
            details={'returned': type(value).__name__},
        )
    return value


# --------------------------------------------------------------------------
# Account resolution
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderAccountResolution:
    """A destination as an external rail reported it.

    Provider-neutral by construction, and deliberately the *same four facts*
    M5's `VerifiedBankAccount` holds — the application layer converts one into
    the other, so no provider payload ever reaches a `Transfer`.
    """

    account_number: str
    bank_code: str
    account_name: str
    bank_name: str


@dataclass(frozen=True)
class ProviderAccountResolutionFailed:
    """The destination could not be resolved, and why.

    Returned rather than raised so an adapter cannot accidentally express
    "unreachable" and "no such account" as the same thing.
    """

    reason: str
    detail: str = ''

    def __post_init__(self) -> None:
        if self.reason not in AccountResolutionFailureReason.ALL:
            raise ProviderResultInvalidError(
                'An account resolution failure needs a known reason.',
                details={'reason': str(self.reason)},
            )


ProviderAccountResolutionResult = Union[
    ProviderAccountResolution, ProviderAccountResolutionFailed
]


# --------------------------------------------------------------------------
# The adapter interface
# --------------------------------------------------------------------------


@runtime_checkable
class TransferProvider(Protocol):
    """What SpendWise needs from an execution rail. Deliberately narrow.

    Two operations, each with one job. There is no ``do_everything``, no status
    query and no webhook handling: resolving an ambiguous outcome later is M7's
    concern, and putting a hook for it here would invite M6 to start guessing.

    An implementation returns results; it does not raise to signal an outcome.
    Programming and configuration errors may still raise — those are bugs, not
    provider outcomes.
    """

    #: Stable internal key stored on each attempt. Not a vendor brand.
    provider_key: str

    def resolve_bank_account(
        self, *, bank_code: str, account_number: str
    ) -> ProviderAccountResolutionResult:
        """Look up who owns a destination account."""
        ...

    def submit_transfer(
        self, request: ProviderTransferRequest
    ) -> ProviderTransferResult:
        """Ask the rail to execute one transfer.

        **Must not retry internally**, and must never convert an ambiguous
        transport outcome into a failure.
        """
        ...
