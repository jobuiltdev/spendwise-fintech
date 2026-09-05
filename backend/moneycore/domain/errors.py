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


# ---------------------------------------------------------------------------
# M2: double-entry ledger
# ---------------------------------------------------------------------------
# Internal domain errors. The ledger has no customer-facing surface, so these
# describe bookkeeping faults to internal callers rather than to end users.


class LedgerError(DomainError):
    """Base for ledger faults, so callers can catch the whole family."""

    code = 'ledger_error'
    http_status = 409
    default_message = 'The ledger operation could not be completed.'


class LedgerUnbalancedError(LedgerError):
    """Debits do not equal credits, so the journal cannot be posted.

    Never absorbed, rounded away, or balanced with an invented suspense entry.
    """

    code = 'ledger_unbalanced'
    default_message = 'A journal must balance: debits must equal credits.'


class InvalidLedgerEntryError(LedgerError):
    """An entry is structurally unusable — bad amount, direction or account."""

    code = 'invalid_ledger_entry'
    http_status = 400
    default_message = 'The ledger entry is not valid.'


class LedgerCurrencyMismatchError(LedgerError):
    """An entry's account currency disagrees with the journal's currency.

    M2 journals are single-currency. There is no conversion here: FX is not
    implemented, and balancing across currencies would silently invent a rate.
    """

    code = 'ledger_currency_mismatch'
    default_message = 'Every entry must use the journal currency.'


class LedgerAccountClosedError(LedgerError):
    """The ledger account is closed and cannot take new postings."""

    code = 'ledger_account_closed'
    default_message = 'That ledger account is closed.'


class JournalAlreadyPostedError(LedgerError):
    """The journal is already posted; posted history is immutable."""

    code = 'journal_already_posted'
    default_message = 'That journal has already been posted.'


class JournalImmutableError(LedgerError):
    """An attempt was made to alter or delete posted financial history."""

    code = 'journal_immutable'
    default_message = 'Posted ledger history cannot be changed or removed.'


class JournalNotPostedError(LedgerError):
    """The operation requires a posted journal."""

    code = 'journal_not_posted'
    default_message = 'That journal has not been posted.'


class JournalAlreadyReversedError(LedgerError):
    """The journal already has a reversal; a second one would double-count."""

    code = 'journal_already_reversed'
    default_message = 'That journal has already been reversed.'


class WalletLedgerAccountNotFoundError(LedgerError):
    """The wallet has no ledger account, so it has no authoritative balance.

    Distinct from a balance of zero. "No ledger relationship exists yet" and
    "the ledger says zero" are different facts, and collapsing them would report
    a fabricated figure for a wallet that has never been mapped.
    """

    code = 'wallet_ledger_account_not_found'
    http_status = 404
    default_message = 'This wallet has no ledger account.'


# ---------------------------------------------------------------------------
# M3: holds and balance projection
# ---------------------------------------------------------------------------
# Internal domain errors. Holds have no customer-facing surface in M3, so these
# describe reservation faults to internal callers.


class HoldError(DomainError):
    """Base for hold faults, so callers can catch the whole family."""

    code = 'hold_error'
    http_status = 409
    default_message = 'The hold operation could not be completed.'


class InvalidHoldAmountError(HoldError):
    """The requested amount is not a usable positive integer minor-unit value."""

    code = 'invalid_hold_amount'
    http_status = 400
    default_message = 'A hold amount must be a positive whole number of minor units.'


class InsufficientAvailableBalanceError(HoldError):
    """The wallet does not have enough available balance to reserve.

    Available, not posted: funds already reserved by another hold are not
    available to reserve twice.
    """

    code = 'insufficient_available_balance'
    default_message = 'There is not enough available balance to place this hold.'


class HoldCurrencyMismatchError(HoldError):
    """The hold currency disagrees with its wallet's."""

    code = 'hold_currency_mismatch'
    default_message = 'A hold must use the wallet currency.'


class WalletNotHoldableError(HoldError):
    """The wallet or its ledger account cannot take a hold right now.

    Covers a closed wallet and a closed ledger account. This is not a capability
    policy — capability restrictions (canSend, canReceive, …) arrive with M9 and
    are deliberately not anticipated here.
    """

    code = 'wallet_not_holdable'
    default_message = 'This wallet cannot hold funds.'


