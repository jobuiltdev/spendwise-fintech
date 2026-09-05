"""What M6 must NOT have introduced.

M6 is the provider *boundary* and a simulator. Resolving an ambiguous outcome,
receiving webhooks and reconciling belong to later milestones, and none of them
appears here.
"""

from pathlib import Path

import pytest
from django.apps import apps

from moneycore.domain.ledger import LedgerAccountType
from moneycore.domain.transfers import VerifiedBankAccount
from moneycore.models import (
    FinancialTransaction,
    FundsHold,
    Journal,
    ProviderExecutionAttempt,
    Transfer,
    Wallet,
)
from moneycore.services.ledger import open_ledger_account
from moneycore.services.transfers import prepare_transfer

pytestmark = pytest.mark.django_db


DESTINATION = VerifiedBankAccount(
    account_number='0123456789',
    bank_code='SIM-001',
    bank_name='Simulated First Bank',
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
# No M7+ concepts
# ---------------------------------------------------------------------------


class TestNoWebhook:
    def test_no_webhook_model_exists(self):
        declared = {
            m.__name__.lower() for m in apps.get_app_config('moneycore').get_models()
        }

        assert declared.isdisjoint({
            'webhook', 'webhookevent', 'webhookdelivery', 'providerevent',
            'inboundevent',
        })

    def test_no_webhook_module_exists(self):
        names = {
            path.stem for path in _moneycore_package().rglob('*.py')
        }

        assert names.isdisjoint({
            'webhooks', 'webhook', 'callbacks', 'inbound',
        })

    def test_no_webhook_handler_or_signature_check_exists(self):
        offenders = []
        for path in _production_sources():
            source = path.read_text(encoding='utf-8').lower()
            for marker in (
                'def handle_webhook', 'webhook_signature', 'verify_signature',
                'hmac', 'x-signature',
            ):
                if marker in source:
                    offenders.append(f'{path.name}: {marker}')

        assert offenders == []

    def test_no_webhook_route_is_registered(self, django_user_model):
        from rest_framework.test import APIClient

        api = APIClient()
        for path in (
            '/api/webhooks/', '/api/webhook/', '/api/providers/webhook/',
            '/api/callbacks/',
        ):
            assert api.post(path, {}, format='json').status_code == 404, path


class TestNoStatusPolling:
    def test_no_polling_entry_point_exists(self):
        from moneycore.services import provider_execution

        for forbidden in (
            'check_transfer_status', 'poll_provider', 'poll', 'fetch_status',
            'refresh_status', 'sync_status', 'resolve_unknown',
            'retry_unknown', 'sweep', 'recover',
        ):
            assert not hasattr(provider_execution, forbidden), forbidden

    def test_the_provider_interface_declares_no_status_query(self):
        from moneycore.domain.providers import TransferProvider

        methods = {n for n in dir(TransferProvider) if not n.startswith('_')}

        assert methods == {'resolve_bank_account', 'submit_transfer'}

    def test_the_simulator_offers_no_status_query(self):
        from moneycore.providers.simulator import SimulatorTransferProvider

        for forbidden in (
            'get_status', 'check_status', 'fetch_transfer', 'lookup_transfer',
        ):
            assert not hasattr(SimulatorTransferProvider, forbidden), forbidden

    def test_no_scheduled_sweep_exists(self):
        offenders = []
        for path in _production_sources():
            source = path.read_text(encoding='utf-8')
            for marker in (
                'schedule(', 'cron', 'periodic_task', 'shared_task',
                'apply_async', 'delay(',
            ):
                if marker in source:
                    offenders.append(f'{path.name}: {marker}')

        assert offenders == []


class TestNoReconciliation:
    def test_no_reconciliation_or_settlement_model_exists(self):
        declared = {
            m.__name__.lower() for m in apps.get_app_config('moneycore').get_models()
        }

        assert declared.isdisjoint({
            'reconciliation', 'reconciliationrecord', 'settlement',
            'settlementrecord', 'settlementbatch', 'statement',
            'outboxmessage', 'payout',
        })

    def test_no_reconciliation_module_exists(self):
        names = {path.stem for path in _moneycore_package().rglob('*.py')}

        assert names.isdisjoint({
            'reconciliation', 'reconcile', 'settlement', 'outbox',
        })

    def test_no_ledger_provider_comparison_exists(self):
        from moneycore.services import provider_execution

        for forbidden in (
            'reconcile', 'compare_ledger', 'settlement_report', 'daily_sweep',
        ):
            assert not hasattr(provider_execution, forbidden), forbidden


class TestNoRealProvider:
    def test_no_vendor_brand_appears_in_the_money_core(self):
        offenders = []
        for path in _production_sources():
            source = path.read_text(encoding='utf-8').lower()
            for brand in (
                'paystack', 'flutterwave', 'monnify', 'interswitch', 'kuda',
                'providus', 'wema', 'sterling',
            ):
                if brand in source:
                    offenders.append(f'{path.name}: {brand}')

        assert offenders == []

    def test_no_http_client_is_imported_anywhere(self):
        offenders = []
        for path in _production_sources():
            source = path.read_text(encoding='utf-8')
            for marker in (
                'import requests', 'import httpx', 'import urllib3',
                'from requests', 'from httpx', 'http.client', 'aiohttp',
                'import socket', 'urlopen',
            ):
                if marker in source:
                    offenders.append(f'{path.name}: {marker}')

        assert offenders == []

    def test_no_provider_credential_or_base_url_setting_exists(self):
        settings_source = (
            _backend_root() / 'spendwise' / 'settings.py'
        ).read_text(encoding='utf-8')

        for forbidden in (
            'PROVIDER_API_KEY', 'PROVIDER_SECRET', 'PROVIDER_BASE_URL',
            'PROVIDER_TIMEOUT', 'PAYSTACK', 'FLUTTERWAVE', 'MONNIFY',
        ):
            assert forbidden not in settings_source, forbidden

    def test_the_only_provider_key_is_the_simulator(self):
        from moneycore.providers.simulator import SIMULATOR_PROVIDER_KEY

        assert SIMULATOR_PROVIDER_KEY == 'simulator'

    def test_no_provider_registry_framework_was_built(self):
        from moneycore.services import provider_execution

        for forbidden in (
            'PROVIDER_REGISTRY', 'register_provider', 'get_provider',
            'load_provider', 'provider_for_key', 'DEFAULT_PROVIDER',
        ):
            assert not hasattr(provider_execution, forbidden), forbidden

    def test_the_provider_is_always_passed_in(self):
        """No global, no configured default, nothing to misconfigure."""
        import inspect

        from moneycore.services.provider_execution import (
            execute_transfer,
            resolve_transfer_destination,
        )

        for function in (execute_transfer, resolve_transfer_destination):
            assert 'provider' in inspect.signature(function).parameters

    def test_no_new_dependency_was_added(self):
        requirements = (
            _backend_root() / 'requirements.txt'
        ).read_text(encoding='utf-8').lower()

        for package in ('celery', 'redis', 'httpx', 'kombu', 'aiohttp'):
            assert package not in requirements


class TestNoPublicApi:
    def test_no_execution_or_resolution_route_is_registered(
        self, django_user_model
    ):
        from rest_framework.test import APIClient

        user = django_user_model.objects.create_user('m6-api-probe')
        api = APIClient()
        api.force_authenticate(user=user)

        for path in (
            '/api/transfers/1/execute/', '/api/execute/', '/api/resolve-account/',
            '/api/verify-account/', '/api/providers/', '/api/attempts/',
            '/api/provider-attempts/', '/api/banks/',
        ):
            assert api.get(path).status_code == 404, f'{path} is routed'
            assert api.post(path, {}, format='json').status_code == 404, path

    def test_the_api_module_exposes_no_provider_names(self):
        import moneycore.api.serializers as serializers
        import moneycore.api.views as views

        for module in (views, serializers):
            names = [n for n in dir(module) if not n.startswith('_')]
            assert not any(
                'Provider' in n or 'Attempt' in n or 'Execute' in n
                for n in names
            ), f'{module.__name__} exposes M6 names: {names}'

    def test_the_m1_financial_account_response_is_unchanged(self, ledger_user):
        from rest_framework.test import APIClient

        from moneycore.services.provisioning import provision_financial_account

        provision_financial_account(ledger_user)
        api = APIClient()
        api.force_authenticate(user=ledger_user)

        body = api.get('/api/financial-account/').json()

        assert set(body) == {'customer', 'account', 'wallets'}
        assert set(body['wallets'][0]) == {'currency', 'status', 'created_at'}

    def test_the_m1_response_leaks_nothing_about_an_attempt(
        self, funded_wallet, ledger_user
    ):
        from rest_framework.test import APIClient

        from moneycore.providers.simulator import SimulatorTransferProvider
        from moneycore.services.ledger import open_ledger_account
        from moneycore.services.provider_execution import execute_transfer

        counterpart = open_ledger_account(
            code='internal:m6-leak-counterpart:NGN',
            name='Internal counterpart',
            account_type=LedgerAccountType.ASSET,
            currency='NGN',
        )
        transfer = prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key='leak-probe'
        )
        execute_transfer(
            transfer,
            provider=SimulatorTransferProvider(),
            counterpart_account=counterpart,
        )

        api = APIClient()
        api.force_authenticate(user=ledger_user)
        body = str(api.get('/api/financial-account/').json()).lower()

        for leaked in (
            'provider', 'simulator', 'attempt', 'sim-', 'sw-', '0123456789',
        ):
            assert leaked not in body


