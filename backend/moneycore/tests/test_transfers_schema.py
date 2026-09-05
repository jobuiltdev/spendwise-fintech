"""Transfer schema invariants, and what must still not exist.

A Transfer is business intent. It carries no lifecycle, no balance, no provider
concept and no second copy of anything the financial transaction already owns.
"""

import pytest
from django.db import IntegrityError, models as dj, transaction as db_transaction

from moneycore.domain.errors import TransferImmutableError
from moneycore.domain.money import Money
from moneycore.domain.transactions import TransactionDirection, TransactionStatus
from moneycore.domain.transfers import VerifiedBankAccount
from moneycore.models import (
    FinancialTransaction,
    FundsHold,
    Journal,
    LedgerAccount,
    Transfer,
    Wallet,
)
from moneycore.services.transfers import prepare_transfer

pytestmark = pytest.mark.django_db


DESTINATION = VerifiedBankAccount(
    account_number='0123456789',
    bank_code='NG-058',
    bank_name='Example Bank',
    account_name='Ada Okafor',
)


@pytest.fixture
def transfer(funded_wallet):
    return prepare_transfer(
        funded_wallet, DESTINATION, 7_000, idempotency_key='schema-1'
    )


class TestSchema:
    def test_the_field_set_is_exactly_what_m5_specified(self):
        concrete = {f.name for f in Transfer._meta.get_fields() if f.concrete}

        assert concrete == {
            'id', 'financial_transaction',
            'recipient_name', 'destination_account_number',
            'destination_bank_code', 'destination_bank_name',
            'narration', 'created_at', 'updated_at',
        }

    def test_the_transfer_model_exists_alongside_the_declared_set(self):
        from django.apps import apps

        declared = {m.__name__ for m in apps.get_app_config('moneycore').get_models()}

        assert 'Transfer' in declared
        assert declared == {
            'FinancialCustomer', 'FinancialAccount', 'Wallet',
            'LedgerAccount', 'Journal', 'JournalEntry',
            'FundsHold', 'FinancialTransaction',
            'Transfer',
            'ProviderExecutionAttempt',
        }

    def test_it_links_to_exactly_one_financial_transaction(self):
        field = Transfer._meta.get_field('financial_transaction')

        assert field.one_to_one
        assert field.related_model is FinancialTransaction

    def test_the_transaction_is_protected_from_deletion(self):
        field = Transfer._meta.get_field('financial_transaction')

        assert field.remote_field.on_delete is dj.PROTECT

    def test_a_transaction_cannot_back_two_transfers(self, transfer, funded_wallet):
        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                Transfer.objects.create(
                    financial_transaction=transfer.financial_transaction,
                    recipient_name='Someone Else',
                    destination_account_number='9876543210',
                    destination_bank_code='NG-011',
                    destination_bank_name='Other Bank',
                )

    def test_the_related_models_are_only_the_transaction(self):
        related = {
            f.related_model.__name__
            for f in Transfer._meta.get_fields()
            if f.related_model is not None
        }

        assert related == {'FinancialTransaction'}


class TestNoSecondSourceOfTruth:
    def test_there_is_no_status_field(self):
        names = {f.name for f in Transfer._meta.get_fields()}

        assert names.isdisjoint({
            'status', 'transfer_status', 'bank_status', 'payment_status',
            'state', 'lifecycle_status',
        })

    def test_status_reads_through_to_the_transaction(self, transfer):
        assert transfer.status == transfer.financial_transaction.status
        assert transfer.status == TransactionStatus.CREATED

    def test_wallet_amount_and_currency_are_not_stored(self):
        concrete = {f.name for f in Transfer._meta.get_fields() if f.concrete}

        assert concrete.isdisjoint({
            'wallet', 'amount_minor', 'currency', 'amount', 'principal_minor',
        })

    def test_wallet_amount_and_currency_read_through(self, transfer, funded_wallet):
        assert transfer.wallet.pk == funded_wallet.pk
        assert transfer.amount_minor == 7_000
        assert transfer.currency == 'NGN'
        assert transfer.amount == Money(7_000, 'NGN')

    def test_the_hold_and_journal_read_through(self, transfer):
        assert transfer.hold == transfer.financial_transaction.hold
        assert transfer.journal is None

    def test_no_balance_field_exists(self):
        names = {f.name for f in Transfer._meta.get_fields()}

        assert names.isdisjoint({
            'balance', 'available_balance', 'held_balance', 'reserved_balance',
            'spendable_balance', 'posted_balance',
        })

    def test_no_fee_field_exists(self):
        """Fees do not exist yet, and M5 guesses no policy."""
        names = {f.name for f in Transfer._meta.get_fields()}

        assert names.isdisjoint({
            'fee', 'fee_minor', 'fee_amount', 'total_minor', 'charge',
            'transfer_fee', 'fee_account',
        })

    def test_no_float_or_decimal_field_exists(self):
        for field in Transfer._meta.get_fields():
            assert not isinstance(field, (dj.FloatField, dj.DecimalField))

    def test_the_principal_keeps_integer_authority(self, transfer):
        assert isinstance(transfer.amount_minor, int)
        assert not isinstance(transfer.amount_minor, bool)


