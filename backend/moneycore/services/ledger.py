"""The authoritative ledger service.

**Every** posted ledger entry is created here. Serializers, views, model
``save()`` methods, signals and ad-hoc helpers never create financial truth: if
a posting did not come through :func:`post_journal`, it did not happen through a
supported path.

What this module guarantees
---------------------------
* A posted journal always has at least two entries and balances exactly.
* Posting is atomic — a failure leaves no journal, no entries, nothing.
* Amounts are integer minor units throughout. There is no rounding here at all:
  callers hand over amounts that are already resolved, and if a caller needs to
  split an amount that does not divide evenly, choosing the remainder policy is
  that caller's job, not the ledger's.
* Balances are derived from posted entries. Nothing caches a balance, so nothing
  can drift out of step with the entries.

What it does not do
-------------------
No holds, no reservations, no available or spendable balance — those are M3. No
transfers, no transaction states, no provider concepts. No currency conversion:
a journal is single-currency, because balancing across currencies would require
an exchange rate this milestone has no business inventing.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from moneycore.domain.errors import (
    InvalidLedgerEntryError,
    JournalAlreadyReversedError,
    JournalNotPostedError,
    LedgerAccountClosedError,
    LedgerCurrencyMismatchError,
    LedgerPostingConflictsWithHoldsError,
    LedgerUnbalancedError,
    WalletLedgerAccountNotFoundError,
)
from moneycore.domain.ledger import (
    EntryDirection,
    JournalStatus,
    LedgerAccountStatus,
    LedgerAccountType,
    is_debit_normal,
    is_valid_amount_minor,
    opposite_direction,
)
from moneycore.domain.currency import is_valid_currency_code
from moneycore.domain.money import Money
from moneycore.models import Journal, JournalEntry, LedgerAccount, Wallet

MINIMUM_ENTRIES_PER_JOURNAL = 2


@dataclass(frozen=True)
class EntryDraft:
    """One proposed side of a posting, before anything is written.

    ``amount_minor`` is a positive magnitude; ``direction`` says which side it
    lands on. A debit is never expressed as a negative credit.
    """

    ledger_account: LedgerAccount
    direction: str
    amount_minor: int
    memo: str = ''


def debit(account: LedgerAccount, amount_minor: int, memo: str = '') -> EntryDraft:
    """A proposed debit. Naming a direction, not moving money."""
    return EntryDraft(account, EntryDirection.DEBIT, amount_minor, memo)


def credit(account: LedgerAccount, amount_minor: int, memo: str = '') -> EntryDraft:
    """A proposed credit. Naming a direction, not moving money."""
    return EntryDraft(account, EntryDirection.CREDIT, amount_minor, memo)


# --------------------------------------------------------------------------
# Ledger account creation
# --------------------------------------------------------------------------


@transaction.atomic
def open_ledger_account(
    *,
    code: str,
    name: str,
    account_type: str,
    currency: str,
    wallet: Wallet | None = None,
) -> LedgerAccount:
    """Create a ledger account.

    Internal only — there is no customer-facing way to create one. When
    ``wallet`` is given the account is that wallet's customer account and must
    share its currency.
    """
    if account_type not in LedgerAccountType.ALL:
        raise InvalidLedgerEntryError(
            f'{account_type!r} is not a ledger account type.',
            details={'account_type': account_type},
        )
    if not is_valid_currency_code(currency):
        raise InvalidLedgerEntryError(
            'currency must be an ISO-4217 alpha-3 code in uppercase.',
            details={'currency': str(currency)},
        )
    if wallet is not None and wallet.currency != currency:
        raise LedgerCurrencyMismatchError(
            'A wallet ledger account must use the wallet currency.',
            details={'wallet_currency': wallet.currency, 'requested': currency},
        )

    return LedgerAccount.objects.create(
        code=code,
        name=name,
        account_type=account_type,
        currency=currency,
        wallet=wallet,
        status=LedgerAccountStatus.ACTIVE,
    )


def wallet_ledger_account_code(wallet: Wallet) -> str:
    """The stable internal code for a wallet's ledger account."""
    return f'wallet:{wallet.pk}:{wallet.currency}'


