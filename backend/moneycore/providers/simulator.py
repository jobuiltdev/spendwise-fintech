"""A deterministic provider adapter for development, tests and the demo.

Nothing here talks to a network. It exists so the *shape* of provider execution
can be exercised end to end — including the outcome that matters most, the
ambiguous one — without a provider partner having been chosen.

Deterministic by construction
-----------------------------
No randomness anywhere. The outcome comes from an explicitly configured
scenario, and the provider reference is derived from the caller's own client
reference, so the same demo produces the same result every time. There is no
"account number 0000000000 means timeout" magic: what happens is configured on
the adapter, not smuggled in through the destination, because a fixture that
looks like real data is how fake behaviour escapes into a real system.

It is a simulator, and it says so
---------------------------------
``provider_key`` is ``'simulator'``. The tiny bank fixture below exists so a
resolved account has a plausible name in a demo; it is **not** a bank
directory, it is not production metadata, and O-28 stays open.

Structured as if it were real I/O
---------------------------------
The adapter is called from outside any database transaction, and the execution
service is written that way deliberately rather than because this happens to be
fast. Optimising the boundary away because the simulator is in-memory would
mean the first real adapter inherits a design that holds row locks across a
network call.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from typing import Final

from moneycore.domain.errors import (
    ProviderWebhookInvalidError,
    ProviderWebhookUnauthenticatedError,
    ReconciliationProviderError,
)
from moneycore.domain.reconciliation import (
    ProviderTransferRecord,
    ProviderTransferRecordPage,
)
from moneycore.domain.recovery import (
    NormalisedWebhookEvent,
    ProviderTransferStatusFailed,
    ProviderTransferStatusResult,
    ProviderTransferStatusSucceeded,
    ProviderTransferStatusUnresolved,
    RecoveryOutcome,
)
from moneycore.domain.providers import (
    AccountResolutionFailureReason,
    ProviderAccountResolution,
    ProviderAccountResolutionFailed,
    ProviderAccountResolutionResult,
    ProviderFailureCode,
    ProviderTransferFailed,
    ProviderTransferRequest,
    ProviderTransferResult,
    ProviderTransferSucceeded,
    ProviderTransferUnknown,
)

SIMULATOR_PROVIDER_KEY: Final = 'simulator'

#: A fixed, published, **test-and-demo-only** signing key. It is deliberately
#: hardcoded and deliberately worthless: no real provider has been chosen, so
#: there is no production secret to hold, and inventing an environment variable
#: for one would be pretending otherwise. The signing scheme below is the
#: simulator's own and matches no real provider's.
SIMULATOR_TEST_WEBHOOK_SECRET: Final = b'simulator-test-secret-not-for-production'

#: Header the simulator signs with. Its own convention, not anyone else's.
SIMULATOR_SIGNATURE_HEADER: Final = 'X-Simulator-Signature'


class TransferScenario:
    """What the simulated rail does with a submitted transfer."""

    #: Definitively accepted and executed.
    SUCCESS: Final = 'success'
    #: Definitively rejected. Nothing moved.
    KNOWN_FAILURE: Final = 'known_failure'
    #: The request was sent and the answer never came back. **The point of the
    #: whole milestone**: the money may or may not have moved.
    AMBIGUOUS_AFTER_SUBMISSION: Final = 'ambiguous_after_submission'
    #: The rail could not be reached at all, and the adapter can prove nothing
    #: was sent. This is a definitive pre-submission failure — kept sharply
    #: distinct from the ambiguous case above.
    UNREACHABLE_BEFORE_SUBMISSION: Final = 'unreachable_before_submission'

    ALL: Final = frozenset({
        SUCCESS,
        KNOWN_FAILURE,
        AMBIGUOUS_AFTER_SUBMISSION,
        UNREACHABLE_BEFORE_SUBMISSION,
    })


class StatusScenario:
    """What a later status lookup reports, independently of the submission.

    Deliberately separate from :class:`TransferScenario`: the whole point of
    recovery is that a submission which came back ambiguous can later be found
    to have succeeded. Configuring the two together would make that story
    impossible to tell.
    """

    #: The rail states definitively that the request succeeded.
    SUCCESS: Final = 'success'
    #: The rail states definitively that it did not execute.
    FAILURE: Final = 'failure'
    #: The rail cannot currently say. Still not a failure.
    UNRESOLVED: Final = 'unresolved'
    #: The rail has no record of it. **Mapped to unresolved**, because for a
    #: request that may never have arrived, "not found" establishes nothing.
    NOT_FOUND: Final = 'not_found'

    ALL: Final = frozenset({SUCCESS, FAILURE, UNRESOLVED, NOT_FOUND})


class AccountResolutionScenario:
    SUCCESS: Final = 'success'
    NOT_FOUND: Final = 'not_found'
    UNAVAILABLE: Final = 'unavailable'

    ALL: Final = frozenset({SUCCESS, NOT_FOUND, UNAVAILABLE})


#: A handful of names so a demo resolution reads plausibly. Simulator-local
#: fixture data — not a bank directory, and not production metadata.
SIMULATED_BANKS: Final = {
    'SIM-001': 'Simulated First Bank',
    'SIM-002': 'Simulated Union Bank',
    'SIM-003': 'Simulated Trust Bank',
}

#: Likewise: a deterministic stand-in for a resolved account name.
SIMULATED_ACCOUNT_NAMES: Final = {
    '0000000001': 'Ada Okafor',
    '0000000002': 'Bola Adeyemi',
    '0000000003': 'Chidi Nwosu',
}
DEFAULT_SIMULATED_ACCOUNT_NAME: Final = 'Simulated Account Holder'


class SimulatorTransferProvider:
    """A provider adapter whose outcome is configured, not guessed.

    Scenarios are set on the instance, so a test or a demo states plainly which
    of the three honest outcomes it is exercising::

        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
        )

    Call counts are recorded under a lock so concurrency tests can prove the
    rail was asked exactly once — the single most important thing to be able to
    prove about a payment integration.
    """

    provider_key: str = SIMULATOR_PROVIDER_KEY

    def __init__(
        self,
        *,
        transfer_scenario: str = TransferScenario.SUCCESS,
        account_resolution_scenario: str = AccountResolutionScenario.SUCCESS,
        failure_code: str = ProviderFailureCode.REQUEST_REJECTED,
        status_scenario: str = StatusScenario.UNRESOLVED,
        status_failure_code: str = ProviderFailureCode.REQUEST_REJECTED,
        webhook_secret: bytes = SIMULATOR_TEST_WEBHOOK_SECRET,
        reconciliation_records=(),
        reconciliation_fails: bool = False,
        reconciliation_page_size: int = 0,
    ) -> None:
        if transfer_scenario not in TransferScenario.ALL:
            raise ValueError(f'Unknown transfer scenario: {transfer_scenario!r}')
        if account_resolution_scenario not in AccountResolutionScenario.ALL:
            raise ValueError(
                f'Unknown resolution scenario: {account_resolution_scenario!r}'
            )
        if failure_code not in ProviderFailureCode.ALL:
            raise ValueError(f'Unknown failure code: {failure_code!r}')
        if status_scenario not in StatusScenario.ALL:
            raise ValueError(f'Unknown status scenario: {status_scenario!r}')
        if status_failure_code not in ProviderFailureCode.ALL:
            raise ValueError(f'Unknown failure code: {status_failure_code!r}')

        self.transfer_scenario = transfer_scenario
        self.account_resolution_scenario = account_resolution_scenario
        self.failure_code = failure_code
        self.status_scenario = status_scenario
        self.status_failure_code = status_failure_code
        self._webhook_secret = webhook_secret
        #: Exactly what this rail will claim it handled. Supplied by the
        #: caller so a scenario is stated rather than generated.
        self.reconciliation_records = tuple(reconciliation_records)
        self.reconciliation_fails = reconciliation_fails
        self.reconciliation_page_size = reconciliation_page_size

        self._lock = threading.Lock()
        self._submit_calls = 0
        self._resolve_calls = 0
        self._status_calls = 0
        self._reconciliation_calls = 0
        #: Every request the rail was actually asked to execute, in order.
        self.submitted_requests: list[ProviderTransferRequest] = []
        #: Hook for tests that need to observe the moment of the call itself —
        #: notably the proof that no database transaction is open during it.
        self.on_submit = None
        #: Hook for tests observing the moment of the listing call —
        #: notably the proof that no database transaction is open.
        self.on_list_records = None

    # -- observation --------------------------------------------------

    @property
    def submit_call_count(self) -> int:
        with self._lock:
            return self._submit_calls

    @property
    def resolve_call_count(self) -> int:
        with self._lock:
            return self._resolve_calls

    @property
    def status_call_count(self) -> int:
        with self._lock:
            return self._status_calls

    @property
    def reconciliation_call_count(self) -> int:
        with self._lock:
            return self._reconciliation_calls

    # -- the adapter interface ----------------------------------------

    def resolve_bank_account(
        self, *, bank_code: str, account_number: str
    ) -> ProviderAccountResolutionResult:
        """Look up who owns a destination account.

        Returns a failure rather than raising, so "unreachable" and "no such
        account" cannot collapse into one another.
        """
        with self._lock:
            self._resolve_calls += 1

        if self.account_resolution_scenario == AccountResolutionScenario.NOT_FOUND:
            return ProviderAccountResolutionFailed(
                reason=AccountResolutionFailureReason.NOT_FOUND,
                detail='No such account at that bank.',
            )
        if self.account_resolution_scenario == AccountResolutionScenario.UNAVAILABLE:
            return ProviderAccountResolutionFailed(
                reason=AccountResolutionFailureReason.UNAVAILABLE,
                detail='The rail could not be reached.',
            )

        return ProviderAccountResolution(
            account_number=account_number,
            bank_code=bank_code,
            account_name=SIMULATED_ACCOUNT_NAMES.get(
                account_number, DEFAULT_SIMULATED_ACCOUNT_NAME
            ),
            bank_name=SIMULATED_BANKS.get(bank_code, 'Simulated Bank'),
        )

    def submit_transfer(
        self, request: ProviderTransferRequest
    ) -> ProviderTransferResult:
        """Ask the simulated rail to execute one transfer.

        Never retries, and never turns an ambiguous transport outcome into a
        failure — the ambiguous scenario returns
        :class:`~moneycore.domain.providers.ProviderTransferUnknown`, which is
        a result, not an exception, precisely so no caller can mistake it for
        one.
        """
        with self._lock:
            self._submit_calls += 1
            self.submitted_requests.append(request)

        if self.on_submit is not None:
            self.on_submit(request)

        if self.transfer_scenario == TransferScenario.SUCCESS:
            return ProviderTransferSucceeded(
                provider_reference=self._reference_for(request)
            )

        if self.transfer_scenario == TransferScenario.KNOWN_FAILURE:
            return ProviderTransferFailed(
                failure_code=self.failure_code,
                provider_reference=self._reference_for(request),
            )

        if self.transfer_scenario == TransferScenario.UNREACHABLE_BEFORE_SUBMISSION:
            # The adapter knows nothing left it, so this is a definitive
            # failure rather than ambiguity. Only ever safe to say when the
            # adapter can actually prove it.
            return ProviderTransferFailed(
                failure_code=ProviderFailureCode.PROVIDER_UNAVAILABLE
            )

        # AMBIGUOUS_AFTER_SUBMISSION: the request went out and the answer did
        # not come back. Whether money moved is genuinely unknown.
        return ProviderTransferUnknown(
            ambiguity_reason='response_not_received',
            provider_reference='',
        )

    # -- recovery -----------------------------------------------------

    def get_transfer_status(
        self, *, client_reference: str, provider_reference: str = ''
    ) -> ProviderTransferStatusResult:
        """Report what became of a request already made.

        Observational only: this sends nothing and moves nothing, which is why
        it is safe to call repeatedly. The answer comes from ``status_scenario``
        and is independent of what the original submission returned — that
        independence is the whole point, because the story worth telling is a
        submission that came back ambiguous and a lookup that later resolves it.
        """
        with self._lock:
            self._status_calls += 1

        if self.status_scenario == StatusScenario.SUCCESS:
            return ProviderTransferStatusSucceeded(
                provider_reference=provider_reference or f'SIM-{client_reference}'
            )

        if self.status_scenario == StatusScenario.FAILURE:
            return ProviderTransferStatusFailed(
                failure_code=self.status_failure_code,
                provider_reference=provider_reference,
            )

        if self.status_scenario == StatusScenario.NOT_FOUND:
            # Deliberately NOT a failure. For a request that may never have
            # arrived, "no record" establishes nothing: it is equally an
            # indexing delay, eventual consistency, or a key this rail does not
            # index. Calling it failure would release a reservation on money
            # that may already have gone.
            return ProviderTransferStatusUnresolved(
                reason='no_record_found',
                provider_reference=provider_reference,
            )

        return ProviderTransferStatusUnresolved(
            reason='still_processing', provider_reference=provider_reference
        )

    # -- webhooks -----------------------------------------------------

    def sign_webhook(self, body: bytes) -> dict:
        """The headers a delivery of ``body`` would carry. Test/demo helper."""
        return {
            SIMULATOR_SIGNATURE_HEADER: hmac.new(
                self._webhook_secret, body, hashlib.sha256
            ).hexdigest()
        }

    def build_webhook(
        self,
        *,
        provider_event_id: str,
        outcome: str,
        client_reference: str = '',
        provider_reference: str = '',
        failure_code: str = '',
    ) -> tuple:
        """A deterministic signed delivery, for tests and the demo.

        Returns the body and its headers, so a caller exercises exactly the
        path a real delivery would: bytes in, verified event out.
        """
        payload = {
            'event_id': provider_event_id,
            'outcome': outcome,
            'client_reference': client_reference,
            'provider_reference': provider_reference,
            'failure_code': failure_code,
        }
        body = json.dumps(payload, sort_keys=True).encode('utf-8')
        return body, self.sign_webhook(body)

    def verify_and_parse_webhook(
        self, *, body: bytes, headers
    ) -> NormalisedWebhookEvent:
        """Authenticate a delivery and normalise it, in one step.

        Deliberately inseparable: exposing a ``parse`` that worked on
        unverified bytes would be an invitation to call it. If the signature
        does not match, this raises and no event exists for anyone to act on.

        The comparison is constant-time. The scheme is the simulator's own and
        is not a claim about any real provider's.
        """
        provided = None
        for name, value in dict(headers).items():
            if name.lower() == SIMULATOR_SIGNATURE_HEADER.lower():
                provided = value
                break

        if not provided:
            raise ProviderWebhookUnauthenticatedError(
                'That webhook could not be authenticated.'
            )

        expected = hmac.new(self._webhook_secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(str(provided), expected):
            # Deliberately says nothing about what was expected.
            raise ProviderWebhookUnauthenticatedError(
                'That webhook could not be authenticated.'
            )

        try:
            payload = json.loads(body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise ProviderWebhookInvalidError(
                'That webhook body could not be read.'
            ) from None

        if not isinstance(payload, dict):
            raise ProviderWebhookInvalidError(
                'That webhook body was not an event.'
            )

        # Provider-shaped keys stop here. Everything past this point is the
        # normalised domain event.
        return NormalisedWebhookEvent(
            provider_key=self.provider_key,
            provider_event_id=payload.get('event_id', ''),
            outcome=payload.get('outcome', ''),
            client_reference=payload.get('client_reference', ''),
            provider_reference=payload.get('provider_reference', ''),
            failure_code=payload.get('failure_code', ''),
        )

    # -- reconciliation -----------------------------------------------

    def list_transfer_records(
        self, *, window, cursor: str = ''
    ) -> ProviderTransferRecordPage:
        """List the transfers this simulated rail believes it handled.

        Read-only and deterministic. The records come from whatever the test or
        demo configured — there is no generation, no randomness and no
        inference — so a scenario states exactly what the provider claims and
        the comparison has something unambiguous to disagree with.

        Records are filtered by ``observed_at`` against the half-open window,
        then paged in a stable order, so the same configuration always produces
        the same pages.
        """
        with self._lock:
            self._reconciliation_calls += 1

        if self.on_list_records is not None:
            self.on_list_records(window)

        if self.reconciliation_fails:
            raise ReconciliationProviderError(
                'The simulated rail could not list its records.'
            )

        in_window = [
            record for record in self.reconciliation_records
            if window.contains(record.observed_at)
        ]
        in_window.sort(key=lambda record: record.provider_record_id)

        if self.reconciliation_page_size <= 0:
            return ProviderTransferRecordPage(records=tuple(in_window))

        offset = int(cursor) if cursor else 0
        page = in_window[offset:offset + self.reconciliation_page_size]
        next_offset = offset + len(page)
        next_cursor = (
            str(next_offset) if next_offset < len(in_window) else ''
        )
        return ProviderTransferRecordPage(
            records=tuple(page), next_cursor=next_cursor
        )

    def record_matching(
        self,
        *,
        provider_record_id: str,
        client_reference: str,
        outcome: str,
        amount_minor: int,
        currency: str,
        observed_at,
        provider_reference: str = '',
        failure_code: str = '',
    ) -> ProviderTransferRecord:
        """Build one record this rail will report. A test/demo helper."""
        return ProviderTransferRecord(
            provider_key=self.provider_key,
            provider_record_id=provider_record_id,
            client_reference=client_reference,
            provider_reference=provider_reference,
            outcome=outcome,
            amount_minor=amount_minor,
            currency=currency,
            observed_at=observed_at,
            failure_code=failure_code,
        )

    # -- helpers ------------------------------------------------------

    @staticmethod
    def _reference_for(request: ProviderTransferRequest) -> str:
        """A deterministic stand-in for the rail's own reference."""
        return f'SIM-{request.client_reference}'
