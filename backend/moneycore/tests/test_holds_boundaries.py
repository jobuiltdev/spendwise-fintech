"""What M3 must NOT have introduced. Groups H and I.

The ledger stays untouched by hold lifecycle, the legacy tracker stays separate,
and no M5+ concept crept in (M4's transaction is now expected).
"""

import pytest
from django.apps import apps

from moneycore.domain.holds import HoldStatus
from moneycore.domain.money import Money
from moneycore.models import FundsHold, Journal, JournalEntry, LedgerAccount, Wallet
from moneycore.services.holds import (
    create_hold,
    expire_due_holds,
    release_hold,
    wallet_balance_projection,
)

pytestmark = pytest.mark.django_db


def _backend_root():
    from pathlib import Path

    import moneycore

    return Path(moneycore.__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# H. Ledger separation
# ---------------------------------------------------------------------------


class TestHoldLifecyclePostsNothing:
    def test_the_full_lifecycle_writes_no_journal(self, funded_wallet):
        from datetime import timedelta

        from django.utils import timezone

        before = (Journal.objects.count(), JournalEntry.objects.count())

        released = create_hold(funded_wallet, 1_000)
        release_hold(released)
        FundsHold.objects.create(
            wallet=funded_wallet, currency='NGN', amount_minor=2_000,
            status=HoldStatus.ACTIVE,
            expires_at=timezone.now() - timedelta(hours=1),
        )
        expire_due_holds(wallet=funded_wallet)
        create_hold(funded_wallet, 3_000)

        assert (Journal.objects.count(), JournalEntry.objects.count()) == before

    def test_the_posted_balance_survives_the_full_lifecycle(self, funded_wallet):
        from moneycore.services.ledger import wallet_posted_balance

        hold = create_hold(funded_wallet, 5_000)
        release_hold(hold)
        create_hold(funded_wallet, 5_000)

        assert wallet_posted_balance(funded_wallet) == Money(10_000, 'NGN')

    def test_the_hold_service_never_imports_the_posting_function(self):
        """It reads balances from the ledger; it never writes to it."""
        import inspect

        from moneycore.services import holds

        source = inspect.getsource(holds)
        assert 'post_journal' not in source
        assert 'JournalEntry' not in source

    def test_a_journal_reversal_does_not_rewrite_a_hold(
        self, funded_wallet, wallet_account, counterpart_account
    ):
        from moneycore.services.ledger import credit, debit, post_journal, reverse_journal

        hold = create_hold(funded_wallet, 1_000)
        journal = post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 500), credit(wallet_account, 500)],
        )

        reverse_journal(journal)

        hold.refresh_from_db()
        assert hold.status == HoldStatus.ACTIVE
        assert hold.amount_minor == 1_000

    def test_ledger_posting_does_not_alter_held_amounts(
        self, funded_wallet, wallet_account, counterpart_account
    ):
        from moneycore.services.ledger import credit, debit, post_journal

        create_hold(funded_wallet, 3_000)

        post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 5_000), credit(wallet_account, 5_000)],
        )

        projection = wallet_balance_projection(funded_wallet)
        assert projection.posted == Money(15_000, 'NGN')
        assert projection.held == Money(3_000, 'NGN')
        assert projection.available == Money(12_000, 'NGN')


class TestLegacyExpenseRemainsSeparate:
    def test_no_legacy_app_imports_the_money_core(self):
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

    def test_the_hold_has_no_expense_relation(self):
        related = {
            f.related_model.__name__
            for f in FundsHold._meta.get_fields()
            if f.related_model is not None
        }

        assert 'Expense' not in related

    def test_creating_an_expense_creates_no_hold(self, ledger_user):
        from datetime import date
        from decimal import Decimal

        from expenses.models import Expense

        Expense.objects.create(
            user=ledger_user, amount=Decimal('42.00'),
            description='Lunch', date=date(2024, 1, 1),
        )

        assert FundsHold.objects.count() == 0

    def test_creating_a_hold_creates_no_expense(self, funded_wallet):
        from expenses.models import Expense

        create_hold(funded_wallet, 1_000)

        assert Expense.objects.count() == 0


# ---------------------------------------------------------------------------
# I. Boundaries
# ---------------------------------------------------------------------------


class TestNoM5OrLaterConcepts:
    """M3 wrote this to exclude the transaction; M4 introduced it deliberately,
    so the guard has moved forward to M5.
    """

    def test_the_app_declares_exactly_the_models_through_m4(self):
        declared = {m.__name__ for m in apps.get_app_config('moneycore').get_models()}

        assert declared == {
            'FinancialCustomer', 'FinancialAccount', 'Wallet',
            'LedgerAccount', 'Journal', 'JournalEntry',
            'FundsHold',
            'FinancialTransaction',
        }

    def test_no_m5_or_later_model_exists(self):
        declared = {
            m.__name__.lower() for m in apps.get_app_config('moneycore').get_models()
        }
        forbidden = {
            'transaction', 'transfer', 'recipient', 'beneficiary', 'provider',
            'webhook', 'reconciliation', 'fee', 'settlement', 'idempotencykey',
            'outboxmessage', 'transactionstate',
        }

        assert declared.isdisjoint(forbidden)

    def test_no_capture_or_settlement_orchestration_exists(self):
        from moneycore.services import holds

        for forbidden in (
            'capture_hold', 'settle_hold', 'commit_hold', 'convert_hold',
            'convert_hold_to_journal', 'capture', 'settle',
        ):
            assert not hasattr(holds, forbidden), f'holds.{forbidden} exists'

    def test_the_hold_service_exposes_only_the_m3_surface(self):
        import inspect

        from moneycore.services import holds

        # Functions *defined* here, not ones imported into the namespace.
        public = sorted(
            name
            for name, value in vars(holds).items()
            if inspect.isfunction(value)
            and value.__module__ == holds.__name__
            and not name.startswith('_')
        )

        assert public == [
            'create_hold', 'expire_due_holds', 'expire_hold', 'held_amount',
            'is_hold_effective', 'release_hold', 'wallet_balance_projection',
        ]

    def test_no_provider_terminology_appears_on_the_hold(self):
        names = {f.name for f in FundsHold._meta.get_fields()}

        assert names.isdisjoint({
            'provider', 'provider_id', 'provider_reference', 'provider_status',
            'bank', 'bank_code', 'account_number', 'virtual_account_number',
        })

    def test_no_scheduling_dependency_was_added(self):
        from pathlib import Path

        requirements = (
            Path(_backend_root()) / 'requirements.txt'
        ).read_text(encoding='utf-8').lower()

        assert 'celery' not in requirements
        assert 'redis' not in requirements