@transaction.atomic
def open_wallet_ledger_account(wallet: Wallet, *, account_type: str) -> LedgerAccount:
    """Open the ledger account that a wallet's posted balance derives from.

    ``account_type`` is **required and has no default**. M2 supports the whole
    conventional taxonomy but takes no position on which class represents a
    customer wallet: whether those funds are a liability of SpendWise, an asset
    held on a customer's behalf, or something else follows from a custody,
    provider and accounting design that has not been settled. Defaulting here
    would quietly settle it, so the caller states it and the decision stays
    visible and OPEN.

    Idempotent: a wallet has exactly one ledger account, enforced by the
    ``OneToOneField``, so calling this twice returns the existing one rather
    than creating a second source of truth. Note that this means the *first*
    call fixes the classification — see :func:`posted_balance`, which reads
    whatever type the account actually carries.
    """
    existing = LedgerAccount.objects.filter(wallet=wallet).first()
    if existing is not None:
        return existing

    return open_ledger_account(
        code=wallet_ledger_account_code(wallet),
        name=f'Wallet {wallet.pk} {wallet.currency}',
        account_type=account_type,
        currency=wallet.currency,
        wallet=wallet,
    )


# --------------------------------------------------------------------------
# Posting
# --------------------------------------------------------------------------


def _validate_entries(currency: str, entries: list[EntryDraft]) -> None:
    """Every structural check, run before a single row is written."""
    if len(entries) < MINIMUM_ENTRIES_PER_JOURNAL:
        raise InvalidLedgerEntryError(
            'A journal needs at least two entries to balance.',
            details={'entry_count': len(entries)},
        )

    for position, entry in enumerate(entries):
        if entry.direction not in EntryDirection.ALL:
            raise InvalidLedgerEntryError(
                f'{entry.direction!r} is not a ledger entry direction.',
                details={'position': position, 'direction': str(entry.direction)},
            )
        if not is_valid_amount_minor(entry.amount_minor):
            # Covers zero, negatives, floats, Decimal, bool and out-of-range.
            raise InvalidLedgerEntryError(
                'amount_minor must be a positive integer number of minor units '
                'within the supported 64-bit range.',
                details={
                    'position': position,
                    'amount_type': type(entry.amount_minor).__name__,
                },
            )
        if not isinstance(entry.ledger_account, LedgerAccount) or entry.ledger_account.pk is None:
            raise InvalidLedgerEntryError(
                'Each entry needs a saved ledger account.',
                details={'position': position},
            )
        if entry.ledger_account.status != LedgerAccountStatus.ACTIVE:
            raise LedgerAccountClosedError(
                f'Ledger account {entry.ledger_account.code} is not active.',
                details={'position': position, 'code': entry.ledger_account.code},
            )
        if entry.ledger_account.currency != currency:
            raise LedgerCurrencyMismatchError(
                'Every entry must use the journal currency.',
                details={
                    'position': position,
                    'journal_currency': currency,
                    'account_currency': entry.ledger_account.currency,
                },
            )

    debits = sum(e.amount_minor for e in entries if e.direction == EntryDirection.DEBIT)
    credits = sum(e.amount_minor for e in entries if e.direction == EntryDirection.CREDIT)
    if debits != credits:
        raise LedgerUnbalancedError(
            f'Debits ({debits}) do not equal credits ({credits}).',
            details={'debits': debits, 'credits': credits},
        )


def _wallet_balance_deltas(entries: list[EntryDraft]) -> dict[int, int]:
    """The net effect this journal will have on each wallet-backed account.

    Keyed by wallet id. Computed from each account's **actual** normal-balance
    semantics, never from an assumption about how a wallet is classified — that
    classification is still open (O-13), so a debit is not hardcoded as a spend:

    * debit-normal account — a debit increases, a credit decreases
    * credit-normal account — a credit increases, a debit decreases

    Several entries touching the same account aggregate. Accounts with no wallet
    are internal and are not represented here.
    """
    deltas: dict[int, int] = {}
    for entry in entries:
        account = entry.ledger_account
        if account.wallet_id is None:
            continue
        increases = (
            entry.direction == EntryDirection.DEBIT
            if is_debit_normal(account.account_type)
            else entry.direction == EntryDirection.CREDIT
        )
        signed = entry.amount_minor if increases else -entry.amount_minor
        deltas[account.wallet_id] = deltas.get(account.wallet_id, 0) + signed
    return deltas