class TestNoGuessedPolicy:
    def test_no_transaction_pin_kyc_or_risk_check_exists(self):
        from moneycore.services import provider_execution

        for forbidden in (
            'verify_pin', 'check_pin', 'require_pin', 'check_kyc', 'kyc_tier',
            'risk_score', 'assess_risk', 'fraud_check',
        ):
            assert not hasattr(provider_execution, forbidden), forbidden

    def test_no_fee_is_computed(self):
        from moneycore.services import provider_execution

        for forbidden in (
            'calculate_fee', 'compute_fee', 'transfer_fee', 'FEE', 'apply_fee',
        ):
            assert not hasattr(provider_execution, forbidden), forbidden

    def test_no_limit_is_hardcoded(self):
        from moneycore.services import provider_execution

        for forbidden in (
            'DAILY_LIMIT', 'MAX_TRANSFER', 'PER_TRANSFER_LIMIT', 'TIER_LIMITS',
        ):
            assert not hasattr(provider_execution, forbidden), forbidden

    def test_no_provider_timeout_value_is_guessed(self):
        """No real provider exists, so no timeout window could be right."""
        from moneycore.services import provider_execution

        for forbidden in (
            'TIMEOUT', 'DEFAULT_TIMEOUT', 'REQUEST_TIMEOUT', 'CONNECT_TIMEOUT',
        ):
            assert not hasattr(provider_execution, forbidden), forbidden

    def test_no_retry_policy_is_defined(self):
        from moneycore.services import provider_execution

        for forbidden in (
            'MAX_RETRIES', 'RETRY_DELAY', 'BACKOFF', 'retry', 'should_retry',
        ):
            assert not hasattr(provider_execution, forbidden), forbidden

    def test_the_settlement_counterpart_is_still_supplied_by_the_caller(self):
        """O-29 stays open: M6 invents no settlement account either."""
        import inspect

        from moneycore.services.provider_execution import execute_transfer

        assert 'counterpart_account' in inspect.signature(
            execute_transfer
        ).parameters

    def test_no_ledger_account_is_seeded_by_m6(self, db):
        from moneycore.models import LedgerAccount

        assert LedgerAccount.objects.count() == 0


