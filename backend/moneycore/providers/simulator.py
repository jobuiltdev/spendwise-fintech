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

import threading
from typing import Final

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
    ) -> None:
        if transfer_scenario not in TransferScenario.ALL:
            raise ValueError(f'Unknown transfer scenario: {transfer_scenario!r}')
        if account_resolution_scenario not in AccountResolutionScenario.ALL:
            raise ValueError(
                f'Unknown resolution scenario: {account_resolution_scenario!r}'
            )
        if failure_code not in ProviderFailureCode.ALL:
            raise ValueError(f'Unknown failure code: {failure_code!r}')

        self.transfer_scenario = transfer_scenario
        self.account_resolution_scenario = account_resolution_scenario
        self.failure_code = failure_code

        self._lock = threading.Lock()
        self._submit_calls = 0
        self._resolve_calls = 0
        #: Every request the rail was actually asked to execute, in order.
        self.submitted_requests: list[ProviderTransferRequest] = []
        #: Hook for tests that need to observe the moment of the call itself —
        #: notably the proof that no database transaction is open during it.
        self.on_submit = None

    # -- observation --------------------------------------------------

    @property
    def submit_call_count(self) -> int:
        with self._lock:
            return self._submit_calls

    @property
    def resolve_call_count(self) -> int:
        with self._lock:
            return self._resolve_calls

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

    # -- helpers ------------------------------------------------------

    @staticmethod
    def _reference_for(request: ProviderTransferRequest) -> str:
        """A deterministic stand-in for the rail's own reference."""
        return f'SIM-{request.client_reference}'
