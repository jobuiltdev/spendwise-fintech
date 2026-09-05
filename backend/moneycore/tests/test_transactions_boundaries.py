"""What M4 must NOT have introduced.

The transaction engine is a state machine and nothing else. It reaches no
network, defines no transfer, exposes no API, fires no signal and knows nothing
about the legacy expense tracker.
"""

from pathlib import Path

import pytest
from django.apps import apps

from moneycore.domain.transactions import TransactionDirection, TransactionStatus
from moneycore.models import FinancialTransaction, FundsHold, Journal, Wallet
from moneycore.services.holds import create_hold
from moneycore.services.transactions import (
    attach_hold,
    create_transaction,
    start_processing,
)

pytestmark = pytest.mark.django_db


def _moneycore_package() -> Path:
    import moneycore

    return Path(moneycore.__file__).parent


def _backend_root() -> Path:
    return _moneycore_package().parent


def _production_sources():
    """Every non-test module in the money core."""
    return [
        path
        for path in _moneycore_package().rglob('*.py')
        if 'tests' not in path.parts
    ]


# ---------------------------------------------------------------------------
# No provider, no network
# ---------------------------------------------------------------------------


class TestNoProviderIntegration:
    def test_the_transaction_carries_no_provider_field(self):
        names = {f.name for f in FinancialTransaction._meta.get_fields()}

        assert names.isdisjoint({
            'provider', 'provider_id', 'provider_name', 'provider_reference',
            'provider_status', 'provider_response', 'external_id',
            'external_reference', 'bank', 'bank_code', 'account_number',
            'virtual_account_number', 'nuban', 'iban', 'rail', 'scheme',
        })

    def test_the_failure_code_is_the_only_outcome_detail_stored(self):
        """A normalised internal code — never a provider payload or message."""
        names = {f.name for f in FinancialTransaction._meta.get_fields()}

        assert 'failure_code' in names
        assert names.isdisjoint({
            'failure_message', 'error_payload', 'provider_error',
            'raw_response', 'response_body', 'stack_trace',
        })

    def test_the_failure_code_field_is_short_enough_to_refuse_payloads(self):
        field = FinancialTransaction._meta.get_field('failure_code')

        assert field.max_length <= 64

    def test_no_http_client_is_imported_anywhere_in_the_money_core(self):
        offenders = []
        for path in _production_sources():
            source = path.read_text(encoding='utf-8')
            for marker in (
                'import requests', 'import httpx', 'import urllib3',
                'from requests', 'from httpx', 'http.client', 'aiohttp',
            ):
                if marker in source:
                    offenders.append(f'{path.name}: {marker}')

        assert offenders == []

    def test_no_provider_vocabulary_appears_in_the_transaction_modules(self):
        from moneycore.domain import transactions as domain
        from moneycore.services import transactions as service

        for module in (domain, service):
            source = Path(module.__file__).read_text(encoding='utf-8').lower()
            for word in ('paystack', 'flutterwave', 'monnify', 'anchor', 'nuban'):
                assert word not in source, f'{module.__name__} mentions {word}'

    def test_no_new_dependency_was_added(self):
        """No queue, scheduler or HTTP client arrived with the engine.

        ``requests`` is deliberately not listed: it predates this work as a
        transitive dependency of the existing Google auth support. What matters
        is that the money core never imports it, which the previous test
        asserts directly.
        """
        requirements = (
            _backend_root() / 'requirements.txt'
        ).read_text(encoding='utf-8').lower()

        for package in ('celery', 'redis', 'httpx', 'kombu', 'aiohttp'):
            assert package not in requirements


# ---------------------------------------------------------------------------
# No M5+ concepts
# ---------------------------------------------------------------------------