# ---------------------------------------------------------------------------
# Separation and earlier invariants
# ---------------------------------------------------------------------------


class TestSeparationOfConcerns:
    def test_the_transfer_gained_no_provider_field(self):
        concrete = {f.name for f in Transfer._meta.get_fields() if f.concrete}

        assert concrete.isdisjoint({
            'provider', 'provider_key', 'provider_reference', 'provider_status',
            'attempt', 'provider_attempt', 'client_reference',
        })

    def test_the_financial_transaction_gained_no_provider_field(self):
        concrete = {
            f.name for f in FinancialTransaction._meta.get_fields() if f.concrete
        }

        assert concrete.isdisjoint({
            'provider', 'provider_key', 'provider_reference', 'provider_status',
            'attempt', 'provider_attempt', 'client_reference',
        })

    def test_the_only_new_link_on_the_transaction_is_the_attempt(self):
        reverse = {
            f.name
            for f in FinancialTransaction._meta.get_fields()
            if f.auto_created and not f.concrete
        }

        assert reverse == {'transfer', 'provider_attempts'}

    def test_the_hold_gained_nothing(self):
        concrete = {f.name for f in FundsHold._meta.get_fields() if f.concrete}

        assert concrete == {
            'id', 'wallet', 'currency', 'amount_minor', 'status',
            'expires_at', 'released_at', 'expired_at', 'reason',
            'created_at', 'updated_at',
        }

    def test_the_simulator_lives_outside_the_transfer_service(self):
        from moneycore.services import transfers

        source = Path(transfers.__file__).read_text(encoding='utf-8')

        assert 'Simulator' not in source
        assert 'provider' not in source.lower().split('"""')[-1]

    def test_transfer_preparation_and_provider_execution_are_separate(self):
        from moneycore.services import provider_execution, transfers

        assert not hasattr(transfers, 'execute_transfer')
        assert not hasattr(provider_execution, 'prepare_transfer')


