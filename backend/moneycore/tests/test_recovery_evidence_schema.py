"""Evidence and receipt schemas, and their immutability.

Both are append-only historical truth, and neither stores a byte of raw
provider transport.
"""

import pytest
from django.db import IntegrityError, models as dj, transaction as db_transaction
from django.utils import timezone

from moneycore.domain.errors import ProviderRecoveryEvidenceImmutableError
from moneycore.domain.ledger import LedgerAccountType
from moneycore.domain.providers import ProviderAttemptStatus
from moneycore.domain.recovery import (
    EvidenceSource,
    RecoveryOutcome,
    WebhookProcessingStatus,
)
from moneycore.domain.transfers import VerifiedBankAccount
from moneycore.models import (
    ProviderExecutionAttempt,
    ProviderRecoveryEvidence,
    ProviderWebhookEvent,
)
from moneycore.providers.simulator import (
    SimulatorTransferProvider,
    TransferScenario,
)
from moneycore.services.ledger import open_ledger_account
from moneycore.services.provider_execution import (
    execute_transfer,
    provider_attempt_for,
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
        code='internal:m7-schema:NGN',
        name='Internal counterpart',
        account_type=LedgerAccountType.ASSET,
        currency='NGN',
    )


@pytest.fixture
def attempt(funded_wallet, settlement_account):
    transfer = prepare_transfer(
        funded_wallet, DESTINATION, 7_000, idempotency_key='schema-1'
    )
    execute_transfer(
        transfer,
        provider=SimulatorTransferProvider(
            transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
        ),
        counterpart_account=settlement_account,
    )
    return provider_attempt_for(transfer)


def make_evidence(attempt, **overrides):
    values = {
        'provider_attempt': attempt,
        'source': EvidenceSource.STATUS_QUERY,
        'outcome': RecoveryOutcome.UNRESOLVED,
        'observed_at': timezone.now(),
    }
    values.update(overrides)
    return ProviderRecoveryEvidence.objects.create(**values)


def make_receipt(**overrides):
    values = {
        'provider_key': 'simulator',
        'provider_event_id': 'evt-1',
        'outcome': RecoveryOutcome.SUCCEEDED,
        'client_reference': 'SW-abc',
    }
    values.update(overrides)
    return ProviderWebhookEvent.objects.create(**values)


class TestTheModelSet:
    def test_the_app_declares_exactly_the_models_through_m7(self):
        from django.apps import apps

        declared = {m.__name__ for m in apps.get_app_config('moneycore').get_models()}

        assert declared == {
            'FinancialCustomer', 'FinancialAccount', 'Wallet',
            'LedgerAccount', 'Journal', 'JournalEntry',
            'FundsHold', 'FinancialTransaction', 'Transfer',
            'ProviderExecutionAttempt',
            'ProviderRecoveryEvidence', 'ProviderWebhookEvent',
        }

    def test_the_two_models_serve_different_concerns(self):
        """A receipt may be unmatched; evidence always belongs to an attempt."""
        evidence_attempt = ProviderRecoveryEvidence._meta.get_field(
            'provider_attempt'
        )
        receipt_attempt = ProviderWebhookEvent._meta.get_field('provider_attempt')

        assert not evidence_attempt.null
        assert receipt_attempt.null


class TestEvidenceSchema:
    def test_the_field_set_is_exactly_what_m7_specified(self):
        concrete = {
            f.name for f in ProviderRecoveryEvidence._meta.get_fields()
            if f.concrete
        }

        assert concrete == {
            'id', 'provider_attempt', 'source', 'outcome',
            'provider_event_id', 'provider_reference', 'failure_code',
            'observed_at', 'created_at',
        }

    def test_it_belongs_to_an_attempt_and_nothing_else(self):
        related = {
            f.related_model.__name__
            for f in ProviderRecoveryEvidence._meta.get_fields()
            if f.related_model is not None
        }

        assert related == {'ProviderExecutionAttempt'}

    def test_the_attempt_is_protected(self):
        field = ProviderRecoveryEvidence._meta.get_field('provider_attempt')

        assert field.remote_field.on_delete is dj.PROTECT

    @pytest.mark.parametrize(
        'forbidden',
        ['raw_payload', 'payload_json', 'response_json', 'body', 'headers',
         'authorization', 'signature', 'secret', 'api_key', 'request_body',
         'http_status', 'url', 'stack_trace'],
    )
    def test_no_raw_transport_field_exists(self, forbidden):
        names = {f.name for f in ProviderRecoveryEvidence._meta.get_fields()}

        assert forbidden not in names

    def test_no_balance_or_amount_field_exists(self):
        names = {f.name for f in ProviderRecoveryEvidence._meta.get_fields()}

        assert names.isdisjoint({
            'amount', 'amount_minor', 'balance', 'fee', 'fee_minor',
        })

    def test_no_float_or_decimal_field_exists(self):
        for field in ProviderRecoveryEvidence._meta.get_fields():
            assert not isinstance(field, (dj.FloatField, dj.DecimalField))