class InvalidHoldTransitionError(HoldError):
    """The requested lifecycle move is not legal from the current state."""

    code = 'invalid_hold_transition'
    default_message = 'That hold status change is not allowed.'


class HoldNotActiveError(HoldError):
    """The operation requires an active hold, and this one is terminal."""

    code = 'hold_not_active'
    default_message = 'That hold is no longer active.'


class HoldNotDueForExpiryError(HoldError):
    """Expiry was requested for a hold whose time has not come.

    Ending a hold early is a release, not an expiry: the two record different
    operational facts and must not be conflated.
    """

    code = 'hold_not_due_for_expiry'
    default_message = 'That hold is not due to expire yet.'


class HoldImmutableError(HoldError):
    """An attempt was made to alter or delete terminal hold history."""

    code = 'hold_immutable'
    default_message = 'A released or expired hold cannot be changed or removed.'


class BalanceProjectionInvalidError(HoldError):
    """The derived balance picture is not financially coherent.

    Raised rather than clamped. If held funds exceed posted funds, or a wallet
    carries a negative posted balance, something upstream is wrong — and
    silently reporting a tidy zero would conceal it behind a plausible number.
    """

    code = 'balance_projection_invalid'
    http_status = 500
    default_message = 'The wallet balance is not in a coherent state.'


class LedgerPostingConflictsWithHoldsError(LedgerError):
    """Posting would drop a wallet's posted balance below its reserved funds.

    Distinct from :class:`InsufficientAvailableBalanceError`, which is a
    *reservation* being refused. This is a *posting* being refused: the money is
    there, but some of it is already spoken for, and letting the journal land
    would leave held funds exceeding posted funds — an invalid state no
    supported operation may create.

    Not resolved by clamping, by silently releasing reservations, or by
    rewriting ledger history. The posting is simply refused.
    """

    code = 'ledger_posting_conflicts_with_holds'
    default_message = (
        'This posting would leave less posted balance than is currently '
        'reserved on the wallet.'
    )


# ---------------------------------------------------------------------------
# M4: the transaction engine
# ---------------------------------------------------------------------------
# Internal domain errors. Transactions have no customer-facing surface in M4.


class TransactionError(DomainError):
    """Base for transaction faults, so callers can catch the whole family."""

    code = 'transaction_error'
    http_status = 409
    default_message = 'The transaction operation could not be completed.'


class TransactionNotFoundError(TransactionError):
    """No transaction matches the reference given."""

    code = 'transaction_not_found'
    http_status = 404
    default_message = 'No such transaction.'


class InvalidTransactionAmountError(TransactionError):
    """The amount is not a usable positive integer minor-unit value."""

    code = 'invalid_transaction_amount'
    http_status = 400
    default_message = (
        'A transaction amount must be a positive whole number of minor units.'
    )


class InvalidTransactionDirectionError(TransactionError):
    """The direction is not one this engine recognises."""

    code = 'invalid_transaction_direction'
    http_status = 400
    default_message = 'That is not a transaction direction.'


class TransactionCurrencyMismatchError(TransactionError):
    """The transaction currency disagrees with its wallet's."""

    code = 'transaction_currency_mismatch'
    default_message = 'A transaction must use the wallet currency.'


class InvalidTransactionTransitionError(TransactionError):
    """The requested lifecycle move is not legal from the current state."""

    code = 'invalid_transaction_transition'
    default_message = 'That transaction status change is not allowed.'


class TransactionAlreadyResolvedError(TransactionError):
    """The transaction has reached a terminal state and will not move again.

    Distinct from a plain illegal transition: this says the outcome is already
    settled, which is what a second concurrent resolver needs to be told.
    """

    code = 'transaction_already_resolved'
    default_message = 'That transaction has already been resolved.'


class TransactionHoldRequiredError(TransactionError):
    """An outgoing transaction cannot start execution without reserved funds.

    Beginning to send money that is not reserved would let the same funds be
    spent twice.
    """

    code = 'transaction_hold_required'
    http_status = 422
    default_message = (
        'An outgoing transaction needs an active reservation before it can start.'
    )


