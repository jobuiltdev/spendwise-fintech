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


class TestNoFinancialSchema:
    def test_the_app_declares_no_models(self):
        """M0 adds the boundary, not the ledger."""
        assert list(apps.get_app_config('moneycore').get_models()) == []

    def test_the_app_has_no_migrations_package(self):
        from importlib.util import find_spec

        assert find_spec('moneycore.migrations') is None


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
