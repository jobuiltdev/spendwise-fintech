"""Transaction schema, intent immutability, and what must not exist."""

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from moneycore.domain.errors import (
    InvalidTransactionAmountError,
    InvalidTransactionDirectionError,
    TransactionCurrencyMismatchError,
    TransactionIntentImmutableError,
)
from moneycore.domain.ledger import MAX_AMOUNT_MINOR
from moneycore.domain.money import Money
from moneycore.domain.transactions import TransactionDirection, TransactionStatus
from moneycore.models import (
    FinancialAccount,
    FinancialTransaction,
    FundsHold,
    LedgerAccount,
    Wallet,
)
from moneycore.services.transactions import create_transaction

pytestmark = pytest.mark.django_db


def make_txn(wallet, **kwargs):
    kwargs.setdefault('direction', TransactionDirection.OUTGOING)
    kwargs.setdefault('amount_minor', 5_000)
    kwargs.setdefault('idempotency_key', 'key-1')
    return create_transaction(wallet, kwargs.pop('direction'), kwargs.pop('amount_minor'), **kwargs)


class TestSchema:
    def test_the_field_set_is_exactly_what_m4_specified(self):
        concrete = {f.name for f in FinancialTransaction._meta.get_fields() if f.concrete}

        assert concrete == {
            'id', 'wallet', 'direction', 'amount_minor', 'currency', 'status',
            'hold', 'journal', 'idempotency_key', 'failure_code',
            'processing_at', 'unknown_at', 'resolved_at',
            'created_at', 'updated_at',
        }

    def test_the_amount_is_a_64_bit_integer(self):
        from django.db import models as dj

        assert isinstance(
            FinancialTransaction._meta.get_field('amount_minor'), dj.BigIntegerField
        )

    def test_no_float_or_decimal_field_exists(self):
        from django.db import models as dj

        for field in FinancialTransaction._meta.get_fields():
            assert not isinstance(field, (dj.FloatField, dj.DecimalField))

    def test_no_provider_or_transfer_field_exists(self):
        names = {f.name for f in FinancialTransaction._meta.get_fields()}

        assert names.isdisjoint({
            'provider', 'provider_reference', 'provider_status', 'provider_id',
            'recipient', 'beneficiary', 'bank', 'bank_code', 'account_number',
            'transfer', 'transfer_id', 'webhook', 'webhook_id', 'settlement',
            'settlement_id', 'fee', 'fee_minor', 'reconciliation',
        })

    def test_no_balance_field_exists_on_any_model(self):
        forbidden = {
            'pending_balance', 'processing_balance', 'transaction_balance',
            'available_balance', 'held_balance', 'balance',
        }

        for model in (FinancialTransaction, Wallet, LedgerAccount, FinancialAccount, FundsHold):
            names = {f.name for f in model._meta.get_fields()}
            assert names.isdisjoint(forbidden)

    def test_the_amount_reads_back_as_money(self, funded_wallet):
        txn = make_txn(funded_wallet, amount_minor=2_500)

        assert txn.amount == Money(2_500, 'NGN')


class TestAmountValidation:
    @pytest.mark.parametrize('amount', [0, -1, -5_000])
    def test_a_non_positive_amount_is_refused(self, funded_wallet, amount):
        with pytest.raises(InvalidTransactionAmountError):
            make_txn(funded_wallet, amount_minor=amount)

    @pytest.mark.parametrize('amount', [100.0, 100.5, '100', None, True])
    def test_a_non_integer_amount_is_refused(self, funded_wallet, amount):
        with pytest.raises(InvalidTransactionAmountError):
            make_txn(funded_wallet, amount_minor=amount)

    def test_a_decimal_amount_is_refused(self, funded_wallet):
        from decimal import Decimal

        with pytest.raises(InvalidTransactionAmountError):
            make_txn(funded_wallet, amount_minor=Decimal('100.00'))

    def test_an_amount_beyond_the_supported_range_is_refused(self, funded_wallet):
        with pytest.raises(InvalidTransactionAmountError):
            make_txn(funded_wallet, amount_minor=MAX_AMOUNT_MINOR + 1)

    def test_a_non_positive_amount_is_refused_by_the_database(self, funded_wallet):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                FinancialTransaction.objects.create(
                    wallet=funded_wallet, direction=TransactionDirection.OUTGOING,
                    amount_minor=0, currency='NGN', idempotency_key='raw-0',
                )


