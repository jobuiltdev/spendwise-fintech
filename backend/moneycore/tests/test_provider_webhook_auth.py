"""Webhook authentication: the gate that must not be walkable around.

The scheme itself is the simulator's own and is not a claim about any real
provider. What is being tested is the *boundary*: that unverified bytes cannot
become an event, and that a rejected delivery changes nothing at all.
"""

import pytest

from moneycore.domain.errors import (
    ProviderWebhookInvalidError,
    ProviderWebhookUnauthenticatedError,
)
from moneycore.domain.ledger import LedgerAccountType
from moneycore.domain.money import Money
from moneycore.domain.recovery import (
    NormalisedWebhookEvent,
    RecoveryOutcome,
)
from moneycore.domain.transactions import TransactionStatus
from moneycore.domain.transfers import VerifiedBankAccount
from moneycore.models import (
    Journal,
    ProviderRecoveryEvidence,
    ProviderWebhookEvent,
)
from moneycore.providers.simulator import (
    SIMULATOR_SIGNATURE_HEADER,
    SimulatorTransferProvider,
    TransferScenario,
)
from moneycore.services.holds import wallet_balance_projection
from moneycore.services.ledger import open_ledger_account
from moneycore.services.provider_execution import (
    execute_transfer,
    provider_attempt_for,
)
from moneycore.services.provider_recovery import ingest_webhook
from moneycore.services.transfers import prepare_transfer

pytestmark = pytest.mark.django_db


DESTINATION = VerifiedBankAccount(
    account_number='0123456789',
    bank_code='SIM-001',
    bank_name='Simulated First Bank',
    account_name='Ada Okafor',
)


@pytest.fixture
def settlement_account(db):
    return open_ledger_account(
        code='internal:m7-auth:NGN',
        name='Internal counterpart',
        account_type=LedgerAccountType.ASSET,
        currency='NGN',
    )


@pytest.fixture
def provider():
    return SimulatorTransferProvider()


@pytest.fixture
def ambiguous(funded_wallet, settlement_account):
    transfer = prepare_transfer(
        funded_wallet, DESTINATION, 7_000, idempotency_key='auth-1'
    )
    execute_transfer(
        transfer,
        provider=SimulatorTransferProvider(
            transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
        ),
        counterpart_account=settlement_account,
    )
    transfer.refresh_from_db()
    return transfer


class TestVerification:
    def test_a_valid_signature_is_accepted(self, provider):
        body, headers = provider.build_webhook(
            provider_event_id='evt-1',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference='SW-abc',
        )

        event = provider.verify_and_parse_webhook(body=body, headers=headers)

        assert isinstance(event, NormalisedWebhookEvent)

    def test_an_invalid_signature_is_rejected(self, provider):
        body, _ = provider.build_webhook(
            provider_event_id='evt-1',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference='SW-abc',
        )

        with pytest.raises(ProviderWebhookUnauthenticatedError):
            provider.verify_and_parse_webhook(
                body=body, headers={SIMULATOR_SIGNATURE_HEADER: 'nonsense'}
            )

    def test_a_missing_signature_is_rejected(self, provider):
        body, _ = provider.build_webhook(
            provider_event_id='evt-1',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference='SW-abc',
        )

        with pytest.raises(ProviderWebhookUnauthenticatedError):
            provider.verify_and_parse_webhook(body=body, headers={})

    def test_a_modified_body_is_rejected(self, provider):
        """The signature covers the body, so tampering breaks it."""
        body, headers = provider.build_webhook(
            provider_event_id='evt-1',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference='SW-abc',
        )
        tampered = body.replace(b'succeeded', b'failed___')

        with pytest.raises(ProviderWebhookUnauthenticatedError):
            provider.verify_and_parse_webhook(body=tampered, headers=headers)

    def test_a_signature_from_a_different_secret_is_rejected(self, provider):
        impostor = SimulatorTransferProvider(webhook_secret=b'a-different-secret')
        body, headers = impostor.build_webhook(
            provider_event_id='evt-1',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference='SW-abc',
        )

        with pytest.raises(ProviderWebhookUnauthenticatedError):
            provider.verify_and_parse_webhook(body=body, headers=headers)

    def test_the_header_name_is_matched_case_insensitively(self, provider):
        body, headers = provider.build_webhook(
            provider_event_id='evt-1',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference='SW-abc',
        )
        lowered = {k.lower(): v for k, v in headers.items()}

        assert provider.verify_and_parse_webhook(body=body, headers=lowered)

    def test_the_rejection_reveals_nothing_about_the_expected_value(
        self, provider
    ):
        body, headers = provider.build_webhook(
            provider_event_id='evt-1',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference='SW-abc',
        )
        expected = headers[SIMULATOR_SIGNATURE_HEADER]

        with pytest.raises(ProviderWebhookUnauthenticatedError) as raised:
            provider.verify_and_parse_webhook(
                body=body, headers={SIMULATOR_SIGNATURE_HEADER: 'wrong'}
            )

        message = str(raised.value) + str(raised.value.details)
        assert expected not in message
        assert 'secret' not in message.lower()

    def test_the_comparison_is_constant_time(self):
        """Timing must not leak the signature a byte at a time."""
        import inspect

        from moneycore.providers import simulator

        source = inspect.getsource(
            simulator.SimulatorTransferProvider.verify_and_parse_webhook
        )

        assert 'compare_digest' in source


