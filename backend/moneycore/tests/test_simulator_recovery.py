"""The simulator's recovery behaviour, and the demo it makes possible.

Determinism is the requirement: the investor story — ambiguity, then later
resolution, with no resubmission — must reproduce exactly every time.
"""

import pytest

from moneycore.domain.errors import ProviderWebhookUnauthenticatedError
from moneycore.domain.ledger import LedgerAccountType
from moneycore.domain.money import Money
from moneycore.domain.providers import ProviderAttemptStatus, ProviderFailureCode
from moneycore.domain.recovery import (
    ProviderTransferStatusFailed,
    ProviderTransferStatusSucceeded,
    ProviderTransferStatusUnresolved,
    RecoveryOutcome,
)
from moneycore.domain.holds import HoldStatus
from moneycore.domain.transactions import TransactionStatus
from moneycore.domain.transfers import VerifiedBankAccount
from moneycore.models import Journal
from moneycore.providers.simulator import (
    SimulatorTransferProvider,
    StatusScenario,
    TransferScenario,
)
from moneycore.services.holds import wallet_balance_projection
from moneycore.services.ledger import open_ledger_account
from moneycore.services.provider_execution import (
    execute_transfer,
    provider_attempt_for,
)
from moneycore.services.provider_recovery import (
    ingest_webhook,
    recover_provider_attempt,
)
from moneycore.services.transfers import prepare_transfer

DESTINATION = VerifiedBankAccount(
    account_number='0123456789',
    bank_code='SIM-001',
    bank_name='Simulated First Bank',
    account_name='Ada Okafor',
)


class TestStatusScenariosAreDeterministic:
    def test_success_returns_a_definitive_success(self):
        provider = SimulatorTransferProvider(
            status_scenario=StatusScenario.SUCCESS
        )

        result = provider.get_transfer_status(client_reference='SW-abc')

        assert isinstance(result, ProviderTransferStatusSucceeded)
        assert result.outcome == RecoveryOutcome.SUCCEEDED

    def test_failure_returns_a_definitive_failure(self):
        provider = SimulatorTransferProvider(
            status_scenario=StatusScenario.FAILURE,
            status_failure_code=ProviderFailureCode.DESTINATION_REJECTED,
        )

        result = provider.get_transfer_status(client_reference='SW-abc')

        assert isinstance(result, ProviderTransferStatusFailed)
        assert result.failure_code == 'destination_rejected'

    def test_unresolved_returns_an_explicit_unresolved(self):
        provider = SimulatorTransferProvider(
            status_scenario=StatusScenario.UNRESOLVED
        )

        result = provider.get_transfer_status(client_reference='SW-abc')

        assert isinstance(result, ProviderTransferStatusUnresolved)
        assert result.reason == 'still_processing'

    def test_not_found_is_unresolved_not_failure(self):
        """The distinction the whole recovery design turns on."""
        provider = SimulatorTransferProvider(
            status_scenario=StatusScenario.NOT_FOUND
        )

        result = provider.get_transfer_status(client_reference='SW-abc')

        assert isinstance(result, ProviderTransferStatusUnresolved)
        assert not isinstance(result, ProviderTransferStatusFailed)
        assert result.reason == 'no_record_found'

    def test_the_same_query_always_answers_the_same(self):
        provider = SimulatorTransferProvider(
            status_scenario=StatusScenario.SUCCESS
        )

        first = provider.get_transfer_status(client_reference='SW-abc')
        second = provider.get_transfer_status(client_reference='SW-abc')

        assert first == second

    def test_status_is_configured_independently_of_submission(self):
        """The point: an ambiguous submission may later be found successful."""
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION,
            status_scenario=StatusScenario.SUCCESS,
        )

        assert provider.transfer_scenario != provider.status_scenario

    def test_an_unknown_status_scenario_is_refused(self):
        with pytest.raises(ValueError):
            SimulatorTransferProvider(status_scenario='maybe')

    def test_querying_never_submits(self):
        provider = SimulatorTransferProvider(
            status_scenario=StatusScenario.SUCCESS
        )

        for _ in range(3):
            provider.get_transfer_status(client_reference='SW-abc')

        assert provider.submit_call_count == 0
        assert provider.status_call_count == 3

    def test_it_performs_no_io_and_no_sleeping(self):
        from pathlib import Path

        from moneycore.providers import simulator

        source = Path(simulator.__file__).read_text(encoding='utf-8')
        for forbidden in (
            'time.sleep', 'import requests', 'import httpx', 'socket',
            'urllib', 'while True',
            # Checked as imports/usage, not as the word: the module docstring
            # says "No randomness anywhere", which is the opposite of a defect.
            'import random', 'random.', 'uuid4', 'secrets',
        ):
            assert forbidden not in source, forbidden