class TestNoSignalsOrImplicitBehaviour:
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

    def test_preparing_a_transfer_creates_no_attempt(self, funded_wallet):
        prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key='no-attempt'
        )

        assert ProviderExecutionAttempt.objects.count() == 0

    def test_provisioning_creates_no_attempt(self, ledger_user):
        from moneycore.services.provisioning import provision_financial_account

        provision_financial_account(ledger_user)

        assert ProviderExecutionAttempt.objects.count() == 0

    def test_creating_an_expense_creates_no_attempt(self, ledger_user):
        from datetime import date
        from decimal import Decimal

        from expenses.models import Expense

        Expense.objects.create(
            user=ledger_user, amount=Decimal('42.00'),
            description='Lunch', date=date(2024, 1, 1),
        )

        assert ProviderExecutionAttempt.objects.count() == 0

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


class TestNoMobileSurface:
    def test_the_mobile_app_knows_nothing_about_providers(self):
        mobile = _backend_root().parent / 'mobile'
        if not mobile.exists():
            pytest.skip('No mobile workspace in this checkout.')

        offenders = [
            str(path)
            for pattern in ('**/*.ts', '**/*.tsx')
            for path in mobile.glob(pattern)
            if 'node_modules' not in path.parts
            and any(
                marker in path.read_text(encoding='utf-8')
                for marker in ('execute_transfer', 'ProviderExecutionAttempt')
            )
        ]

        assert offenders == []


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

        for pattern in (
            '0001_initial.py', '0002_*.py', '0003_*.py', '0004_*.py', '0005_*.py'
        ):
            path = next(migrations_dir.glob(pattern))
            assert 'ProviderExecutionAttempt' not in path.read_text(
                encoding='utf-8'
            )

    def test_the_new_migration_carries_no_data_operation(self):
        migrations_dir = _moneycore_package() / 'migrations'
        source = next(migrations_dir.glob('0006_*.py')).read_text(encoding='utf-8')

        for forbidden in ('RunPython', 'RunSQL', 'bulk_create', 'objects.create'):
            assert forbidden not in source


class TestEarlierMilestonesUntouched:
    def test_no_model_stores_a_balance(self):
        forbidden = {
            'balance', 'balance_minor', 'available_balance', 'held_balance',
            'reserved_balance', 'spendable_balance', 'cached_balance',
        }

        for model in (
            Wallet, FundsHold, FinancialTransaction, Journal, Transfer,
            ProviderExecutionAttempt,
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

    def test_an_unmapped_wallet_still_has_no_posted_balance(self, wallet):
        from moneycore.domain.errors import WalletLedgerAccountNotFoundError
        from moneycore.services.ledger import wallet_posted_balance

        with pytest.raises(WalletLedgerAccountNotFoundError):
            wallet_posted_balance(wallet)

    def test_the_ledger_still_has_no_default_wallet_classification(self):
        from moneycore.services import ledger

        assert not hasattr(ledger, 'WALLET_LEDGER_ACCOUNT_TYPE')

    def test_unknown_is_still_not_terminal_for_a_transaction(self):
        from moneycore.domain.transactions import TransactionStatus

        assert TransactionStatus.UNKNOWN not in TransactionStatus.TERMINAL

    def test_there_is_still_no_cancellation(self):
        from moneycore.domain.transactions import TransactionStatus

        assert TransactionStatus.ALL == {
            'created', 'processing', 'unknown', 'succeeded', 'failed'
        }

    def test_holds_still_oversubscribe_nothing(self, funded_wallet):
        from moneycore.domain.errors import InsufficientAvailableBalanceError
        from moneycore.services.holds import create_hold

        create_hold(funded_wallet, 7_000)

        with pytest.raises(InsufficientAvailableBalanceError):
            create_hold(funded_wallet, 7_000)