class TestParsingIsInseparableFromVerification:
    def test_there_is_no_standalone_parse_method(self, provider):
        """A parse an attacker could reach would defeat the whole gate."""
        for forbidden in (
            'parse_webhook', 'parse', 'decode_webhook', 'read_webhook',
        ):
            assert not hasattr(provider, forbidden), forbidden

    def test_the_recovery_protocol_declares_only_the_combined_operation(self):
        from moneycore.domain.recovery import TransferRecoveryProvider

        methods = {
            name for name in dir(TransferRecoveryProvider)
            if not name.startswith('_')
        }

        assert 'verify_and_parse_webhook' in methods
        assert 'parse_webhook' not in methods

    def test_the_service_never_reads_the_body_itself(self):
        """No provider-shaped dictionary reaches financial orchestration."""
        from pathlib import Path

        from moneycore.services import provider_recovery

        source = Path(provider_recovery.__file__).read_text(encoding='utf-8')

        for forbidden in (
            'json.loads', "payload[", "['data']", 'request.body', 'hmac',
            'signature',
        ):
            assert forbidden not in source, forbidden


class TestAnInvalidDeliveryChangesNothing:
    def test_it_is_refused(self, ambiguous, provider, settlement_account):
        attempt = provider_attempt_for(ambiguous)
        body, _ = provider.build_webhook(
            provider_event_id='evt-forged',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference=attempt.client_reference,
        )

        with pytest.raises(ProviderWebhookUnauthenticatedError):
            ingest_webhook(
                provider=provider, body=body,
                headers={SIMULATOR_SIGNATURE_HEADER: 'forged'},
                counterpart_account=settlement_account,
            )

    def test_no_transaction_is_resolved(
        self, ambiguous, provider, settlement_account
    ):
        attempt = provider_attempt_for(ambiguous)
        body, _ = provider.build_webhook(
            provider_event_id='evt-forged-2',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference=attempt.client_reference,
        )

        with pytest.raises(ProviderWebhookUnauthenticatedError):
            ingest_webhook(
                provider=provider, body=body,
                headers={SIMULATOR_SIGNATURE_HEADER: 'forged'},
                counterpart_account=settlement_account,
            )

        ambiguous.refresh_from_db()
        assert ambiguous.status == TransactionStatus.UNKNOWN

    def test_no_hold_is_released(self, ambiguous, provider, settlement_account):
        from moneycore.domain.holds import HoldStatus

        attempt = provider_attempt_for(ambiguous)
        body, _ = provider.build_webhook(
            provider_event_id='evt-forged-3',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference=attempt.client_reference,
        )

        with pytest.raises(ProviderWebhookUnauthenticatedError):
            ingest_webhook(
                provider=provider, body=body,
                headers={SIMULATOR_SIGNATURE_HEADER: 'forged'},
                counterpart_account=settlement_account,
            )

        ambiguous.hold.refresh_from_db()
        assert ambiguous.hold.status == HoldStatus.ACTIVE

    def test_no_journal_is_posted(self, ambiguous, provider, settlement_account):
        attempt = provider_attempt_for(ambiguous)
        before = Journal.objects.count()
        body, _ = provider.build_webhook(
            provider_event_id='evt-forged-4',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference=attempt.client_reference,
        )

        with pytest.raises(ProviderWebhookUnauthenticatedError):
            ingest_webhook(
                provider=provider, body=body,
                headers={SIMULATOR_SIGNATURE_HEADER: 'forged'},
                counterpart_account=settlement_account,
            )

        assert Journal.objects.count() == before

    def test_the_attempt_is_not_touched(
        self, ambiguous, provider, settlement_account
    ):
        from moneycore.domain.providers import ProviderAttemptStatus

        attempt = provider_attempt_for(ambiguous)
        body, _ = provider.build_webhook(
            provider_event_id='evt-forged-5',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference=attempt.client_reference,
        )

        with pytest.raises(ProviderWebhookUnauthenticatedError):
            ingest_webhook(
                provider=provider, body=body,
                headers={SIMULATOR_SIGNATURE_HEADER: 'forged'},
                counterpart_account=settlement_account,
            )

        attempt.refresh_from_db()
        assert attempt.status == ProviderAttemptStatus.UNKNOWN

    def test_no_event_or_evidence_is_stored(
        self, ambiguous, provider, settlement_account
    ):
        """An unauthenticated request is not an event, so it cannot fill tables."""
        attempt = provider_attempt_for(ambiguous)
        body, _ = provider.build_webhook(
            provider_event_id='evt-forged-6',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference=attempt.client_reference,
        )

        with pytest.raises(ProviderWebhookUnauthenticatedError):
            ingest_webhook(
                provider=provider, body=body,
                headers={SIMULATOR_SIGNATURE_HEADER: 'forged'},
                counterpart_account=settlement_account,
            )

        assert ProviderWebhookEvent.objects.count() == 0
        assert ProviderRecoveryEvidence.objects.count() == 0

    def test_the_balance_is_untouched(
        self, ambiguous, provider, settlement_account
    ):
        attempt = provider_attempt_for(ambiguous)
        body, _ = provider.build_webhook(
            provider_event_id='evt-forged-7',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference=attempt.client_reference,
        )

        with pytest.raises(ProviderWebhookUnauthenticatedError):
            ingest_webhook(
                provider=provider, body=body,
                headers={SIMULATOR_SIGNATURE_HEADER: 'forged'},
                counterpart_account=settlement_account,
            )

        projection = wallet_balance_projection(ambiguous.wallet)
        assert projection.posted == Money(10_000, 'NGN')
        assert projection.held == Money(7_000, 'NGN')

    def test_a_valid_delivery_still_works_afterwards(
        self, ambiguous, provider, settlement_account
    ):
        attempt = provider_attempt_for(ambiguous)
        body, headers = provider.build_webhook(
            provider_event_id='evt-after-forgery',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference=attempt.client_reference,
        )

        with pytest.raises(ProviderWebhookUnauthenticatedError):
            ingest_webhook(
                provider=provider, body=body,
                headers={SIMULATOR_SIGNATURE_HEADER: 'forged'},
                counterpart_account=settlement_account,
            )

        ingest_webhook(
            provider=provider, body=body, headers=headers,
            counterpart_account=settlement_account,
        )

        ambiguous.refresh_from_db()
        assert ambiguous.status == TransactionStatus.SUCCEEDED


