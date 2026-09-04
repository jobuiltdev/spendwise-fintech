"""Shared fixtures for the money-core tests."""

import pytest
from django.contrib.auth.models import User

from moneycore.domain.ledger import LedgerAccountType
from moneycore.services.ledger import open_ledger_account, open_wallet_ledger_account
from moneycore.services.provisioning import provision_financial_account


@pytest.fixture
def ledger_user(db):
    return User.objects.create_user('ledger-user')


@pytest.fixture
def wallet(ledger_user):
    return provision_financial_account(ledger_user).wallet


@pytest.fixture
def wallet_account(wallet):
    """A ledger account mapped to the customer wallet.

    The classification is passed explicitly because M2 takes no position on
    which class represents a customer wallet — that follows from a custody and
    accounting design still open. Liability is used here only so the fixture is
    concrete; no test may assert it is *the* wallet classification.
    """
    return open_wallet_ledger_account(wallet, account_type=LedgerAccountType.LIABILITY)


@pytest.fixture
def counterpart_account(db):
    """A neutral internal counterpart so postings have two legitimate sides.

    Debit-normal, deliberately generic. It asserts nothing about custody: it is
    not a bank, settlement, provider or trust account, and it exists only so
    tests can post real double-entry rather than a single-sided adjustment.
    """
    return open_ledger_account(
        code='internal:test-counterpart:NGN',
        name='Internal test counterpart',
        account_type=LedgerAccountType.ASSET,
        currency='NGN',
    )


@pytest.fixture
def make_ledger_account(db):
    def _make(code, account_type=LedgerAccountType.ASSET, currency='NGN', wallet=None):
        return open_ledger_account(
            code=code,
            name=f'Account {code}',
            account_type=account_type,
            currency=currency,
            wallet=wallet,
        )

    return _make
