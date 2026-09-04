"""Domain errors for the financial core, and their API representation.

These exist for **new fintech code only**. Legacy SpendWise endpoints keep the
error shapes they already have — DRF's ``{"detail": ...}`` and the hand-rolled
``{"error": "..."}`` strings — and this milestone deliberately does not migrate
them.

Every domain error carries:

* a stable, machine-readable ``code`` a client can branch on;
* a human-readable ``message`` that is safe to show a customer;
* optional structured ``details``, used only where a client genuinely needs the
  extra fields;
* an explicit HTTP status.

A correlation id is attached by the exception handler rather than the exception
itself, so raising code never has to thread it through.

**Provider errors never leak.** ``ProviderUnavailableError`` keeps whatever a
provider said in a private attribute that is never serialised; the customer sees
a generic, safe message.
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """Base class for every financial-core error surfaced through the API."""

    code: str = 'domain_error'
    http_status: int = 400
    default_message: str = 'The request could not be completed.'

    def __init__(self, message: str | None = None, *, details: dict[str, Any] | None = None) -> None:
        self.message = message or self.default_message
        self.details = details or {}
        super().__init__(self.message)

    def as_payload(self, correlation_id: str | None = None) -> dict[str, Any]:
        """The response body for this error.

        ``details`` and ``correlation_id`` are omitted when absent rather than
        emitted as nulls, so the shape stays small and unambiguous.
        """
        error: dict[str, Any] = {'code': self.code, 'message': self.message}
        if self.details:
            error['details'] = self.details
        if correlation_id:
            error['correlation_id'] = correlation_id
        return {'error': error}


class ValidationError(DomainError):
    """The request was well-formed but its contents were not acceptable."""

    code = 'validation_error'
    http_status = 400
    default_message = 'The submitted data was not valid.'


class AuthorizationError(DomainError):
    """The caller is authenticated but not permitted to do this."""

    code = 'not_permitted'
    http_status = 403
    default_message = 'You do not have permission to perform this action.'


class NotFoundError(DomainError):
    """The referenced resource does not exist, or is not visible to the caller."""

    code = 'not_found'
    http_status = 404
    default_message = 'The requested resource was not found.'


class ConflictError(DomainError):
    """The request conflicts with the current state of the resource."""

    code = 'conflict'
    http_status = 409
    default_message = 'The request conflicts with the current state.'


class PreconditionFailedError(DomainError):
    """A domain precondition was not met, so the request was not attempted."""

    code = 'precondition_failed'
    http_status = 422
    default_message = 'A precondition for this request was not met.'


class ProviderUnavailableError(DomainError):
    """An upstream provider could not be reached or gave an unusable answer.

    The provider's own wording is captured for logs and never serialised: raw
    upstream text is not something a customer should ever be shown, and it is
    exactly where provider vocabulary would otherwise leak into the product.
    """

    code = 'provider_unavailable'
    http_status = 502
    default_message = 'That service is temporarily unavailable. Please try again.'

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        provider_detail: str | None = None,
    ) -> None:
        super().__init__(message, details=details)
        # Private on purpose: never included in as_payload().
        self._provider_detail = provider_detail

    @property
    def provider_detail(self) -> str | None:
        """The raw upstream detail, for logging only."""
        return self._provider_detail


# ---------------------------------------------------------------------------
# M1: customer / financial account / wallet
# ---------------------------------------------------------------------------


class FinancialAccountNotFoundError(NotFoundError):
    """The user has no financial account.

    Raised by services that need one. The read API does **not** use this: a user
    without a financial relationship is a normal, expected state (Tier 0), not
    an error, and treating it as one would make the activation path look like a
    failure.
    """

    code = 'financial_account_not_found'
    default_message = 'No financial account exists for this user.'


class InvalidAccountTransitionError(ConflictError):
    """The requested lifecycle move is not legal from the current state."""

    code = 'invalid_account_transition'
    default_message = 'That account status change is not allowed.'


class InvalidWalletTransitionError(ConflictError):
    """The requested wallet lifecycle move is not legal from the current state."""

    code = 'invalid_wallet_transition'
    default_message = 'That wallet status change is not allowed.'


class WalletAlreadyExistsError(ConflictError):
    """The account already holds a wallet in this currency."""

    code = 'wallet_already_exists'
    default_message = 'A wallet already exists for this currency.'
