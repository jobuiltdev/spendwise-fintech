"""The money core boundary is registered, importable, and carries no schema.

M0 creates the seam only. If any of these start failing because the app grew a
model, that is M1 work and the assertions here should be revisited deliberately
rather than deleted.
"""

import pytest
from django.apps import apps


class TestAppRegistration:
    def test_the_app_is_installed(self):
        assert apps.is_installed('moneycore')

    def test_the_app_config_is_the_project_one(self):
        config = apps.get_app_config('moneycore')

        assert config.name == 'moneycore'
        assert config.verbose_name == 'Money core'


class TestSchemaMatchesTheCurrentMilestone:
    """M0 asserted no models; M1 asserted its three; M2 adds the ledger.

    Each milestone supersedes the last deliberately, and the assertion stays
    tight rather than becoming loose: the app holds exactly the M1 and M2
    entities, so anything from M3 onward — a hold, a transfer, a transaction —
    fails the build the moment it appears.
    """

    M1_MODELS = {'FinancialCustomer', 'FinancialAccount', 'Wallet'}
    M2_MODELS = {'LedgerAccount', 'Journal', 'JournalEntry'}
    M3_MODELS = {'FundsHold'}

    def test_the_app_declares_exactly_the_models_through_m3(self):
        declared = {
            model.__name__ for model in apps.get_app_config('moneycore').get_models()
        }

        assert declared == self.M1_MODELS | self.M2_MODELS | self.M3_MODELS

    def test_no_m4_or_later_model_has_appeared(self):
        declared = {
            model.__name__.lower()
            for model in apps.get_app_config('moneycore').get_models()
        }
        forbidden = {
            'reservation', 'balance', 'balanceprojection',
            'availablebalance', 'transaction', 'transfer', 'recipient',
            'beneficiary', 'provider', 'webhook', 'reconciliation', 'fee',
            'settlement', 'idempotencykey', 'outboxmessage',
        }

        assert declared.isdisjoint(forbidden)

    def test_the_app_has_the_expected_migrations(self):
        from importlib.util import find_spec
        from pathlib import Path

        assert find_spec('moneycore.migrations') is not None

        migrations_dir = Path(find_spec('moneycore.migrations').origin).parent
        applied = sorted(
            path.stem for path in migrations_dir.glob('*.py') if path.stem != '__init__'
        )
        assert len(applied) == 3
        assert applied[0] == '0001_initial'

    def test_the_m1_migration_was_not_rewritten(self):
        """Committed migration history is append-only."""
        from importlib.util import find_spec
        from pathlib import Path

        migrations_dir = Path(find_spec('moneycore.migrations').origin).parent
        initial = (migrations_dir / '0001_initial.py').read_text(encoding='utf-8')

        # The ledger and holds arrived in later migrations, never by editing
        # this one.
        assert 'LedgerAccount' not in initial
        assert 'Journal' not in initial
        assert 'FundsHold' not in initial

    def test_the_m2_migration_was_not_rewritten(self):
        """Committed migration history stays append-only."""
        from importlib.util import find_spec
        from pathlib import Path

        migrations_dir = Path(find_spec('moneycore.migrations').origin).parent
        ledger = next(migrations_dir.glob('0002_*.py')).read_text(encoding='utf-8')

        assert 'FundsHold' not in ledger


class TestDomainImports:
    def test_money_is_importable_from_the_domain_package(self):
        from moneycore.domain.money import Money

        assert Money(1, 'NGN').minor_units == 1

    def test_errors_are_importable_from_the_domain_package(self):
        from moneycore.domain.errors import DomainError

        assert issubclass(DomainError, Exception)

    def test_the_exception_handler_is_importable(self):
        from moneycore.api.exception_handler import domain_exception_handler

        assert callable(domain_exception_handler)


class TestNotWiredIntoLegacyBehaviour:
    def test_no_legacy_module_imports_the_money_core(self):
        """Money must stay unused by the existing tracker at M0."""
        from pathlib import Path

        backend = Path(__file__).resolve().parents[2]
        legacy_apps = [
            'accounts', 'expenses', 'categories', 'budgets',
            'groups', 'analytics', 'recurring', 'reports',
        ]
        offenders = [
            str(path.relative_to(backend))
            for app in legacy_apps
            for path in (backend / app).rglob('*.py')
            if 'moneycore' in path.read_text(encoding='utf-8')
        ]

        assert offenders == []