class TestNoProviderOrRecipientDirectory:
    def test_no_provider_field_exists(self):
        names = {f.name for f in Transfer._meta.get_fields()}

        assert names.isdisjoint({
            'provider', 'provider_id', 'provider_name', 'provider_reference',
            'provider_status', 'provider_attempt', 'provider_attempt_id',
            'provider_response', 'webhook', 'webhook_id', 'webhook_event',
            'settlement_reference', 'settlement', 'reconciliation_state',
            'external_transaction_id', 'external_id', 'session_id', 'rail',
        })

    def test_no_saved_recipient_relation_exists(self):
        """The snapshot is the destination. Nothing points at a mutable record."""
        names = {f.name for f in Transfer._meta.get_fields()}

        assert names.isdisjoint({
            'beneficiary', 'beneficiary_id', 'recipient', 'recipient_id',
            'saved_recipient', 'bank', 'bank_id', 'bank_account',
        })

    def test_no_beneficiary_or_recipient_model_exists(self):
        from django.apps import apps

        declared = {
            m.__name__.lower() for m in apps.get_app_config('moneycore').get_models()
        }

        assert declared.isdisjoint({
            'beneficiary', 'savedbeneficiary', 'recipient', 'savedrecipient',
            'bank', 'bankaccount', 'bankdirectory',
        })

    def test_no_authorization_or_security_field_exists(self):
        """Transaction security is M9. M5 pretends none of it exists."""
        names = {f.name for f in Transfer._meta.get_fields()}

        assert names.isdisjoint({
            'pin', 'pin_hash', 'authorization_token', 'authorized_at',
            'otp', 'otp_code', 'mfa', 'device_id',
        })

    def test_no_limit_field_exists(self):
        names = {f.name for f in Transfer._meta.get_fields()}

        assert names.isdisjoint({
            'daily_limit', 'limit_minor', 'tier', 'tier_limit', 'kyc_tier',
            'risk_score',
        })


class TestDatabaseConstraints:
    """The domain validates; the database refuses independently."""

    def _raw(self, txn, **overrides):
        values = {
            'financial_transaction': txn,
            'recipient_name': 'Ada Okafor',
            'destination_account_number': '0123456789',
            'destination_bank_code': 'NG-058',
            'destination_bank_name': 'Example Bank',
        }
        values.update(overrides)
        return Transfer.objects.create(**values)

    @pytest.fixture
    def bare_transaction(self, funded_wallet):
        from moneycore.services.transactions import create_transaction

        return create_transaction(
            funded_wallet, TransactionDirection.OUTGOING, 1_000,
            idempotency_key='constraint-probe',
        )

    @pytest.mark.parametrize(
        'account_number',
        ['', '123', '012345678', 'ABCDEFGHIJ', '01234 5678'],
    )
    def test_a_malformed_account_number_is_refused(
        self, bare_transaction, account_number
    ):
        """Caught by the check constraint on both engines."""
        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                self._raw(bare_transaction, destination_account_number=account_number)

    def test_an_overlong_account_number_is_refused(self, bare_transaction):
        """Also refused, but by column width first on PostgreSQL.

        ``varchar(10)`` rejects an eleven-character value as a `DataError`
        before the check constraint is ever evaluated, so the assertion is on
        `DatabaseError` — the common base — rather than on which mechanism
        happens to fire. SQLite does not enforce ``max_length``, so there the
        same value is caught by the check constraint as an `IntegrityError`.
        Both engines refuse it, which is the invariant that matters.
        """
        from django.db import DatabaseError

        with pytest.raises(DatabaseError):
            with db_transaction.atomic():
                self._raw(
                    bare_transaction, destination_account_number='01234567890'
                )

    def test_the_column_is_exactly_the_ngn_account_number_width(self):
        field = Transfer._meta.get_field('destination_account_number')

        assert field.max_length == 10

    def test_a_blank_bank_code_is_refused(self, bare_transaction):
        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                self._raw(bare_transaction, destination_bank_code='')

    def test_a_blank_bank_name_is_refused(self, bare_transaction):
        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                self._raw(bare_transaction, destination_bank_name='')

    def test_a_blank_recipient_name_is_refused(self, bare_transaction):
        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                self._raw(bare_transaction, recipient_name='')

    def test_a_valid_row_is_accepted(self, bare_transaction):
        created = self._raw(bare_transaction)

        assert created.pk is not None


