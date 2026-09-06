"""Being told what happened, rather than asking.

Two invariants carry this file: an authenticated redelivery resolves money at
most once, and evidence that contradicts settled truth is escalated rather than
acted on.
"""

import pytest

from moneycore.domain.errors import (
    ProviderRecoveryConflictError,
    ProviderRecoveryReferenceMismatchError,
    ProviderWebhookConflictError,
    ProviderWebhookInvalidError,
    ProviderWebhookUnauthenticatedError,
    ProviderWebhookUnmatchedError,
)
from moneycore.domain.holds import HoldStatus
from moneycore.domain.ledger import LedgerAccountType
from moneycore.domain.money import Money
from moneycore.domain.providers import ProviderAttemptStatus, ProviderFailureCode
from moneycore.domain.recovery import (
    EvidenceSource,
    RecoveryOutcome,
    WebhookProcessingStatus,
)
from moneycore.domain.transactions import TransactionStatus
from moneycore.domain.transfers import VerifiedBankAccount
from moneycore.models import (
    Journal,
    ProviderExecutionAttempt,
    ProviderRecoveryEvidence,
    ProviderWebhookEvent,
)
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
    recovery_evidence_for,
)
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
        code='internal:m7-webhook-counterpart:NGN',
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
        funded_wallet, DESTINATION, 7_000, idempotency_key='webhook-1'
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


def deliver(provider, settlement_account, **event):
    body, headers = provider.build_webhook(**event)
    return ingest_webhook(
        provider=provider,
        body=body,
        headers=headers,
        counterpart_account=settlement_account,
    )


class TestWebhookResolvesUnknown:
    @pytest.fixture
    def delivered(self, ambiguous, provider, settlement_account):
        attempt = provider_attempt_for(ambiguous)
        receipt = deliver(
            provider, settlement_account,
            provider_event_id='evt-success-1',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference=attempt.client_reference,
        )
        ambiguous.refresh_from_db()
        return ambiguous, attempt, receipt

    def test_the_transaction_succeeds(self, delivered):
        transfer, _, _ = delivered

        assert transfer.status == TransactionStatus.SUCCEEDED

    def test_the_reservation_is_released(self, delivered):
        transfer, _, _ = delivered

        transfer.hold.refresh_from_db()
        assert transfer.hold.status == HoldStatus.RELEASED

    def test_exactly_one_journal_is_posted(self, delivered):
        transfer, _, _ = delivered

        assert transfer.journal is not None
        assert Journal.objects.count() == 2

    def test_the_balance_picture_is_exact(self, delivered):
        transfer, _, _ = delivered

        projection = wallet_balance_projection(transfer.wallet)
        assert projection.posted == Money(3_000, 'NGN')
        assert projection.held == Money(0, 'NGN')
        assert projection.available == Money(3_000, 'NGN')

    def test_the_attempt_remains_unknown(self, delivered):
        _, attempt, _ = delivered

        attempt.refresh_from_db()
        assert attempt.status == ProviderAttemptStatus.UNKNOWN

    def test_the_receipt_is_marked_processed(self, delivered):
        _, _, receipt = delivered

        receipt.refresh_from_db()
        assert receipt.processing_status == WebhookProcessingStatus.PROCESSED
        assert receipt.processed_at is not None

    def test_evidence_records_the_webhook_source(self, delivered):
        _, attempt, _ = delivered

        evidence = recovery_evidence_for(attempt).get()
        assert evidence.source == EvidenceSource.WEBHOOK
        assert evidence.outcome == RecoveryOutcome.SUCCEEDED
        assert evidence.provider_event_id == 'evt-success-1'

    def test_nothing_was_submitted(self, delivered, provider):
        assert provider.submit_call_count == 0

    def test_a_failure_webhook_resolves_the_other_way(
        self, ambiguous, provider, settlement_account
    ):
        attempt = provider_attempt_for(ambiguous)

        deliver(
            provider, settlement_account,
            provider_event_id='evt-failed-1',
            outcome=RecoveryOutcome.FAILED,
            client_reference=attempt.client_reference,
            failure_code=ProviderFailureCode.DESTINATION_REJECTED,
        )
        ambiguous.refresh_from_db()
        ambiguous.hold.refresh_from_db()

        assert ambiguous.status == TransactionStatus.FAILED
        assert ambiguous.hold.status == HoldStatus.RELEASED
        assert Journal.objects.count() == 1
        assert wallet_balance_projection(
            ambiguous.wallet
        ).available == Money(10_000, 'NGN')

    def test_an_unresolved_webhook_changes_nothing(
        self, ambiguous, provider, settlement_account
    ):
        attempt = provider_attempt_for(ambiguous)

        receipt = deliver(
            provider, settlement_account,
            provider_event_id='evt-unresolved-1',
            outcome=RecoveryOutcome.UNRESOLVED,
            client_reference=attempt.client_reference,
        )
        ambiguous.refresh_from_db()
        ambiguous.hold.refresh_from_db()

        assert ambiguous.status == TransactionStatus.UNKNOWN
        assert ambiguous.hold.status == HoldStatus.ACTIVE
        assert Journal.objects.count() == 1
        assert receipt.processing_status == WebhookProcessingStatus.PROCESSED
        assert recovery_evidence_for(attempt).get().outcome == (
            RecoveryOutcome.UNRESOLVED
        )


