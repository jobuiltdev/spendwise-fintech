"""M0 must not change any existing API response.

The domain exception handler is registered globally and the correlation
middleware runs on every request, so both are proved here to leave legacy
endpoints exactly as they were. The 182 characterization tests are the broader
proof; these assert the specific shapes M0 could plausibly have disturbed.
"""

import pytest

pytestmark = pytest.mark.django_db


class TestLegacyErrorShapesUnchanged:
    def test_an_unauthenticated_request_keeps_drfs_detail_shape(self, api):
        response = api.get('/api/expenses/')

        body = response.json()
        assert response.status_code == 401
        assert 'detail' in body
        assert 'error' not in body

    def test_a_serializer_validation_error_keeps_its_per_field_shape(self, auth_client):
        response = auth_client.post(
            '/api/expenses/',
            {'amount': '0', 'description': 'x', 'date': '2024-01-15'},
            format='json',
        )

        body = response.json()
        assert response.status_code == 400
        assert 'amount' in body
        assert 'error' not in body

    def test_a_custom_action_error_is_still_a_plain_string_under_error(self, auth_client):
        """accounts.LogoutView returns {"error": "<string>"}, not an object."""
        response = auth_client.post('/api/token/logout/', {}, format='json')

        assert response.status_code == 400
        assert isinstance(response.json()['error'], str)

    def test_a_missing_object_keeps_drfs_detail_shape(self, auth_client):
        response = auth_client.get('/api/expenses/999999/')

        assert response.status_code == 404
        assert 'detail' in response.json()

    def test_a_permission_denied_keeps_drfs_detail_shape(self, other_client, user, make_expense):
        theirs = make_expense(user)

        response = other_client.patch(
            f'/api/expenses/{theirs.id}/', {'amount': '1.00'}, format='json'
        )

        assert response.status_code == 404
        assert 'detail' in response.json()


class TestLegacySuccessResponsesUnchanged:
    def test_a_successful_list_body_is_untouched(self, auth_client, user, make_expense):
        make_expense(user, '10.00')

        body = auth_client.get('/api/expenses/').json()

        # Still DRF's paginated envelope, with no M0 additions.
        assert set(body) == {'count', 'next', 'previous', 'results'}

    def test_a_successful_detail_body_carries_no_m0_fields(self, auth_client, user, make_expense):
        expense = make_expense(user, '10.00')

        body = auth_client.get(f'/api/expenses/{expense.id}/').json()

        assert 'correlation_id' not in body
        assert 'error' not in body

    def test_correlation_id_is_a_header_not_a_body_field(self, auth_client, user, make_expense):
        """The id travels in the response header; bodies are unchanged."""
        make_expense(user, '10.00')

        response = auth_client.get('/api/expenses/')

        assert response['X-Correlation-ID']
        assert 'X-Correlation-ID' not in response.json()


class TestDatabaseConfiguration:
    def test_the_configured_engine_is_one_m0_supports(self, settings):
        engine = settings.DATABASES['default']['ENGINE']

        assert engine in {
            'django.db.backends.sqlite3',
            'django.db.backends.postgresql',
        }

    def test_queries_work_against_whichever_engine_is_configured(self, user, make_expense):
        """Same assertion on SQLite and PostgreSQL — that is the point."""
        from decimal import Decimal
        from expenses.models import Expense

        make_expense(user, '12.34')

        assert Expense.objects.get().amount == Decimal('12.34')