def _lock_wallets(wallet_ids) -> list[Wallet]:
    """Lock the given wallet rows, always in ascending primary-key order.

    A deterministic order is what stops two journals touching the same pair of
    wallets from locking them in opposite orders and deadlocking.
    """
    if not wallet_ids:
        return []
    return list(
        Wallet.objects.select_for_update()
        .filter(pk__in=sorted(wallet_ids))
        .order_by('pk')
    )


def _require_posting_leaves_reservations_covered(deltas: dict[int, int]) -> None:
    """Refuse a posting that would leave a wallet's funds below its holds.

    Called with the affected wallet rows already locked, so the posted balance
    and held total read here cannot move before this journal commits.

    Only wallet-backed accounts are checked: an internal ledger account carries
    no customer reservations, so nothing constrains its balance here.
    """
    # Imported inside the function: holds (M3) builds on the ledger (M2), so the
    # module-level dependency runs that way. This one reverse reference is what
    # lets the posting service honour reservations, and a local import keeps the
    # package-level direction intact.
    from moneycore.services.holds import held_amount

    for wallet_id, delta in deltas.items():
        wallet = Wallet.objects.get(pk=wallet_id)
        account = LedgerAccount.objects.get(wallet=wallet)

        current = posted_balance(account).minor_units
        resulting = current + delta
        held = held_amount(wallet).minor_units

        if resulting < held:
            raise LedgerPostingConflictsWithHoldsError(
                'This posting would leave less posted balance than is '
                'currently reserved on the wallet.',
                details={
                    'wallet': wallet_id,
                    'currency': wallet.currency,
                    'posted_minor': current,
                    'delta_minor': delta,
                    'resulting_posted_minor': resulting,
                    'held_minor': held,
                },
            )


@transaction.atomic
def post_journal(
    *,
    currency: str,
    entries: list[EntryDraft],
    description: str = '',
    reference: str = '',
    reverses: Journal | None = None,
) -> Journal:
    """Validate, write and post one balanced journal, atomically.

    The journal and every entry commit together or not at all: everything runs
    inside one transaction, and any rejection raises before or during it, so a
    half-written journal is never visible to another reader.

    The journal is created as a draft and posted in the same transaction, so no
    completed call leaves a draft behind.

    **Wallet serialisation.** If any entry touches a wallet-backed ledger
    account, that wallet's row is locked before anything is read or written, in
    the same way a hold locks it. The wallet row is the single gate for every
    operational change to spendability, so a posting and a reservation for the
    same wallet can never evaluate against each other's stale view. Under that
    lock the posting refuses to reduce a wallet's balance below the funds
    already reserved against it, which is the invariant
    ``effective_held <= posted`` — enforced whichever of the two operations
    reaches the lock first.
    """
    if not is_valid_currency_code(currency):
        raise InvalidLedgerEntryError(
            'currency must be an ISO-4217 alpha-3 code in uppercase.',
            details={'currency': str(currency)},
        )

    _validate_entries(currency, entries)

    # Lock every affected wallet, in a deterministic order, before reading the
    # balances the guard below depends on.
    deltas = _wallet_balance_deltas(entries)
    _lock_wallets(deltas.keys())
    _require_posting_leaves_reservations_covered(deltas)

    journal = Journal.objects.create(
        status=JournalStatus.DRAFT,
        currency=currency,
        description=description,
        reference=reference,
        reverses=reverses,
    )

    for sequence, entry in enumerate(entries, start=1):
        JournalEntry.objects.create(
            journal=journal,
            ledger_account=entry.ledger_account,
            direction=entry.direction,
            amount_minor=entry.amount_minor,
            sequence=sequence,
            memo=entry.memo,
        )

    journal.status = JournalStatus.POSTED
    journal.posted_at = timezone.now()
    journal.save(update_fields=['status', 'posted_at'])
    return journal


# --------------------------------------------------------------------------
# Reversal
# --------------------------------------------------------------------------