class TestDuplicateDelivery:
    """Rails redeliver. Money must move at most once."""

    def test_the_same_event_twice_resolves_once(
        self, ambiguous, provider, settlement_account
    ):
        attempt = provider_attempt_for(ambiguous)
        event = {
            'provider_event_id': 'evt-dup',
            'outcome': RecoveryOutcome.SUCCEEDED,
            'client_reference': attempt.client_reference,
        }

        first = deliver(provider, settlement_account, **event)
        second = deliver(provider, settlement_account, **event)

        ambiguous.refresh_from_db()
        assert first.pk == second.pk
        assert ProviderWebhookEvent.objects.count() == 1
        assert Journal.objects.count() == 2
        assert ambiguous.status == TransactionStatus.SUCCEEDED

    def test_five_deliveries_resolve_once(
        self, ambiguous, provider, settlement_account
    ):
        attempt = provider_attempt_for(ambiguous)
        event = {
            'provider_event_id': 'evt-dup-5',
            'outcome': RecoveryOutcome.SUCCEEDED,
            'client_reference': attempt.client_reference,
        }

        for _ in range(5):
            deliver(provider, settlement_account, **event)

        assert ProviderWebhookEvent.objects.count() == 1
        assert ProviderRecoveryEvidence.objects.count() == 1
        assert Journal.objects.count() == 2
        assert wallet_balance_projection(
            ambiguous.wallet
        ).posted == Money(3_000, 'NGN')

    def test_the_hold_is_released_once(
        self, ambiguous, provider, settlement_account
    ):
        attempt = provider_attempt_for(ambiguous)
        event = {
            'provider_event_id': 'evt-dup-hold',
            'outcome': RecoveryOutcome.SUCCEEDED,
            'client_reference': attempt.client_reference,
        }

        deliver(provider, settlement_account, **event)
        deliver(provider, settlement_account, **event)

        ambiguous.hold.refresh_from_db()
        assert ambiguous.hold.status == HoldStatus.RELEASED

    def test_a_reused_event_id_with_different_contents_conflicts(
        self, ambiguous, provider, settlement_account
    ):
        """Not a redelivery: the same identity saying something else."""
        attempt = provider_attempt_for(ambiguous)
        deliver(
            provider, settlement_account,
            provider_event_id='evt-shifty',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference=attempt.client_reference,
        )

        with pytest.raises(ProviderWebhookConflictError):
            deliver(
                provider, settlement_account,
                provider_event_id='evt-shifty',
                outcome=RecoveryOutcome.FAILED,
                client_reference=attempt.client_reference,
                failure_code=ProviderFailureCode.REQUEST_REJECTED,
            )

    def test_the_original_receipt_is_not_overwritten(
        self, ambiguous, provider, settlement_account
    ):
        attempt = provider_attempt_for(ambiguous)
        deliver(
            provider, settlement_account,
            provider_event_id='evt-keep',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference=attempt.client_reference,
        )

        with pytest.raises(ProviderWebhookConflictError):
            deliver(
                provider, settlement_account,
                provider_event_id='evt-keep',
                outcome=RecoveryOutcome.FAILED,
                client_reference=attempt.client_reference,
                failure_code=ProviderFailureCode.REQUEST_REJECTED,
            )

        receipt = ProviderWebhookEvent.objects.get()
        assert receipt.outcome == RecoveryOutcome.SUCCEEDED
        assert receipt.failure_code == ''

    def test_a_different_event_id_for_the_same_outcome_is_accepted(
        self, ambiguous, provider, settlement_account
    ):
        """A genuinely separate delivery, agreeing. Idempotent financially."""
        attempt = provider_attempt_for(ambiguous)

        deliver(
            provider, settlement_account,
            provider_event_id='evt-a',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference=attempt.client_reference,
        )
        deliver(
            provider, settlement_account,
            provider_event_id='evt-b',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference=attempt.client_reference,
        )

        ambiguous.refresh_from_db()
        assert ProviderWebhookEvent.objects.count() == 2
        assert ProviderRecoveryEvidence.objects.count() == 2
        assert Journal.objects.count() == 2  # still one transfer journal
        assert ambiguous.status == TransactionStatus.SUCCEEDED


