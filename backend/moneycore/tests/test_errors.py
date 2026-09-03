"""Tests for the financial-core domain errors and their API rendering.

Pure unit tests — no database. Proof that the globally-registered handler leaves
legacy endpoint responses untouched lives in
``backend/tests/test_m0_legacy_compatibility.py``, where the API fixtures are.
"""

import pytest
from rest_framework.exceptions import NotFound as DRFNotFound
from rest_framework.test import APIRequestFactory

from moneycore.api.exception_handler import domain_exception_handler
from moneycore.domain.errors import (
    AuthorizationError,
    ConflictError,
    DomainError,
    NotFoundError,
    PreconditionFailedError,
    ProviderUnavailableError,
    ValidationError,
)


class TestErrorDefinitions:
    @pytest.mark.parametrize(
        'error_class,code,status',
        [
            (DomainError, 'domain_error', 400),
            (ValidationError, 'validation_error', 400),
            (AuthorizationError, 'not_permitted', 403),
            (NotFoundError, 'not_found', 404),
            (ConflictError, 'conflict', 409),
            (PreconditionFailedError, 'precondition_failed', 422),
            (ProviderUnavailableError, 'provider_unavailable', 502),
        ],
    )
    def test_each_error_has_an_explicit_code_and_http_status(
        self, error_class, code, status
    ):
        assert error_class.code == code
        assert error_class.http_status == status

    def test_codes_are_unique(self):
        codes = [
            cls.code
            for cls in (
                DomainError,
                ValidationError,
                AuthorizationError,
                NotFoundError,
                ConflictError,
                PreconditionFailedError,
                ProviderUnavailableError,
            )
        ]
        assert len(codes) == len(set(codes))

    def test_every_error_is_a_domain_error(self):
        for cls in (
            ValidationError,
            AuthorizationError,
            NotFoundError,
            ConflictError,
            PreconditionFailedError,
            ProviderUnavailableError,
        ):
            assert issubclass(cls, DomainError)

    def test_falls_back_to_a_safe_default_message(self):
        assert ValidationError().message == ValidationError.default_message

    def test_accepts_a_specific_message(self):
        assert ValidationError('Amount must be positive.').message == 'Amount must be positive.'


class TestPayload:
    def test_carries_the_code_and_message(self):
        payload = ValidationError('Amount must be positive.').as_payload()

        assert payload == {
            'error': {'code': 'validation_error', 'message': 'Amount must be positive.'}
        }

    def test_includes_structured_details_when_given(self):
        payload = ValidationError('Bad field.', details={'field': 'amount'}).as_payload()

        assert payload['error']['details'] == {'field': 'amount'}

    def test_omits_details_when_absent(self):
        assert 'details' not in ValidationError().as_payload()['error']

    def test_includes_the_correlation_id_when_given(self):
        payload = ValidationError().as_payload('abc-123')

        assert payload['error']['correlation_id'] == 'abc-123'

    def test_omits_the_correlation_id_when_absent(self):
        assert 'correlation_id' not in ValidationError().as_payload()['error']


class TestProviderErrorsDoNotLeak:
    def test_the_raw_provider_detail_is_never_serialised(self):
        error = ProviderUnavailableError(
            provider_detail='ACME-BANK 503 upstream_timeout account=99887766'
        )

        payload = error.as_payload('abc-123')

        serialised = str(payload)
        assert 'ACME-BANK' not in serialised
        assert '99887766' not in serialised

    def test_the_customer_sees_a_safe_generic_message(self):
        error = ProviderUnavailableError(provider_detail='raw upstream noise')

        assert error.as_payload()['error']['message'] == (
            'That service is temporarily unavailable. Please try again.'
        )

    def test_the_detail_is_still_available_for_logging(self):
        error = ProviderUnavailableError(provider_detail='raw upstream noise')

        assert error.provider_detail == 'raw upstream noise'


class TestExceptionHandler:
    def _context(self):
        return {'request': APIRequestFactory().get('/')}

    def test_renders_a_domain_error_with_its_own_status(self):
        response = domain_exception_handler(NotFoundError(), self._context())

        assert response.status_code == 404
        assert response.data['error']['code'] == 'not_found'

    def test_renders_the_message_and_details(self):
        error = ValidationError('Amount must be positive.', details={'field': 'amount'})

        response = domain_exception_handler(error, self._context())

        assert response.data['error']['message'] == 'Amount must be positive.'
        assert response.data['error']['details'] == {'field': 'amount'}

    @pytest.mark.parametrize(
        'error_class,status',
        [
            (ValidationError, 400),
            (AuthorizationError, 403),
            (NotFoundError, 404),
            (ConflictError, 409),
            (PreconditionFailedError, 422),
            (ProviderUnavailableError, 502),
        ],
    )
    def test_maps_every_error_to_its_http_status(self, error_class, status):
        response = domain_exception_handler(error_class(), self._context())

        assert response.status_code == status

    def test_delegates_non_domain_exceptions_to_drf(self):
        """A DRF exception keeps DRF's own shape, not the domain one."""
        response = domain_exception_handler(DRFNotFound(), self._context())

        assert response.status_code == 404
        assert 'detail' in response.data
        assert 'error' not in response.data

    def test_returns_none_for_unhandled_exceptions_as_drf_does(self):
        """An unhandled exception must still become a 500, not be swallowed."""
        assert domain_exception_handler(ValueError('boom'), self._context()) is None
