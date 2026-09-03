"""Correlation ids: generation, echo, safety, and availability to logging."""

import logging
import re
import uuid

import pytest

from spendwise.correlation import (
    CORRELATION_ID_HEADER,
    CorrelationIdFilter,
    generate_correlation_id,
    get_correlation_id,
    resolve_correlation_id,
    set_correlation_id,
    reset_correlation_id,
)

pytestmark = pytest.mark.django_db

UUID_PATTERN = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
)


class TestGeneration:
    def test_generates_a_uuid(self):
        assert UUID_PATTERN.match(generate_correlation_id())

    def test_ids_are_unique(self):
        assert len({generate_correlation_id() for _ in range(100)}) == 100

    def test_the_id_carries_no_identifying_information(self):
        """A random UUID4 encodes no user, host, or timestamp."""
        parsed = uuid.UUID(generate_correlation_id())

        assert parsed.version == 4


class TestInboundIdSafety:
    @pytest.mark.parametrize(
        'inbound',
        ['abc12345', 'a' * 64, '11111111-2222-3333-4444-555555555555'],
    )
    def test_accepts_a_safe_inbound_id(self, inbound):
        assert resolve_correlation_id(inbound) == inbound

    @pytest.mark.parametrize(
        'inbound',
        [
            None,
            '',
            'short',                      # under the minimum length
            'a' * 65,                     # over the maximum length
            'has space',
            'newline\ninjected',
            'semi;colon',
            '<script>alert(1)</script>',
            'unicode-ᴜɴꜱᴀꜰᴇ',
        ],
    )
    def test_replaces_an_unsafe_or_absent_inbound_id(self, inbound):
        resolved = resolve_correlation_id(inbound)

        assert resolved != inbound
        assert UUID_PATTERN.match(resolved)


class TestRequestLifecycle:
    def test_every_response_carries_a_correlation_id(self, api):
        response = api.get('/api/expenses/')

        assert UUID_PATTERN.match(response[CORRELATION_ID_HEADER])

    def test_an_authenticated_response_carries_one_too(self, auth_client):
        response = auth_client.get('/api/expenses/')

        assert response[CORRELATION_ID_HEADER]

    def test_a_safe_inbound_id_is_echoed_back(self, api):
        response = api.get(
            '/api/expenses/', headers={'x-correlation-id': 'inbound-safe-1234'}
        )

        assert response[CORRELATION_ID_HEADER] == 'inbound-safe-1234'

    def test_an_unsafe_inbound_id_is_replaced_not_reflected(self, api):
        response = api.get(
            '/api/expenses/', headers={'x-correlation-id': 'bad value\nwith newline'}
        )

        assert response[CORRELATION_ID_HEADER] != 'bad value\nwith newline'
        assert UUID_PATTERN.match(response[CORRELATION_ID_HEADER])

    def test_separate_requests_get_separate_ids(self, api):
        first = api.get('/api/expenses/')[CORRELATION_ID_HEADER]
        second = api.get('/api/expenses/')[CORRELATION_ID_HEADER]

        assert first != second

    def test_an_error_response_still_carries_an_id(self, auth_client):
        response = auth_client.get('/api/expenses/999999/')

        assert response.status_code == 404
        assert response[CORRELATION_ID_HEADER]

    def test_the_id_does_not_leak_after_the_request(self, api):
        api.get('/api/expenses/')

        assert get_correlation_id() is None


class TestLoggingIntegration:
    def test_the_filter_injects_the_current_id(self):
        token = set_correlation_id('logging-test-id')
        record = logging.LogRecord('n', logging.INFO, __file__, 1, 'msg', None, None)
        try:
            CorrelationIdFilter().filter(record)
        finally:
            reset_correlation_id(token)

        assert record.correlation_id == 'logging-test-id'

    def test_the_filter_uses_a_placeholder_outside_a_request(self):
        record = logging.LogRecord('n', logging.INFO, __file__, 1, 'msg', None, None)

        CorrelationIdFilter().filter(record)

        assert record.correlation_id == '-'

    def test_the_filter_never_drops_a_record(self):
        record = logging.LogRecord('n', logging.INFO, __file__, 1, 'msg', None, None)

        assert CorrelationIdFilter().filter(record) is True


class TestAvailabilityToErrorHandling:
    def test_a_domain_error_payload_picks_up_the_active_id(self):
        from moneycore.api.exception_handler import domain_exception_handler
        from moneycore.domain.errors import ValidationError
        from rest_framework.test import APIRequestFactory

        token = set_correlation_id('handler-visible-id')
        try:
            response = domain_exception_handler(
                ValidationError(), {'request': APIRequestFactory().get('/')}
            )
        finally:
            reset_correlation_id(token)

        assert response.data['error']['correlation_id'] == 'handler-visible-id'