class TestContradictoryEvidence:
    """Never auto-corrected. Recorded, escalated, and left alone."""

    @pytest.fixture
    def succeeded(self, ambiguous, provider, settlement_account):
        attempt = provider_attempt_for(ambiguous)
        deliver(
            provider, settlement_account,
            provider_event_id='evt-won',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference=attempt.client_reference,
        )
        ambiguous.refresh_from_db()
        return ambiguous, attempt

    def test_a_later_failure_is_refused(
        self, succeeded, provider, settlement_account
    ):
        _, attempt = succeeded

        with pytest.raises(ProviderRecoveryConflictError):
            deliver(
                provider, settlement_account,
                provider_event_id='evt-contradiction',
                outcome=RecoveryOutcome.FAILED,
                client_reference=attempt.client_reference,
                failure_code=ProviderFailureCode.REQUEST_REJECTED,
            )

    def test_the_settled_outcome_is_untouched(
        self, succeeded, provider, settlement_account
    ):
        transfer, attempt = succeeded

        with pytest.raises(ProviderRecoveryConflictError):
            deliver(
                provider, settlement_account,
                provider_event_id='evt-contradiction-2',
                outcome=RecoveryOutcome.FAILED,
                client_reference=attempt.client_reference,
                failure_code=ProviderFailureCode.REQUEST_REJECTED,
            )

        transfer.refresh_from_db()
        assert transfer.status == TransactionStatus.SUCCEEDED
        assert transfer.journal is not None

    def test_no_journal_is_reversed_or_added(
        self, succeeded, provider, settlement_account
    ):
        _, attempt = succeeded
        before = Journal.objects.count()

        with pytest.raises(ProviderRecoveryConflictError):
            deliver(
                provider, settlement_account,
                provider_event_id='evt-contradiction-3',
                outcome=RecoveryOutcome.FAILED,
                client_reference=attempt.client_reference,
                failure_code=ProviderFailureCode.REQUEST_REJECTED,
            )

        assert Journal.objects.count() == before

    def test_the_balance_is_untouched(
        self, succeeded, provider, settlement_account
    ):
        transfer, attempt = succeeded
        before = wallet_balance_projection(transfer.wallet)

        with pytest.raises(ProviderRecoveryConflictError):
            deliver(
                provider, settlement_account,
                provider_event_id='evt-contradiction-4',
                outcome=RecoveryOutcome.FAILED,
                client_reference=attempt.client_reference,
                failure_code=ProviderFailureCode.REQUEST_REJECTED,
            )

        assert wallet_balance_projection(transfer.wallet) == before

    def test_the_contradiction_is_kept_and_flagged(
        self, succeeded, provider, settlement_account
    ):
        """Discarding it would hide exactly what operations must see."""
        _, attempt = succeeded

        with pytest.raises(ProviderRecoveryConflictError):
            deliver(
                provider, settlement_account,
                provider_event_id='evt-contradiction-5',
                outcome=RecoveryOutcome.FAILED,
                client_reference=attempt.client_reference,
                failure_code=ProviderFailureCode.REQUEST_REJECTED,
            )

        receipt = ProviderWebhookEvent.objects.get(
            provider_event_id='evt-contradiction-5'
        )
        assert receipt.processing_status == WebhookProcessingStatus.CONFLICTED
        assert receipt.outcome == RecoveryOutcome.FAILED

    def test_the_contradicting_evidence_row_survives(
        self, succeeded, provider, settlement_account
    ):
        _, attempt = succeeded

        with pytest.raises(ProviderRecoveryConflictError):
            deliver(
                provider, settlement_account,
                provider_event_id='evt-contradiction-6',
                outcome=RecoveryOutcome.FAILED,
                client_reference=attempt.client_reference,
                failure_code=ProviderFailureCode.REQUEST_REJECTED,
            )

        outcomes = [e.outcome for e in recovery_evidence_for(attempt)]
        assert RecoveryOutcome.SUCCEEDED in outcomes
        assert RecoveryOutcome.FAILED in outcomes

    def test_a_failure_then_success_also_conflicts(
        self, ambiguous, provider, settlement_account
    ):
        attempt = provider_attempt_for(ambiguous)
        deliver(
            provider, settlement_account,
            provider_event_id='evt-failed-first',
            outcome=RecoveryOutcome.FAILED,
            client_reference=attempt.client_reference,
            failure_code=ProviderFailureCode.REQUEST_REJECTED,
        )

        with pytest.raises(ProviderRecoveryConflictError):
            deliver(
                provider, settlement_account,
                provider_event_id='evt-success-after',
                outcome=RecoveryOutcome.SUCCEEDED,
                client_reference=attempt.client_reference,
            )

        ambiguous.refresh_from_db()
        assert ambiguous.status == TransactionStatus.FAILED
        assert Journal.objects.count() == 1

    def test_an_unresolved_webhook_after_settlement_is_harmless(
        self, succeeded, provider, settlement_account
    ):
        """It asserts nothing, so it contradicts nothing."""
        transfer, attempt = succeeded

        receipt = deliver(
            provider, settlement_account,
            provider_event_id='evt-late-unresolved',
            outcome=RecoveryOutcome.UNRESOLVED,
            client_reference=attempt.client_reference,
        )

        transfer.refresh_from_db()
        assert receipt.processing_status == WebhookProcessingStatus.PROCESSED
        assert transfer.status == TransactionStatus.SUCCEEDED


