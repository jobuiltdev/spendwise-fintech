"""Run and item schemas, their constraints, and their immutability.

Both are append-only audit history. Neither is financial truth, and neither
stores a byte of raw provider transport.
"""

from datetime import timedelta

import pytest
from django.db import IntegrityError, models as dj, transaction as db_transaction
from django.utils import timezone

from moneycore.domain.errors import (
    ReconciliationItemImmutableError,
    ReconciliationRunImmutableError,
)
from moneycore.domain.ledger import LedgerAccountType
from moneycore.domain.reconciliation import (
    ReconciliationOutcome,
    ReconciliationRunStatus,
    ReconciliationStatus,
)
from moneycore.domain.transfers import VerifiedBankAccount
from moneycore.models import (
    ProviderExecutionAttempt,
    ProviderReconciliationItem,
    ProviderReconciliationRun,
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
        code='internal:m8-schema:NGN',
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
            transfer_scenario=TransferScenario.SUCCESS
        ),
        counterpart_account=settlement_account,
    )
    return provider_attempt_for(transfer)


@pytest.fixture
def run(db):
    now = timezone.now()
    return ProviderReconciliationRun.objects.create(
        provider_key='simulator',
        window_start=now - timedelta(hours=1),
        window_end=now + timedelta(hours=1),
        status=ReconciliationRunStatus.STARTED,
    )


def make_item(run, attempt=None, **overrides):
    values = {
        'reconciliation_run': run,
        'provider_attempt': attempt,
        'provider_key': 'simulator',
        'provider_record_id': 'rec-1',
        'overall_status': ReconciliationStatus.MATCHED,
        'provider_outcome': ReconciliationOutcome.SUCCEEDED,
        'internal_outcome': ReconciliationOutcome.SUCCEEDED,
        'provider_amount_minor': 7_000,
        'internal_amount_minor': 7_000,
        'provider_currency': 'NGN',
        'internal_currency': 'NGN',
    }
    values.update(overrides)
    return ProviderReconciliationItem.objects.create(**values)


class TestTheModelSet:
    def test_the_app_declares_exactly_the_models_through_m8(self):
        from django.apps import apps

        declared = {m.__name__ for m in apps.get_app_config('moneycore').get_models()}

        assert declared == {
            'FinancialCustomer', 'FinancialAccount', 'Wallet',
            'LedgerAccount', 'Journal', 'JournalEntry',
            'FundsHold', 'FinancialTransaction', 'Transfer',
            'ProviderExecutionAttempt',
            'ProviderRecoveryEvidence', 'ProviderWebhookEvent',
            'ProviderReconciliationRun', 'ProviderReconciliationItem',
        }


class TestRunSchema:
    def test_the_field_set_is_exactly_what_m8_specified(self):
        concrete = {
            f.name for f in ProviderReconciliationRun._meta.get_fields()
            if f.concrete
        }

        assert concrete == {
            'id', 'provider_key', 'window_start', 'window_end', 'status',
            'started_at', 'completed_at', 'created_at',
        }

    def test_no_persisted_counter_exists(self):
        names = {f.name for f in ProviderReconciliationRun._meta.get_fields()}

        assert names.isdisjoint({
            'matched_count', 'discrepancy_count', 'item_count',
            'total_records', 'provider_only_count',
        })

    def test_no_raw_provider_data_field_exists(self):
        names = {f.name for f in ProviderReconciliationRun._meta.get_fields()}

        assert names.isdisjoint({
            'raw_export', 'payload', 'body', 'statement', 'file', 'csv',
        })

    def test_an_invalid_status_is_refused(self, db):
        now = timezone.now()

        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                ProviderReconciliationRun.objects.create(
                    provider_key='simulator',
                    window_start=now,
                    window_end=now + timedelta(hours=1),
                    status='partial_success',
                )

    def test_a_blank_provider_key_is_refused(self, db):
        now = timezone.now()

        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                ProviderReconciliationRun.objects.create(
                    provider_key='',
                    window_start=now,
                    window_end=now + timedelta(hours=1),
                )

    def test_an_inverted_window_is_refused_by_the_database(self, db):
        now = timezone.now()

        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                ProviderReconciliationRun.objects.create(
                    provider_key='simulator',
                    window_start=now,
                    window_end=now - timedelta(hours=1),
                )

    def test_a_finished_run_must_record_when(self, db):
        now = timezone.now()

        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                ProviderReconciliationRun.objects.create(
                    provider_key='simulator',
                    window_start=now,
                    window_end=now + timedelta(hours=1),
                    status=ReconciliationRunStatus.COMPLETED,
                    completed_at=None,
                )

    def test_a_started_run_must_not_record_a_finish_time(self, db):
        now = timezone.now()

        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                ProviderReconciliationRun.objects.create(
                    provider_key='simulator',
                    window_start=now,
                    window_end=now + timedelta(hours=1),
                    status=ReconciliationRunStatus.STARTED,
                    completed_at=now,
                )


