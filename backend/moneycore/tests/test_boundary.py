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


class TestSchemaIsExactlyM1:
    """M0 asserted the app had no models at all. M1 deliberately supersedes that.

    The assertion is now tighter rather than weaker: the app holds exactly the
    three M1 entities and nothing else, so a ledger, journal, posting, hold,
    transfer or transaction model appearing here fails the build.
    """

    M1_MODELS = {'FinancialCustomer', 'FinancialAccount', 'Wallet'}

    def test_the_app_declares_exactly_the_m1_models(self):
        declared = {
            model.__name__ for model in apps.get_app_config('moneycore').get_models()
        }

        assert declared == self.M1_MODELS

    def test_no_m2_or_later_model_has_appeared(self):
        declared = {
            model.__name__.lower()
            for model in apps.get_app_config('moneycore').get_models()
        }
        forbidden = {
            'ledger', 'ledgeraccount', 'ledgerentry', 'journal', 'journalentry',
            'posting', 'hold', 'reservation', 'balance', 'balanceprojection',
            'transaction', 'transfer', 'recipient', 'beneficiary', 'provider',
            'webhook', 'reconciliation', 'fee', 'settlement',
        }

        assert declared.isdisjoint(forbidden)

    def test_the_app_has_exactly_one_migration(self):
        from importlib.util import find_spec
        from pathlib import Path

        assert find_spec('moneycore.migrations') is not None

        migrations_dir = Path(find_spec('moneycore.migrations').origin).parent
        applied = sorted(
            path.stem for path in migrations_dir.glob('*.py') if path.stem != '__init__'
        )
        assert applied == ['0001_initial']


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
