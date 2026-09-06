"""Learning the truth after the immediate provider call could not tell us.

Pure domain. No I/O, no database, no vendor.

M6 ends with three honest readings of one interaction, and one of them —
``unknown`` — is an admission rather than an answer. M7 is how that admission
gets resolved: by *asking* about the request that was already sent, or by being
*told* about it, and never by sending it again.

Recovery is observational
-------------------------
There is exactly one question M7 may ask a rail: **"what happened to this
request I already made?"** There is no path from anything here to
``submit_transfer``. An unresolved operation may already have moved a
customer's money, so "I don't know, send it again" is the one response that can
turn an ambiguity into a double debit.

Evidence, not edits
-------------------
M6 made a ``ProviderExecutionAttempt`` an immutable record of what *one
interaction observed*. Later knowledge does not change what was observed then,
so recovery never rewrites an attempt — it appends new evidence about it. An
attempt that returned ``unknown`` stays ``unknown`` forever, even once the
transfer is known to have succeeded, because that is what actually happened at
that moment. The financial transaction is what resolves.

That distinction is what keeps the audit trail honest: "we did not know, and
then a webhook told us" is a different and more truthful story than "it
succeeded all along".

Three outcomes again, deliberately
----------------------------------
Recovery reuses the same three-way shape as M6, for the same reason: a lookup
that cannot determine the answer is **not** a failed transfer. Provider
indexing delays, eventual consistency and temporarily unavailable records all
look identical to "never happened", and treating them as failure would release
a customer's reservation on money that may already have left.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Protocol, Union, runtime_checkable

from moneycore.domain.errors import (
    ProviderRecoveryResultInvalidError,
    ProviderWebhookInvalidError,
)
from moneycore.domain.providers import (
    PROVIDER_REFERENCE_MAX_LENGTH,
    ProviderFailureCode,
    normalise_provider_reference,
)

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------


class RecoveryOutcome:
    """What a piece of recovery evidence says about the transfer.

    ``UNRESOLVED`` is a first-class answer, not a degenerate failure. It covers
    every way a rail can decline to tell us: still processing, cannot currently
    determine, record temporarily unavailable, lookup itself ambiguous.
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

    #: Outcomes that may resolve a financial transaction. UNRESOLVED may not.
    DEFINITIVE: Final = frozenset({SUCCEEDED, FAILED})


class EvidenceSource:
    """Where a piece of evidence came from.

    Kept on the record because "we asked" and "we were told" are different
    operational facts, and an investigator will want to know which.
    """

    STATUS_QUERY: Final = 'status_query'
    WEBHOOK: Final = 'webhook'

    CHOICES: Final = [
        (STATUS_QUERY, 'Status query'),
        (WEBHOOK, 'Webhook'),
    ]

    ALL: Final = frozenset({STATUS_QUERY, WEBHOOK})


class WebhookProcessingStatus:
    """What became of a received webhook. Deliberately three values.

    An event that failed authentication is never stored as a trusted event at
    all, so there is no ``invalid`` state here — rejection happens before
    anything is persisted.
    """

    #: Accepted and recorded, not yet applied.
    RECEIVED: Final = 'received'
    #: Applied, or found to require no financial change.
    PROCESSED: Final = 'processed'
    #: Contradicts what SpendWise already holds as settled truth.
    CONFLICTED: Final = 'conflicted'
    #: Authentic, but names a reference no attempt corresponds to.
    UNMATCHED: Final = 'unmatched'

    CHOICES: Final = [
        (RECEIVED, 'Received'),
        (PROCESSED, 'Processed'),
        (CONFLICTED, 'Conflicted'),
        (UNMATCHED, 'Unmatched'),
    ]

    ALL: Final = frozenset({RECEIVED, PROCESSED, CONFLICTED, UNMATCHED})


PROVIDER_EVENT_ID_MAX_LENGTH: Final = 128
CLIENT_REFERENCE_LOOKUP_MAX_LENGTH: Final = 64

_CONTROL_CHARACTERS: Final = re.compile(r'[\x00-\x1f\x7f]')


