"""Schema-level guarantees for the M1 financial entities.

The most important test in this file is the one asserting Wallet has no balance
field. That is the boundary M2 depends on: if a balance column ever appears
here, the ledger stops being the single source of truth for what money exists.
"""

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction

from moneycore.domain.lifecycle import AccountStatus, CustomerStatus, WalletStatus
from moneycore.models import FinancialAccount, FinancialCustomer, Wallet

pytestmark = pytest.mark.django_db


@pytest.fixture
def customer(db):
    return FinancialCustomer.objects.create(user=User.objects.create_user('alice'))


@pytest.fixture
def account(customer):
    return FinancialAccount.objects.create(customer=customer)


class TestWalletHoldsNoBalance:
    """M1 wallets are containers. The ledger (M2) owns what money exists."""

    FORBIDDEN_FIELDS = [
        'balance',
        'available_balance',
        'ledger_balance',
        'spendable_balance',
        'pending_balance',
        'reserved_balance',
        'held_balance',
        'total_balance',
        'amount',
        'minor_units',
    ]

    def test_no_balance_field_exists(self):
        field_names = {field.name for field in Wallet._meta.get_fields()}

        assert field_names.isdisjoint(self.FORBIDDEN_FIELDS)

    def test_the_whole_field_set_is_exactly_what_m1_specified(self):
        concrete = {
            field.name for field in Wallet._meta.get_fields() if field.concrete
        }

        assert concrete == {
            'id', 'financial_account', 'currency', 'status', 'created_at', 'updated_at'
        }

    def test_no_numeric_money_column_of_any_name(self):
        """A DecimalField or IntegerField here would be a balance by another name."""
        from django.db import models

        numeric = [
            field.name
            for field in Wallet._meta.get_fields()
            if isinstance(field, (models.DecimalField, models.FloatField))
            or (isinstance(field, models.IntegerField) and field.name != 'id')
        ]

        assert numeric == []

    def test_the_wallet_exposes_no_money_mutating_helpers(self):
        for forbidden in ('credit', 'debit', 'deposit', 'withdraw', 'adjust_balance'):
            assert not hasattr(Wallet, forbidden)

    def test_no_other_m1_model_carries_a_balance_either(self):
        for model in (FinancialCustomer, FinancialAccount):
            names = {field.name for field in model._meta.get_fields()}
            assert names.isdisjoint(self.FORBIDDEN_FIELDS)


class TestNoProviderOrAccountNumberFields:
    """Provider integration is M6; a fabricated account number is never allowed."""

    FORBIDDEN_FIELDS = [
        'provider', 'provider_id', 'provider_customer_id', 'provider_wallet_id',
        'provider_reference', 'bank_account_number', 'account_number',
        'bank_code', 'virtual_account_number', 'nuban', 'iban', 'external_id',
    ]

    @pytest.mark.parametrize('model', [FinancialCustomer, FinancialAccount, Wallet])
    def test_the_model_has_no_provider_or_account_number_field(self, model):
        names = {field.name for field in model._meta.get_fields()}

        assert names.isdisjoint(self.FORBIDDEN_FIELDS)


class TestFinancialCustomer:
    def test_a_user_may_have_at_most_one_financial_customer(self, customer):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                FinancialCustomer.objects.create(user=customer.user)

    def test_it_defaults_to_active(self, customer):
        assert customer.status == CustomerStatus.ACTIVE

    def test_it_records_timestamps(self, customer):
        assert customer.created_at is not None
        assert customer.updated_at is not None

    def test_it_does_not_duplicate_identity_data(self):
        """Email, phone and names stay owned by the accounts/profile domain."""
        names = {field.name for field in FinancialCustomer._meta.get_fields()}

        assert names.isdisjoint(
            {'email', 'phone', 'phone_number', 'first_name', 'last_name', 'full_name'}
        )

    def test_an_invalid_status_is_rejected_by_the_database(self, customer):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                FinancialCustomer.objects.filter(pk=customer.pk).update(status='bogus')

    def test_deleting_the_user_removes_the_financial_customer(self, customer):
        customer.user.delete()

        assert not FinancialCustomer.objects.filter(pk=customer.pk).exists()


class TestFinancialAccount:
    def test_a_customer_may_have_at_most_one_account(self, account):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                FinancialAccount.objects.create(customer=account.customer)

    def test_a_new_account_is_pending_not_active(self, account):
        """Provisioned is not the same as usable."""
        assert account.status == AccountStatus.PENDING_ACTIVATION

    def test_lifecycle_timestamps_start_empty(self, account):
        assert account.activated_at is None
        assert account.closed_at is None

    def test_an_invalid_status_is_rejected_by_the_database(self, account):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                FinancialAccount.objects.filter(pk=account.pk).update(status='frozen')

    def test_deleting_the_customer_removes_the_account(self, account):
        account.customer.delete()

        assert not FinancialAccount.objects.filter(pk=account.pk).exists()


class TestWallet:
    def test_one_wallet_per_currency_per_account(self, account):
        Wallet.objects.create(financial_account=account, currency='NGN')

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Wallet.objects.create(financial_account=account, currency='NGN')

    def test_the_same_currency_is_allowed_on_a_different_account(self, account):
        other = FinancialAccount.objects.create(
            customer=FinancialCustomer.objects.create(
                user=User.objects.create_user('bob')
            )
        )
        Wallet.objects.create(financial_account=account, currency='NGN')

        Wallet.objects.create(financial_account=other, currency='NGN')

        assert Wallet.objects.filter(currency='NGN').count() == 2

    def test_an_account_may_hold_several_currencies(self, account):
        Wallet.objects.create(financial_account=account, currency='NGN')
        Wallet.objects.create(financial_account=account, currency='USD')

        assert account.wallets.count() == 2

    def test_a_new_wallet_is_active(self, account):
        wallet = Wallet.objects.create(financial_account=account, currency='NGN')

        assert wallet.status == WalletStatus.ACTIVE

    @pytest.mark.parametrize('currency', ['ngn', 'Ngn', 'NG', 'N1G', '123'])
    def test_a_malformed_currency_is_rejected_by_the_database(self, account, currency):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Wallet.objects.create(financial_account=account, currency=currency)

    @pytest.mark.parametrize('currency', ['ngn', 'NG', 'N1G'])
    def test_a_malformed_currency_is_also_rejected_by_validation(self, account, currency):
        wallet = Wallet(financial_account=account, currency=currency)

        with pytest.raises(DjangoValidationError):
            wallet.full_clean(exclude=['financial_account'])

    def test_currency_validation_matches_the_money_value_type(self, account):
        """One rule, whether the code is in memory or in a column."""
        from moneycore.domain.money import Money

        for code in ('NGN', 'USD', 'EUR'):
            assert Money(1, code).currency == code
            Wallet(financial_account=account, currency=code).full_clean(
                exclude=['financial_account']
            )

    def test_an_invalid_status_is_rejected_by_the_database(self, account):
        wallet = Wallet.objects.create(financial_account=account, currency='NGN')

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Wallet.objects.filter(pk=wallet.pk).update(status='suspended')

    def test_deleting_the_account_removes_its_wallets(self, account):
        wallet = Wallet.objects.create(financial_account=account, currency='NGN')

        account.delete()

        assert not Wallet.objects.filter(pk=wallet.pk).exists()