class TestEvidenceConstraints:
    def test_an_invalid_source_is_refused(self, attempt):
        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                make_evidence(attempt, source='guesswork')

    def test_an_invalid_outcome_is_refused(self, attempt):
        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                make_evidence(attempt, outcome='settling')

    @pytest.mark.parametrize(
        'outcome', [RecoveryOutcome.SUCCEEDED, RecoveryOutcome.UNRESOLVED],
    )
    def test_only_a_failure_may_carry_a_failure_code(self, attempt, outcome):
        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                make_evidence(
                    attempt, outcome=outcome, failure_code='request_rejected'
                )

    def test_a_failure_may_carry_one(self, attempt):
        evidence = make_evidence(
            attempt,
            outcome=RecoveryOutcome.FAILED,
            failure_code='request_rejected',
        )

        assert evidence.failure_code == 'request_rejected'

    def test_one_webhook_delivery_yields_at_most_one_observation(self, attempt):
        make_evidence(
            attempt, source=EvidenceSource.WEBHOOK, provider_event_id='evt-x'
        )

        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                make_evidence(
                    attempt,
                    source=EvidenceSource.WEBHOOK,
                    provider_event_id='evt-x',
                )

    def test_status_queries_may_repeat_freely(self, attempt):
        """They have no provider observation id, and asking again is safe."""
        for _ in range(3):
            make_evidence(attempt, source=EvidenceSource.STATUS_QUERY)

        assert ProviderRecoveryEvidence.objects.count() == 3


class TestEvidenceIsAppendOnly:
    @pytest.mark.parametrize(
        'field,value',
        [
            ('outcome', RecoveryOutcome.SUCCEEDED),
            ('source', EvidenceSource.WEBHOOK),
            ('provider_reference', 'SIM-rewritten'),
            ('provider_event_id', 'evt-rewritten'),
        ],
    )
    def test_it_cannot_be_rewritten(self, attempt, field, value):
        evidence = make_evidence(attempt)

        setattr(evidence, field, value)
        with pytest.raises(ProviderRecoveryEvidenceImmutableError):
            evidence.save()

    def test_the_attempt_link_cannot_be_rewritten(
        self, attempt, funded_wallet, settlement_account
    ):
        evidence = make_evidence(attempt)
        other = prepare_transfer(
            funded_wallet, DESTINATION, 1_000, idempotency_key='schema-other'
        )
        execute_transfer(
            other,
            provider=SimulatorTransferProvider(
                transfer_scenario=TransferScenario.KNOWN_FAILURE
            ),
            counterpart_account=settlement_account,
        )

        evidence.provider_attempt = provider_attempt_for(other)
        with pytest.raises(ProviderRecoveryEvidenceImmutableError):
            evidence.save()

    def test_the_stored_row_survives_a_refused_edit(self, attempt):
        evidence = make_evidence(attempt)

        evidence.outcome = RecoveryOutcome.SUCCEEDED
        with pytest.raises(ProviderRecoveryEvidenceImmutableError):
            evidence.save()

        evidence.refresh_from_db()
        assert evidence.outcome == RecoveryOutcome.UNRESOLVED

    def test_bulk_update_is_refused(self, attempt):
        make_evidence(attempt)

        with pytest.raises(ProviderRecoveryEvidenceImmutableError):
            ProviderRecoveryEvidence.objects.all().update(
                outcome=RecoveryOutcome.SUCCEEDED
            )

    def test_it_cannot_be_deleted(self, attempt):
        evidence = make_evidence(attempt)

        with pytest.raises(ProviderRecoveryEvidenceImmutableError):
            evidence.delete()

        assert ProviderRecoveryEvidence.objects.filter(pk=evidence.pk).exists()

    def test_a_queryset_delete_is_refused(self, attempt):
        make_evidence(attempt)

        with pytest.raises(ProviderRecoveryEvidenceImmutableError):
            ProviderRecoveryEvidence.objects.all().delete()


