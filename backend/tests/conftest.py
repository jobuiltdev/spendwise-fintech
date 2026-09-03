"""Shared fixtures for the characterization suite.

These tests describe how SpendWise behaves *today*, so that the fintech
evolution can change things deliberately rather than by accident. They exercise
the public API and model boundaries rather than internals.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from budgets.models import Budget
from categories.models import Category
from expenses.models import Expense, PaymentMethod


@pytest.fixture(autouse=True)
def fast_password_hashing(settings):
    """Use a cheap hasher for the whole suite.

    The default PBKDF2 hasher is deliberately slow, which is right in
    production and pure overhead across hundreds of test users. Test-only —
    settings.py is untouched.
    """
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@pytest.fixture
def api():
    """An unauthenticated API client."""
    return APIClient()


@pytest.fixture
def make_user(db):
    """Create a user. Profile and UserPreference arrive via post_save signal."""
    def _make(username="alice", password="correct-horse-battery", **extra):
        return User.objects.create_user(username=username, password=password, **extra)

    return _make


@pytest.fixture
def user(make_user):
    return make_user("alice", email="alice@example.com")


@pytest.fixture
def other_user(make_user):
    return make_user("bob", email="bob@example.com")


@pytest.fixture
def auth_client(user):
    """API client authenticated as `user`, bypassing the token endpoints.

    Token issuance itself is characterized in test_auth.py; everywhere else the
    interesting behaviour is the resource, not the login.
    """
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def other_client(other_user):
    client = APIClient()
    client.force_authenticate(user=other_user)
    return client


@pytest.fixture
def make_category(db):
    def _make(owner, name="Food", **extra):
        return Category.objects.create(user=owner, name=name, **extra)

    return _make


@pytest.fixture
def category(user, make_category):
    return make_category(user, "Food")


@pytest.fixture
def make_expense(db):
    def _make(owner, amount="10.00", when=None, **extra):
        return Expense.objects.create(
            user=owner,
            amount=Decimal(amount),
            description=extra.pop("description", "Lunch"),
            date=when or date(2024, 1, 15),
            **extra,
        )

    return _make


@pytest.fixture
def make_payment_method(db):
    def _make(owner, name="Cash", **extra):
        return PaymentMethod.objects.create(user=owner, name=name, **extra)

    return _make


@pytest.fixture
def make_budget(db):
    def _make(owner, cat, amount="100.00", start=None, **extra):
        return Budget.objects.create(
            user=owner,
            category=cat,
            amount=Decimal(amount),
            start_date=start or date(2024, 1, 1),
            **extra,
        )

    return _make
