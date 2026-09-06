"""What M8 must NOT do.

The first section is the whole point of the milestone: reconciliation detects
and records, and there is no path from it to moving money.
"""

import ast
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
    ProviderReconciliationItem,
    ProviderReconciliationRun,
    Transfer,
    Wallet,
)
from moneycore.services.ledger import open_ledger_account

pytestmark = pytest.mark.django_db


DESTINATION = VerifiedBankAccount(
    account_number='0123456789',
    bank_code='SIM-001',
    bank_name='Simulated First Bank',
    account_name='Ada Okafor',
)

#: Every service that can change what a customer's money is doing.
FINANCIAL_MUTATORS = {
    'succeed_transfer', 'fail_transfer', 'mark_transfer_unknown',
    'recover_provider_attempt', 'ingest_webhook',
    'execute_transfer', 'submit_transfer', 'prepare_transfer',
    'post_journal', 'reverse_journal',
    'create_hold', 'release_hold', 'expire_hold',
    'succeed_transaction', 'fail_transaction', 'mark_unknown',
    'start_processing', 'attach_hold', 'create_transaction',
}


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


def _service_source() -> str:
    from moneycore.services import reconciliation

    return Path(reconciliation.__file__).read_text(encoding='utf-8')


def _service_code() -> str:
    """The service with every docstring removed.

    The module explains at length what it never does, so a plain text
    search finds those very words in the prose forbidding them. These
    checks are about executable code.
    """
    tree = ast.parse(_service_source())

    for node in ast.walk(tree):
        if not isinstance(
            node,
            (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]

    return ast.unparse(ast.fix_missing_locations(tree))


@pytest.fixture
def settlement_account(db):
    return open_ledger_account(
        code='internal:m8-boundaries:NGN',
        name='Internal counterpart',
        account_type=LedgerAccountType.ASSET,
        currency='NGN',
    )


# ---------------------------------------------------------------------------
# The invariant the milestone exists for
# ---------------------------------------------------------------------------


class TestReconciliationNeverMutatesMoney:
    def test_no_financial_mutator_is_called(self):
        """AST walk over every call node, not a text search over prose."""
        tree = ast.parse(_service_code())

        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    called.add(node.func.attr)
                elif isinstance(node.func, ast.Name):
                    called.add(node.func.id)

        assert called.isdisjoint(FINANCIAL_MUTATORS), sorted(
            called & FINANCIAL_MUTATORS
        )

    def test_no_financial_mutator_is_imported(self):
        tree = ast.parse(_service_code())

        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    imported.add(alias.name)

        assert imported.isdisjoint(FINANCIAL_MUTATORS)
        assert 'provider_recovery' not in imported
        assert 'provider_execution' not in imported

    def test_it_imports_no_mutating_service_module(self):
        source = _service_code()

        for module in (
            'services.transfers', 'services.transactions', 'services.holds',
            'services.provider_recovery', 'services.provider_execution',
        ):
            assert module not in source, module

    def test_it_assigns_no_financial_status(self):
        """`status ==` is a comparison; `status =` on a financial row is not."""
        tree = ast.parse(_service_code())

        assigned = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign)
                    else [node.target]
                )
                for target in targets:
                    if isinstance(target, ast.Attribute):
                        assigned.add(target.attr)

        # `financial_transaction` is the locked row being handed to an
        # in-memory attempt object, and every `.save()` names the run.
        assert assigned <= {'status', 'completed_at', 'financial_transaction'}
        source = _service_code()
        assert 'txn.status =' not in source
        assert 'hold.status =' not in source
        assert 'transfer.status =' not in source

        saved = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'save'
        ]
        assert saved, 'the run itself is saved; an empty set would be vacuous'
        for call in saved:
            assert getattr(call.func.value, 'id', None) == 'run', ast.unparse(call)
            fields = {
                element.value
                for keyword in call.keywords if keyword.arg == 'update_fields'
                for element in keyword.value.elts
            }
            assert fields == {'status', 'completed_at'}, fields

    def test_it_creates_no_ledger_row(self):
        source = _service_code()

        for forbidden in (
            'Journal.objects.create', 'JournalEntry.objects.create',
            'Journal.objects', 'JournalEntry',
        ):
            assert forbidden not in source, forbidden

    def test_it_touches_no_hold(self):
        source = _service_code()

        for forbidden in (
            'FundsHold.objects', 'release_hold', 'hold.save',
        ):
            assert forbidden not in source, forbidden

    def test_it_writes_only_its_own_tables(self):
        """Every ORM write targets a reconciliation model."""
        tree = ast.parse(_service_code())

        written = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {'create', 'bulk_create', 'update'}
                and isinstance(node.func.value, ast.Attribute)
            ):
                owner = node.func.value.value
                written.add(getattr(owner, 'id', ''))

        assert written <= {
            'ProviderReconciliationRun', 'ProviderReconciliationItem'
        }, written

    def test_no_balance_is_adjusted(self):
        source = _service_code()

        for forbidden in (
            'adjust_balance', 'correct_balance', 'wallet.balance',
            'set_balance', 'compensat',
        ):
            assert forbidden not in source, forbidden

    def test_no_manual_resolution_entry_point_exists(self):
        from moneycore.services import reconciliation

        for forbidden in (
            'mark_matched', 'force_match', 'force_success', 'force_failure',
            'manual_reconcile', 'resolve_item', 'approve', 'override',
            'repair', 'fix',
        ):
            assert not hasattr(reconciliation, forbidden), forbidden

    def test_no_resubmission_path_exists(self):
        source = _service_code()

        for forbidden in ('retry', 'resubmit', 'submit', 'backoff'):
            assert forbidden not in source.lower().split('"""')[-1], forbidden

    def test_it_creates_no_provider_attempt(self):
        source = _service_code()

        assert 'ProviderExecutionAttempt.objects.create' not in source