class TestCorrelation:
    def test_a_client_reference_locates_the_attempt(
        self, ambiguous, provider, settlement_account
    ):
        attempt = provider_attempt_for(ambiguous)

        receipt = deliver(
            provider, settlement_account,
            provider_event_id='evt-by-client',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference=attempt.client_reference,
        )

        assert receipt.provider_attempt_id == attempt.pk

    def test_a_provider_reference_alone_can_locate_it(
        self, funded_wallet, settlement_account
    ):
        transfer = prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key='by-provider-ref'
        )
        execute_transfer(
            transfer,
            provider=SimulatorTransferProvider(
                transfer_scenario=TransferScenario.KNOWN_FAILURE
            ),
            counterpart_account=settlement_account,
        )
        attempt = provider_attempt_for(transfer)
        assert attempt.provider_reference

        provider = SimulatorTransferProvider()
        receipt = deliver(
            provider, settlement_account,
            provider_event_id='evt-by-provider',
            outcome=RecoveryOutcome.UNRESOLVED,
            provider_reference=attempt.provider_reference,
        )

        assert receipt.provider_attempt_id == attempt.pk

    def test_references_naming_different_attempts_are_refused(
        self, funded_wallet, provider, settlement_account
    ):
        """Guessing which to believe is how money gets misattributed."""
        first = prepare_transfer(
            funded_wallet, DESTINATION, 3_000, idempotency_key='mismatch-a'
        )
        execute_transfer(
            first,
            provider=SimulatorTransferProvider(
                transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
            ),
            counterpart_account=settlement_account,
        )
        second = prepare_transfer(
            funded_wallet, DESTINATION, 3_000, idempotency_key='mismatch-b'
        )
        execute_transfer(
            second,
            provider=SimulatorTransferProvider(
                transfer_scenario=TransferScenario.KNOWN_FAILURE
            ),
            counterpart_account=settlement_account,
        )

        attempt_a = provider_attempt_for(first)
        attempt_b = provider_attempt_for(second)

        with pytest.raises(ProviderRecoveryReferenceMismatchError):
            deliver(
                provider, settlement_account,
                provider_event_id='evt-mismatch',
                outcome=RecoveryOutcome.SUCCEEDED,
                client_reference=attempt_a.client_reference,
                provider_reference=attempt_b.provider_reference,
            )

    def test_matching_never_falls_back_to_amount_or_account(self):
        """A plausible-looking match is not a match."""
        import inspect

        from moneycore.services import provider_recovery

        source = inspect.getsource(provider_recovery._match_attempt)

        for forbidden in (
            'amount_minor', 'destination_account_number', 'recipient_name',
            'wallet', 'currency',
        ):
            assert forbidden not in source, forbidden