class TestNoProviderOrRecipientConcepts:
    def test_the_app_declares_exactly_the_models_through_m5(self):
        declared = {m.__name__ for m in apps.get_app_config('moneycore').get_models()}

        assert declared == {
            'FinancialCustomer', 'FinancialAccount', 'Wallet',
            'LedgerAccount', 'Journal', 'JournalEntry',
            'FundsHold',
            'FinancialTransaction',
            'Transfer',
        }

    def test_no_m6_or_later_model_exists(self):
        declared = {
            m.__name__.lower() for m in apps.get_app_config('moneycore').get_models()
        }

        assert declared.isdisjoint({
            'transferrequest', 'recipient', 'beneficiary',
            'provider', 'providerattempt', 'webhook', 'webhookevent',
            'reconciliation', 'fee', 'settlement', 'payout', 'card',
            'outboxmessage',
        })

    def test_the_transaction_names_no_counterparty(self):
        names = {f.name for f in FinancialTransaction._meta.get_fields()}

        assert names.isdisjoint({
            'recipient', 'beneficiary', 'counterparty', 'destination',
            'destination_wallet', 'source_wallet', 'payee', 'payer',
        })

    def test_direction_stays_two_valued(self):
        """Business operation types are M5's concern, not the engine's."""
        assert TransactionDirection.ALL == {'outgoing', 'incoming'}

    def test_no_operation_type_taxonomy_was_introduced(self):
        from moneycore.domain import transactions

        for forbidden in (
            'TransactionType', 'OperationType', 'TransferType',
            'PaymentType', 'TRANSACTION_TYPES',
        ):
            assert not hasattr(transactions, forbidden)

    def test_the_service_exposes_only_the_m4_surface(self):
        import inspect

        from moneycore.services import transactions

        public = sorted(
            name
            for name, value in vars(transactions).items()
            if inspect.isfunction(value)
            and value.__module__ == transactions.__name__
            and not name.startswith('_')
        )

        assert public == [
            'attach_hold', 'create_transaction', 'fail_transaction',
            'mark_unknown', 'start_processing', 'succeed_transaction',
        ]

    def test_no_transfer_orchestration_exists(self):
        from moneycore.services import transactions

        for forbidden in (
            'send_money', 'transfer', 'execute_transfer', 'initiate_transfer',
            'send_transaction', 'submit_to_provider', 'retry_transaction',
            'reconcile_transaction', 'create_draft', 'save_draft',
        ):
            assert not hasattr(transactions, forbidden), (
                f'transactions.{forbidden} exists'
            )

    def test_the_engine_invents_no_ledger_accounts(self):
        """Success posts what it is given; M5 decides which accounts a transfer uses."""
        source = Path(
            __import__('moneycore.services.transactions', fromlist=['x']).__file__
        ).read_text(encoding='utf-8')

        for forbidden in (
            'open_ledger_account', 'open_wallet_ledger_account',
            'LedgerAccountType',
        ):
            assert forbidden not in source

    def test_the_app_has_exactly_five_migrations(self):
        migrations_dir = _moneycore_package() / 'migrations'
        applied = sorted(
            path.stem
            for path in migrations_dir.glob('*.py')
            if path.stem != '__init__'
        )

        assert len(applied) == 5
        assert applied[0] == '0001_initial'

    def test_the_earlier_migrations_were_not_rewritten(self):
        migrations_dir = _moneycore_package() / 'migrations'

        for pattern in ('0001_initial.py', '0002_*.py', '0003_*.py'):
            path = next(migrations_dir.glob(pattern))
            assert 'FinancialTransaction' not in path.read_text(encoding='utf-8')

    def test_the_transaction_migration_was_not_rewritten_by_m5(self):
        migrations_dir = _moneycore_package() / 'migrations'
        engine = next(migrations_dir.glob('0004_*.py')).read_text(encoding='utf-8')

        assert 'Transfer' not in engine