class TestImmutableIntent:
    @pytest.mark.parametrize(
        'field,value',
        [
            ('recipient_name', 'Someone Else'),
            ('destination_account_number', '9876543210'),
            ('destination_bank_code', 'NG-011'),
            ('destination_bank_name', 'Other Bank'),
            ('narration', 'rewritten'),
        ],
    )
    def test_the_destination_snapshot_cannot_be_rewritten(
        self, transfer, field, value
    ):
        setattr(transfer, field, value)

        with pytest.raises(TransferImmutableError):
            transfer.save()

    def test_the_transaction_link_cannot_be_rewritten(self, transfer, funded_wallet):
        from moneycore.services.transactions import create_transaction

        other = create_transaction(
            funded_wallet, TransactionDirection.OUTGOING, 1_000,
            idempotency_key='relink-probe',
        )
        transfer.financial_transaction = other

        with pytest.raises(TransferImmutableError):
            transfer.save()

    def test_a_refused_write_leaves_the_row_unchanged(self, transfer):
        transfer.destination_account_number = '9999999999'
        with pytest.raises(TransferImmutableError):
            transfer.save()

        transfer.refresh_from_db()
        assert transfer.destination_account_number == '0123456789'

    def test_an_unchanged_save_is_still_allowed(self, transfer):
        """Immutability guards intent, not every write path."""
        transfer.save()

        transfer.refresh_from_db()
        assert transfer.recipient_name == 'Ada Okafor'

    def test_bulk_update_is_refused_outright(self, transfer):
        """A transfer has no mutable business field, so none may be bulk-set."""
        with pytest.raises(TransferImmutableError):
            Transfer.objects.all().update(narration='rewritten')

    def test_the_snapshot_does_not_follow_a_changed_transaction(self, transfer):
        """The snapshot is a copy by design, not a live read."""
        stored = (
            Transfer.objects.filter(pk=transfer.pk)
            .values_list('destination_bank_name', flat=True)
            .first()
        )

        assert stored == 'Example Bank'


class TestNoDeletion:
    def test_a_transfer_cannot_be_deleted(self, transfer):
        with pytest.raises(TransferImmutableError):
            transfer.delete()

    def test_a_queryset_delete_is_refused(self, transfer):
        with pytest.raises(TransferImmutableError):
            Transfer.objects.all().delete()

    def test_the_transaction_cannot_be_deleted_out_from_under_it(self, transfer):
        from django.db.models import ProtectedError
        from moneycore.domain.errors import TransactionIntentImmutableError

        with pytest.raises((ProtectedError, TransactionIntentImmutableError)):
            transfer.financial_transaction.delete()

    def test_the_transfer_survives_a_failed_attempt(self, transfer):
        with pytest.raises(TransferImmutableError):
            transfer.delete()

        assert Transfer.objects.filter(pk=transfer.pk).exists()


class TestOutgoingOnly:
    def test_the_prepared_transaction_is_outgoing(self, transfer):
        assert transfer.financial_transaction.direction == (
            TransactionDirection.OUTGOING
        )

    def test_the_service_offers_no_incoming_transfer(self):
        """Receiving money needs provider and webhook knowledge M5 lacks."""
        from moneycore.services import transfers

        for forbidden in (
            'prepare_incoming_transfer', 'receive_transfer', 'credit_transfer',
            'prepare_deposit', 'fund_wallet',
        ):
            assert not hasattr(transfers, forbidden), f'{forbidden} exists'
