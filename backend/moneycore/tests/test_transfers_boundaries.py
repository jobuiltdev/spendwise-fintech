"""What M5 must NOT have introduced.

M5 is domain and application infrastructure. It reaches no network, exposes no
customer API, saves no recipient, guesses no fee or limit, and leaves every
production-dependent decision open.
"""

from pathlib import Path

import pytest
from django.apps import apps

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


def _moneycore_package() -> Path:
    import moneycore

    return Path(moneycore.__file__).parent


def _backend_root() -> Path:
    return _moneycore_package().parent


def _production_sources():
    return [
        path
        for path in _moneycore_package().rglob('*.py')
        if 'tests' not in path.parts
    ]


# ---------------------------------------------------------------------------
# No provider
# ---------------------------------------------------------------------------


class TestNoProviderImplementation:
    def test_no_http_client_is_imported_anywhere_in_the_money_core(self):
        offenders = []
        for path in _production_sources():
            source = path.read_text(encoding='utf-8')
            for marker in (
                'import requests', 'import httpx', 'import urllib3',
                'from requests', 'from httpx', 'http.client', 'aiohttp',
                'import socket',
            ):
                if marker in source:
                    offenders.append(f'{path.name}: {marker}')

        assert offenders == []

    def test_no_provider_model_exists(self):
        declared = {
            m.__name__.lower() for m in apps.get_app_config('moneycore').get_models()
        }

        assert declared.isdisjoint({
            'provider', 'providerattempt', 'providerconfig', 'webhook',
            'webhookevent', 'reconciliation', 'reconciliationrecord',
            'settlement', 'settlementbatch', 'payout', 'outboxmessage',
        })

    def test_no_provider_adapter_lives_in_the_services_package(self):
        """M6 added services/provider_execution.py, which is orchestration.

        The adapters themselves live in moneycore/providers/, and nothing that
        speaks a provider protocol belongs beside the money services.
        """
        services = _moneycore_package() / 'services'
        names = {path.stem for path in services.glob('*.py')}

        assert names.isdisjoint({
            'providers', 'provider', 'simulator', 'webhooks', 'reconciliation',
            'settlement', 'banks', 'bank_directory',
        })

    def test_no_provider_brand_appears_in_the_money_core(self):
        offenders = []
        for path in _production_sources():
            source = path.read_text(encoding='utf-8').lower()
            for brand in (
                'paystack', 'flutterwave', 'monnify', 'interswitch', 'kuda',
            ):
                if brand in source:
                    offenders.append(f'{path.name}: {brand}')

        assert offenders == []

    def test_no_new_dependency_was_added(self):
        requirements = (
            _backend_root() / 'requirements.txt'
        ).read_text(encoding='utf-8').lower()

        for package in ('celery', 'redis', 'httpx', 'kombu', 'aiohttp'):
            assert package not in requirements

    def test_the_transfer_carries_no_provider_or_external_reference(self):
        names = {f.name for f in Transfer._meta.get_fields()}

        assert names.isdisjoint({
            'provider', 'provider_id', 'provider_reference', 'provider_status',
            'provider_attempt', 'provider_attempt_id', 'webhook_id',
            'settlement_reference', 'reconciliation_state',
            'external_transaction_id', 'session_id',
        })


# ---------------------------------------------------------------------------
# No saved beneficiary
# ---------------------------------------------------------------------------