class TestWebhookConstruction:
    def test_a_built_webhook_verifies(self):
        provider = SimulatorTransferProvider()

        body, headers = provider.build_webhook(
            provider_event_id='evt-1',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference='SW-abc',
        )

        assert provider.verify_and_parse_webhook(body=body, headers=headers)

    def test_the_same_event_builds_identically(self):
        provider = SimulatorTransferProvider()
        fields = {
            'provider_event_id': 'evt-1',
            'outcome': RecoveryOutcome.SUCCEEDED,
            'client_reference': 'SW-abc',
        }

        assert provider.build_webhook(**fields) == provider.build_webhook(**fields)

    def test_two_providers_with_one_secret_agree(self):
        first = SimulatorTransferProvider()
        second = SimulatorTransferProvider()

        body, headers = first.build_webhook(
            provider_event_id='evt-1',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference='SW-abc',
        )

        assert second.verify_and_parse_webhook(body=body, headers=headers)

    def test_a_failure_webhook_carries_its_code(self):
        provider = SimulatorTransferProvider()

        body, headers = provider.build_webhook(
            provider_event_id='evt-1',
            outcome=RecoveryOutcome.FAILED,
            client_reference='SW-abc',
            failure_code=ProviderFailureCode.REQUEST_REJECTED,
        )
        event = provider.verify_and_parse_webhook(body=body, headers=headers)

        assert event.failure_code == 'request_rejected'