class TestNoDraftOrAbandonmentConcept:
    """Locked architecture §8.5, enforced structurally.

    Backing out before execution is accepted does not create a financial
    transaction. A `FinancialTransaction` is not a persisted draft of a UI
    flow, and if persisted drafts are ever needed they belong to the
    transfer/product layer — which M5 must keep separate.
    """

    def test_the_status_vocabulary_has_no_cancellation_or_draft(self):
        assert TransactionStatus.ALL == {
            'created', 'processing', 'unknown', 'succeeded', 'failed'
        }

    def test_terminal_is_exactly_succeeded_and_failed(self):
        assert TransactionStatus.TERMINAL == {'succeeded', 'failed'}

    def test_no_draft_model_exists(self):
        declared = {
            m.__name__.lower() for m in apps.get_app_config('moneycore').get_models()
        }

        assert declared.isdisjoint({
            'transactiondraft', 'transferdraft', 'draft', 'pendingtransfer',
            'transactionintent',
        })

    def test_the_database_refuses_a_cancelled_status(self, funded_wallet):
        """Not merely an application rule — the check constraint rejects it."""
        from django.db import IntegrityError, transaction as db_transaction

        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                FinancialTransaction.objects.create(
                    wallet=funded_wallet,
                    direction=TransactionDirection.OUTGOING,
                    amount_minor=1_000,
                    currency=funded_wallet.currency,
                    status='cancelled',
                    idempotency_key='rejected-cancelled',
                )

    def test_no_cancellation_vocabulary_survives_anywhere_in_the_money_core(self):
        offenders = [
            path.name
            for path in _production_sources()
            if 'CANCELLED' in path.read_text(encoding='utf-8')
            or 'def cancel' in path.read_text(encoding='utf-8')
        ]

        assert offenders == []


# ---------------------------------------------------------------------------
# No API surface
# ---------------------------------------------------------------------------


class TestNoCustomerFacingTransactionApi:
    def test_no_transaction_route_is_registered(self, django_user_model):
        from rest_framework.test import APIClient

        user = django_user_model.objects.create_user('m4-api-probe')
        api = APIClient()
        api.force_authenticate(user=user)

        for path in (
            '/api/transactions/', '/api/financial-transactions/',
            '/api/transfers/', '/api/send/', '/api/payments/',
            '/api/money/transactions/',
        ):
            assert api.get(path).status_code == 404, f'{path} is routed'

    def test_the_api_module_exposes_no_transaction_names(self):
        import moneycore.api.serializers as serializers
        import moneycore.api.views as views

        for module in (views, serializers):
            names = [n for n in dir(module) if not n.startswith('_')]
            assert not any(
                'Transaction' in n or 'Transfer' in n for n in names
            ), f'{module.__name__} exposes M4 names: {names}'

    def test_the_m1_financial_account_response_is_unchanged(self, ledger_user):
        from rest_framework.test import APIClient

        from moneycore.services.provisioning import provision_financial_account

        provision_financial_account(ledger_user)
        api = APIClient()
        api.force_authenticate(user=ledger_user)

        body = api.get('/api/financial-account/').json()

        assert set(body) == {'customer', 'account', 'wallets'}
        assert set(body['wallets'][0]) == {'currency', 'status', 'created_at'}

    def test_the_m1_response_leaks_nothing_about_a_live_transaction(
        self, funded_wallet, ledger_user
    ):
        from rest_framework.test import APIClient

        txn = create_transaction(
            funded_wallet, TransactionDirection.OUTGOING, 7_000,
            idempotency_key='api-leak-probe',
        )
        start_processing(attach_hold(txn, create_hold(funded_wallet, 7_000)))

        api = APIClient()
        api.force_authenticate(user=ledger_user)
        body = str(api.get('/api/financial-account/').json())

        for leaked in (
            'transaction', 'processing', 'api-leak-probe', '7000', '10000',
        ):
            assert leaked not in body.lower()


# ---------------------------------------------------------------------------
# No implicit behaviour
# ---------------------------------------------------------------------------


class TestNoSignalsOrSchedulers:
    def test_the_money_core_still_registers_no_signal_receivers(self):
        offenders = [
            path.name
            for path in _production_sources()
            if any(
                marker in path.read_text(encoding='utf-8')
                for marker in ('@receiver', '.connect(')
            )
        ]

        assert offenders == []

    def test_provisioning_creates_no_transaction(self, ledger_user):
        from moneycore.services.provisioning import provision_financial_account

        provision_financial_account(ledger_user)

        assert FinancialTransaction.objects.count() == 0

    def test_creating_a_hold_creates_no_transaction(self, funded_wallet):
        create_hold(funded_wallet, 1_000)

        assert FinancialTransaction.objects.count() == 0

    def test_posting_a_journal_creates_no_transaction(
        self, wallet_account, counterpart_account
    ):
        from moneycore.services.ledger import credit, debit, post_journal

        post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 1_000), credit(wallet_account, 1_000)],
        )

        assert FinancialTransaction.objects.count() == 0

    def test_creating_a_transaction_posts_nothing_and_reserves_nothing(
        self, funded_wallet
    ):
        journals, holds = Journal.objects.count(), FundsHold.objects.count()

        create_transaction(
            funded_wallet, TransactionDirection.OUTGOING, 1_000,
            idempotency_key='inert-intent',
        )

        assert Journal.objects.count() == journals
        assert FundsHold.objects.count() == holds

    def test_the_engine_creates_no_hold_of_its_own(self, funded_wallet):
        """Reserving funds is M3's job and stays an explicit caller decision."""
        from moneycore.services import transactions

        source = Path(transactions.__file__).read_text(encoding='utf-8')
        assert 'create_hold' not in source

    def test_no_expiry_scheduler_was_introduced(self):
        from moneycore.services import transactions

        for forbidden in (
            'expire_unknown', 'sweep_unknown', 'resolve_stale', 'reap',
            'run_periodic', 'schedule',
        ):
            assert not hasattr(transactions, forbidden)