class TestRunImmutability:
    def _complete(self, run):
        run.status = ReconciliationRunStatus.COMPLETED
        run.completed_at = timezone.now()
        run.save()
        return run

    def test_a_started_run_may_still_be_completed(self, run):
        self._complete(run)

        run.refresh_from_db()
        assert run.status == ReconciliationRunStatus.COMPLETED

    def test_a_completed_run_cannot_be_reopened(self, run):
        self._complete(run)

        run.status = ReconciliationRunStatus.STARTED
        with pytest.raises(ReconciliationRunImmutableError):
            run.save()

    def test_a_failed_run_is_never_promoted(self, run):
        run.status = ReconciliationRunStatus.FAILED_INTERNAL
        run.completed_at = timezone.now()
        run.save()

        run.status = ReconciliationRunStatus.COMPLETED
        with pytest.raises(ReconciliationRunImmutableError):
            run.save()

    @pytest.mark.parametrize(
        'field', ['provider_key', 'window_start', 'window_end'],
    )
    def test_what_was_compared_cannot_be_rewritten(self, run, field):
        values = {
            'provider_key': 'other',
            'window_start': run.window_start - timedelta(days=1),
            'window_end': run.window_end + timedelta(days=1),
        }
        setattr(run, field, values[field])

        with pytest.raises(ReconciliationRunImmutableError):
            run.save()

    def test_bulk_update_of_finished_runs_is_refused(self, run):
        self._complete(run)

        with pytest.raises(ReconciliationRunImmutableError):
            ProviderReconciliationRun.objects.all().update(provider_key='other')

    def test_a_run_cannot_be_deleted(self, run):
        with pytest.raises(ReconciliationRunImmutableError):
            run.delete()

        assert ProviderReconciliationRun.objects.filter(pk=run.pk).exists()

    def test_a_queryset_delete_is_refused(self, run):
        with pytest.raises(ReconciliationRunImmutableError):
            ProviderReconciliationRun.objects.all().delete()


