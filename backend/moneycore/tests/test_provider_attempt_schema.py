"""ProviderExecutionAttempt schema, and what must never reach it.

An attempt records one interaction. It stores no raw payload, no HTTP detail
and no credential — what crosses the boundary is already normalised.
"""

import pytest
from django.db import IntegrityError, models as dj, transaction as db_transaction
from django.utils import timezone

from moneycore.domain.errors import ProviderAttemptImmutableError
from moneycore.domain.ledger import LedgerAccountType
from moneycore.domain.providers import (
    CLIENT_REFERENCE_MAX_LENGTH,
    PROVIDER_REFERENCE_MAX_LENGTH,
    ProviderAttemptStatus,
    ProviderOperation,
)
from moneycore.domain.transfers import VerifiedBankAccount
from moneycore.models import (
    FinancialTransaction,
    ProviderExecutionAttempt,
    Transfer,
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
        code='internal:m6-schema-counterpart:NGN',
        name='Internal counterpart',
        account_type=LedgerAccountType.ASSET,
        currency='NGN',
    )


@pytest.fixture
def prepared(funded_wallet):
    return prepare_transfer(
        funded_wallet, DESTINATION, 7_000, idempotency_key='schema-1'
    )


@pytest.fixture
def succeeded(prepared, settlement_account):
    transfer = execute_transfer(
        prepared,
        provider=SimulatorTransferProvider(
            transfer_scenario=TransferScenario.SUCCESS
        ),
        counterpart_account=settlement_account,
    )
    return provider_attempt_for(transfer)


class TestSchema:
    def test_the_field_set_is_exactly_what_m6_specified(self):
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

    def test_it_links_to_the_financial_transaction(self):
        field = ProviderExecutionAttempt._meta.get_field('financial_transaction')

        assert field.related_model is FinancialTransaction
        assert field.remote_field.on_delete is dj.PROTECT

    def test_it_does_not_link_to_the_transfer(self):
        """The transaction is the financial lifecycle; the transfer is intent.

        M7 added reverse accessors for recovery evidence and webhook receipts —
        both columns live on *those* models, so the attempt still points only
        at the transaction.
        """
        concrete_related = {
            f.related_model.__name__
            for f in ProviderExecutionAttempt._meta.concrete_fields
            if f.related_model is not None
        }

        assert concrete_related == {'FinancialTransaction'}

        related = {
            f.related_model.__name__
            for f in ProviderExecutionAttempt._meta.get_fields()
            if f.related_model is not None
        }
        assert related == {
            'FinancialTransaction',
            'ProviderRecoveryEvidence',
            'ProviderWebhookEvent',
        }
        assert 'Transfer' not in related

    def test_the_provider_reference_is_nullable(self):
        field = ProviderExecutionAttempt._meta.get_field('provider_reference')

        assert field.blank
        assert field.max_length == PROVIDER_REFERENCE_MAX_LENGTH

    def test_the_failure_code_is_nullable_and_bounded(self):
        field = ProviderExecutionAttempt._meta.get_field('failure_code')

        assert field.blank
        assert field.max_length <= 64

    def test_the_client_reference_is_bounded(self):
        field = ProviderExecutionAttempt._meta.get_field('client_reference')

        assert field.max_length == CLIENT_REFERENCE_MAX_LENGTH

    def test_no_attempt_number_was_invented(self):
        """M6 has no retry, so a sequence number would describe nothing."""
        names = {f.name for f in ProviderExecutionAttempt._meta.get_fields()}

        assert 'attempt_number' not in names