@pytest.mark.django_db
class TestTheInvestorDemo:
    """Three deterministic stories, and no resubmission in any of them."""

    @pytest.fixture
    def settlement_account(self, db):
        return open_ledger_account(
            code='internal:m7-demo:NGN',
            name='Internal counterpart',
            account_type=LedgerAccountType.ASSET,
            currency='NGN',
        )

    def test_scenario_a_normal_success(self, funded_wallet, settlement_account):
        transfer = prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key='demo-a'
        )
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.SUCCESS
        )

        execute_transfer(
            transfer, provider=provider, counterpart_account=settlement_account
        )
        transfer.refresh_from_db()

        assert transfer.status == TransactionStatus.SUCCEEDED
        assert transfer.journal is not None
        assert wallet_balance_projection(
            funded_wallet
        ).available == Money(3_000, 'NGN')

    def test_scenario_b_ambiguity_then_status_recovery(
        self, funded_wallet, settlement_account
    ):
        """PROCESSING -> UNKNOWN -> SUCCEEDED, without ever resending."""
        transfer = prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key='demo-b'
        )
        submitting = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
        )
        execute_transfer(
            transfer, provider=submitting,
            counterpart_account=settlement_account,
        )
        transfer.refresh_from_db()
        transfer.hold.refresh_from_db()

        # The customer would see "Confirming" here: funds still reserved.
        assert transfer.status == TransactionStatus.UNKNOWN
        assert transfer.hold.status == HoldStatus.ACTIVE
        assert transfer.journal is None
        assert wallet_balance_projection(
            funded_wallet
        ).available == Money(3_000, 'NGN')

        recovering = SimulatorTransferProvider(
            status_scenario=StatusScenario.SUCCESS
        )
        recover_provider_attempt(
            provider_attempt_for(transfer),
            provider=recovering,
            counterpart_account=settlement_account,
        )
        transfer.refresh_from_db()
        transfer.hold.refresh_from_db()

        assert transfer.status == TransactionStatus.SUCCEEDED
        assert transfer.hold.status == HoldStatus.RELEASED
        assert transfer.journal is not None
        assert wallet_balance_projection(
            funded_wallet
        ).posted == Money(3_000, 'NGN')

        # Nothing was ever sent twice.
        assert submitting.submit_call_count == 1
        assert recovering.submit_call_count == 0

    def test_scenario_b_via_webhook_instead(
        self, funded_wallet, settlement_account
    ):
        transfer = prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key='demo-b2'
        )
        execute_transfer(
            transfer,
            provider=SimulatorTransferProvider(
                transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
            ),
            counterpart_account=settlement_account,
        )
        attempt = provider_attempt_for(transfer)

        provider = SimulatorTransferProvider()
        body, headers = provider.build_webhook(
            provider_event_id='demo-evt-1',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference=attempt.client_reference,
        )
        ingest_webhook(
            provider=provider, body=body, headers=headers,
            counterpart_account=settlement_account,
        )
        transfer.refresh_from_db()

        assert transfer.status == TransactionStatus.SUCCEEDED
        assert provider.submit_call_count == 0
        assert attempt.status == ProviderAttemptStatus.UNKNOWN

    def test_scenario_c_ambiguity_then_definitive_failure(
        self, funded_wallet, settlement_account
    ):
        transfer = prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key='demo-c'
        )
        execute_transfer(
            transfer,
            provider=SimulatorTransferProvider(
                transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
            ),
            counterpart_account=settlement_account,
        )

        recovering = SimulatorTransferProvider(
            status_scenario=StatusScenario.FAILURE
        )
        recover_provider_attempt(
            provider_attempt_for(transfer),
            provider=recovering,
            counterpart_account=settlement_account,
        )
        transfer.refresh_from_db()
        transfer.hold.refresh_from_db()

        assert transfer.status == TransactionStatus.FAILED
        assert transfer.hold.status == HoldStatus.RELEASED
        assert transfer.journal is None
        assert wallet_balance_projection(
            funded_wallet
        ).available == Money(10_000, 'NGN')
        assert recovering.submit_call_count == 0

    def test_the_whole_story_reproduces_identically(
        self, funded_wallet, settlement_account
    ):
        """Run the ambiguity-then-resolution story twice; same outcome."""
        outcomes = []
        for index in range(2):
            transfer = prepare_transfer(
                funded_wallet, DESTINATION, 3_000,
                idempotency_key=f'demo-repeat-{index}',
            )
            execute_transfer(
                transfer,
                provider=SimulatorTransferProvider(
                    transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
                ),
                counterpart_account=settlement_account,
            )
            transfer.refresh_from_db()
            intermediate = transfer.status

            recover_provider_attempt(
                provider_attempt_for(transfer),
                provider=SimulatorTransferProvider(
                    status_scenario=StatusScenario.SUCCESS
                ),
                counterpart_account=settlement_account,
            )
            transfer.refresh_from_db()
            outcomes.append((intermediate, transfer.status))

        assert outcomes[0] == outcomes[1]
        assert outcomes[0] == (
            TransactionStatus.UNKNOWN, TransactionStatus.SUCCEEDED
        )

    def test_the_audit_trail_tells_the_true_story(
        self, funded_wallet, settlement_account
    ):
        """Attempt: we did not know. Evidence: then we were told."""
        from moneycore.services.provider_recovery import recovery_evidence_for

        transfer = prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key='demo-audit'
        )
        execute_transfer(
            transfer,
            provider=SimulatorTransferProvider(
                transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
            ),
            counterpart_account=settlement_account,
        )
        attempt = provider_attempt_for(transfer)

        recover_provider_attempt(
            attempt,
            provider=SimulatorTransferProvider(
                status_scenario=StatusScenario.UNRESOLVED
            ),
            counterpart_account=settlement_account,
        )
        attempt.refresh_from_db()
        recover_provider_attempt(
            attempt,
            provider=SimulatorTransferProvider(
                status_scenario=StatusScenario.SUCCESS
            ),
            counterpart_account=settlement_account,
        )
        transfer.refresh_from_db()
        attempt.refresh_from_db()

        assert attempt.status == ProviderAttemptStatus.UNKNOWN
        assert [e.outcome for e in recovery_evidence_for(attempt)] == [
            RecoveryOutcome.UNRESOLVED,
            RecoveryOutcome.SUCCEEDED,
        ]
        assert transfer.status == TransactionStatus.SUCCEEDED