class TestDirectionValidation:
    @pytest.mark.parametrize('direction', ['outgoing', 'incoming'])
    def test_both_directions_are_accepted(self, funded_wallet, direction):
        txn = make_txn(funded_wallet, direction=direction, idempotency_key=direction)

        assert txn.direction == direction

    @pytest.mark.parametrize(
        'direction',
        ['debit_transfer', 'bank_transfer', 'card_payment', 'refund', 'fee', 'cashout'],
    )
    def test_provider_shaped_directions_are_refused(self, funded_wallet, direction):
        """Business operation types are M5's concern, not this engine's."""
        with pytest.raises(InvalidTransactionDirectionError):
            make_txn(funded_wallet, direction=direction)

    def test_the_direction_set_is_exactly_two_values(self):
        assert TransactionDirection.ALL == {'outgoing', 'incoming'}

    def test_an_invalid_direction_is_refused_by_the_database(self, funded_wallet):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                FinancialTransaction.objects.create(
                    wallet=funded_wallet, direction='sideways',
                    amount_minor=100, currency='NGN', idempotency_key='raw-d',
                )


class TestCurrency:
    def test_it_defaults_to_the_wallet_currency(self, funded_wallet):
        assert make_txn(funded_wallet).currency == funded_wallet.currency

    def test_a_mismatched_currency_is_refused(self, funded_wallet):
        with pytest.raises(TransactionCurrencyMismatchError):
            make_txn(funded_wallet, currency='USD')

    def test_a_malformed_currency_is_refused_by_the_database(self, funded_wallet):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                FinancialTransaction.objects.create(
                    wallet=funded_wallet, direction=TransactionDirection.OUTGOING,
                    amount_minor=100, currency='ngn', idempotency_key='raw-c',
                )


class TestStatusConstraints:
    def test_a_new_transaction_is_created(self, funded_wallet):
        assert make_txn(funded_wallet).status == TransactionStatus.CREATED

    def test_an_invalid_status_is_refused_by_the_database(self, funded_wallet):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                FinancialTransaction.objects.create(
                    wallet=funded_wallet, direction=TransactionDirection.OUTGOING,
                    amount_minor=100, currency='NGN', idempotency_key='raw-s',
                    status='confirming',
                )

    def test_succeeded_requires_a_journal_at_the_database(self, funded_wallet):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                FinancialTransaction.objects.create(
                    wallet=funded_wallet, direction=TransactionDirection.OUTGOING,
                    amount_minor=100, currency='NGN', idempotency_key='raw-j',
                    status=TransactionStatus.SUCCEEDED, journal=None,
                    resolved_at=timezone.now(),
                )

    def test_a_non_succeeded_transaction_may_not_carry_a_journal(
        self, funded_wallet, wallet_account, counterpart_account
    ):
        from moneycore.services.ledger import credit, debit, post_journal

        journal = post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 100), credit(wallet_account, 100)],
        )

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                FinancialTransaction.objects.create(
                    wallet=funded_wallet, direction=TransactionDirection.OUTGOING,
                    amount_minor=100, currency='NGN', idempotency_key='raw-j2',
                    status=TransactionStatus.PROCESSING, journal=journal,
                )

    def test_a_resolved_transaction_must_record_when(self, funded_wallet):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                FinancialTransaction.objects.create(
                    wallet=funded_wallet, direction=TransactionDirection.OUTGOING,
                    amount_minor=100, currency='NGN', idempotency_key='raw-r',
                    status=TransactionStatus.FAILED, resolved_at=None,
                )

    def test_an_unresolved_transaction_must_not_record_a_resolution_time(
        self, funded_wallet
    ):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                FinancialTransaction.objects.create(
                    wallet=funded_wallet, direction=TransactionDirection.OUTGOING,
                    amount_minor=100, currency='NGN', idempotency_key='raw-r2',
                    status=TransactionStatus.PROCESSING, resolved_at=timezone.now(),
                )


