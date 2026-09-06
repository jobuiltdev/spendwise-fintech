"""What M7 must NOT have introduced.

The first section is the one that matters: there is no path from recovery to
submitting a transfer. Everything else is the usual milestone hygiene.
"""

import ast
from pathlib import Path

import pytest
from django.apps import apps

from moneycore.domain.ledger import LedgerAccountType
from moneycore.domain.transfers import VerifiedBankAccount
from moneycore.models import (
    FinancialTransaction,
    Journal,
    ProviderExecutionAttempt,
    ProviderRecoveryEvidence,
    ProviderWebhookEvent,
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


def _recovery_source() -> str:
    from moneycore.services import provider_recovery

    return Path(provider_recovery.__file__).read_text(encoding='utf-8')


# ---------------------------------------------------------------------------
# The non-negotiable invariant
# ---------------------------------------------------------------------------


class TestRecoveryNeverSubmits:
    """Zero transfer-submission path. Proved several independent ways."""

    def test_no_call_to_submit_transfer_exists_in_the_recovery_module(self):
        """AST walk: every call node, not a text search over prose."""
        tree = ast.parse(_recovery_source())

        offenders = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {
                'submit_transfer', 'execute_transfer', 'submit', 'resubmit',
            }
        ]

        assert offenders == []

    def test_no_bare_submission_function_is_called_either(self):
        """Covers a plain `submit_transfer(...)` as well as `x.submit_transfer(...)`."""
        tree = ast.parse(_recovery_source())

        offenders = [
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {
                'submit_transfer', 'execute_transfer', 'submit', 'resubmit',
            }
        ]

        assert offenders == []

    def test_the_recovery_module_imports_nothing_that_submits(self):
        tree = ast.parse(_recovery_source())

        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported.add(alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)

        assert 'execute_transfer' not in imported
        assert 'provider_execution' not in imported
        assert not any('submit' in name for name in imported)

    def test_the_recovery_protocol_has_no_submission_method(self):
        from moneycore.domain.recovery import TransferRecoveryProvider

        assert not hasattr(TransferRecoveryProvider, 'submit_transfer')

    def test_no_retry_or_backoff_construct_exists(self):
        """Checked against code, not the docstrings that forbid them."""
        lines = [
            line for line in _recovery_source().splitlines()
            if line.strip() and not line.strip().startswith('#')
        ]
        code = chr(10).join(lines)
        body = code.split('"""')[-1]

        for forbidden in (
            'while ', 'time.sleep', 'for attempt in range', 'backoff',
            'reschedule(',
        ):
            assert forbidden not in body, forbidden

    def test_recovery_creates_no_second_attempt(self):
        tree = ast.parse(_recovery_source())

        offenders = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'create'
            and isinstance(node.func.value, ast.Attribute)
            and getattr(node.func.value.value, 'id', '') == (
                'ProviderExecutionAttempt'
            )
        ]

        assert offenders == []

    def test_no_recovery_path_produces_an_extra_attempt(
        self, funded_wallet, settlement_account
    ):
        from moneycore.providers.simulator import (
            SimulatorTransferProvider,
            StatusScenario,
            TransferScenario,
        )
        from moneycore.services.provider_execution import (
            execute_transfer,
            provider_attempt_for,
        )
        from moneycore.services.provider_recovery import recover_provider_attempt

        transfer = prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key='no-second'
        )
        execute_transfer(
            transfer,
            provider=SimulatorTransferProvider(
                transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
            ),
            counterpart_account=settlement_account,
        )
        provider = SimulatorTransferProvider(
            status_scenario=StatusScenario.UNRESOLVED
        )

        for _ in range(3):
            recover_provider_attempt(
                provider_attempt_for(transfer),
                provider=provider,
                counterpart_account=settlement_account,
            )

        assert ProviderExecutionAttempt.objects.count() == 1
        assert provider.submit_call_count == 0


# ---------------------------------------------------------------------------
# No direct financial mutation
# ---------------------------------------------------------------------------