def _normalise_identifier(value: object, *, field: str, max_length: int) -> str:
    if value is None:
        return ''
    if not isinstance(value, str):
        raise ProviderRecoveryResultInvalidError(
            f'{field} must be text.',
            details={'field': field, 'type': type(value).__name__},
        )

    cleaned = value.strip()
    if len(cleaned) > max_length:
        raise ProviderRecoveryResultInvalidError(
            f'{field} is longer than {max_length} characters.',
            details={'field': field, 'length': len(cleaned)},
        )
    if _CONTROL_CHARACTERS.search(cleaned):
        raise ProviderRecoveryResultInvalidError(
            f'{field} must not contain control characters.',
            details={'field': field},
        )
    return cleaned


# --------------------------------------------------------------------------
# Status query results
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderTransferStatusSucceeded:
    """The rail states definitively that this request succeeded."""

    provider_reference: str = ''

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            'provider_reference',
            normalise_provider_reference(self.provider_reference),
        )

    @property
    def outcome(self) -> str:
        return RecoveryOutcome.SUCCEEDED


@dataclass(frozen=True)
class ProviderTransferStatusFailed:
    """The rail states definitively that this request did not execute."""

    failure_code: str
    provider_reference: str = ''

    def __post_init__(self) -> None:
        if self.failure_code not in ProviderFailureCode.ALL:
            raise ProviderRecoveryResultInvalidError(
                'A definitive recovery failure needs a normalised code.',
                details={'failure_code': str(self.failure_code)},
            )
        object.__setattr__(
            self,
            'provider_reference',
            normalise_provider_reference(self.provider_reference),
        )

    @property
    def outcome(self) -> str:
        return RecoveryOutcome.FAILED


@dataclass(frozen=True)
class ProviderTransferStatusUnresolved:
    """The rail cannot currently say. **This is not a failure.**

    Notably this is where ``not found`` belongs unless an adapter can prove
    that its rail means it definitively — see :func:`not_found_is_unresolved`.
    """

    reason: str = ''
    provider_reference: str = ''

    def __post_init__(self) -> None:
        if self.reason and self.reason in ProviderFailureCode.ALL:
            raise ProviderRecoveryResultInvalidError(
                'An unresolved status must not carry a failure code.',
                details={'reason': self.reason},
            )
        object.__setattr__(
            self,
            'provider_reference',
            normalise_provider_reference(self.provider_reference),
        )

    @property
    def outcome(self) -> str:
        return RecoveryOutcome.UNRESOLVED


ProviderTransferStatusResult = Union[
    ProviderTransferStatusSucceeded,
    ProviderTransferStatusFailed,
    ProviderTransferStatusUnresolved,
]

PROVIDER_STATUS_RESULT_TYPES: Final = (
    ProviderTransferStatusSucceeded,
    ProviderTransferStatusFailed,
    ProviderTransferStatusUnresolved,
)


def require_status_result(value: object) -> ProviderTransferStatusResult:
    """Refuse anything that is not one of the three honest status answers."""
    if not isinstance(value, PROVIDER_STATUS_RESULT_TYPES):
        raise ProviderRecoveryResultInvalidError(
            'A status lookup must return a definitive success, a definitive '
            'failure, or an explicit unresolved.',
            details={'returned': type(value).__name__},
        )
    return value


#: Documented for adapters, and asserted by test.
#:
#: A rail answering "no such request" tells us nothing reliable about a
#: :data:`~moneycore.domain.providers.ProviderAttemptStatus.STARTED` attempt,
#: because SpendWise does not know whether the request ever arrived. It may
#: equally mean an indexing delay, eventual consistency, a lookup key the rail
#: does not index, or a record it cannot locate yet. Treating it as failure
#: would release a reservation on money that may already have gone.
#:
#: An adapter may only return
#: :class:`ProviderTransferStatusFailed` for a not-found answer if that rail's
#: documented semantics make it definitive. None does today, so M7 maps it to
#: unresolved and leaves the question open.
NOT_FOUND_IS_DEFINITIVE: Final = False


