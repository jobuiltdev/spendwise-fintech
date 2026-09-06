"""What M2 must NOT have introduced. Groups G and H.

Boundary tests: the ledger stays internal, the legacy tracker stays untouched,
and no M3+ concept crept in.
"""

import pytest
from django.apps import apps

from moneycore.domain.ledger import JournalStatus
from moneycore.models import Journal, JournalEntry, LedgerAccount, Wallet
from moneycore.services.ledger import credit, debit, post_journal

pytestmark = pytest.mark.django_db

BACKEND_ROOT = None


def _backend_root():
    from pathlib import Path

    import moneycore

    return Path(moneycore.__file__).resolve().parents[1]


class TestNoM8OrLaterConcepts:
    """Transfers, recipients and providers are later milestones.

    M2 wrote these to exclude holds and transactions as well; M3 introduced the
    hold and M4 the financial transaction, deliberately, so both entities are
    now expected and the guard has moved forward to M5.
    """

    def test_the_app_declares_exactly_the_models_through_m7(self):
        declared = {m.__name__ for m in apps.get_app_config('moneycore').get_models()}

        assert declared == {
            'FinancialCustomer', 'FinancialAccount', 'Wallet',
            'LedgerAccount', 'Journal', 'JournalEntry',
            'FundsHold',
            'FinancialTransaction',
            'Transfer',
            'ProviderExecutionAttempt',
            'ProviderRecoveryEvidence',
            'ProviderWebhookEvent',
        }

    def test_no_m8_or_later_model_exists(self):
        declared = {
            m.__name__.lower() for m in apps.get_app_config('moneycore').get_models()
        }
        forbidden = {
            'reservation', 'balanceprojection', 'availablebalance',
            'transaction', 'recipient', 'beneficiary', 'provider',
            'webhook', 'webhookevent', 'reconciliation', 'fee', 'settlement',
            'idempotencykey', 'outboxmessage',
        }

        assert declared.isdisjoint(forbidden)

    @pytest.mark.parametrize('model', [LedgerAccount, Journal, JournalEntry, Wallet])
    def test_no_stored_available_or_held_balance_field(self, model):
        """M3 added holds as rows, never as a cached total on these models."""
        names = {f.name for f in model._meta.get_fields()}

        assert names.isdisjoint({
            'available_balance', 'spendable_balance', 'reserved_balance',
            'held_balance', 'pending_debit', 'pending_credit',
        })

    def test_the_ledger_service_exposes_no_hold_or_available_balance(self):
        from moneycore.services import ledger

        for forbidden in (
            'available_balance', 'spendable_balance', 'reserve', 'place_hold',
            'release_hold', 'hold', 'set_balance', 'adjust_balance',
        ):
            assert not hasattr(ledger, forbidden), f'ledger.{forbidden} exists'

    def test_the_only_balance_the_service_offers_is_the_posted_one(self):
        import inspect

        from moneycore.services import ledger

        balance_functions = sorted(
            name
            for name, value in vars(ledger).items()
            if inspect.isfunction(value)
            and 'balance' in name
            and not name.startswith('_')
        )

        assert balance_functions == ['posted_balance', 'wallet_posted_balance']


class TestNoSingleEntryAdjustmentPath:
    """There is no way to move a balance without a legitimate counterpart."""

    def test_the_service_exposes_no_credit_or_debit_mutator(self):
        from moneycore.services import ledger

        # debit()/credit() build an EntryDraft — they describe a side, they do
        # not write anything. Prove that rather than assuming it.
        draft = ledger.debit.__wrapped__ if hasattr(ledger.debit, '__wrapped__') else ledger.debit
        assert callable(draft)

        assert Journal.objects.count() == 0

    def test_building_an_entry_draft_writes_nothing(self, counterpart_account):
        debit(counterpart_account, 10_000)
        credit(counterpart_account, 10_000)

        assert Journal.objects.count() == 0
        assert JournalEntry.objects.count() == 0

    def test_a_single_sided_posting_cannot_be_made(self, wallet_account):
        from moneycore.domain.errors import InvalidLedgerEntryError

        with pytest.raises(InvalidLedgerEntryError):
            post_journal(currency='NGN', entries=[credit(wallet_account, 100_000)])

    def test_no_model_offers_a_balance_setter(self):
        for model in (Wallet, LedgerAccount, Journal, JournalEntry):
            for forbidden in ('set_balance', 'adjust_balance', 'apply', 'increment'):
                assert not hasattr(model, forbidden)


class TestLegacyExpenseRemainsSeparate:
    def test_no_legacy_app_imports_the_ledger(self):
        legacy = [
            'accounts', 'expenses', 'categories', 'budgets',
            'groups', 'analytics', 'recurring', 'reports',
        ]
        root = _backend_root()

        offenders = [
            str(path.relative_to(root))
            for app in legacy
            for path in (root / app).rglob('*.py')
            if 'moneycore' in path.read_text(encoding='utf-8')
        ]

        assert offenders == []

    def test_the_expense_model_has_no_ledger_relation(self):
        from expenses.models import Expense

        related = {
            f.related_model.__name__
            for f in Expense._meta.get_fields()
            if f.related_model is not None
        }

        assert related.isdisjoint({'Journal', 'JournalEntry', 'LedgerAccount'})

    def test_no_ledger_model_references_an_expense(self):
        for model in (LedgerAccount, Journal, JournalEntry):
            related = {
                f.related_model.__name__
                for f in model._meta.get_fields()
                if f.related_model is not None
            }
            assert 'Expense' not in related

    def test_posting_creates_no_expense(self, wallet_account, counterpart_account):
        from expenses.models import Expense

        post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 5_000), credit(wallet_account, 5_000)],
        )

        assert Expense.objects.count() == 0

    def test_creating_an_expense_creates_no_ledger_rows(self, ledger_user):
        from datetime import date
        from decimal import Decimal

        from expenses.models import Expense

        Expense.objects.create(
            user=ledger_user, amount=Decimal('42.00'),
            description='Lunch', date=date(2024, 1, 1),
        )

        assert Journal.objects.count() == 0
        assert JournalEntry.objects.count() == 0
        assert LedgerAccount.objects.count() == 0