class TestNoSavedBeneficiary:
    """The immutable snapshot is enough. A saved-recipient product feature can
    arrive later without touching transfer history."""

    def test_no_beneficiary_or_recipient_model_exists(self):
        declared = {
            m.__name__.lower() for m in apps.get_app_config('moneycore').get_models()
        }

        assert declared.isdisjoint({
            'beneficiary', 'savedbeneficiary', 'recipient', 'savedrecipient',
            'payee', 'contact', 'bank', 'bankaccount', 'bankdirectory',
        })

    def test_the_transfer_points_at_no_mutable_recipient_record(self):
        related = {
            f.related_model.__name__
            for f in Transfer._meta.get_fields()
            if f.related_model is not None
        }

        assert related == {'FinancialTransaction'}

    def test_the_snapshot_is_self_contained(self, funded_wallet):
        transfer = prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key='self-contained'
        )

        stored = Transfer.objects.filter(pk=transfer.pk).values().get()
        assert stored['recipient_name'] == 'Ada Okafor'
        assert stored['destination_account_number'] == '0123456789'
        assert stored['destination_bank_code'] == 'NG-058'
        assert stored['destination_bank_name'] == 'Example Bank'

    def test_no_saved_recipient_service_exists(self):
        from moneycore.services import transfers

        for forbidden in (
            'save_beneficiary', 'save_recipient', 'list_beneficiaries',
            'list_recipients', 'add_beneficiary', 'delete_beneficiary',
        ):
            assert not hasattr(transfers, forbidden), f'{forbidden} exists'


# ---------------------------------------------------------------------------
# No customer API
# ---------------------------------------------------------------------------


class TestNoCustomerTransferApi:
    def test_no_transfer_route_is_registered(self, django_user_model):
        from rest_framework.test import APIClient

        user = django_user_model.objects.create_user('m5-api-probe')
        api = APIClient()
        api.force_authenticate(user=user)

        for path in (
            '/api/transfers/', '/api/transfer/', '/api/send/', '/api/send-money/',
            '/api/payments/', '/api/beneficiaries/', '/api/recipients/',
            '/api/banks/', '/api/resolve-account/', '/api/verify-account/',
            '/api/money/transfers/',
        ):
            assert api.get(path).status_code == 404, f'{path} is routed'

    @pytest.mark.parametrize('method', ['post', 'put', 'patch', 'delete'])
    def test_no_transfer_write_route_is_registered(
        self, django_user_model, method
    ):
        from rest_framework.test import APIClient

        user = django_user_model.objects.create_user(f'm5-write-probe-{method}')
        api = APIClient()
        api.force_authenticate(user=user)

        response = getattr(api, method)('/api/transfers/')
        assert response.status_code == 404

    def test_the_api_module_exposes_no_transfer_names(self):
        import moneycore.api.serializers as serializers
        import moneycore.api.views as views

        for module in (views, serializers):
            names = [n for n in dir(module) if not n.startswith('_')]
            assert not any(
                'Transfer' in n or 'Beneficiary' in n or 'Recipient' in n
                for n in names
            ), f'{module.__name__} exposes M5 names: {names}'

    def test_the_m1_financial_account_response_is_unchanged(self, ledger_user):
        from rest_framework.test import APIClient

        from moneycore.services.provisioning import provision_financial_account

        provision_financial_account(ledger_user)
        api = APIClient()
        api.force_authenticate(user=ledger_user)

        body = api.get('/api/financial-account/').json()

        assert set(body) == {'customer', 'account', 'wallets'}
        assert set(body['wallets'][0]) == {'currency', 'status', 'created_at'}

    def test_the_m1_response_leaks_nothing_about_a_live_transfer(
        self, funded_wallet, ledger_user
    ):
        from rest_framework.test import APIClient

        prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key='api-leak'
        )

        api = APIClient()
        api.force_authenticate(user=ledger_user)
        body = str(api.get('/api/financial-account/').json()).lower()

        for leaked in (
            'transfer', '0123456789', 'ada okafor', 'ng-058', '7000', '10000',
        ):
            assert leaked not in body


# ---------------------------------------------------------------------------
# No guessed policy
# ---------------------------------------------------------------------------