@transaction.atomic
def reverse_journal(original: Journal, *, description: str = '') -> Journal:
    """Post a new journal that is the exact opposite of ``original``.

    The original is never touched: it is not edited, not deleted and not marked
    as though it never happened. The correction is a new posted journal whose
    ``reverses`` points back at it, and because that field is a ``OneToOne`` the
    database itself allows at most one reversal per original.

    A reversal is itself a posted journal, so reversing a reversal would be a
    second-order correction. That is refused here — the rule stays "one reversal
    per posted journal", and reversal-of-reversal is not something the current
    architecture defines.
    """
    if not original.is_posted:
        raise JournalNotPostedError(
            'Only a posted journal can be reversed.',
            details={'status': original.status},
        )

    # select_for_update on the original: two concurrent reversal attempts would
    # otherwise both read "no reversal yet" and race to insert. The lock makes
    # them queue, and the OneToOne on `reverses` is the final guard if they
    # somehow do not.
    locked = Journal.objects.select_for_update().get(pk=original.pk)

    if Journal.objects.filter(reverses=locked).exists():
        raise JournalAlreadyReversedError(
            'That journal already has a reversal.',
            details={'journal': locked.pk},
        )
    if locked.reverses_id is not None:
        raise JournalAlreadyReversedError(
            'A reversal cannot itself be reversed.',
            details={'journal': locked.pk, 'reverses': locked.reverses_id},
        )

    mirrored = [
        EntryDraft(
            ledger_account=entry.ledger_account,
            direction=opposite_direction(entry.direction),
            amount_minor=entry.amount_minor,
            memo=entry.memo,
        )
        for entry in locked.entries.select_related('ledger_account').order_by('sequence')
    ]

    return post_journal(
        currency=locked.currency,
        entries=mirrored,
        description=description or f'Reversal of journal {locked.pk}',
        reference=locked.reference,
        reverses=locked,
    )


# --------------------------------------------------------------------------
# Balance derivation
# --------------------------------------------------------------------------


def posted_balance(account: LedgerAccount) -> Money:
    """The account's balance, derived from posted entries only.

    Sign convention: the result is positive when the account holds a balance in
    its **normal** direction. Debit-normal accounts (asset, expense) are
    ``debits − credits``; credit-normal accounts (liability, equity, revenue)
    are ``credits − debits``. So a customer wallet account holding money reads
    as a positive figure.

    Drafts are excluded. Integer arithmetic throughout — no float, no Decimal,
    no rounding — and nothing is cached, so this cannot drift from the entries
    it is computed from.

    This is the *posted* balance and only that. Available, spendable, reserved
    and held balances do not exist yet; they arrive with holds in M3.
    """
    totals = JournalEntry.objects.filter(
        ledger_account=account,
        journal__status=JournalStatus.POSTED,
    ).aggregate(
        debits=Sum('amount_minor', filter=Q(direction=EntryDirection.DEBIT)),
        credits=Sum('amount_minor', filter=Q(direction=EntryDirection.CREDIT)),
    )

    debits = totals['debits'] or 0
    credits = totals['credits'] or 0
    net = debits - credits if is_debit_normal(account.account_type) else credits - debits
    return Money(net, account.currency)


def wallet_posted_balance(wallet: Wallet) -> Money:
    """The wallet's posted balance, derived from its ledger account.

    Raises :class:`~moneycore.domain.errors.WalletLedgerAccountNotFoundError`
    when the wallet has no ledger account.

    That case is **not** a zero balance. A wallet with no ledger relationship
    has no authoritative balance at all, and answering zero would state a
    financial fact the ledger has not established — the same fabrication as a
    fake ₦0 on a screen. A *mapped* account with no posted entries is a
    different thing entirely, and does return exactly zero.

    This deliberately does not create the missing account: when a wallet's
    ledger account is opened is an open question (O-14), and provisioning one as
    a side effect of reading a balance would answer it by accident.
    """
    account = LedgerAccount.objects.filter(wallet=wallet).first()
    if account is None:
        raise WalletLedgerAccountNotFoundError(
            'This wallet has no ledger account, so it has no posted balance.',
            details={'wallet': wallet.pk, 'currency': wallet.currency},
        )
    return posted_balance(account)