class TestUnmatchedWebhook:
    def test_an_unknown_reference_is_refused(self, provider, settlement_account):
        with pytest.raises(ProviderWebhookUnmatchedError):
            deliver(
                provider, settlement_account,
                provider_event_id='evt-orphan',
                outcome=RecoveryOutcome.SUCCEEDED,
                client_reference='SW-nothing-here',
            )

    def test_it_creates_no_transaction(self, provider, settlement_account):
        from moneycore.models import FinancialTransaction, Transfer

        with pytest.raises(ProviderWebhookUnmatchedError):
            deliver(
                provider, settlement_account,
                provider_event_id='evt-orphan-2',
                outcome=RecoveryOutcome.SUCCEEDED,
                client_reference='SW-nothing-here',
            )

        assert FinancialTransaction.objects.count() == 0
        assert Transfer.objects.count() == 0
        assert Journal.objects.count() == 0

    def test_it_is_kept_for_operations_to_see(self, provider, settlement_account):
        """An authentic rail describing money we have no record of matters."""
        with pytest.raises(ProviderWebhookUnmatchedError):
            deliver(
                provider, settlement_account,
                provider_event_id='evt-orphan-3',
                outcome=RecoveryOutcome.SUCCEEDED,
                client_reference='SW-nothing-here',
            )

        receipt = ProviderWebhookEvent.objects.get(
            provider_event_id='evt-orphan-3'
        )
        assert receipt.processing_status == WebhookProcessingStatus.UNMATCHED
        assert receipt.provider_attempt_id is None

    def test_it_is_never_attached_to_the_nearest_transfer(
        self, ambiguous, provider, settlement_account
    ):
        with pytest.raises(ProviderWebhookUnmatchedError):
            deliver(
                provider, settlement_account,
                provider_event_id='evt-orphan-4',
                outcome=RecoveryOutcome.SUCCEEDED,
                client_reference='SW-not-a-real-reference',
            )

        ambiguous.refresh_from_db()
        assert ambiguous.status == TransactionStatus.UNKNOWN
        assert ProviderRecoveryEvidence.objects.count() == 0


class TestMalformedDelivery:
    def test_an_unparseable_body_is_refused(self, provider, settlement_account):
        headers = provider.sign_webhook(b'not json')

        with pytest.raises(ProviderWebhookInvalidError):
            ingest_webhook(
                provider=provider, body=b'not json', headers=headers,
                counterpart_account=settlement_account,
            )

    def test_an_event_without_an_id_is_refused(self, provider, settlement_account):
        with pytest.raises(ProviderWebhookInvalidError):
            deliver(
                provider, settlement_account,
                provider_event_id='',
                outcome=RecoveryOutcome.SUCCEEDED,
                client_reference='SW-abc',
            )

    def test_an_unknown_outcome_is_refused(self, provider, settlement_account):
        with pytest.raises(ProviderWebhookInvalidError):
            deliver(
                provider, settlement_account,
                provider_event_id='evt-bad-outcome',
                outcome='settling',
                client_reference='SW-abc',
            )

    def test_a_malformed_delivery_stores_nothing(
        self, ambiguous, provider, settlement_account
    ):
        with pytest.raises(ProviderWebhookInvalidError):
            deliver(
                provider, settlement_account,
                provider_event_id='evt-bad',
                outcome='settling',
                client_reference='SW-abc',
            )

        assert ProviderWebhookEvent.objects.count() == 0
        assert ProviderRecoveryEvidence.objects.count() == 0

    def test_a_nonsense_adapter_event_is_refused(self, settlement_account):
        class BrokenProvider:
            provider_key = 'broken'

            def verify_and_parse_webhook(self, *, body, headers):
                return {'outcome': 'succeeded'}

        with pytest.raises(ProviderWebhookInvalidError):
            ingest_webhook(
                provider=BrokenProvider(), body=b'{}', headers={},
                counterpart_account=settlement_account,
            )