class TestNoCustomerFacingHoldApi:
    def test_no_hold_or_balance_route_is_registered(self, django_user_model):
        from rest_framework.test import APIClient

        user = django_user_model.objects.create_user('m3-api-probe')
        api = APIClient()
        api.force_authenticate(user=user)

        for path in (
            '/api/holds/', '/api/funds-holds/', '/api/reservations/',
            '/api/balance/', '/api/balances/', '/api/wallet-balance/',
            '/api/available-balance/',
        ):
            assert api.get(path).status_code == 404, f'{path} is routed'

    def test_the_api_module_exposes_no_hold_or_balance_names(self):
        import moneycore.api.serializers as serializers
        import moneycore.api.views as views

        for module in (views, serializers):
            names = [n for n in dir(module) if not n.startswith('_')]
            assert not any(
                'Hold' in n or 'Balance' in n or 'Projection' in n for n in names
            ), f'{module.__name__} exposes M3 names: {names}'

    def test_the_m1_financial_account_response_is_unchanged(self, ledger_user):
        from rest_framework.test import APIClient

        from moneycore.services.provisioning import provision_financial_account

        provision_financial_account(ledger_user)
        api = APIClient()
        api.force_authenticate(user=ledger_user)

        body = api.get('/api/financial-account/').json()

        assert set(body) == {'customer', 'account', 'wallets'}
        assert set(body['wallets'][0]) == {'currency', 'status', 'created_at'}

    def test_the_m1_response_stays_balance_free_even_with_funds_and_holds(
        self, ledger_user, counterpart_account
    ):
        from rest_framework.test import APIClient

        from moneycore.domain.ledger import LedgerAccountType
        from moneycore.services.ledger import (
            credit,
            debit,
            open_wallet_ledger_account,
            post_journal,
        )
        from moneycore.services.provisioning import provision_financial_account

        result = provision_financial_account(ledger_user)
        account = open_wallet_ledger_account(
            result.wallet, account_type=LedgerAccountType.LIABILITY
        )
        post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 25_000), credit(account, 25_000)],
        )
        create_hold(result.wallet, 5_000)

        api = APIClient()
        api.force_authenticate(user=ledger_user)
        body = str(api.get('/api/financial-account/').json())

        for leaked in ('balance', 'held', 'available', '25000', '5000', '20000'):
            assert leaked not in body


class TestNoSignals:
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

    def test_saving_a_wallet_creates_no_hold(self, funded_wallet):
        funded_wallet.save()

        assert FundsHold.objects.count() == 0

    def test_provisioning_creates_no_hold(self, ledger_user):
        from moneycore.services.provisioning import provision_financial_account

        provision_financial_account(ledger_user)

        assert FundsHold.objects.count() == 0

    def test_posting_a_journal_creates_no_hold(
        self, wallet_account, counterpart_account
    ):
        from moneycore.services.ledger import credit, debit, post_journal

        post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 1_000), credit(wallet_account, 1_000)],
        )

        assert FundsHold.objects.count() == 0


class TestM2InvariantsUntouched:
    def test_journals_remain_immutable(self, wallet_account, counterpart_account):
        from moneycore.domain.errors import JournalImmutableError
        from moneycore.services.ledger import credit, debit, post_journal

        journal = post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 100), credit(wallet_account, 100)],
        )

        journal.description = 'rewritten'
        with pytest.raises(JournalImmutableError):
            journal.save()

    def test_unbalanced_posting_is_still_refused(
        self, wallet_account, counterpart_account
    ):
        from moneycore.domain.errors import LedgerUnbalancedError
        from moneycore.services.ledger import credit, debit, post_journal

        with pytest.raises(LedgerUnbalancedError):
            post_journal(
                currency='NGN',
                entries=[debit(counterpart_account, 100), credit(wallet_account, 99)],
            )

    def test_the_ledger_still_has_no_default_wallet_classification(self):
        from moneycore.services import ledger

        assert not hasattr(ledger, 'WALLET_LEDGER_ACCOUNT_TYPE')

    def test_an_unmapped_wallet_still_has_no_posted_balance(self, wallet):
        from moneycore.domain.errors import WalletLedgerAccountNotFoundError
        from moneycore.services.ledger import wallet_posted_balance

        with pytest.raises(WalletLedgerAccountNotFoundError):
            wallet_posted_balance(wallet)