class TestItemSchema:
    def test_the_field_set_is_exactly_what_m8_specified(self):
        concrete = {
            f.name for f in ProviderReconciliationItem._meta.get_fields()
            if f.concrete
        }

        assert concrete == {
            'id', 'reconciliation_run', 'provider_attempt',
            'provider_key', 'provider_record_id',
            'client_reference', 'provider_reference',
            'overall_status',
            'provider_outcome', 'internal_outcome',
            'provider_amount_minor', 'internal_amount_minor',
            'provider_currency', 'internal_currency',
            'outcome_mismatch', 'amount_mismatch',
            'currency_mismatch', 'reference_mismatch',
            'has_recovery_conflict',
            'observed_at', 'created_at',
        }

    def test_amounts_are_integers(self):
        for name in ('provider_amount_minor', 'internal_amount_minor'):
            field = ProviderReconciliationItem._meta.get_field(name)
            assert isinstance(field, dj.BigIntegerField)

    def test_no_float_or_decimal_field_exists(self):
        for field in ProviderReconciliationItem._meta.get_fields():
            assert not isinstance(field, (dj.FloatField, dj.DecimalField))

    def test_mismatches_are_independent_flags(self):
        for name in (
            'outcome_mismatch', 'amount_mismatch',
            'currency_mismatch', 'reference_mismatch',
        ):
            field = ProviderReconciliationItem._meta.get_field(name)
            assert isinstance(field, dj.BooleanField)

    def test_no_json_or_text_blob_is_used_for_discrepancies(self):
        """Booleans query and constrain identically on both engines."""
        for field in ProviderReconciliationItem._meta.get_fields():
            if not field.concrete:
                continue
            assert not isinstance(field, (dj.JSONField, dj.TextField))

    @pytest.mark.parametrize(
        'forbidden',
        ['raw_payload', 'payload', 'body', 'response', 'headers',
         'provider_status', 'native_status', 'statement_line'],
    )
    def test_no_raw_provider_data_field_exists(self, forbidden):
        names = {f.name for f in ProviderReconciliationItem._meta.get_fields()}

        assert forbidden not in names

    def test_it_links_only_to_a_run_and_an_attempt(self):
        related = {
            f.related_model.__name__
            for f in ProviderReconciliationItem._meta.get_fields()
            if f.related_model is not None
        }

        assert related == {
            'ProviderReconciliationRun', 'ProviderExecutionAttempt'
        }

    def test_both_relations_are_protected(self):
        for name in ('reconciliation_run', 'provider_attempt'):
            field = ProviderReconciliationItem._meta.get_field(name)
            assert field.remote_field.on_delete is dj.PROTECT

    def test_discrepancy_codes_are_derived(self, run, attempt):
        item = make_item(
            run, attempt,
            overall_status=ReconciliationStatus.DISCREPANCY,
            amount_mismatch=True,
            currency_mismatch=True,
        )

        assert item.discrepancy_codes == (
            'amount_mismatch', 'currency_mismatch'
        )


class TestItemConstraints:
    def test_an_invalid_status_is_refused(self, run, attempt):
        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                make_item(run, attempt, overall_status='reconciled')

    def test_a_matched_item_cannot_carry_a_mismatch(self, run, attempt):
        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                make_item(
                    run, attempt,
                    overall_status=ReconciliationStatus.MATCHED,
                    amount_mismatch=True,
                )

    def test_a_discrepancy_must_carry_at_least_one_mismatch(self, run, attempt):
        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                make_item(
                    run, attempt,
                    overall_status=ReconciliationStatus.DISCREPANCY,
                )

    def test_an_internal_only_item_must_have_no_record_id(self, run, attempt):
        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                make_item(
                    run, attempt,
                    overall_status=ReconciliationStatus.INTERNAL_ONLY,
                    provider_record_id='rec-1',
                )

    def test_a_provider_only_item_must_have_no_attempt(self, run, attempt):
        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                make_item(
                    run, attempt,
                    overall_status=ReconciliationStatus.PROVIDER_ONLY,
                )

    def test_a_matched_item_must_have_both_sides(self, run):
        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                make_item(
                    run, None, overall_status=ReconciliationStatus.MATCHED
                )

    def test_one_provider_record_per_run(self, run, attempt, funded_wallet,
                                         settlement_account):
        make_item(run, attempt, provider_record_id='rec-dup')

        second = prepare_transfer(
            funded_wallet, DESTINATION, 1_000, idempotency_key='schema-2'
        )
        execute_transfer(
            second,
            provider=SimulatorTransferProvider(
                transfer_scenario=TransferScenario.KNOWN_FAILURE
            ),
            counterpart_account=settlement_account,
        )

        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                make_item(
                    run, provider_attempt_for(second),
                    provider_record_id='rec-dup',
                )

    def test_one_attempt_per_run(self, run, attempt):
        make_item(run, attempt, provider_record_id='rec-a')

        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                make_item(run, attempt, provider_record_id='rec-b')

    def test_the_same_record_may_appear_in_a_later_run(self, run, attempt, db):
        make_item(run, attempt, provider_record_id='rec-shared')
        now = timezone.now()
        later = ProviderReconciliationRun.objects.create(
            provider_key='simulator',
            window_start=now - timedelta(hours=1),
            window_end=now + timedelta(hours=1),
        )

        item = make_item(later, attempt, provider_record_id='rec-shared')

        assert item.pk is not None


