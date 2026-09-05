"""The transfer service: prepare a bank transfer, and account for its success.

M5 adds exactly two things M4 could not do on its own:

1. **A transfer intent** — a destination, prepared atomically together with the
   financial transaction and the reservation that backs it.
2. **Transfer accounting** — the balanced entries that make a successful
   transfer true in the ledger. M4 deliberately accepts prepared entries
   because it does not know what a transfer is; this module is what knows.

What this module is not
-----------------------
It contacts nothing. There is no provider, no bank API, no account-verification
call, no webhook, no reconciliation and no settlement engine. It cannot start
provider execution, and it deliberately offers no ``start_transfer``: M6 is the
first milestone allowed to begin execution, and a function here that pretended
to would be fabricating one. Reaching ``PROCESSING`` goes through M4's
``start_processing`` until then.

There is also no cancellation, no draft and no abandonment, carried forward
from the locked §8.5 and M4-12. Preparing a transfer is the point at which the
transfer is **accepted for execution** — not the point at which a customer
starts typing. Everything before acceptance produces no financial transaction
at all.

Lock ordering
-------------
Unchanged from M2/M3/M4. The ``Wallet`` row is the serialisation gate for
everything affecting spendability, so preparation takes it first and everything
else follows underneath it::

    1. the Wallet row                    (M2's ascending-primary-key helper)
    2. the FinancialTransaction row      (M4)
    3. the FundsHold row                 (M3)
    4. the Transfer row
    5. ledger posting on success         (re-enters locks already held)

No path here takes a transaction, hold or transfer lock before a wallet lock,
so M5 introduces no new deadlock cycle. Taking the wallet lock at the top of
``prepare_transfer`` is also what makes concurrent duplicate preparation
resolve cleanly: competing callers for one wallet serialise, and the second one
sees the first's committed work rather than racing it.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction

from moneycore.domain.errors import (
    InvalidTransferAmountError,
    TransferAccountingInvalidError,
    TransferCurrencyMismatchError,
    TransferIdempotencyConflictError,
    TransferTransactionMismatchError,
)
from moneycore.domain.ledger import (
    EntryDirection,
    is_debit_normal,
    is_valid_amount_minor,
)
from moneycore.domain.transactions import TransactionDirection, TransactionStatus
from moneycore.domain.transfers import (
    VerifiedBankAccount,
    as_snapshot,
    normalise_narration,
)
from moneycore.models import FinancialTransaction, LedgerAccount, Transfer, Wallet
from moneycore.services.holds import create_hold
from moneycore.services.ledger import (
    EntryDraft,
    _lock_wallets,
    _wallet_balance_deltas,
)
from moneycore.services.transactions import (
    attach_hold,
    create_transaction,
    fail_transaction,
    mark_unknown,
    succeed_transaction,
)


# --------------------------------------------------------------------------
# Preparation
# --------------------------------------------------------------------------


def _wallet_ledger_account(wallet: Wallet) -> LedgerAccount:
    """The wallet's ledger account, or M2's error if it has none.

    Deliberately not created on demand: when a wallet's ledger account is
    opened is still open (O-14), and provisioning one as a side effect here
    would answer it by accident.
    """
    from moneycore.domain.errors import WalletLedgerAccountNotFoundError

    account = LedgerAccount.objects.filter(wallet=wallet).first()
    if account is None:
        raise WalletLedgerAccountNotFoundError(
            'This wallet has no ledger account, so it cannot send a transfer.',
            details={'wallet': wallet.pk, 'currency': wallet.currency},
        )
    return account


def _validate_principal(wallet: Wallet, amount_minor: int, currency: str | None) -> str:
    if not is_valid_amount_minor(amount_minor):
        # Covers zero, negatives, float, Decimal, str, bool and out-of-range.
        raise InvalidTransferAmountError(
            'A transfer amount must be a positive whole number of minor units '
            'within the supported 64-bit range.',
            details={'amount_type': type(amount_minor).__name__},
        )

    resolved = currency or wallet.currency
    if resolved != wallet.currency:
        # No FX in M5: balancing across currencies needs an exchange rate this
        # milestone has no business inventing.
        raise TransferCurrencyMismatchError(
            'A transfer must use the wallet currency.',
            details={'wallet_currency': wallet.currency, 'requested': resolved},
        )
    return resolved


def _require_same_transfer_intent(
    existing: Transfer, destination: VerifiedBankAccount, narration: str
) -> None:
    """The critical M5 idempotency rule.

    M4's key identifies ``(wallet, direction, amount, currency, key)``. A
    transfer's intent also includes **where the money is going**, so the same
    key with the same amount but a different destination is a *different*
    transfer. Returning the earlier one would send money to the wrong person,
    so it raises instead.
    """
    differences = []
    if existing.recipient_name != destination.account_name:
        differences.append('recipient_name')
    if existing.destination_account_number != destination.account_number:
        differences.append('destination_account_number')
    if existing.destination_bank_code != destination.bank_code:
        differences.append('destination_bank_code')
    if existing.destination_bank_name != destination.bank_name:
        differences.append('destination_bank_name')
    if existing.narration != narration:
        differences.append('narration')

    if differences:
        raise TransferIdempotencyConflictError(
            'That idempotency key was already used for a transfer with '
            'different details.',
            details={
                'transfer': existing.pk,
                'conflicting_fields': sorted(differences),
                # The existing destination is deliberately NOT echoed back:
                # a caller replaying someone else's key should not be told
                # where that money went.
            },
        )


@transaction.atomic
def prepare_transfer(
    wallet: Wallet,
    destination: VerifiedBankAccount,
    amount_minor: int,
    *,
    idempotency_key: str,
    narration: str = '',
    currency: str | None = None,
) -> Transfer:
    """Accept one outgoing bank transfer for execution.

    Creates — atomically, all or none — the transfer intent, the financial
    transaction that will carry its lifecycle, and the reservation that backs
    it. There is no state in which a transaction exists without its hold, or a
    hold exists without the transfer it was reserved for.

    ``destination`` is a :class:`~moneycore.domain.transfers.VerifiedBankAccount`,
    which the caller asserts was verified by something trustworthy. M5 performs
    no verification and offers no way to obtain one; M6 does.

    Idempotent on ``(wallet, idempotency_key)``, reusing M4's mechanism rather
    than inventing a competing one — and extending it with the destination
    check that M4 cannot make (see :func:`_require_same_transfer_intent`).

    The reservation is **principal only**. Transfer fees do not exist yet and
    M5 invents none (O-25 stays open); M4 already permits a hold larger than
    the principal, so a future fee policy fits without changing anything here.
    """
    if not isinstance(destination, VerifiedBankAccount):
        # The type *is* the assertion that the destination was verified. A raw
        # dict or string has made no such assertion.
        raise TransferTransactionMismatchError(
            'A transfer needs a verified bank destination.',
            details={'destination_type': type(destination).__name__},
        )

    resolved_currency = _validate_principal(wallet, amount_minor, currency)
    stored_narration = normalise_narration(narration)

    # The gate. Everything below runs under it, so two callers preparing
    # against one wallet serialise instead of racing.
    _lock_wallets([wallet.pk])
    _wallet_ledger_account(wallet)

    txn = create_transaction(
        wallet,
        TransactionDirection.OUTGOING,
        amount_minor,
        idempotency_key=idempotency_key,
        currency=resolved_currency,
    )

    existing = Transfer.objects.filter(financial_transaction=txn).first()
    if existing is not None:
        _require_same_transfer_intent(existing, destination, stored_narration)
        return existing

    # A transaction under this key exists but is not a transfer's. Either M4
    # was used directly with the same key, or something already moved it on.
    # Silently adopting it would attach a transfer to an operation it did not
    # describe.
    if txn.status != TransactionStatus.CREATED or txn.hold_id is not None:
        raise TransferTransactionMismatchError(
            'That idempotency key already names a financial transaction that '
            'is not available to back a transfer.',
            details={'transaction': txn.pk, 'status': txn.status},
        )

    hold = create_hold(
        wallet,
        amount_minor,
        reason=f'transfer:{idempotency_key}',
    )
    txn = attach_hold(txn, hold)

    try:
        with transaction.atomic():
            return Transfer.objects.create(
                financial_transaction=txn,
                narration=stored_narration,
                **as_snapshot(destination),
            )
    except IntegrityError:
        # Another caller claimed this transaction's transfer first. Return
        # theirs if it is the same transfer, or raise if it is not — never
        # leak the integrity error itself.
        winner = Transfer.objects.get(financial_transaction=txn)
        _require_same_transfer_intent(winner, destination, stored_narration)
        return winner


# --------------------------------------------------------------------------
# Transfer accounting
# --------------------------------------------------------------------------


def transfer_principal_entries(
    transfer: Transfer, counterpart_account: LedgerAccount
) -> list[EntryDraft]:
    """The balanced entries that make this transfer's success true.

    This is what M5 adds over M4: M4 posts what it is given precisely because
    it does not know what a transfer is, and this is the function that does.

    **Direction is derived, never hardcoded.** O-13 is still open — how a
    customer wallet is classified follows from a custody and accounting design
    that has not been made — so "debit the wallet to send money" is not a rule
    this milestone is entitled to write down. What is certain is the *effect*:
    sending money reduces the wallet's posted balance in its own normal
    direction. Which entry side achieves that follows from the account's actual
    ``account_type``:

    * debit-normal wallet account — reducing it is a **credit**
    * credit-normal wallet account — reducing it is a **debit**

    The counterpart takes the balancing side. M5 says nothing about *which*
    internal account that should be: the settlement taxonomy depends on the
    custody and provider arrangement and stays open (O-29), so the caller
    supplies it.
    """
    wallet_account = _wallet_ledger_account(transfer.wallet)
    principal = transfer.amount_minor

    wallet_side = (
        EntryDirection.CREDIT
        if is_debit_normal(wallet_account.account_type)
        else EntryDirection.DEBIT
    )
    counterpart_side = (
        EntryDirection.DEBIT
        if wallet_side == EntryDirection.CREDIT
        else EntryDirection.CREDIT
    )

    return [
        EntryDraft(wallet_account, wallet_side, principal, memo='transfer principal'),
        EntryDraft(
            counterpart_account, counterpart_side, principal, memo='transfer principal'
        ),
    ]


def _require_principal_effect(transfer: Transfer, entries: list[EntryDraft]) -> None:
    """The wallet's normal balance must fall by exactly the principal.

    This closes the M4 limitation that success could, in principle, post
    accounting unrelated to the transaction's amount. Expressed as a net effect
    rather than as an entry direction, so it holds identically whether the
    wallet's ledger account is debit-normal or credit-normal.
    """
    deltas = _wallet_balance_deltas(entries)
    expected = -transfer.amount_minor
    actual = deltas.get(transfer.wallet.pk, 0)

    if actual != expected:
        raise TransferAccountingInvalidError(
            'A successful transfer must reduce the wallet posted balance by '
            'exactly the transfer principal.',
            details={
                'transfer': transfer.pk,
                'principal_minor': transfer.amount_minor,
                'wallet_delta_minor': actual,
                'expected_delta_minor': expected,
            },
        )

    other_wallets = sorted(set(deltas) - {transfer.wallet.pk})
    if other_wallets:
        # A bank transfer leaves the SpendWise wallet system. Moving another
        # customer's wallet balance here would be a different product
        # operation wearing a transfer's clothes.
        raise TransferAccountingInvalidError(
            'Transfer accounting must not move another wallet balance.',
            details={'transfer': transfer.pk, 'other_wallets': other_wallets},
        )


def _require_usable_counterpart(
    transfer: Transfer, counterpart_account: LedgerAccount
) -> None:
    wallet_account = _wallet_ledger_account(transfer.wallet)

    if counterpart_account.pk == wallet_account.pk:
        raise TransferAccountingInvalidError(
            'A transfer cannot use the sending wallet account as its own '
            'counterpart.',
            details={'transfer': transfer.pk, 'account': counterpart_account.code},
        )
    if counterpart_account.wallet_id is not None:
        # M5 deliberately requires an internal counterpart. Which internal
        # account is right depends on the custody and settlement design and
        # stays open (O-29) — but a customer wallet is not it, and an internal
        # wallet-to-wallet movement is a separate product operation that would
        # need its own design.
        raise TransferAccountingInvalidError(
            'A bank transfer counterpart must be an internal ledger account, '
            'not a customer wallet.',
            details={'transfer': transfer.pk, 'account': counterpart_account.code},
        )
    if counterpart_account.currency != transfer.currency:
        raise TransferCurrencyMismatchError(
            'The transfer counterpart must use the transfer currency.',
            details={
                'transfer': transfer.pk,
                'transfer_currency': transfer.currency,
                'account_currency': counterpart_account.currency,
            },
        )


def _require_own_transaction(transfer: Transfer) -> FinancialTransaction:
    """The transfer's transaction, checked rather than assumed.

    Only the direction is checked here, and that is deliberate. Amount,
    currency and wallet are **read-through properties** on `Transfer`, not
    stored columns, so "the transfer and its transaction disagree about the
    money" is not a state this model can represent — there is only ever one
    copy of each fact. Validating an impossible disagreement would be theatre;
    not storing it is the actual guarantee.

    Direction is different: it lives on the transaction, and a `Transfer` row
    written by raw ORM could name an incoming one.
    """
    txn = transfer.financial_transaction

    if txn.direction != TransactionDirection.OUTGOING:
        raise TransferTransactionMismatchError(
            'A transfer must be backed by an outgoing transaction.',
            details={'transfer': transfer.pk, 'direction': txn.direction},
        )
    return txn


# --------------------------------------------------------------------------
# Outcomes — thin wrappers over M4, never a second state machine
# --------------------------------------------------------------------------


@transaction.atomic
def succeed_transfer(
    transfer: Transfer,
    *,
    counterpart_account: LedgerAccount,
    description: str = '',
    reference: str = '',
) -> Transfer:
    """Record that this transfer succeeded, with the accounting to prove it.

    M5 derives the entries and checks their effect; **M4 does the rest**. The
    reservation release, the journal posting and the status change all happen
    inside M4's single atomic block, so this adds no second orchestration and
    no second chance to get atomicity wrong. Nothing here releases a hold or
    creates a journal by hand.

    If anything is rejected — here or in the ledger — the whole thing rolls
    back: the transaction stays where it was, the hold stays active, and no
    journal survives.
    """
    txn = _require_own_transaction(transfer)
    _require_usable_counterpart(transfer, counterpart_account)

    entries = transfer_principal_entries(transfer, counterpart_account)
    _require_principal_effect(transfer, entries)

    succeed_transaction(
        txn,
        entries=entries,
        description=description or f'Transfer {transfer.pk}',
        reference=reference,
    )

    transfer.refresh_from_db()
    return transfer


@transaction.atomic
def fail_transfer(transfer: Transfer, *, failure_code: str = '') -> Transfer:
    """Record an authoritative failure: the money is known not to have moved.

    A thin wrapper over M4, adding only the check that the transaction really
    belongs to this transfer. It maps no provider error and invents no failure
    reason. **An absent, timed-out or ambiguous answer is not this** — that is
    :func:`mark_transfer_unknown`.
    """
    txn = _require_own_transaction(transfer)
    fail_transaction(txn, failure_code=failure_code)

    transfer.refresh_from_db()
    return transfer


@transaction.atomic
def mark_transfer_unknown(transfer: Transfer) -> Transfer:
    """Record that this transfer's outcome cannot yet be established.

    Delegates to M4 unchanged: nothing is posted, the reservation **stays
    active**, and no balance moves. There is no transfer-level pending,
    confirming or awaiting-confirmation state — the transaction's ``unknown``
    is the only one, and "Confirming" is the product's word for it.
    """
    txn = _require_own_transaction(transfer)
    mark_unknown(txn)

    transfer.refresh_from_db()
    return transfer
