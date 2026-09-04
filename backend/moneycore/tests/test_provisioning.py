"""Provisioning: explicit, idempotent, and inert toward the legacy product."""

import pytest
from django.contrib.auth.models import User

from moneycore.domain.lifecycle import AccountStatus, CustomerStatus, WalletStatus
from moneycore.models import FinancialAccount, FinancialCustomer, Wallet
from moneycore.services.provisioning import (
    get_financial_relationship,
    provision_financial_account,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def user(db):
    return User.objects.create_user('alice', password='correct-horse-battery')


class TestNoAutomaticProvisioning:
    """Nothing gives a user a financial relationship by accident."""

    def test_creating_a_user_creates_no_financial_rows(self, db):
        User.objects.create_user('newcomer')

        assert FinancialCustomer.objects.count() == 0
        assert FinancialAccount.objects.count() == 0
        assert Wallet.objects.count() == 0

    def test_registering_through_the_api_creates_no_financial_rows(self, client):
        client.post(
            '/api/register/',
            {
                'username': 'newcomer',
                'email': 'newcomer@example.com',
                'password': 'correct-horse-battery',
                'password2': 'correct-horse-battery',
            },
            content_type='application/json',
        )

        assert User.objects.filter(username='newcomer').exists()
        assert FinancialCustomer.objects.count() == 0

    def test_the_money_core_registers_no_signal_receivers_at_all(self):
        """A signal here would silently enrol every existing SpendWise user."""
        from pathlib import Path

        import moneycore

        package = Path(moneycore.__file__).parent
        offenders = [
            str(path.relative_to(package))
            for path in package.rglob('*.py')
            if 'tests' not in path.parts
            and any(
                marker in path.read_text(encoding='utf-8')
                for marker in ('@receiver', 'post_save.connect', 'pre_save.connect')
            )
        ]

        assert offenders == []

    def test_the_app_config_runs_no_ready_hook(self):
        """budgets/apps.py wires signals in ready(); moneycore must not."""
        from django.apps import apps

        config = apps.get_app_config('moneycore')

        assert type(config).ready is apps.get_app_config('expenses').__class__.ready

    def test_an_unprovisioned_user_has_no_relationship(self, user):
        assert get_financial_relationship(user) is None


class TestProvisioning:
    def test_creates_the_customer_account_and_wallet(self, user):
        result = provision_financial_account(user)

        assert result.customer.user == user
        assert result.account.customer == result.customer
        assert result.wallet.financial_account == result.account

    def test_the_initial_wallet_is_ngn(self, user):
        assert provision_financial_account(user).wallet.currency == 'NGN'

    def test_the_customer_starts_active(self, user):
        assert provision_financial_account(user).customer.status == CustomerStatus.ACTIVE

    def test_the_account_starts_pending_not_active(self, user):
        """Provisioning is not activation."""
        assert provision_financial_account(user).account.status == AccountStatus.PENDING_ACTIVATION

    def test_the_account_is_not_marked_activated(self, user):
        assert provision_financial_account(user).account.activated_at is None

    def test_the_wallet_starts_active(self, user):
        assert provision_financial_account(user).wallet.status == WalletStatus.ACTIVE

    def test_it_reports_what_it_created(self, user):
        result = provision_financial_account(user)

        assert result.customer_created
        assert result.account_created
        assert result.wallet_created
        assert result.newly_provisioned

    def test_a_non_default_currency_can_be_provisioned(self, user):
        result = provision_financial_account(user, currency='USD')

        assert result.wallet.currency == 'USD'


class TestIdempotence:
    def test_calling_twice_creates_no_duplicates(self, user):
        provision_financial_account(user)
        provision_financial_account(user)

        assert FinancialCustomer.objects.count() == 1
        assert FinancialAccount.objects.count() == 1
        assert Wallet.objects.count() == 1

    def test_calling_many_times_converges(self, user):
        for _ in range(5):
            provision_financial_account(user)

        assert FinancialCustomer.objects.count() == 1
        assert FinancialAccount.objects.count() == 1
        assert Wallet.objects.count() == 1

    def test_a_repeat_call_returns_the_same_rows(self, user):
        first = provision_financial_account(user)
        second = provision_financial_account(user)

        assert second.customer.pk == first.customer.pk
        assert second.account.pk == first.account.pk
        assert second.wallet.pk == first.wallet.pk

    def test_a_repeat_call_reports_that_it_created_nothing(self, user):
        provision_financial_account(user)

        second = provision_financial_account(user)

        assert not second.customer_created
        assert not second.account_created
        assert not second.wallet_created
        assert not second.newly_provisioned

    def test_a_repeat_call_does_not_reset_lifecycle_state(self, user):
        from moneycore.services.lifecycle import transition_account

        account = provision_financial_account(user).account
        transition_account(account, AccountStatus.ACTIVE)

        result = provision_financial_account(user)

        assert result.account.status == AccountStatus.ACTIVE

    def test_the_database_constraint_is_the_final_guard(self, user):
        """Idempotence rests on uniqueness, not on application-level locking."""
        from django.db import IntegrityError, transaction

        provision_financial_account(user)

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                FinancialCustomer.objects.create(user=user)

    def test_two_users_are_provisioned_independently(self, user):
        other = User.objects.create_user('bob')

        provision_financial_account(user)
        provision_financial_account(other)

        assert FinancialCustomer.objects.count() == 2
        assert Wallet.objects.count() == 2


class TestNoLegacySideEffects:
    def test_it_does_not_alter_the_profile(self, user):
        before = (user.profile.currency, user.profile.monthly_income)

        provision_financial_account(user)

        user.profile.refresh_from_db()
        assert (user.profile.currency, user.profile.monthly_income) == before

    def test_it_creates_no_expenses_categories_budgets_or_groups(self, user):
        from budgets.models import Budget
        from categories.models import Category
        from expenses.models import Expense
        from groups.models import Group

        provision_financial_account(user)

        assert Expense.objects.count() == 0
        assert Category.objects.count() == 0
        assert Budget.objects.count() == 0
        assert Group.objects.count() == 0

    def test_it_does_not_touch_the_users_auth_record(self, user):
        before = (user.username, user.email, user.password, user.is_active)

        provision_financial_account(user)

        user.refresh_from_db()
        assert (user.username, user.email, user.password, user.is_active) == before

    def test_no_expense_becomes_financial_history(self, user):
        """M1 introduces no transaction history at all."""
        from expenses.models import Expense
        from datetime import date
        from decimal import Decimal

        Expense.objects.create(
            user=user, amount=Decimal('10.00'), description='Lunch', date=date(2024, 1, 1)
        )

        provision_financial_account(user)

        # The expense is untouched, and nothing links it to the money core.
        expense = Expense.objects.get()
        assert expense.amount == Decimal('10.00')
        assert not any(
            'moneycore' in str(field.related_model)
            for field in Expense._meta.get_fields()
            if field.related_model is not None
        )


class TestNoExternalCalls:
    def test_provisioning_makes_no_network_request(self, user, monkeypatch):
        import socket

        def fail(*args, **kwargs):
            raise AssertionError('provisioning attempted a network connection')

        monkeypatch.setattr(socket.socket, 'connect', fail)
        monkeypatch.setattr(socket, 'create_connection', fail)

        result = provision_financial_account(user)

        assert result.wallet.currency == 'NGN'


class TestGetFinancialRelationship:
    def test_returns_the_customer_with_account_and_wallets(self, user):
        provision_financial_account(user)

        customer = get_financial_relationship(user)

        assert customer is not None
        assert customer.financial_account is not None
        assert [w.currency for w in customer.financial_account.wallets.all()] == ['NGN']

    def test_returns_none_for_a_user_without_a_relationship(self, user):
        assert get_financial_relationship(user) is None

    def test_does_not_return_another_users_relationship(self, user):
        other = User.objects.create_user('bob')
        provision_financial_account(other)

        assert get_financial_relationship(user) is None