class TestItemImmutability:
    @pytest.mark.parametrize(
        'field,value',
        [
            ('overall_status', ReconciliationStatus.DISCREPANCY),
            ('provider_record_id', 'rec-rewritten'),
            ('provider_amount_minor', 9_999),
            ('internal_amount_minor', 9_999),
            ('provider_currency', 'USD'),
            ('outcome_mismatch', True),
            ('has_recovery_conflict', True),
        ],
    )
    def test_a_finding_cannot_be_rewritten(self, run, attempt, field, value):
        item = make_item(run, attempt)

        setattr(item, field, value)
        with pytest.raises(ReconciliationItemImmutableError):
            item.save()

    def test_the_stored_finding_survives_a_refused_edit(self, run, attempt):
        item = make_item(run, attempt)

        item.overall_status = ReconciliationStatus.DISCREPANCY
        with pytest.raises(ReconciliationItemImmutableError):
            item.save()

        item.refresh_from_db()
        assert item.overall_status == ReconciliationStatus.MATCHED

    def test_the_run_link_cannot_be_rewritten(self, run, attempt, db):
        item = make_item(run, attempt)
        now = timezone.now()
        other = ProviderReconciliationRun.objects.create(
            provider_key='simulator',
            window_start=now,
            window_end=now + timedelta(hours=1),
        )

        item.reconciliation_run = other
        with pytest.raises(ReconciliationItemImmutableError):
            item.save()

    def test_bulk_update_is_refused(self, run, attempt):
        make_item(run, attempt)

        with pytest.raises(ReconciliationItemImmutableError):
            ProviderReconciliationItem.objects.all().update(
                overall_status=ReconciliationStatus.DISCREPANCY
            )

    def test_an_item_cannot_be_deleted(self, run, attempt):
        item = make_item(run, attempt)

        with pytest.raises(ReconciliationItemImmutableError):
            item.delete()

        assert ProviderReconciliationItem.objects.filter(pk=item.pk).exists()

    def test_a_queryset_delete_is_refused(self, run, attempt):
        make_item(run, attempt)

        with pytest.raises(ReconciliationItemImmutableError):
            ProviderReconciliationItem.objects.all().delete()


class TestReconciliationAddedNothingToFinancialModels:
    def test_the_attempt_gained_only_a_reverse_accessor(self):
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

    def test_the_reverse_accessors_are_exactly_what_m7_and_m8_added(self):
        reverse = {
            f.name
            for f in ProviderExecutionAttempt._meta.get_fields()
            if f.auto_created and not f.concrete
        }

        assert reverse == {
            'recovery_evidence', 'webhook_events', 'reconciliation_items'
        }

    def test_no_financial_model_stores_a_reconciliation_status(self):
        from moneycore.models import (
            FinancialTransaction, FundsHold, Journal, Transfer, Wallet,
        )

        for model in (
            Wallet, FundsHold, FinancialTransaction, Journal, Transfer
        ):
            names = {f.name for f in model._meta.get_fields()}
            assert names.isdisjoint({
                'reconciled', 'reconciliation_status', 'reconciled_at',
                'last_reconciled', 'matched',
            }), model.__name__