class TestNothingRawIsStored:
    @pytest.mark.parametrize(
        'forbidden',
        ['raw_response', 'response_body', 'request_body', 'payload',
         'headers', 'http_status', 'status_code', 'url', 'endpoint',
         'api_key', 'secret', 'token', 'credentials', 'signature',
         'stack_trace', 'traceback', 'error_message', 'provider_message'],
    )
    def test_no_transport_or_credential_field_exists(self, forbidden):
        names = {f.name for f in ProviderExecutionAttempt._meta.get_fields()}

        assert forbidden not in names

    def test_no_balance_field_exists(self):
        names = {f.name for f in ProviderExecutionAttempt._meta.get_fields()}

        assert names.isdisjoint({
            'balance', 'amount', 'amount_minor', 'available_balance',
            'held_balance', 'fee', 'fee_minor',
        })

    def test_no_destination_is_duplicated_here(self):
        """Transfer already owns immutable destination intent."""
        names = {f.name for f in ProviderExecutionAttempt._meta.get_fields()}

        assert names.isdisjoint({
            'destination_account_number', 'destination_bank_code',
            'recipient_name', 'account_number', 'bank_code', 'narration',
        })

    def test_no_vendor_specific_field_name_exists(self):
        names = {f.name for f in ProviderExecutionAttempt._meta.get_fields()}

        assert names.isdisjoint({
            'paystack_reference', 'flutterwave_id', 'session_id',
            'nip_session_id', 'transfer_code', 'recipient_code',
        })

    def test_no_float_or_decimal_field_exists(self):
        for field in ProviderExecutionAttempt._meta.get_fields():
            assert not isinstance(field, (dj.FloatField, dj.DecimalField))


class TestDatabaseConstraints:
    def _raw(self, txn, **overrides):
        values = {
            'financial_transaction': txn,
            'provider_key': 'simulator',
            'operation': ProviderOperation.SUBMIT_TRANSFER,
            'status': ProviderAttemptStatus.STARTED,
            'client_reference': 'SW-abc',
        }
        values.update(overrides)
        return ProviderExecutionAttempt.objects.create(**values)

    @pytest.fixture
    def txn(self, prepared):
        return prepared.financial_transaction

    def test_a_valid_row_is_accepted(self, txn):
        assert self._raw(txn).pk is not None

    def test_an_invalid_status_is_refused(self, txn):
        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                self._raw(txn, status='pending')

    def test_an_invalid_operation_is_refused(self, txn):
        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                self._raw(txn, operation='reconcile')

    def test_a_blank_provider_key_is_refused(self, txn):
        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                self._raw(txn, provider_key='')

    def test_a_blank_client_reference_is_refused(self, txn):
        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                self._raw(txn, client_reference='')

    def test_two_attempts_for_one_transaction_are_refused(self, txn):
        """The database-level guarantee that a transfer cannot be sent twice."""
        self._raw(txn)

        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                self._raw(txn)

    def test_a_finished_attempt_must_record_when(self, txn):
        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                self._raw(
                    txn, status=ProviderAttemptStatus.SUCCEEDED, finished_at=None
                )

    def test_an_unfinished_attempt_must_not_record_a_finish_time(self, txn):
        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                self._raw(
                    txn,
                    status=ProviderAttemptStatus.STARTED,
                    finished_at=timezone.now(),
                )

    @pytest.mark.parametrize(
        'status',
        [ProviderAttemptStatus.STARTED, ProviderAttemptStatus.SUCCEEDED,
         ProviderAttemptStatus.UNKNOWN],
    )
    def test_only_a_failure_may_carry_a_failure_code(self, txn, status):
        finished_at = (
            None if status == ProviderAttemptStatus.STARTED else timezone.now()
        )

        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                self._raw(
                    txn, status=status, finished_at=finished_at,
                    failure_code='request_rejected',
                )

    @pytest.mark.parametrize(
        'status',
        [ProviderAttemptStatus.STARTED, ProviderAttemptStatus.SUCCEEDED,
         ProviderAttemptStatus.FAILED],
    )
    def test_only_an_ambiguous_outcome_may_carry_an_ambiguity_reason(
        self, txn, status
    ):
        finished_at = (
            None if status == ProviderAttemptStatus.STARTED else timezone.now()
        )
        extra = (
            {'failure_code': 'request_rejected'}
            if status == ProviderAttemptStatus.FAILED else {}
        )

        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                self._raw(
                    txn, status=status, finished_at=finished_at,
                    ambiguity_reason='response_not_received', **extra,
                )

    def test_a_transaction_with_an_attempt_cannot_be_deleted(self, txn):
        from django.db.models import ProtectedError

        from moneycore.domain.errors import TransactionIntentImmutableError

        self._raw(txn)

        with pytest.raises((ProtectedError, TransactionIntentImmutableError)):
            txn.delete()


