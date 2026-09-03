"""Smoke tests proving the test infrastructure itself works.

These assert nothing about product behaviour — they only confirm that Django is
configured, the app registry loads, and a test database can be created. Real
behaviour tests belong alongside the app they cover (see docs/TESTING.md).
"""

import pytest
from django.apps import apps
from django.conf import settings


def test_django_settings_are_configured():
    """Settings load without a .env file present."""
    assert settings.configured
    assert settings.SECRET_KEY


def test_project_apps_are_installed():
    """The app registry is ready and the project's own apps are loaded."""
    installed = {app.label for app in apps.get_app_configs()}
    expected = {
        "accounts",
        "analytics",
        "budgets",
        "categories",
        "expenses",
        "groups",
        "recurring",
        "reports",
    }
    assert expected <= installed


@pytest.mark.django_db
def test_test_database_is_usable():
    """A test database is created and can be queried."""
    from django.contrib.auth.models import User

    assert User.objects.count() == 0