class TestLocksAreObservationOnly:
    """M8 locks to read consistently, and locks nothing more than that.

    Narrows the original M8 claim that reconciliation took no locks at all.
    Under ``READ COMMITTED`` that left successive reads of one transfer able to
    straddle a concurrent resolution, so an observation lock is now taken — but
    only on the two rows that settle the reads, and never on a wallet.
    """

    def _locking_calls(self):
        """Every ``select_for_update()`` in the service, with its model."""
        tree = ast.parse(_service_code())
        found = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'select_for_update'
            ):
                owner = node.func.value
                # `<Model>.objects.select_for_update()`
                model = getattr(getattr(owner, 'value', None), 'id', None)
                found.append((model, node))
        # `ast.walk` is breadth-first; locking order is a source-order claim.
        return sorted(found, key=lambda pair: (pair[1].lineno, pair[1].col_offset))

    def test_it_locks_only_the_transaction_and_the_attempt(self):
        models = {model for model, _ in self._locking_calls()}

        assert models == {'FinancialTransaction', 'ProviderExecutionAttempt'}

    def test_it_never_locks_a_wallet_hold_journal_or_ledger_account(self):
        models = {model for model, _ in self._locking_calls()}

        assert models.isdisjoint({
            'Wallet', 'FundsHold', 'Journal', 'JournalEntry', 'LedgerAccount',
            'FinancialAccount', 'FinancialCustomer', 'Transfer',
        })

    def test_it_takes_no_lock_through_a_join(self):
        """``FOR UPDATE`` must name one table, so no locking query joins.

        ``select_for_update().select_related(...)`` would lock the joined rows
        too — silently acquiring the hold and journal locks this milestone
        exists to avoid.
        """
        for _, call in self._locking_calls():
            chained = ast.unparse(call)
            assert 'select_related' not in chained, chained

        source = _service_code()
        assert 'select_related' not in source

    def test_it_locks_the_transaction_before_the_attempt(self):
        """Source order, so the canonical wallet -> txn -> attempt holds."""
        order = [model for model, _ in self._locking_calls()]

        assert order == ['FinancialTransaction', 'ProviderExecutionAttempt']

    def test_it_borrows_no_mutator_lock_helper(self):
        source = _service_code()

        assert '_lock_wallets' not in source
        assert '_lock_transaction' not in source
        assert '_lock_for_transition' not in source

    def test_every_lock_is_ordered_by_primary_key(self):
        """Deterministic ordering is what keeps two runs from deadlocking."""
        tree = ast.parse(_service_code())
        ordered = 0
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'order_by'
            ):
                continue
            if 'select_for_update' not in ast.unparse(node):
                continue
            assert [a.value for a in node.args] == ['pk'], ast.unparse(node)
            ordered += 1

        assert ordered == 2, 'both locking queries must be ordered'

    def test_no_lock_query_is_ordered_by_provider_data(self):
        source = _service_code()

        for forbidden in (
            "order_by('provider_record_id')", "order_by('client_reference')",
            "order_by('provider_reference')", "order_by('observed_at')",
        ):
            assert forbidden not in source, forbidden


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