def not_found_is_unresolved() -> bool:
    """Whether a not-found lookup answer must be treated as unresolved."""
    return not NOT_FOUND_IS_DEFINITIVE


# --------------------------------------------------------------------------
# Normalised webhook event
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class NormalisedWebhookEvent:
    """A provider webhook, reduced to the facts the money core can act on.

    Produced **only** by an adapter that has already authenticated the request,
    so no unverified event can reach financial orchestration. Deliberately
    carries no payload, no headers and no signature: what is not normalised is
    not evidence.

    ``provider_event_id`` is the rail's own identity for this delivery, and is
    what makes redelivery idempotent. A rail that cannot supply one is an
    adapter-specific problem for whichever milestone integrates it.
    """

    provider_key: str
    provider_event_id: str
    outcome: str
    client_reference: str = ''
    provider_reference: str = ''
    failure_code: str = ''

    def __post_init__(self) -> None:
        if not isinstance(self.provider_key, str) or not self.provider_key.strip():
            raise ProviderWebhookInvalidError(
                'A webhook event needs a provider key.',
            )
        object.__setattr__(self, 'provider_key', self.provider_key.strip())

        event_id = _normalise_identifier(
            self.provider_event_id,
            field='provider_event_id',
            max_length=PROVIDER_EVENT_ID_MAX_LENGTH,
        )
        if not event_id:
            # Without it, redelivery cannot be told from a second real event.
            raise ProviderWebhookInvalidError(
                'A webhook event needs a stable provider event id.',
            )
        object.__setattr__(self, 'provider_event_id', event_id)

        if self.outcome not in RecoveryOutcome.ALL:
            raise ProviderWebhookInvalidError(
                'A webhook event needs a known outcome.',
                details={'outcome': str(self.outcome)},
            )

        object.__setattr__(
            self,
            'client_reference',
            _normalise_identifier(
                self.client_reference,
                field='client_reference',
                max_length=CLIENT_REFERENCE_LOOKUP_MAX_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            'provider_reference',
            _normalise_identifier(
                self.provider_reference,
                field='provider_reference',
                max_length=PROVIDER_REFERENCE_MAX_LENGTH,
            ),
        )

        if self.outcome == RecoveryOutcome.FAILED:
            if self.failure_code not in ProviderFailureCode.ALL:
                raise ProviderWebhookInvalidError(
                    'A failed webhook event needs a normalised failure code.',
                    details={'failure_code': str(self.failure_code)},
                )
        elif self.failure_code:
            raise ProviderWebhookInvalidError(
                'Only a failed webhook event may carry a failure code.',
                details={'outcome': self.outcome},
            )

        if not self.client_reference and not self.provider_reference:
            raise ProviderWebhookInvalidError(
                'A webhook event must name the request it is about.',
            )

    @property
    def is_definitive(self) -> bool:
        return self.outcome in RecoveryOutcome.DEFINITIVE


# --------------------------------------------------------------------------
# The recovery interface
# --------------------------------------------------------------------------


@runtime_checkable
class TransferRecoveryProvider(Protocol):
    """What SpendWise needs from a rail in order to learn the truth.

    Two operations, and **neither of them sends anything**. There is
    deliberately no submission method on this Protocol: a recovery service that
    is typed against it cannot reach one.
    """

    provider_key: str

    def get_transfer_status(
        self, *, client_reference: str, provider_reference: str = ''
    ) -> ProviderTransferStatusResult:
        """Ask what became of a request already made.

        Observational and safe to repeat: it moves no money. Identified by
        SpendWise's own ``client_reference`` where possible, because we own it.
        """
        ...

    def verify_and_parse_webhook(
        self, *, body: bytes, headers
    ) -> NormalisedWebhookEvent:
        """Authenticate an inbound delivery and normalise it, in one step.

        Deliberately a single operation: separating them would leave a
        ``parse`` that application code could call on unverified bytes. If the
        request does not authenticate, this raises and no event exists.
        """
        ...
