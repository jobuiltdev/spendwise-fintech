"""Request correlation ids.

Every request gets an id, the id comes back on the response, and it is available
to logging and to error handling without being threaded through call signatures.
That is the whole scope: this is not distributed tracing, and there is no
sampling, propagation to outbound calls, or span model here.

The id is held in a ``ContextVar`` so domain code (which has no request object)
can read it, and reset after each request so ids never bleed between requests on
a reused worker thread.
"""

from __future__ import annotations

import logging
import re
import uuid
from contextvars import ContextVar
from typing import Final

CORRELATION_ID_HEADER: Final = 'X-Correlation-ID'

# An inbound id is echoed only when it is unmistakably safe: a bounded run of
# ASCII letters, digits and dashes. Anything else — punctuation, whitespace,
# control characters, something enormous — is discarded and replaced rather than
# reflected back into logs and response headers.
_SAFE_INBOUND_ID: Final = re.compile(r'^[A-Za-z0-9-]{8,64}$')

_correlation_id: ContextVar[str | None] = ContextVar('correlation_id', default=None)


def get_correlation_id() -> str | None:
    """The current request's correlation id, if one is set."""
    return _correlation_id.get()


def set_correlation_id(value: str | None):
    """Set the correlation id, returning the token needed to reset it."""
    return _correlation_id.set(value)


def reset_correlation_id(token) -> None:
    _correlation_id.reset(token)


def generate_correlation_id() -> str:
    """A fresh id.

    A random UUID4: it carries no user, account, host or timing information, so
    it is safe to log and to hand back to a client.
    """
    return str(uuid.uuid4())


def resolve_correlation_id(inbound: str | None) -> str:
    """Use a caller-supplied id when it is safe, otherwise generate one."""
    if inbound and _SAFE_INBOUND_ID.match(inbound):
        return inbound
    return generate_correlation_id()


class CorrelationIdMiddleware:
    """Attach a correlation id to every request and response.

    Sits at the top of the middleware stack so the id exists before anything
    else can log or fail.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        correlation_id = resolve_correlation_id(
            request.headers.get(CORRELATION_ID_HEADER)
        )
        request.correlation_id = correlation_id
        token = set_correlation_id(correlation_id)
        try:
            response = self.get_response(request)
            response[CORRELATION_ID_HEADER] = correlation_id
            return response
        finally:
            # Always reset, including when a view raises, so the id cannot leak
            # into the next request handled by this thread.
            reset_correlation_id(token)


class CorrelationIdFilter(logging.Filter):
    """Make ``%(correlation_id)s`` usable in any log format string."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id() or '-'
        return True