class TestImmutableIntent:
    @pytest.mark.parametrize(
        'field,value',
        [
            ('direction', TransactionDirection.INCOMING),
            ('amount_minor', 99_999),
            ('currency', 'USD'),
            ('idempotency_key', 'rewritten'),
        ],
    )
    def test_intent_cannot_be_rewritten(self, funded_wallet, field, value):
        txn = make_txn(funded_wallet)

        setattr(txn, field, value)

        with pytest.raises(TransactionIntentImmutableError):
            txn.save()

    def test_the_wallet_cannot_be_reassigned(self, funded_wallet, wallet):
        from django.contrib.auth.models import User

        from moneycore.services.provisioning import provision_financial_account

        other = provision_financial_account(
            User.objects.create_user('intent-other')
        ).wallet
        txn = make_txn(funded_wallet)

        txn.wallet = other

        with pytest.raises(TransactionIntentImmutableError):
            txn.save()

    def test_a_refused_write_leaves_the_row_unchanged(self, funded_wallet):
        txn = make_txn(funded_wallet, amount_minor=5_000)

        txn.amount_minor = 1
        with pytest.raises(TransactionIntentImmutableError):
            txn.save()

        txn.refresh_from_db()
        assert txn.amount_minor == 5_000

    def test_a_bulk_update_touching_resolved_rows_is_refused(self, funded_wallet):
        from moneycore.services.transactions import fail_transaction

        txn = make_txn(funded_wallet)
        fail_transaction(txn, failure_code='limit_exceeded')

        with pytest.raises(TransactionIntentImmutableError):
            FinancialTransaction.objects.all().update(amount_minor=1)

    def test_status_may_still_move_through_the_service(self, funded_wallet):
        from moneycore.services.transactions import fail_transaction

        txn = make_txn(funded_wallet)

        failed = fail_transaction(txn, failure_code='limit_exceeded')

        assert failed.status == TransactionStatus.FAILED


class TestNoDeletion:
    def test_a_transaction_cannot_be_deleted(self, funded_wallet):
        txn = make_txn(funded_wallet)

        with pytest.raises(TransactionIntentImmutableError):
            txn.delete()

    def test_a_queryset_delete_is_refused(self, funded_wallet):
        make_txn(funded_wallet)

        with pytest.raises(TransactionIntentImmutableError):
            FinancialTransaction.objects.all().delete()

    def test_the_row_survives(self, funded_wallet):
        txn = make_txn(funded_wallet)

        with pytest.raises(TransactionIntentImmutableError):
            txn.delete()

        assert FinancialTransaction.objects.filter(pk=txn.pk).exists()

    def test_a_wallet_with_transactions_cannot_be_deleted(self, funded_wallet):
        from django.db.models import ProtectedError

        make_txn(funded_wallet)

        with pytest.raises(ProtectedError):
            funded_wallet.delete()


class TestHoldRelationship:
    def test_a_hold_funds_at_most_one_transaction(self, funded_wallet):
        from moneycore.services.holds import create_hold
        from moneycore.services.transactions import attach_hold

        hold = create_hold(funded_wallet, 5_000)
        first = make_txn(funded_wallet, idempotency_key='one')
        second = make_txn(funded_wallet, idempotency_key='two')
        attach_hold(first, hold)

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                FinancialTransaction.objects.filter(pk=second.pk).update(hold=hold)

    def test_a_transaction_starts_with_no_hold(self, funded_wallet):
        assert make_txn(funded_wallet).hold_id is None

    def test_a_transaction_starts_with_no_journal(self, funded_wallet):
        assert make_txn(funded_wallet).journal_id is None