class TestRecoveryUsesTheExistingServices:
    def test_it_never_assigns_a_transaction_status(self):
        """AST, not text: `status ==` is a comparison, `status =` is the sin.

        The only attribute this module may assign is a webhook receipt's own
        `processing_status`, which is our handling of a delivery rather than
        any financial state.
        """
        tree = ast.parse(_recovery_source())

        assigned = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for target in targets:
                    if isinstance(target, ast.Attribute):
                        assigned.add(target.attr)

        assert 'status' not in assigned
        assert assigned <= {'processing_status', 'processed_at'}

    def test_it_never_creates_a_journal(self):
        source = _recovery_source()

        assert 'Journal.objects' not in source
        assert 'post_journal' not in source

    def test_it_never_releases_a_hold(self):
        source = _recovery_source()

        assert 'release_hold' not in source
        assert 'FundsHold' not in source

    def test_it_resolves_through_m4_m5_only(self):
        source = _recovery_source()

        assert 'succeed_transfer' in source
        assert 'fail_transfer' in source

    def test_it_never_reverses_a_journal(self):
        source = _recovery_source()

        for forbidden in (
            'reverse_journal', 'reverses', 'compensat', 'adjust_balance',
        ):
            assert forbidden not in source, forbidden


# ---------------------------------------------------------------------------
# No M8+ concepts
# ---------------------------------------------------------------------------


class TestNoReconciliation:
    def test_no_reconciliation_or_settlement_model_exists(self):
        declared = {
            m.__name__.lower() for m in apps.get_app_config('moneycore').get_models()
        }

        assert declared.isdisjoint({
            'reconciliation', 'reconciliationrecord', 'settlement',
            'settlementrecord', 'settlementbatch', 'providerstatement',
            'statement', 'outboxmessage',
        })

    def test_no_reconciliation_module_exists(self):
        names = {path.stem for path in _moneycore_package().rglob('*.py')}

        assert names.isdisjoint({
            'reconciliation', 'reconcile', 'settlement', 'statements', 'outbox',
        })

    def test_no_balance_comparison_exists(self):
        from moneycore.services import provider_recovery

        for forbidden in (
            'reconcile', 'compare_ledger', 'import_statement',
            'settlement_report', 'daily_sweep', 'adjust',
        ):
            assert not hasattr(provider_recovery, forbidden), forbidden

    def test_the_model_set_is_exactly_m7(self):
        declared = {m.__name__ for m in apps.get_app_config('moneycore').get_models()}

        assert declared == {
            'FinancialCustomer', 'FinancialAccount', 'Wallet',
            'LedgerAccount', 'Journal', 'JournalEntry',
            'FundsHold', 'FinancialTransaction', 'Transfer',
            'ProviderExecutionAttempt',
            'ProviderRecoveryEvidence', 'ProviderWebhookEvent',
        }


class TestNoSchedulerOrQueue:
    def test_no_celery_or_redis_is_imported(self):
        offenders = []
        for path in _production_sources():
            source = path.read_text(encoding='utf-8')
            for marker in (
                'import celery', 'from celery', 'shared_task', 'apply_async',
                'import redis', 'from redis', '.delay(',
            ):
                if marker in source:
                    offenders.append(f'{path.name}: {marker}')

        assert offenders == []

    def test_no_dependency_was_added(self):
        requirements = (
            _backend_root() / 'requirements.txt'
        ).read_text(encoding='utf-8').lower()

        for package in ('celery', 'redis', 'httpx', 'kombu', 'aiohttp'):
            assert package not in requirements

    def test_no_polling_cadence_is_defined(self):
        """Production polling frequency stays open."""
        from moneycore.services import provider_recovery

        for forbidden in (
            'POLL_INTERVAL', 'RECOVERY_INTERVAL', 'SWEEP_INTERVAL',
            'MAX_ATTEMPTS', 'RETRY_DELAY', 'schedule',
        ):
            assert not hasattr(provider_recovery, forbidden), forbidden

    def test_the_sweep_helper_only_reads(self):
        """It lists what needs looking at; it does not act."""
        import inspect

        from moneycore.services import provider_recovery

        source = inspect.getsource(provider_recovery.attempts_awaiting_recovery)

        assert 'filter' in source
        assert 'recover_provider_attempt' not in source


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

    def test_no_http_client_is_imported(self):
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

    def test_no_production_credential_setting_exists(self):
        settings_source = (
            _backend_root() / 'spendwise' / 'settings.py'
        ).read_text(encoding='utf-8')

        for forbidden in (
            'PROVIDER_API_KEY', 'PROVIDER_SECRET', 'PROVIDER_BASE_URL',
            'PROVIDER_TIMEOUT', 'WEBHOOK_SECRET', 'SIGNING_KEY',
        ):
            assert forbidden not in settings_source, forbidden

    def test_the_provider_is_always_passed_in(self):
        import inspect

        from moneycore.services.provider_recovery import (
            ingest_webhook,
            recover_provider_attempt,
        )

        for function in (recover_provider_attempt, ingest_webhook):
            assert 'provider' in inspect.signature(function).parameters


