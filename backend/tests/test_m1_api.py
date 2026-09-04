"""The M1 read endpoint: ownership, truthful absence, and what it must not leak.

Lives here rather than under moneycore/tests/ because it uses the API client
fixtures in tests/conftest.py.
"""

import pytest
from django.contrib.auth.models import User

from moneycore.domain.lifecycle import AccountStatus
from moneycore.services.lifecycle import transition_account
from moneycore.services.provisioning import provision_financial_account

pytestmark = pytest.mark.django_db

URL = '/api/financial-account/'

BALANCE_WORDS = [
    'balance', 'available', 'ledger', 'spendable', 'pending_balance',
    'reserved', 'held', 'total', 'amount', 'minor_units', 'kobo',
]

PROVIDER_WORDS = [
    'provider', 'account_number', 'bank_code', 'nuban', 'iban',
    'virtual_account', 'external_id', 'bank_name',
]


class TestAuthentication:
    def test_anonymous_access_is_rejected(self, api):
        assert api.get(URL).status_code == 401

    def test_an_authenticated_user_gets_a_response(self, auth_client):
        assert auth_client.get(URL).status_code == 200


class TestUnprovisionedUser:
    """A Tier 0 user is a normal state, not an error and not a zero balance."""

    def test_absence_is_reported_as_a_success(self, auth_client):
        assert auth_client.get(URL).status_code == 200

    def test_the_relationship_is_explicitly_null(self, auth_client):
        body = auth_client.get(URL).json()

        assert body == {'customer': None, 'account': None, 'wallets': []}

    def test_no_fake_zero_appears_anywhere(self, auth_client):
        body = auth_client.get(URL).json()

        assert '0.00' not in str(body)
        assert '₦' not in str(body)

    def test_it_is_not_a_404(self, auth_client):
        """A 404 would make the activation path look like a failure."""
        assert auth_client.get(URL).status_code != 404


class TestProvisionedUser:
    @pytest.fixture
    def provisioned(self, user):
        return provision_financial_account(user)

    def test_it_reports_the_customer_status(self, auth_client, provisioned):
        body = auth_client.get(URL).json()

        assert body['customer']['status'] == 'active'

    def test_it_reports_the_account_as_pending_until_activated(self, auth_client, provisioned):
        body = auth_client.get(URL).json()

        assert body['account']['status'] == 'pending_activation'

    def test_it_reports_the_ngn_wallet(self, auth_client, provisioned):
        body = auth_client.get(URL).json()

        assert body['wallets'] == [
            {
                'currency': 'NGN',
                'status': 'active',
                'created_at': body['wallets'][0]['created_at'],
            }
        ]

    def test_it_reflects_an_activated_account(self, auth_client, user, provisioned):
        transition_account(provisioned.account, AccountStatus.ACTIVE)

        body = auth_client.get(URL).json()

        assert body['account']['status'] == 'active'
        assert body['account']['activated_at'] is not None

    def test_it_lists_several_wallets(self, auth_client, provisioned):
        from moneycore.services.lifecycle import open_wallet

        open_wallet(provisioned.account, 'USD')

        body = auth_client.get(URL).json()

        assert {w['currency'] for w in body['wallets']} == {'NGN', 'USD'}