class TestScopeIsTransferLevel:
    def test_no_statement_or_settlement_model_exists(self):
        declared = {
            m.__name__.lower() for m in apps.get_app_config('moneycore').get_models()
        }

        assert declared.isdisjoint({
            'providerstatement', 'statement', 'statementline',
            'settlement', 'settlementrecord', 'settlementbatch',
            'chargeback', 'card', 'deposit', 'payout', 'feerecord',
        })

    def test_the_model_set_is_exactly_m8(self):
        declared = {m.__name__ for m in apps.get_app_config('moneycore').get_models()}

        assert declared == {
            'FinancialCustomer', 'FinancialAccount', 'Wallet',
            'LedgerAccount', 'Journal', 'JournalEntry',
            'FundsHold', 'FinancialTransaction', 'Transfer',
            'ProviderExecutionAttempt',
            'ProviderRecoveryEvidence', 'ProviderWebhookEvent',
            'ProviderReconciliationRun', 'ProviderReconciliationItem',
        }

    def test_no_balance_level_reconciliation_exists(self):
        from moneycore.services import reconciliation

        for forbidden in (
            'reconcile_balances', 'reconcile_account', 'reconcile_settlement',
            'import_statement', 'reconcile_batch',
        ):
            assert not hasattr(reconciliation, forbidden), forbidden

    def test_no_fee_or_tax_concept_appears(self):
        source = _service_code()

        for forbidden in ('fee_', 'tax', 'chargeback', 'interchange'):
            assert forbidden not in source.lower(), forbidden


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

    def test_no_cadence_is_defined(self):
        """Production reconciliation frequency stays open."""
        from moneycore.services import reconciliation

        for forbidden in (
            'RECONCILIATION_INTERVAL', 'DAILY', 'CRON', 'SCHEDULE',
            'RUN_AT', 'MAX_WINDOW', 'DEFAULT_WINDOW',
        ):
            assert not hasattr(reconciliation, forbidden), forbidden

    def test_no_sleeping_or_looping_construct_exists(self):
        lines = [
            line for line in _service_code().splitlines()
            if line.strip() and not line.strip().startswith('#')
        ]
        code = chr(10).join(lines).split('"""')[-1]

        for forbidden in ('while ', 'time.sleep', 'schedule('):
            assert forbidden not in code, forbidden