class TestNoCustomerApi:
    def test_no_recovery_or_webhook_route_is_registered(self, django_user_model):
        from rest_framework.test import APIClient

        user = django_user_model.objects.create_user('m7-api-probe')
        api = APIClient()
        api.force_authenticate(user=user)

        for path in (
            '/api/provider-webhooks/', '/api/provider-webhooks/simulator/',
            '/api/webhooks/', '/api/webhook/', '/api/callbacks/',
            '/api/transfers/1/retry/', '/api/transfers/1/resolve/',
            '/api/transfers/1/refresh/', '/api/recovery/', '/api/evidence/',
        ):
            assert api.get(path).status_code == 404, f'{path} is routed'
            assert api.post(path, {}, format='json').status_code == 404, path

    def test_the_api_module_exposes_no_recovery_names(self):
        import moneycore.api.serializers as serializers
        import moneycore.api.views as views

        for module in (views, serializers):
            names = [n for n in dir(module) if not n.startswith('_')]
            assert not any(
                'Recovery' in n or 'Webhook' in n or 'Evidence' in n
                or 'Retry' in n or 'Resolve' in n
                for n in names
            ), f'{module.__name__} exposes M7 names: {names}'

    def test_the_m1_financial_account_response_is_unchanged(self, ledger_user):
        from rest_framework.test import APIClient

        from moneycore.services.provisioning import provision_financial_account

        provision_financial_account(ledger_user)
        api = APIClient()
        api.force_authenticate(user=ledger_user)

        body = api.get('/api/financial-account/').json()

        assert set(body) == {'customer', 'account', 'wallets'}
        assert set(body['wallets'][0]) == {'currency', 'status', 'created_at'}

    def test_no_manual_operations_action_exists(self):
        """Mark-successful, force-fail and friends are M11, not M7."""
        from moneycore.services import provider_recovery

        for forbidden in (
            'mark_successful', 'mark_failed', 'force_success', 'force_fail',
            'release_funds', 'adjust_balance', 'manual_resolve', 'override',
        ):
            assert not hasattr(provider_recovery, forbidden), forbidden

    def test_no_admin_registration_was_added(self):
        admin = _moneycore_package() / 'admin.py'
        if not admin.exists():
            return

        source = admin.read_text(encoding='utf-8')
        for forbidden in (
            'ProviderRecoveryEvidence', 'ProviderWebhookEvent',
            'mark_successful', 'mark_failed',
        ):
            assert forbidden not in source, forbidden


class TestNoRawProviderData:
    def test_neither_model_stores_transport_data(self):
        for model in (ProviderRecoveryEvidence, ProviderWebhookEvent):
            names = {f.name for f in model._meta.get_fields()}
            assert names.isdisjoint({
                'raw_payload', 'payload_json', 'response_json', 'body',
                'headers', 'authorization', 'signature', 'secret', 'api_key',
                'request_body', 'http_status', 'url', 'cookies',
            }), model.__name__

    def test_the_service_never_touches_a_request_object(self):
        source = _recovery_source()

        for forbidden in ('request.body', 'request.META', 'request.headers'):
            assert forbidden not in source, forbidden

    def test_no_payload_digest_was_added_without_a_reason(self):
        """Not added: the provider event id is the idempotency key."""
        for model in (ProviderRecoveryEvidence, ProviderWebhookEvent):
            names = {f.name for f in model._meta.get_fields()}
            assert 'payload_digest' not in names
            assert 'body_sha256' not in names