class TestNoBalanceIsEverReturned:
    @pytest.fixture(autouse=True)
    def provisioned(self, user):
        return provision_financial_account(user)

    def test_the_wallet_object_has_exactly_the_expected_keys(self, auth_client):
        wallet = auth_client.get(URL).json()['wallets'][0]

        assert set(wallet) == {'currency', 'status', 'created_at'}

    @pytest.mark.parametrize('word', BALANCE_WORDS)
    def test_no_balance_shaped_key_appears(self, auth_client, word):
        body = auth_client.get(URL).json()

        def keys(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    yield key
                    yield from keys(value)
            elif isinstance(node, list):
                for item in node:
                    yield from keys(item)

        assert not any(word in key for key in keys(body))

    def test_the_response_contains_no_numeric_money_value(self, auth_client):
        body = auth_client.get(URL).json()

        assert all(
            not isinstance(value, (int, float))
            for value in body['wallets'][0].values()
        )


class TestNoProviderOrAccountNumberIsEverReturned:
    @pytest.fixture(autouse=True)
    def provisioned(self, user):
        return provision_financial_account(user)

    @pytest.mark.parametrize('word', PROVIDER_WORDS)
    def test_no_provider_shaped_key_appears(self, auth_client, word):
        assert word not in str(auth_client.get(URL).json())

    def test_no_digit_sequence_resembling_an_account_number(self, auth_client):
        import re

        body = str(auth_client.get(URL).json())

        # A 10-digit NUBAN would be the obvious fabrication risk.
        assert not re.search(r'\d{10}', body)


class TestOwnershipIsolation:
    def test_a_user_never_sees_another_users_relationship(
        self, auth_client, user, other_user
    ):
        provision_financial_account(other_user)

        body = auth_client.get(URL).json()

        assert body == {'customer': None, 'account': None, 'wallets': []}

    def test_each_user_sees_their_own_rows(self, auth_client, other_client, user, other_user):
        mine_rows = provision_financial_account(user)
        their_rows = provision_financial_account(other_user)
        # Distinguish the two relationships by a status only one of them has.
        transition_account(mine_rows.account, AccountStatus.ACTIVE)

        mine = auth_client.get(URL).json()
        theirs = other_client.get(URL).json()

        assert mine['account']['status'] == 'active'
        assert theirs['account']['status'] == 'pending_activation'
        assert their_rows.account.status == AccountStatus.PENDING_ACTIVATION

    def test_the_endpoint_exposes_no_identifier_to_tamper_with(self, auth_client, user):
        provision_financial_account(user)

        body = auth_client.get(URL).json()

        # No ids anywhere: ownership is structural, not a checked lookup.
        assert 'id' not in body['customer']
        assert 'id' not in body['account']
        assert 'id' not in body['wallets'][0]

    def test_there_is_no_lookup_by_id_route(self, auth_client, user):
        result = provision_financial_account(user)

        assert auth_client.get(f'{URL}{result.customer.pk}/').status_code == 404


class TestNoWriteSurface:
    @pytest.fixture(autouse=True)
    def provisioned(self, user):
        return provision_financial_account(user)

    @pytest.mark.parametrize('method', ['post', 'put', 'patch', 'delete'])
    def test_write_methods_are_not_allowed(self, auth_client, method):
        response = getattr(auth_client, method)(URL, {}, format='json')

        assert response.status_code == 405

    def test_a_client_cannot_activate_its_own_account(self, auth_client, user):
        auth_client.post(URL, {'account': {'status': 'active'}}, format='json')

        user.financial_customer.financial_account.refresh_from_db()
        assert user.financial_customer.financial_account.status == AccountStatus.PENDING_ACTIVATION

    def test_a_client_cannot_create_a_wallet(self, auth_client, user):
        auth_client.post(URL, {'wallets': [{'currency': 'USD'}]}, format='json')

        assert user.financial_customer.financial_account.wallets.count() == 1

    def test_there_is_no_router_crud_surface(self, auth_client):
        for path in ('/api/wallets/', '/api/financial-customers/', '/api/financial-accounts/'):
            assert auth_client.get(path).status_code == 404


class TestM0ConventionsStillHold:
    def test_the_response_still_carries_a_correlation_id(self, auth_client, user):
        provision_financial_account(user)

        response = auth_client.get(URL)

        assert response['X-Correlation-ID']

    def test_the_correlation_id_is_not_in_the_body(self, auth_client, user):
        provision_financial_account(user)

        assert 'correlation_id' not in auth_client.get(URL).json()

    def test_an_unauthenticated_request_keeps_drfs_legacy_shape(self, api):
        """The new endpoint does not change how DRF reports auth failures."""
        body = api.get(URL).json()

        assert 'detail' in body
        assert 'error' not in body
