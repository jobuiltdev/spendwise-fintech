"""Lifecycle transitions for accounts and wallets.

Transitions go through the service boundary. Direct status assignment is not a
supported way to move an account, and these tests describe the rules the service
enforces rather than the field's ability to hold a string.
"""

import pytest
from django.contrib.auth.models import User

from moneycore.domain.errors import (
    InvalidAccountTransitionError,
    InvalidWalletTransitionError,
    WalletAlreadyExistsError,
)
from moneycore.domain.lifecycle import (
    ACCOUNT_TRANSITIONS,
    WALLET_TRANSITIONS,
    AccountStatus,
    WalletStatus,
    can_transition_account,
    can_transition_wallet,
)
from moneycore.models import Wallet
from moneycore.services.lifecycle import open_wallet, transition_account, transition_wallet
from moneycore.services.provisioning import provision_financial_account

pytestmark = pytest.mark.django_db


@pytest.fixture
def account(db):
    return provision_financial_account(User.objects.create_user('alice')).account


class TestAccountStateSet:
    def test_the_states_are_exactly_the_four_m1_defined(self):
        assert AccountStatus.ALL == {'pending_activation', 'active', 'suspended', 'closed'}

    def test_restricted_is_deliberately_absent(self):
        """Restriction is capability-driven (M9), not an account lifecycle state."""
        assert 'restricted' not in AccountStatus.ALL

    def test_closed_is_terminal(self):
        assert ACCOUNT_TRANSITIONS[AccountStatus.CLOSED] == frozenset()

    def test_every_state_has_a_transition_rule(self):
        assert set(ACCOUNT_TRANSITIONS) == AccountStatus.ALL


class TestAllowedAccountTransitions:
    @pytest.mark.parametrize(
        'start,target',
        [
            (AccountStatus.PENDING_ACTIVATION, AccountStatus.ACTIVE),
            (AccountStatus.PENDING_ACTIVATION, AccountStatus.CLOSED),
            (AccountStatus.ACTIVE, AccountStatus.SUSPENDED),
            (AccountStatus.ACTIVE, AccountStatus.CLOSED),
            (AccountStatus.SUSPENDED, AccountStatus.ACTIVE),
            (AccountStatus.SUSPENDED, AccountStatus.CLOSED),
        ],
    )
    def test_the_rule_table_allows_it(self, start, target):
        assert can_transition_account(start, target)

    def test_a_pending_account_can_be_activated(self, account):
        transition_account(account, AccountStatus.ACTIVE)

        account.refresh_from_db()
        assert account.status == AccountStatus.ACTIVE

    def test_activation_stamps_the_time(self, account):
        transition_account(account, AccountStatus.ACTIVE)

        account.refresh_from_db()
        assert account.activated_at is not None

    def test_reactivation_after_suspension_keeps_the_original_activation_time(self, account):
        transition_account(account, AccountStatus.ACTIVE)
        account.refresh_from_db()
        first_activation = account.activated_at

        transition_account(account, AccountStatus.SUSPENDED)
        transition_account(account, AccountStatus.ACTIVE)

        account.refresh_from_db()
        assert account.activated_at == first_activation

    def test_closing_stamps_the_time(self, account):
        transition_account(account, AccountStatus.CLOSED)

        account.refresh_from_db()
        assert account.closed_at is not None

    def test_a_pending_account_can_be_closed_without_ever_activating(self, account):
        transition_account(account, AccountStatus.CLOSED)

        account.refresh_from_db()
        assert account.status == AccountStatus.CLOSED
        assert account.activated_at is None