class TestNoGuessedPolicy:
    def test_no_fee_is_computed_anywhere(self):
        from moneycore.services import transfers

        for forbidden in (
            'calculate_fee', 'compute_fee', 'transfer_fee', 'fee_for',
            'FEE', 'FEE_MINOR', 'FEE_PERCENTAGE', 'apply_fee',
        ):
            assert not hasattr(transfers, forbidden), f'{forbidden} exists'

    def test_no_limit_is_hardcoded(self):
        from moneycore.services import transfers

        for forbidden in (
            'DAILY_LIMIT', 'MAX_TRANSFER', 'PER_TRANSFER_LIMIT', 'TIER_LIMITS',
            'MAXIMUM_BALANCE', 'check_limits', 'enforce_limits',
        ):
            assert not hasattr(transfers, forbidden), f'{forbidden} exists'

    def test_no_naira_figure_is_embedded_in_the_transfer_modules(self):
        """A guessed regulatory limit would look like a big round number."""
        import re

        from moneycore.domain import transfers as domain
        from moneycore.services import transfers as service

        for module in (domain, service):
            source = Path(module.__file__).read_text(encoding='utf-8')
            # Any integer literal of five or more digits would be a currency
            # figure; the only numeric constants M5 has are field lengths.
            assert not re.search(r'\b\d{5,}\b', source), module.__name__

    def test_no_kyc_or_risk_check_exists(self):
        from moneycore.services import transfers

        for forbidden in (
            'check_kyc', 'kyc_tier', 'require_kyc', 'risk_score', 'assess_risk',
            'fraud_check', 'device_check',
        ):
            assert not hasattr(transfers, forbidden), f'{forbidden} exists'

    def test_no_transaction_pin_or_authorization_exists(self):
        """Transaction security is M9."""
        from moneycore.services import transfers

        for forbidden in (
            'verify_pin', 'check_pin', 'require_pin', 'authorize',
            'authorise', 'require_authorization',
        ):
            assert not hasattr(transfers, forbidden), f'{forbidden} exists'

    def test_the_hold_gets_no_default_duration(self, funded_wallet):
        """O-17 stays open: no provider timeout window is known."""
        transfer = prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key='no-expiry'
        )

        assert transfer.hold.expires_at is None

    def test_no_scheduler_or_queue_was_introduced(self):
        offenders = []
        for path in _production_sources():
            source = path.read_text(encoding='utf-8')
            for marker in ('celery', 'Celery', 'from redis', 'import redis'):
                if marker in source:
                    offenders.append(f'{path.name}: {marker}')

        assert offenders == []


# ---------------------------------------------------------------------------
# No implicit behaviour
# ---------------------------------------------------------------------------


class TestNoSignalsOrSideEffects:
    def test_the_money_core_still_registers_no_signal_receivers(self):
        offenders = [
            path.name
            for path in _production_sources()
            if any(
                marker in path.read_text(encoding='utf-8')
                for marker in ('@receiver', '.connect(', 'post_save', 'pre_save')
            )
        ]

        assert offenders == []

    def test_provisioning_creates_no_transfer(self, ledger_user):
        from moneycore.services.provisioning import provision_financial_account

        provision_financial_account(ledger_user)

        assert Transfer.objects.count() == 0

    def test_posting_a_journal_creates_no_transfer(
        self, wallet_account, counterpart_account
    ):
        from moneycore.services.ledger import credit, debit, post_journal

        post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 1_000), credit(wallet_account, 1_000)],
        )

        assert Transfer.objects.count() == 0

    def test_creating_a_transaction_creates_no_transfer(self, funded_wallet):
        from moneycore.domain.transactions import TransactionDirection
        from moneycore.services.transactions import create_transaction

        create_transaction(
            funded_wallet, TransactionDirection.OUTGOING, 1_000,
            idempotency_key='no-transfer',
        )

        assert Transfer.objects.count() == 0

    def test_no_ledger_account_is_seeded_by_m5(self, db):
        """No settlement or custody account is created behind the scenes."""
        assert LedgerAccount.objects.count() == 0


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

    def test_creating_an_expense_creates_no_transfer(self, ledger_user):
        from datetime import date
        from decimal import Decimal

        from expenses.models import Expense

        Expense.objects.create(
            user=ledger_user, amount=Decimal('42.00'),
            description='Lunch', date=date(2024, 1, 1),
        )

        assert Transfer.objects.count() == 0

    def test_preparing_a_transfer_creates_no_expense(self, funded_wallet):
        from expenses.models import Expense

        prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key='no-expense'
        )

        assert Expense.objects.count() == 0