class TestImmutability:
    def test_a_finished_attempt_cannot_be_reopened(self, succeeded):
        succeeded.status = ProviderAttemptStatus.FAILED
        succeeded.failure_code = 'request_rejected'

        with pytest.raises(ProviderAttemptImmutableError):
            succeeded.save()

    def test_the_stored_row_survives_a_refused_edit(self, succeeded):
        succeeded.status = ProviderAttemptStatus.FAILED

        with pytest.raises(ProviderAttemptImmutableError):
            succeeded.save()

        succeeded.refresh_from_db()
        assert succeeded.status == ProviderAttemptStatus.SUCCEEDED

    @pytest.mark.parametrize(
        'field,value',
        [
            ('provider_key', 'other'),
            ('client_reference', 'SW-rewritten'),
        ],
    )
    def test_the_claim_cannot_be_rewritten(self, prepared, field, value):
        attempt = ProviderExecutionAttempt.objects.create(
            financial_transaction=prepared.financial_transaction,
            provider_key='simulator',
            operation=ProviderOperation.SUBMIT_TRANSFER,
            status=ProviderAttemptStatus.STARTED,
            client_reference='SW-abc',
        )
        setattr(attempt, field, value)
        with pytest.raises(ProviderAttemptImmutableError):
            attempt.save()

    def test_the_transaction_link_cannot_be_rewritten(
        self, prepared, funded_wallet
    ):
        from moneycore.services.transactions import create_transaction
        from moneycore.domain.transactions import TransactionDirection

        attempt = ProviderExecutionAttempt.objects.create(
            financial_transaction=prepared.financial_transaction,
            provider_key='simulator',
            operation=ProviderOperation.SUBMIT_TRANSFER,
            status=ProviderAttemptStatus.STARTED,
            client_reference='SW-abc',
        )
        other = create_transaction(
            funded_wallet, TransactionDirection.OUTGOING, 1_000,
            idempotency_key='relink',
        )

        attempt.financial_transaction = other
        with pytest.raises(ProviderAttemptImmutableError):
            attempt.save()

    def test_an_unfinished_attempt_may_still_be_finalised(self, prepared):
        """Immutability guards the observation, not the act of recording it."""
        attempt = ProviderExecutionAttempt.objects.create(
            financial_transaction=prepared.financial_transaction,
            provider_key='simulator',
            operation=ProviderOperation.SUBMIT_TRANSFER,
            status=ProviderAttemptStatus.STARTED,
            client_reference='SW-abc',
        )

        attempt.status = ProviderAttemptStatus.SUCCEEDED
        attempt.finished_at = timezone.now()
        attempt.save()

        attempt.refresh_from_db()
        assert attempt.status == ProviderAttemptStatus.SUCCEEDED

    def test_bulk_update_of_finished_rows_is_refused(self, succeeded):
        with pytest.raises(ProviderAttemptImmutableError):
            ProviderExecutionAttempt.objects.all().update(provider_reference='x')


class TestNoDeletion:
    def test_an_attempt_cannot_be_deleted(self, succeeded):
        with pytest.raises(ProviderAttemptImmutableError):
            succeeded.delete()

    def test_a_queryset_delete_is_refused(self, succeeded):
        with pytest.raises(ProviderAttemptImmutableError):
            ProviderExecutionAttempt.objects.all().delete()

    def test_it_survives_a_refused_delete(self, succeeded):
        with pytest.raises(ProviderAttemptImmutableError):
            succeeded.delete()

        assert ProviderExecutionAttempt.objects.filter(pk=succeeded.pk).exists()


class TestAttemptSemantics:
    def test_a_started_attempt_may_have_been_submitted(self, prepared):
        """The conservative reading, and the only safe one."""
        attempt = ProviderExecutionAttempt.objects.create(
            financial_transaction=prepared.financial_transaction,
            provider_key='simulator',
            operation=ProviderOperation.SUBMIT_TRANSFER,
            status=ProviderAttemptStatus.STARTED,
            client_reference='SW-abc',
        )

        assert attempt.may_have_been_submitted
        assert not attempt.is_finished

    def test_a_failed_attempt_did_not_move_money(self, prepared, settlement_account):
        transfer = execute_transfer(
            prepared,
            provider=SimulatorTransferProvider(
                transfer_scenario=TransferScenario.KNOWN_FAILURE
            ),
            counterpart_account=settlement_account,
        )

        assert not provider_attempt_for(transfer).may_have_been_submitted

    def test_a_succeeded_attempt_is_finished(self, succeeded):
        assert succeeded.is_finished
        assert succeeded.may_have_been_submitted
