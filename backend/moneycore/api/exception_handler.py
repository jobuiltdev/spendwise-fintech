"""DRF exception handling for financial-core domain errors.

Registered globally as ``REST_FRAMEWORK['EXCEPTION_HANDLER']``, but deliberately
inert for everything except :class:`~moneycore.domain.errors.DomainError`:
anything else is handed straight to DRF's own handler and its response returned
untouched. No legacy endpoint raises a DomainError, so no existing response
shape, status code or body changes — which the existing suite proves.
"""

from __future__ import annotations

import logging

from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from moneycore.domain.errors import DomainError, ProviderUnavailableError
from spendwise.correlation import get_correlation_id

logger = logging.getLogger(__name__)


def domain_exception_handler(exc, context):
    """Render DomainErrors; defer everything else to DRF unchanged."""
    if not isinstance(exc, DomainError):
        return drf_exception_handler(exc, context)

    correlation_id = get_correlation_id()

    if isinstance(exc, ProviderUnavailableError) and exc.provider_detail:
        # The upstream's own wording is logged, never returned to the caller.
        logger.warning(
            'Provider unavailable (%s): %s', exc.code, exc.provider_detail
        )
    else:
        logger.info('Domain error (%s): %s', exc.code, exc.message)

    return Response(exc.as_payload(correlation_id), status=exc.http_status)