class TestNoMobileSurface:
    def test_the_mobile_app_knows_nothing_about_transfers(self):
        """M5 is a backend milestone. Nothing in the app was touched."""
        mobile = _backend_root().parent / 'mobile'
        if not mobile.exists():
            pytest.skip('No mobile workspace in this checkout.')

        offenders = [
            str(path)
            for pattern in ('**/*.ts', '**/*.tsx')
            for path in mobile.glob(pattern)
            if 'node_modules' not in path.parts
            and 'prepare_transfer' in path.read_text(encoding='utf-8')
        ]

        assert offenders == []


# ---------------------------------------------------------------------------
# Migrations and earlier invariants
# ---------------------------------------------------------------------------


class TestMigrations:
    def test_the_app_has_exactly_six_migrations(self):
        migrations_dir = _moneycore_package() / 'migrations'
        applied = sorted(
            path.stem
            for path in migrations_dir.glob('*.py')
            if path.stem != '__init__'
        )

        assert len(applied) == 6
        assert applied[0] == '0001_initial'
        assert applied[-1].startswith('0006_')

    def test_the_earlier_migrations_were_not_rewritten(self):
        migrations_dir = _moneycore_package() / 'migrations'

        for pattern in ('0001_initial.py', '0002_*.py', '0003_*.py', '0004_*.py'):
            path = next(migrations_dir.glob(pattern))
            assert 'Transfer' not in path.read_text(encoding='utf-8')

    def test_the_new_migration_carries_no_data_operation(self):
        migrations_dir = _moneycore_package() / 'migrations'
        source = next(migrations_dir.glob('0005_*.py')).read_text(encoding='utf-8')

        for forbidden in ('RunPython', 'RunSQL', 'bulk_create', 'objects.create'):
            assert forbidden not in source


class TestEarlierMilestonesUntouched:
    def test_no_model_stores_a_balance(self):
        forbidden = {
            'balance', 'balance_minor', 'available_balance', 'held_balance',
            'reserved_balance', 'spendable_balance', 'cached_balance',
        }

        for model in (
            Wallet, FundsHold, FinancialTransaction, Journal, Transfer
        ):
            names = {f.name for f in model._meta.get_fields()}
            assert names.isdisjoint(forbidden), model.__name__

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

    def test_an_unmapped_wallet_still_has_no_posted_balance(self, wallet):
        from moneycore.domain.errors import WalletLedgerAccountNotFoundError
        from moneycore.services.ledger import wallet_posted_balance

        with pytest.raises(WalletLedgerAccountNotFoundError):
            wallet_posted_balance(wallet)

    def test_the_ledger_still_has_no_default_wallet_classification(self):
        """O-13 stays open. M5 takes no position either."""
        from moneycore.services import ledger

        assert not hasattr(ledger, 'WALLET_LEDGER_ACCOUNT_TYPE')

    def test_holds_still_oversubscribe_nothing(self, funded_wallet):
        from moneycore.domain.errors import InsufficientAvailableBalanceError
        from moneycore.services.holds import create_hold

        create_hold(funded_wallet, 7_000)

        with pytest.raises(InsufficientAvailableBalanceError):
            create_hold(funded_wallet, 7_000)

    def test_unknown_is_still_not_terminal(self):
        from moneycore.domain.transactions import TransactionStatus

        assert TransactionStatus.UNKNOWN not in TransactionStatus.TERMINAL

    def test_the_transaction_engine_gained_no_transfer_column(self):
        """M5 owns the relationship, so FinancialTransaction stays general."""
        concrete = {
            f.name for f in FinancialTransaction._meta.get_fields() if f.concrete
        }

        assert concrete.isdisjoint({
            'transfer', 'transfer_id', 'destination', 'recipient_name',
            'destination_account_number', 'destination_bank_code',
        })

    def test_the_reverse_links_are_the_transfer_and_its_provider_attempts(self):
        """M5 asserted the transfer alone; M6 added execution attempts.

        Still no *column* on FinancialTransaction for either.
        """
        reverse = {
            f.name
            for f in FinancialTransaction._meta.get_fields()
            if f.auto_created and not f.concrete
        }

        assert reverse == {'transfer', 'provider_attempts'}