# ---------------------------------------------------------------------------
# Separation from the legacy tracker
# ---------------------------------------------------------------------------


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

    def test_the_transaction_has_no_expense_relation(self):
        related = {
            f.related_model.__name__
            for f in FinancialTransaction._meta.get_fields()
            if f.related_model is not None
        }

        assert related == {'Wallet', 'FundsHold', 'Journal', 'Transfer'}
        assert 'Expense' not in related

    def test_creating_an_expense_creates_no_transaction(self, ledger_user):
        from datetime import date
        from decimal import Decimal

        from expenses.models import Expense

        Expense.objects.create(
            user=ledger_user, amount=Decimal('42.00'),
            description='Lunch', date=date(2024, 1, 1),
        )

        assert FinancialTransaction.objects.count() == 0

    def test_creating_a_transaction_creates_no_expense(self, funded_wallet):
        from expenses.models import Expense

        create_transaction(
            funded_wallet, TransactionDirection.OUTGOING, 1_000,
            idempotency_key='no-expense',
        )

        assert Expense.objects.count() == 0

    def test_failing_a_transaction_creates_no_expense(self, funded_wallet):
        from expenses.models import Expense
        from moneycore.services.transactions import fail_transaction

        txn = create_transaction(
            funded_wallet, TransactionDirection.OUTGOING, 1_000,
            idempotency_key='no-expense-2',
        )
        fail_transaction(txn, failure_code='limit_exceeded')

        assert Expense.objects.count() == 0


# ---------------------------------------------------------------------------
# No mobile changes
# ---------------------------------------------------------------------------


class TestNoMobileSurface:
    def test_the_mobile_app_knows_nothing_about_transactions(self):
        """M4 is a backend milestone. Nothing in the app was touched."""
        mobile = _backend_root().parent / 'mobile'
        if not mobile.exists():
            pytest.skip('No mobile workspace in this checkout.')

        offenders = [
            str(path)
            for pattern in ('**/*.ts', '**/*.tsx')
            for path in mobile.glob(pattern)
            if 'node_modules' not in path.parts
            and 'financialTransaction' in path.read_text(encoding='utf-8')
        ]

        assert offenders == []


# ---------------------------------------------------------------------------
# Earlier invariants still hold
# ---------------------------------------------------------------------------


class TestEarlierMilestonesUntouched:
    def test_no_model_stores_a_balance(self):
        forbidden = {
            'balance', 'balance_minor', 'available_balance', 'held_balance',
            'reserved_balance', 'spendable_balance', 'cached_balance',
        }

        for model in (Wallet, FundsHold, FinancialTransaction, Journal):
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
        from moneycore.services import ledger

        assert not hasattr(ledger, 'WALLET_LEDGER_ACCOUNT_TYPE')

    def test_holds_still_oversubscribe_nothing(self, funded_wallet):
        from moneycore.domain.errors import InsufficientAvailableBalanceError

        create_hold(funded_wallet, 7_000)

        with pytest.raises(InsufficientAvailableBalanceError):
            create_hold(funded_wallet, 7_000)

    def test_unknown_is_still_not_terminal(self):
        """The invariant the whole milestone rests on."""
        assert TransactionStatus.UNKNOWN not in TransactionStatus.TERMINAL