class TestNoRealProvider:
    def test_no_vendor_brand_appears_in_the_money_core(self):
        offenders = []
        for path in _production_sources():
            source = path.read_text(encoding='utf-8').lower()
            for brand in (
                'paystack', 'flutterwave', 'monnify', 'interswitch', 'kuda',
                'nomba', 'stripe', 'providus', 'wema',
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

    def test_no_credential_or_base_url_setting_exists(self):
        settings_source = (
            _backend_root() / 'spendwise' / 'settings.py'
        ).read_text(encoding='utf-8')

        for forbidden in (
            'PROVIDER_API_KEY', 'PROVIDER_SECRET', 'PROVIDER_BASE_URL',
            'RECONCILIATION_URL', 'STATEMENT_PATH', 'SFTP',
        ):
            assert forbidden not in settings_source, forbidden

    def test_the_provider_is_always_passed_in(self):
        import inspect

        from moneycore.services.reconciliation import reconcile_transfers

        assert 'provider' in inspect.signature(
            reconcile_transfers
        ).parameters


class TestNoCustomerOrOpsSurface:
    def test_no_reconciliation_route_is_registered(self, django_user_model):
        from rest_framework.test import APIClient

        user = django_user_model.objects.create_user('m8-api-probe')
        api = APIClient()
        api.force_authenticate(user=user)

        for path in (
            '/api/reconciliation/', '/api/reconciliations/', '/api/runs/',
            '/api/discrepancies/', '/api/reconciliation-items/',
            '/api/ops/reconciliation/',
        ):
            assert api.get(path).status_code == 404, f'{path} is routed'
            assert api.post(path, {}, format='json').status_code == 404, path

    def test_the_api_module_exposes_no_reconciliation_names(self):
        import moneycore.api.serializers as serializers
        import moneycore.api.views as views

        for module in (views, serializers):
            names = [n for n in dir(module) if not n.startswith('_')]
            assert not any(
                'Reconcil' in n or 'Discrepancy' in n or 'Run' in n
                for n in names
            ), f'{module.__name__} exposes M8 names: {names}'

    def test_the_m1_financial_account_response_is_unchanged(self, ledger_user):
        from rest_framework.test import APIClient

        from moneycore.services.provisioning import provision_financial_account

        provision_financial_account(ledger_user)
        api = APIClient()
        api.force_authenticate(user=ledger_user)

        body = api.get('/api/financial-account/').json()

        assert set(body) == {'customer', 'account', 'wallets'}
        assert set(body['wallets'][0]) == {'currency', 'status', 'created_at'}

    def test_no_admin_registration_was_added(self):
        admin = _moneycore_package() / 'admin.py'
        if not admin.exists():
            return

        source = admin.read_text(encoding='utf-8')
        for forbidden in (
            'ProviderReconciliationRun', 'ProviderReconciliationItem',
            'mark_matched', 'force_match',
        ):
            assert forbidden not in source, forbidden


class TestNoRawProviderData:
    def test_neither_model_stores_transport_data(self):
        for model in (ProviderReconciliationRun, ProviderReconciliationItem):
            names = {f.name for f in model._meta.get_fields()}
            assert names.isdisjoint({
                'raw_payload', 'payload', 'payload_json', 'body', 'headers',
                'response', 'statement_file', 'csv', 'export',
            }), model.__name__

    def test_the_service_never_reads_a_request_object(self):
        source = _service_code()

        for forbidden in ('request.body', 'request.META', 'json.loads'):
            assert forbidden not in source, forbidden

    def test_money_is_never_a_float(self):
        from django.db import models as dj

        for model in (ProviderReconciliationRun, ProviderReconciliationItem):
            for field in model._meta.get_fields():
                assert not isinstance(field, (dj.FloatField, dj.DecimalField))

        source = _service_code()
        assert 'float(' not in source
        assert 'Decimal' not in source


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
            Wallet, FundsHold, FinancialTransaction, Journal, Transfer,
            ProviderExecutionAttempt, ProviderReconciliationRun,
            ProviderReconciliationItem,
        ):
            names = {f.name for f in model._meta.get_fields()}
            assert names.isdisjoint(forbidden), model.__name__

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

    def test_recovery_still_never_submits(self):
        from moneycore.services import provider_recovery

        source = Path(provider_recovery.__file__).read_text(encoding='utf-8')
        tree = ast.parse(source)

        offenders = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'submit_transfer'
        ]

        assert offenders == []


class TestMigrations:
    def test_the_app_has_exactly_eight_migrations(self):
        migrations_dir = _moneycore_package() / 'migrations'
        applied = sorted(
            path.stem
            for path in migrations_dir.glob('*.py')
            if path.stem != '__init__'
        )

        assert len(applied) == 8
        assert applied[0] == '0001_initial'
        assert applied[-1].startswith('0008_')

    def test_the_earlier_migrations_were_not_rewritten(self):
        migrations_dir = _moneycore_package() / 'migrations'

        for pattern in (
            '0001_initial.py', '0002_*.py', '0003_*.py', '0004_*.py',
            '0005_*.py', '0006_*.py', '0007_*.py',
        ):
            path = next(migrations_dir.glob(pattern))
            source = path.read_text(encoding='utf-8')
            assert 'ProviderReconciliationRun' not in source
            assert 'ProviderReconciliationItem' not in source

    def test_the_new_migration_carries_no_data_operation(self):
        migrations_dir = _moneycore_package() / 'migrations'
        source = next(migrations_dir.glob('0008_*.py')).read_text(encoding='utf-8')

        for forbidden in ('RunPython', 'RunSQL', 'bulk_create', 'objects.create'):
            assert forbidden not in source

    def test_nothing_is_seeded(self, db):
        from moneycore.models import LedgerAccount

        assert LedgerAccount.objects.count() == 0
        assert ProviderReconciliationRun.objects.count() == 0
        assert ProviderReconciliationItem.objects.count() == 0


class TestNoMobileSurface:
    def test_the_mobile_app_knows_nothing_about_reconciliation(self):
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
                    'reconcile_transfers', 'ProviderReconciliation',
                    'reconciliation',
                )
            )
        ]

        assert offenders == []