class TestTheSecretIsClearlyNonProduction:
    def test_it_is_a_test_constant_not_a_setting(self):
        from django.conf import settings

        for forbidden in (
            'WEBHOOK_SECRET', 'PROVIDER_WEBHOOK_SECRET', 'SIMULATOR_SECRET',
            'PROVIDER_API_KEY', 'PROVIDER_SECRET',
        ):
            assert not hasattr(settings, forbidden), forbidden

    def test_it_names_itself_as_non_production(self):
        from moneycore.providers.simulator import SIMULATOR_TEST_WEBHOOK_SECRET

        assert b'not-for-production' in SIMULATOR_TEST_WEBHOOK_SECRET

    def test_it_is_injectable_so_tests_can_vary_it(self):
        first = SimulatorTransferProvider(webhook_secret=b'one')
        second = SimulatorTransferProvider(webhook_secret=b'two')

        body, headers = first.build_webhook(
            provider_event_id='evt-1',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference='SW-abc',
        )

        with pytest.raises(ProviderWebhookUnauthenticatedError):
            second.verify_and_parse_webhook(body=body, headers=headers)

    def test_no_settings_module_mentions_a_provider_credential(self):
        from pathlib import Path

        import moneycore

        settings_source = (
            Path(moneycore.__file__).resolve().parents[1]
            / 'spendwise' / 'settings.py'
        ).read_text(encoding='utf-8')

        for forbidden in (
            'WEBHOOK_SECRET', 'PROVIDER_API_KEY', 'PROVIDER_SECRET',
            'PROVIDER_BASE_URL', 'SIGNING_KEY',
        ):
            assert forbidden not in settings_source, forbidden