# ---------------------------------------------------------------------------
# Earlier milestones
# ---------------------------------------------------------------------------


class TestEarlierMilestonesUntouched:
    def test_no_model_stores_a_balance(self):
        forbidden = {
            'balance', 'balance_minor', 'available_balance', 'held_balance',
            'reserved_balance', 'spendable_balance', 'cached_balance',
        }

        for model in (
            Wallet, FinancialTransaction, Journal, Transfer,
            ProviderExecutionAttempt, ProviderRecoveryEvidence,
            ProviderWebhookEvent,
        ):
            names = {f.name for f in model._meta.get_fields()}
            assert names.isdisjoint(forbidden), model.__name__

    def test_the_transfer_gained_no_provider_field(self):
        concrete = {f.name for f in Transfer._meta.get_fields() if f.concrete}

        assert concrete.isdisjoint({
            'provider', 'provider_reference', 'provider_status', 'status',
            'recovery_status', 'webhook_id',
        })

    def test_the_financial_transaction_gained_no_provider_field(self):
        concrete = {
            f.name for f in FinancialTransaction._meta.get_fields() if f.concrete
        }

        assert concrete.isdisjoint({
            'provider', 'provider_reference', 'provider_status',
            'recovery_status', 'evidence',
        })

    def test_there_is_still_no_cancellation(self):
        from moneycore.domain.transactions import TransactionStatus

        assert TransactionStatus.ALL == {
            'created', 'processing', 'unknown', 'succeeded', 'failed'
        }

    def test_unknown_is_still_not_terminal(self):
        from moneycore.domain.transactions import TransactionStatus

        assert TransactionStatus.UNKNOWN not in TransactionStatus.TERMINAL

    def test_the_ledger_still_has_no_default_wallet_classification(self):
        from moneycore.services import ledger

        assert not hasattr(ledger, 'WALLET_LEDGER_ACCOUNT_TYPE')

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

    def test_no_signal_receiver_was_added(self):
        offenders = [
            path.name
            for path in _production_sources()
            if any(
                marker in path.read_text(encoding='utf-8')
                for marker in ('@receiver', '.connect(', 'post_save', 'pre_save')
            )
        ]

        assert offenders == []


class TestMigrations:
    def test_the_app_has_exactly_seven_migrations(self):
        migrations_dir = _moneycore_package() / 'migrations'
        applied = sorted(
            path.stem
            for path in migrations_dir.glob('*.py')
            if path.stem != '__init__'
        )

        assert len(applied) == 7
        assert applied[0] == '0001_initial'
        assert applied[-1].startswith('0007_')

    def test_the_earlier_migrations_were_not_rewritten(self):
        migrations_dir = _moneycore_package() / 'migrations'

        for pattern in (
            '0001_initial.py', '0002_*.py', '0003_*.py', '0004_*.py',
            '0005_*.py', '0006_*.py',
        ):
            path = next(migrations_dir.glob(pattern))
            source = path.read_text(encoding='utf-8')
            assert 'ProviderRecoveryEvidence' not in source
            assert 'ProviderWebhookEvent' not in source

    def test_the_new_migration_carries_no_data_operation(self):
        migrations_dir = _moneycore_package() / 'migrations'
        source = next(migrations_dir.glob('0007_*.py')).read_text(encoding='utf-8')

        for forbidden in ('RunPython', 'RunSQL', 'bulk_create', 'objects.create'):
            assert forbidden not in source

    def test_no_provider_data_is_seeded(self, db):
        from moneycore.models import LedgerAccount

        assert LedgerAccount.objects.count() == 0
        assert ProviderWebhookEvent.objects.count() == 0


class TestNoMobileSurface:
    def test_the_mobile_app_knows_nothing_about_recovery(self):
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
                for marker in (
                    'recover_provider_attempt', 'ingest_webhook',
                    'ProviderRecoveryEvidence',
                )
            )
        ]

        assert offenders == []


@pytest.fixture
def settlement_account(db):
    return open_ledger_account(
        code='internal:m7-boundaries:NGN',
        name='Internal counterpart',
        account_type=LedgerAccountType.ASSET,
        currency='NGN',
    )