class TestWebhookReceiptSchema:
    def test_the_field_set_is_exactly_what_m7_specified(self):
        concrete = {
            f.name for f in ProviderWebhookEvent._meta.get_fields()
            if f.concrete
        }

        assert concrete == {
            'id', 'provider_key', 'provider_event_id', 'outcome',
            'client_reference', 'provider_reference', 'failure_code',
            'processing_status', 'provider_attempt',
            'received_at', 'processed_at', 'created_at',
        }

    @pytest.mark.parametrize(
        'forbidden',
        ['raw_payload', 'payload_json', 'body', 'headers', 'authorization',
         'signature', 'secret', 'api_key', 'request_body', 'payload_digest'],
    )
    def test_no_raw_transport_or_credential_field_exists(self, forbidden):
        names = {f.name for f in ProviderWebhookEvent._meta.get_fields()}

        assert forbidden not in names

    def test_a_duplicate_event_identity_is_refused(self):
        make_receipt(provider_event_id='evt-dup')

        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                make_receipt(provider_event_id='evt-dup')

    def test_the_same_id_from_a_different_provider_is_allowed(self):
        make_receipt(provider_event_id='evt-shared')

        other = make_receipt(
            provider_key='other-rail', provider_event_id='evt-shared'
        )

        assert other.pk is not None

    def test_a_blank_event_id_is_refused(self):
        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                make_receipt(provider_event_id='')

    def test_a_blank_provider_key_is_refused(self):
        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                make_receipt(provider_key='')

    def test_an_invalid_processing_status_is_refused(self):
        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                make_receipt(processing_status='retrying')

    def test_there_is_no_retry_or_queue_state(self):
        assert WebhookProcessingStatus.ALL.isdisjoint({
            'retrying', 'queued', 'dead_letter', 'pending', 'invalid'
        })

    def test_only_a_failure_may_carry_a_failure_code(self):
        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                make_receipt(
                    outcome=RecoveryOutcome.SUCCEEDED,
                    failure_code='request_rejected',
                )

    def test_what_the_rail_said_cannot_be_rewritten(self):
        receipt = make_receipt(provider_event_id='evt-fixed')

        receipt.outcome = RecoveryOutcome.FAILED
        with pytest.raises(ProviderRecoveryEvidenceImmutableError):
            receipt.save()

    def test_the_processing_status_may_still_move(self):
        """Immutability guards what was reported, not our handling of it."""
        receipt = make_receipt(provider_event_id='evt-movable')

        receipt.processing_status = WebhookProcessingStatus.PROCESSED
        receipt.processed_at = timezone.now()
        receipt.save()

        receipt.refresh_from_db()
        assert receipt.processing_status == WebhookProcessingStatus.PROCESSED

    def test_it_cannot_be_deleted(self):
        receipt = make_receipt(provider_event_id='evt-permanent')

        with pytest.raises(ProviderRecoveryEvidenceImmutableError):
            receipt.delete()


class TestClientReferenceUniqueness:
    """The M7 correlation invariant, made structural rather than probabilistic."""

    def test_two_attempts_cannot_share_a_client_reference(
        self, attempt, funded_wallet, settlement_account
    ):
        second = prepare_transfer(
            funded_wallet, DESTINATION, 1_000, idempotency_key='clashing'
        )
        execute_transfer(
            second,
            provider=SimulatorTransferProvider(
                transfer_scenario=TransferScenario.KNOWN_FAILURE
            ),
            counterpart_account=settlement_account,
        )
        other = provider_attempt_for(second)

        # The M6 queryset guard refuses this first...
        from moneycore.domain.errors import ProviderAttemptImmutableError

        with pytest.raises(ProviderAttemptImmutableError):
            ProviderExecutionAttempt.objects.filter(pk=other.pk).update(
                client_reference=attempt.client_reference
            )

        # ...and past it entirely, the database refuses it too.
        from django.db import connection

        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        'UPDATE moneycore_providerexecutionattempt '
                        'SET client_reference = %s WHERE id = %s',
                        [attempt.client_reference, other.pk],
                    )

    def test_lookup_by_client_reference_is_unambiguous(self, attempt):
        found = ProviderExecutionAttempt.objects.filter(
            client_reference=attempt.client_reference
        )

        assert found.count() == 1
        assert found.get().pk == attempt.pk

    def test_m6_still_generates_them_uniquely(
        self, funded_wallet, settlement_account
    ):
        """The constraint documents what M6 already guaranteed by construction."""
        references = set()
        for index in range(3):
            transfer = prepare_transfer(
                funded_wallet, DESTINATION, 1_000,
                idempotency_key=f'unique-{index}',
            )
            execute_transfer(
                transfer,
                provider=SimulatorTransferProvider(
                    transfer_scenario=TransferScenario.KNOWN_FAILURE
                ),
                counterpart_account=settlement_account,
            )
            references.add(provider_attempt_for(transfer).client_reference)

        assert len(references) == 3


class TestAttemptImmutabilityIsUnchanged:
    def test_an_unknown_attempt_still_cannot_be_rewritten(self, attempt):
        from moneycore.domain.errors import ProviderAttemptImmutableError

        assert attempt.status == ProviderAttemptStatus.UNKNOWN

        attempt.status = ProviderAttemptStatus.SUCCEEDED
        with pytest.raises(ProviderAttemptImmutableError):
            attempt.save()

    def test_recovery_added_only_reverse_accessors_to_the_attempt(self):
        reverse = {
            f.name
            for f in ProviderExecutionAttempt._meta.get_fields()
            if f.auto_created and not f.concrete
        }

        assert reverse == {'recovery_evidence', 'webhook_events'}

    def test_the_attempt_gained_no_column(self):
        concrete = {
            f.name for f in ProviderExecutionAttempt._meta.get_fields()
            if f.concrete
        }

        assert concrete == {
            'id', 'financial_transaction', 'provider_key', 'operation',
            'status', 'client_reference', 'provider_reference',
            'failure_code', 'ambiguity_reason',
            'started_at', 'finished_at', 'created_at', 'updated_at',
        }