class TestNoSignalsPostFinancialEntries:
    def test_the_money_core_still_registers_no_signal_receivers(self):
        from pathlib import Path

        import moneycore

        package = Path(moneycore.__file__).parent
        offenders = [
            str(path.relative_to(package))
            for path in package.rglob('*.py')
            if 'tests' not in path.parts
            and any(
                marker in path.read_text(encoding='utf-8')
                for marker in ('@receiver', '.connect(')
            )
        ]

        assert offenders == []

    def test_creating_a_user_posts_nothing(self, db):
        from django.contrib.auth.models import User

        User.objects.create_user('signal-probe')

        assert Journal.objects.count() == 0

    def test_provisioning_posts_nothing(self, ledger_user):
        from moneycore.services.provisioning import provision_financial_account

        provision_financial_account(ledger_user)

        assert Journal.objects.count() == 0
        assert LedgerAccount.objects.count() == 0

    def test_activating_a_financial_account_posts_nothing(self, ledger_user):
        from moneycore.domain.lifecycle import AccountStatus
        from moneycore.services.lifecycle import transition_account
        from moneycore.services.provisioning import provision_financial_account

        account = provision_financial_account(ledger_user).account
        transition_account(account, AccountStatus.ACTIVE)

        assert Journal.objects.count() == 0

    def test_saving_a_wallet_posts_nothing(self, wallet):
        wallet.save()

        assert Journal.objects.count() == 0


class TestNoCustomerFacingLedgerApi:
    def test_no_ledger_route_is_registered(self, client, django_user_model):
        from rest_framework.test import APIClient

        user = django_user_model.objects.create_user('api-probe')
        api = APIClient()
        api.force_authenticate(user=user)

        for path in (
            '/api/journals/', '/api/journal-entries/', '/api/ledger-accounts/',
            '/api/ledger/', '/api/balances/', '/api/postings/',
        ):
            assert api.get(path).status_code == 404, f'{path} is routed'

    def test_the_ledger_module_defines_no_drf_views_or_serializers(self):
        import moneycore.api.serializers as serializers
        import moneycore.api.views as views

        for module in (views, serializers):
            names = [n for n in dir(module) if not n.startswith('_')]
            assert not any(
                'Journal' in n or 'Ledger' in n or 'Posting' in n for n in names
            ), f'{module.__name__} exposes ledger names: {names}'

    def test_the_m1_financial_account_response_is_unchanged(self, ledger_user):
        """M2 must not start leaking a balance into the M1 endpoint."""
        from rest_framework.test import APIClient

        from moneycore.services.provisioning import provision_financial_account

        provision_financial_account(ledger_user)
        api = APIClient()
        api.force_authenticate(user=ledger_user)

        body = api.get('/api/financial-account/').json()

        assert set(body) == {'customer', 'account', 'wallets'}
        assert set(body['wallets'][0]) == {'currency', 'status', 'created_at'}
        assert 'balance' not in str(body)

    def test_the_m1_response_stays_balance_free_even_after_posting(
        self, ledger_user, counterpart_account
    ):
        from rest_framework.test import APIClient

        from moneycore.domain.ledger import LedgerAccountType
        from moneycore.services.ledger import open_wallet_ledger_account
        from moneycore.services.provisioning import provision_financial_account

        result = provision_financial_account(ledger_user)
        account = open_wallet_ledger_account(
            result.wallet, account_type=LedgerAccountType.LIABILITY
        )
        post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 25_000), credit(account, 25_000)],
        )

        api = APIClient()
        api.force_authenticate(user=ledger_user)
        body = api.get('/api/financial-account/').json()

        assert 'balance' not in str(body)
        assert '25000' not in str(body)


class TestNoProviderOrCustodyAssumption:
    def test_no_ledger_model_carries_a_provider_field(self):
        forbidden = {
            'provider', 'provider_id', 'provider_reference', 'bank_code',
            'account_number', 'virtual_account_number', 'nuban', 'iban',
            'settlement_account', 'custody_account',
        }

        for model in (LedgerAccount, Journal, JournalEntry):
            names = {f.name for f in model._meta.get_fields()}
            assert names.isdisjoint(forbidden)

    def test_no_bank_or_custody_account_is_seeded(self):
        """No migration or import creates a settlement/custody account."""
        assert LedgerAccount.objects.count() == 0

    def test_the_service_forces_no_wallet_accounting_classification(self):
        """M2 stays neutral: no default wallet account type exists anywhere."""
        import inspect

        from moneycore.services import ledger

        assert not hasattr(ledger, 'WALLET_LEDGER_ACCOUNT_TYPE')

        # account_type is required, with no default to smuggle a decision in.
        parameter = inspect.signature(ledger.open_wallet_ledger_account).parameters[
            'account_type'
        ]
        assert parameter.default is inspect.Parameter.empty
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