class TransactionHoldInvalidError(TransactionError):
    """The hold cannot serve this transaction.

    Wrong wallet, wrong currency, no longer effective, or already attached to
    another transaction.
    """

    code = 'transaction_hold_invalid'
    http_status = 422
    default_message = 'That reservation cannot be used for this transaction.'


class TransactionIdempotencyConflictError(TransactionError):
    """The key was reused with a different intent.

    Returning the original transaction would silently ignore what the caller
    actually asked for, so this is refused instead.
    """

    code = 'transaction_idempotency_conflict'
    default_message = (
        'That idempotency key was already used for a different transaction.'
    )


class TransactionSuccessRequiresJournalError(TransactionError):
    """Success must correspond to posted financial truth.

    A transaction cannot be marked succeeded without a posted journal recording
    what actually moved.
    """

    code = 'transaction_success_requires_journal'
    default_message = 'A successful transaction must have a posted journal.'


class TransactionIntentImmutableError(TransactionError):
    """An attempt was made to rewrite a transaction's settled intent."""

    code = 'transaction_intent_immutable'
    default_message = (
        'A transaction\'s wallet, direction, amount, currency and idempotency '
        'key cannot be changed after creation.'
    )


# ===========================================================================
# M5: transfers
# ===========================================================================
# The transfer domain owns business intent — who the money is going to — and
# nothing about how it is executed. No error here names a provider, carries a
# provider payload, or describes a bank-network outcome; those belong to M6.


class TransferError(DomainError):
    """Base class for every transfer-domain failure."""

    code = 'transfer_error'
    default_message = 'The transfer could not be completed.'


class TransferNotFoundError(TransferError, NotFoundError):
    """No transfer exists for the given reference."""

    code = 'transfer_not_found'
    default_message = 'That transfer does not exist.'


class InvalidTransferAmountError(TransferError, ValidationError):
    """The principal is not a usable amount.

    Covers zero, negatives, floats, `Decimal`, `bool` and out-of-range values.
    A transfer principal is a positive whole number of minor units.
    """

    code = 'invalid_transfer_amount'
    default_message = (
        'A transfer amount must be a positive whole number of minor units.'
    )


class TransferCurrencyMismatchError(TransferError, ValidationError):
    """The transfer, its wallet and its transaction must share one currency.

    M5 performs no currency conversion: a transfer that changed currency would
    need an exchange rate this milestone has no business inventing.
    """

    code = 'transfer_currency_mismatch'
    default_message = 'A transfer must use the wallet currency.'


class InvalidTransferDestinationError(TransferError, ValidationError):
    """The destination is not a usable bank account.

    Raised for a malformed account number, an unusable bank code, or a missing
    bank or recipient name. It never means "this account does not exist" — M5
    performs no verification and makes no such claim.
    """

    code = 'invalid_transfer_destination'
    default_message = 'That transfer destination is not valid.'


class TransferIdempotencyConflictError(TransferError, ConflictError):
    """The idempotency key was already used for a different transfer.

    Critically, this includes a *different destination*: the same key, wallet,
    amount and currency with a different recipient is a different transfer, and
    quietly returning the earlier one would send money to the wrong person.
    """

    code = 'transfer_idempotency_conflict'
    default_message = (
        'That idempotency key was already used for a different transfer.'
    )


class TransferTransactionMismatchError(TransferError, ConflictError):
    """The transfer and financial transaction do not belong together.

    Also raised when an idempotency key already names a financial transaction
    that is not a transfer's, or is no longer an untouched intent that a
    transfer can back.
    """

    code = 'transfer_transaction_mismatch'
    default_message = (
        'That financial transaction does not belong to this transfer.'
    )


class TransferAccountingInvalidError(TransferError, ValidationError):
    """The proposed entries do not represent this transfer's principal.

    The wallet's posted normal balance must fall by exactly the transfer
    principal — no more, no less — whichever way the wallet's ledger account is
    classified.
    """

    code = 'transfer_accounting_invalid'
    default_message = (
        'The transfer accounting does not match the transfer principal.'
    )


class TransferImmutableError(TransferError, ConflictError):
    """An attempt was made to rewrite or delete settled transfer intent."""

    code = 'transfer_immutable'
    default_message = (
        'A transfer\'s destination, narration and transaction cannot be '
        'changed after preparation.'
    )