class TestRejectedAccountTransitions:
    def test_a_closed_account_cannot_be_reactivated(self, account):
        transition_account(account, AccountStatus.CLOSED)

        with pytest.raises(InvalidAccountTransitionError):
            transition_account(account, AccountStatus.ACTIVE)

    def test_a_closed_account_cannot_be_suspended(self, account):
        transition_account(account, AccountStatus.CLOSED)

        with pytest.raises(InvalidAccountTransitionError):
            transition_account(account, AccountStatus.SUSPENDED)

    def test_a_pending_account_cannot_be_suspended(self, account):
        """There is nothing to suspend before the account is usable."""
        with pytest.raises(InvalidAccountTransitionError):
            transition_account(account, AccountStatus.SUSPENDED)

    def test_a_no_op_self_transition_is_refused(self, account):
        with pytest.raises(InvalidAccountTransitionError):
            transition_account(account, AccountStatus.PENDING_ACTIVATION)

    def test_an_unknown_status_is_refused(self, account):
        with pytest.raises(InvalidAccountTransitionError):
            transition_account(account, 'restricted')

    def test_a_refused_transition_leaves_the_row_untouched(self, account):
        with pytest.raises(InvalidAccountTransitionError):
            transition_account(account, 'nonsense')

        account.refresh_from_db()
        assert account.status == AccountStatus.PENDING_ACTIVATION

    def test_the_error_reports_both_states(self, account):
        with pytest.raises(InvalidAccountTransitionError) as excinfo:
            transition_account(account, AccountStatus.SUSPENDED)

        assert excinfo.value.details == {
            'current': AccountStatus.PENDING_ACTIVATION,
            'requested': AccountStatus.SUSPENDED,
        }

    def test_the_error_uses_the_m0_domain_shape(self, account):
        with pytest.raises(InvalidAccountTransitionError) as excinfo:
            transition_account(account, AccountStatus.SUSPENDED)

        payload = excinfo.value.as_payload('corr-1')
        assert payload['error']['code'] == 'invalid_account_transition'
        assert payload['error']['correlation_id'] == 'corr-1'
        assert excinfo.value.http_status == 409


class TestWalletLifecycle:
    def test_the_states_are_exactly_active_and_closed(self):
        assert WalletStatus.ALL == {'active', 'closed'}

    def test_a_wallet_is_not_independently_suspendable(self):
        """Suspension is an account-level concern in the current architecture."""
        assert 'suspended' not in WalletStatus.ALL

    def test_every_state_has_a_transition_rule(self):
        assert set(WALLET_TRANSITIONS) == WalletStatus.ALL

    def test_an_active_wallet_can_be_closed(self, account):
        wallet = account.wallets.get()

        transition_wallet(wallet, WalletStatus.CLOSED)

        wallet.refresh_from_db()
        assert wallet.status == WalletStatus.CLOSED

    def test_a_closed_wallet_cannot_be_reopened(self, account):
        wallet = account.wallets.get()
        transition_wallet(wallet, WalletStatus.CLOSED)

        with pytest.raises(InvalidWalletTransitionError):
            transition_wallet(wallet, WalletStatus.ACTIVE)

    def test_can_transition_wallet_matches_the_service(self):
        assert can_transition_wallet(WalletStatus.ACTIVE, WalletStatus.CLOSED)
        assert not can_transition_wallet(WalletStatus.CLOSED, WalletStatus.ACTIVE)

    def test_an_unknown_wallet_status_is_refused(self, account):
        with pytest.raises(InvalidWalletTransitionError):
            transition_wallet(account.wallets.get(), 'suspended')


class TestOpeningAdditionalWallets:
    def test_a_second_currency_can_be_opened(self, account):
        wallet = open_wallet(account, 'USD')

        assert wallet.currency == 'USD'
        assert account.wallets.count() == 2

    def test_a_duplicate_currency_is_refused(self, account):
        with pytest.raises(WalletAlreadyExistsError) as excinfo:
            open_wallet(account, 'NGN')

        assert excinfo.value.code == 'wallet_already_exists'
        assert excinfo.value.http_status == 409

    def test_a_wallet_cannot_be_opened_on_a_closed_account(self, account):
        transition_account(account, AccountStatus.CLOSED)

        with pytest.raises(InvalidAccountTransitionError):
            open_wallet(account, 'USD')

    def test_a_refused_open_creates_no_row(self, account):
        transition_account(account, AccountStatus.CLOSED)

        with pytest.raises(InvalidAccountTransitionError):
            open_wallet(account, 'USD')

        assert not Wallet.objects.filter(currency='USD').exists()

    def test_a_malformed_currency_is_refused_before_the_database(self, account):
        from django.core.exceptions import ValidationError as DjangoValidationError

        with pytest.raises(DjangoValidationError):
            open_wallet(account, 'ngn')

    def test_a_wallet_may_be_opened_on_a_pending_account(self, account):
        """Containers can exist before the account becomes usable."""
        assert account.status == AccountStatus.PENDING_ACTIVATION

        assert open_wallet(account, 'USD').currency == 'USD'
