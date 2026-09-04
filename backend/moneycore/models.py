"""Persistent schema for the financial core.

Three entities, deliberately separate:

* :class:`FinancialCustomer` — the user's relationship with the regulated-money
  product, distinct from the legacy ``auth.User`` identity and the SpendWise
  ``Profile`` tracker domain.
* :class:`FinancialAccount` — that customer's top-level account lifecycle.
* :class:`Wallet` — a currency-specific container belonging to the account.

**No balance lives here.** A wallet is an identity and a container, nothing
more. The ledger (M2) becomes the authoritative source of what money exists, and
balance projection (M3) the source of what is spendable. Putting a balance
column on a wallet now would create a second, competing source of truth that M2
would then have to unpick — so there is no ``balance``, no ``available_balance``,
no ``pending``/``reserved``/``held`` field, no credit/debit helper, and no signal
keeping any such column in step.

Identity data (email, phone, names) is **not** duplicated here. It stays owned by
the accounts/profile domain; a fintech-specific snapshot is not needed at M1 and
would immediately start drifting.

Provider identifiers and account numbers are absent by design. Provider
integration is M6, and fabricating an account number before receive-money
infrastructure exists would put a fake bank account in front of a customer.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.validators import RegexValidator
from django.db import models

from moneycore.domain.currency import (
    CURRENCY_CODE_LENGTH,
    CURRENCY_CODE_PATTERN,
    INVALID_CURRENCY_MESSAGE,
)
from django.utils import timezone

from moneycore.domain.errors import (
    HoldImmutableError,
    JournalImmutableError,
    TransactionIntentImmutableError,
)
from moneycore.domain.holds import HoldStatus, hold_is_terminal, is_effectively_active
from moneycore.domain.transactions import (
    TransactionDirection,
    TransactionStatus,
    execution_may_have_started,
)
from moneycore.domain.transactions import is_terminal as transaction_is_terminal
from moneycore.domain.money import Money
from moneycore.domain.ledger import (
    EntryDirection,
    JournalStatus,
    LedgerAccountStatus,
    LedgerAccountType,
    is_debit_normal,
)
from moneycore.domain.lifecycle import AccountStatus, CustomerStatus, WalletStatus

# Same rule the Money value type applies, so a currency code means the same
# thing in the database as it does in memory.
currency_code_validator = RegexValidator(
    regex=CURRENCY_CODE_PATTERN,
    message=INVALID_CURRENCY_MESSAGE,
)


class FinancialCustomer(models.Model):
    """A user's participation in the regulated-money product.

    Optional by design: a user can use every legacy SpendWise feature without
    ever having one. Nothing provisions this automatically — no signal watches
    ``User`` — so existing users do not silently acquire a financial
    relationship.
    """

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='financial_customer',
    )
    status = models.CharField(
        max_length=20,
        choices=CustomerStatus.CHOICES,
        default=CustomerStatus.ACTIVE,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'financial customer'
        verbose_name_plural = 'financial customers'
        constraints = [
            models.CheckConstraint(
                check=models.Q(status__in=sorted(CustomerStatus.ALL)),
                name='moneycore_customer_status_valid',
            ),
        ]

    def __str__(self) -> str:
        return f'FinancialCustomer<{self.user.username}: {self.status}>'


class FinancialAccount(models.Model):
    """The customer's top-level financial account and its lifecycle.

    One account per customer at V1. Multiple accounts or product types are not
    modelled, because nothing in the current architecture needs them and a
    speculative shape would constrain M2–M5 for no benefit.
    """

    customer = models.OneToOneField(
        FinancialCustomer,
        on_delete=models.CASCADE,
        related_name='financial_account',
    )
    status = models.CharField(
        max_length=20,
        choices=AccountStatus.CHOICES,
        default=AccountStatus.PENDING_ACTIVATION,
    )
    # Recorded because the lifecycle distinguishes "provisioned" from "usable",
    # and knowing when an account became usable is needed the moment money
    # movement arrives. Null until the account is first activated.
    activated_at = models.DateTimeField(null=True, blank=True)
    # Closure is terminal, so the moment it happened is not recoverable from the
    # status alone.
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'financial account'
        verbose_name_plural = 'financial accounts'
        constraints = [
            models.CheckConstraint(
                check=models.Q(status__in=sorted(AccountStatus.ALL)),
                name='moneycore_account_status_valid',
            ),
        ]

    def __str__(self) -> str:
        return f'FinancialAccount<{self.customer_id}: {self.status}>'


class Wallet(models.Model):
    """A currency-specific container belonging to a financial account.

    Identity only. See the module docstring: this model holds no balance, and
    the ledger owns what money exists.
    """

    financial_account = models.ForeignKey(
        FinancialAccount,
        on_delete=models.CASCADE,
        related_name='wallets',
    )
    currency = models.CharField(
        max_length=CURRENCY_CODE_LENGTH,
        validators=[currency_code_validator],
    )
    status = models.CharField(
        max_length=20,
        choices=WalletStatus.CHOICES,
        default=WalletStatus.ACTIVE,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'wallet'
        verbose_name_plural = 'wallets'
        constraints = [
            # One wallet per currency per account. This is the database-level
            # guard that makes provisioning safe to call concurrently.
            models.UniqueConstraint(
                fields=['financial_account', 'currency'],
                name='moneycore_wallet_unique_currency_per_account',
            ),
            models.CheckConstraint(
                check=models.Q(status__in=sorted(WalletStatus.ALL)),
                name='moneycore_wallet_status_valid',
            ),
            models.CheckConstraint(
                check=models.Q(currency__regex=r'^[A-Z]{3}$'),
                name='moneycore_wallet_currency_format_valid',
            ),
        ]

    def __str__(self) -> str:
        return f'Wallet<{self.financial_account_id}: {self.currency} {self.status}>'


# ===========================================================================
# M2: the double-entry ledger
# ===========================================================================
# Posted ledger entries are the single source of financial truth. No balance is
# stored anywhere — not on Wallet (M1), not on LedgerAccount — because a second
# copy of a balance is a second thing that can be wrong. Balances are derived by
# moneycore.services.ledger.posted_balance from posted entries only.
#
# Everything below is written through moneycore.services.ledger. The save and
# delete overrides here are a backstop that makes posted history refuse to
# change through ordinary ORM paths; they are not a substitute for the service.


class LedgerAccount(models.Model):
    """An accounting bucket in double-entry bookkeeping.

    Distinct from :class:`FinancialAccount`, which is a customer's product
    relationship, and from :class:`Wallet`, which is a currency container. A
    LedgerAccount is bookkeeping machinery: the thing entries are posted against.

    An account with a ``wallet`` is that wallet's customer account. An account
    without one is internal — a counterpart that some internal process owns. No
    provider, bank, custody or settlement account is defined here: that taxonomy
    depends on a banking partner that has not been chosen.
    """

    code = models.CharField(
        max_length=64,
        unique=True,
        help_text='Stable internal identifier. Not customer-facing.',
    )
    name = models.CharField(max_length=128, help_text='Internal label.')
    account_type = models.CharField(
        max_length=20,
        choices=LedgerAccountType.CHOICES,
    )
    currency = models.CharField(
        max_length=CURRENCY_CODE_LENGTH,
        validators=[currency_code_validator],
    )
    # A wallet has at most one ledger account, and an account belongs to at most
    # one wallet. Null means an internal account with no customer owner.
    wallet = models.OneToOneField(
        Wallet,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='ledger_account',
    )
    status = models.CharField(
        max_length=20,
        choices=LedgerAccountStatus.CHOICES,
        default=LedgerAccountStatus.ACTIVE,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'ledger account'
        verbose_name_plural = 'ledger accounts'
        constraints = [
            models.CheckConstraint(
                check=models.Q(account_type__in=sorted(LedgerAccountType.ALL)),
                name='moneycore_ledger_account_type_valid',
            ),
            models.CheckConstraint(
                check=models.Q(status__in=sorted(LedgerAccountStatus.ALL)),
                name='moneycore_ledger_account_status_valid',
            ),
            models.CheckConstraint(
                check=models.Q(currency__regex=r'^[A-Z]{3}$'),
                name='moneycore_ledger_account_currency_format_valid',
            ),
        ]
        indexes = [
            models.Index(fields=['currency', 'account_type']),
        ]

    def __str__(self) -> str:
        return f'LedgerAccount<{self.code}: {self.account_type} {self.currency}>'

    @property
    def is_debit_normal(self) -> bool:
        return is_debit_normal(self.account_type)


class PostedHistoryQuerySet(models.QuerySet):
    """Refuses bulk writes that would rewrite posted financial history.

    ``update()`` and ``delete()`` bypass model ``save()``/``delete()``, so they
    are closed here too. Unposted rows stay freely writable — only posted truth
    is protected.
    """

    def _posted_rows_present(self) -> bool:
        raise NotImplementedError

    def update(self, **kwargs):
        if self._posted_rows_present():
            raise JournalImmutableError(
                'Posted ledger history cannot be updated in bulk.'
            )
        return super().update(**kwargs)

    def delete(self):
        if self._posted_rows_present():
            raise JournalImmutableError(
                'Posted ledger history cannot be deleted in bulk.'
            )
        return super().delete()


class JournalQuerySet(PostedHistoryQuerySet):
    def _posted_rows_present(self) -> bool:
        return self.filter(status=JournalStatus.POSTED).exists()

    def posted(self):
        return self.filter(status=JournalStatus.POSTED)


class JournalEntryQuerySet(PostedHistoryQuerySet):
    def _posted_rows_present(self) -> bool:
        return self.filter(journal__status=JournalStatus.POSTED).exists()

    def posted(self):
        return self.filter(journal__status=JournalStatus.POSTED)


class Journal(models.Model):
    """One accounting event, and the unit of the balancing invariant.

    Created and posted inside a single transaction by
    :func:`moneycore.services.ledger.post_journal`, so a draft never survives a
    completed posting call. Once posted it is immutable: corrections are made by
    posting a *new* reversing journal, never by editing this one.

    Single-currency by construction. Every entry's account must use
    ``currency``, which is what makes "debits equal credits" a meaningful
    statement — balancing across currencies would require an exchange rate, and
    FX is not implemented.
    """

    status = models.CharField(
        max_length=20,
        choices=JournalStatus.CHOICES,
        default=JournalStatus.DRAFT,
    )
    currency = models.CharField(
        max_length=CURRENCY_CODE_LENGTH,
        validators=[currency_code_validator],
    )
    description = models.CharField(max_length=255, blank=True)
    # Provider-neutral internal reference. Deliberately not unique: it carries
    # no defined semantics yet, and idempotency keys are M4.
    reference = models.CharField(max_length=128, blank=True)
    posted_at = models.DateTimeField(null=True, blank=True)
    # The journal this one reverses. OneToOne, so the database allows at most
    # one reversal per original — enforced without touching the original row.
    reverses = models.OneToOneField(
        'self',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='reversed_by',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = JournalQuerySet.as_manager()

    class Meta:
        verbose_name = 'journal'
        verbose_name_plural = 'journals'
        constraints = [
            models.CheckConstraint(
                check=models.Q(status__in=sorted(JournalStatus.ALL)),
                name='moneycore_journal_status_valid',
            ),
            models.CheckConstraint(
                check=models.Q(currency__regex=r'^[A-Z]{3}$'),
                name='moneycore_journal_currency_format_valid',
            ),
            # A posted journal has a posting time; a draft has none.
            models.CheckConstraint(
                check=(
                    models.Q(status=JournalStatus.POSTED, posted_at__isnull=False)
                    | models.Q(status=JournalStatus.DRAFT, posted_at__isnull=True)
                ),
                name='moneycore_journal_posted_at_matches_status',
            ),
        ]
        indexes = [
            models.Index(fields=['status', 'posted_at']),
        ]

    def __str__(self) -> str:
        return f'Journal<{self.pk}: {self.status} {self.currency}>'

    @property
    def is_posted(self) -> bool:
        return self.status == JournalStatus.POSTED

    def save(self, *args, **kwargs):
        """Refuse to rewrite a journal already posted in the database.

        The one permitted change to such a row is the draft to posted
        transition itself, which the posting service performs before the row is
        posted in the database.
        """
        if self.pk is not None:
            already_posted = Journal.objects.filter(
                pk=self.pk, status=JournalStatus.POSTED
            ).exists()
            if already_posted:
                raise JournalImmutableError(
                    'A posted journal cannot be modified. Post a reversing '
                    'journal instead.'
                )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.is_posted:
            raise JournalImmutableError('A posted journal cannot be deleted.')
        return super().delete(*args, **kwargs)


class JournalEntry(models.Model):
    """A single debit or credit against one ledger account.

    Magnitude and direction are separate: ``amount_minor`` is always a positive
    integer number of the currency's smallest unit, and ``direction`` says which
    side it lands on. A debit is never stored as a negative credit, and no
    decimal or floating-point amount exists anywhere in the ledger.

    The entry has no currency of its own — it uses its account's, and the
    posting service requires that to equal the journal's.
    """

    journal = models.ForeignKey(
        Journal,
        on_delete=models.PROTECT,
        related_name='entries',
    )
    ledger_account = models.ForeignKey(
        LedgerAccount,
        on_delete=models.PROTECT,
        related_name='entries',
    )
    direction = models.CharField(max_length=10, choices=EntryDirection.CHOICES)
    # 64-bit signed integer. The database bound is storage capacity, not a
    # product limit — what a customer may move is a later policy decision.
    amount_minor = models.BigIntegerField()
    sequence = models.PositiveSmallIntegerField(
        help_text='Position within the journal, for stable ordering.'
    )
    memo = models.CharField(max_length=255, blank=True)

    objects = JournalEntryQuerySet.as_manager()

    class Meta:
        verbose_name = 'journal entry'
        verbose_name_plural = 'journal entries'
        ordering = ['journal_id', 'sequence']
        constraints = [
            models.CheckConstraint(
                check=models.Q(amount_minor__gt=0),
                name='moneycore_entry_amount_positive',
            ),
            models.CheckConstraint(
                check=models.Q(direction__in=sorted(EntryDirection.ALL)),
                name='moneycore_entry_direction_valid',
            ),
            models.UniqueConstraint(
                fields=['journal', 'sequence'],
                name='moneycore_entry_unique_sequence_per_journal',
            ),
        ]
        indexes = [
            models.Index(fields=['ledger_account', 'direction']),
        ]

    def __str__(self) -> str:
        return (
            f'JournalEntry<{self.journal_id}#{self.sequence}: '
            f'{self.direction} {self.amount_minor}>'
        )

    @property
    def currency(self) -> str:
        """Inherited from the account. Entries never carry their own."""
        return self.ledger_account.currency

    def _journal_is_posted(self) -> bool:
        return Journal.objects.filter(
            pk=self.journal_id, status=JournalStatus.POSTED
        ).exists()

    def save(self, *args, **kwargs):
        """Entries are write-once, and none may join a posted journal."""
        if self.pk is not None:
            raise JournalImmutableError(
                'A ledger entry cannot be modified once written. Post a '
                'reversing journal instead.'
            )
        if self._journal_is_posted():
            raise JournalImmutableError(
                'An entry cannot be added to a posted journal.'
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self._journal_is_posted():
            raise JournalImmutableError(
                'An entry of a posted journal cannot be deleted.'
            )
        return super().delete(*args, **kwargs)


class FundsHoldQuerySet(models.QuerySet):
    """Query helpers, plus a guard on terminal hold history.

    ``update()`` and ``delete()`` bypass model ``save()``/``delete()``, so they
    are closed here too for terminal rows. Active holds stay writable — the
    service still has to transition them.
    """

    def effectively_active(self, at=None):
        """Holds that actually reserve funds at ``at`` (default now).

        ACTIVE and not past their expiry. Written as a query so the held total
        is a single aggregate rather than a Python loop, and so correctness
        never depends on a cleanup job having run.
        """
        moment = at if at is not None else timezone.now()
        return self.filter(status=HoldStatus.ACTIVE).filter(
            models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=moment)
        )

    def _terminal_rows_present(self) -> bool:
        return self.filter(status__in=sorted(HoldStatus.TERMINAL)).exists()

    def update(self, **kwargs):
        if self._terminal_rows_present():
            raise HoldImmutableError(
                'Released or expired holds cannot be updated in bulk.'
            )
        return super().update(**kwargs)

    def delete(self):
        raise HoldImmutableError(
            'Holds are financial operational history and cannot be deleted.'
        )


# ===========================================================================
# M3: holds
# ===========================================================================
# A hold reserves already-posted funds. It never moves money, so its whole life
# posts nothing to the ledger — see moneycore.domain.holds for why that
# separation matters.
#
# Note what is still absent, deliberately: there is no held_balance,
# available_balance, reserved_balance or spendable_balance column anywhere in
# this module or on Wallet, LedgerAccount or FinancialAccount. The held total is
# aggregated from these rows and the available balance is computed, so neither
# can drift from the reservations it describes.


class FundsHold(models.Model):
    """A temporary reservation against a wallet's posted funds.

    Named ``FundsHold`` so the persistence name cannot be confused with the
    ``hold`` verb used across the service layer; the domain concept is simply a
    hold, and there is no separate "reservation" model — one concept, one name.

    The amount is a positive integer number of minor units, matching the ledger
    exactly: no float, no ``Decimal``, no negative-amount convention, and no
    zero-value hold.
    """

    wallet = models.ForeignKey(
        Wallet,
        on_delete=models.PROTECT,
        related_name='holds',
    )
    # Denormalised from the wallet so a hold is self-describing and the
    # currency-consistency rule is enforceable as a database constraint. The
    # service refuses any mismatch before a row is written.
    currency = models.CharField(
        max_length=CURRENCY_CODE_LENGTH,
        validators=[currency_code_validator],
    )
    # 64-bit signed, matching JournalEntry.amount_minor. The bound is storage
    # capacity, never a product limit.
    amount_minor = models.BigIntegerField()
    status = models.CharField(
        max_length=20,
        choices=HoldStatus.CHOICES,
        default=HoldStatus.ACTIVE,
    )
    # Optional. Null means the hold does not expire on its own. M3 sets no
    # default duration: how long a reservation should live depends on transfer
    # timeout windows and provider reservation semantics that are still open.
    expires_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    expired_at = models.DateTimeField(null=True, blank=True)
    # Free-form internal note. Deliberately not an enum: a fixed vocabulary
    # would have to name transfers, cards or providers, and coupling a hold to
    # transaction types M3 does not have is exactly what to avoid.
    reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = FundsHoldQuerySet.as_manager()

    class Meta:
        verbose_name = 'funds hold'
        verbose_name_plural = 'funds holds'
        constraints = [
            models.CheckConstraint(
                check=models.Q(amount_minor__gt=0),
                name='moneycore_hold_amount_positive',
            ),
            models.CheckConstraint(
                check=models.Q(status__in=sorted(HoldStatus.ALL)),
                name='moneycore_hold_status_valid',
            ),
            models.CheckConstraint(
                check=models.Q(currency__regex=r'^[A-Z]{3}$'),
                name='moneycore_hold_currency_format_valid',
            ),
            # A released hold records when; a hold that is not released does not.
            models.CheckConstraint(
                check=(
                    models.Q(status=HoldStatus.RELEASED, released_at__isnull=False)
                    | (~models.Q(status=HoldStatus.RELEASED) & models.Q(released_at__isnull=True))
                ),
                name='moneycore_hold_released_at_matches_status',
            ),
            models.CheckConstraint(
                check=(
                    models.Q(status=HoldStatus.EXPIRED, expired_at__isnull=False)
                    | (~models.Q(status=HoldStatus.EXPIRED) & models.Q(expired_at__isnull=True))
                ),
                name='moneycore_hold_expired_at_matches_status',
            ),
        ]
        indexes = [
            # The shape of the held-total query: active holds for one wallet,
            # filtered by expiry.
            models.Index(fields=['wallet', 'status', 'expires_at']),
        ]

    def __str__(self) -> str:
        return (
            f'FundsHold<{self.pk}: wallet {self.wallet_id} '
            f'{self.amount_minor} {self.currency} {self.status}>'
        )

    @property
    def is_terminal(self) -> bool:
        return hold_is_terminal(self.status)

    def is_effectively_active(self, at=None) -> bool:
        """Whether this hold actually reserves funds at ``at`` (default now)."""
        return is_effectively_active(
            self.status, self.expires_at, at if at is not None else timezone.now()
        )

    @property
    def amount(self) -> Money:
        """The reserved amount as a Money value."""
        return Money(self.amount_minor, self.currency)

    def _stored_status(self) -> str | None:
        return (
            FundsHold.objects.filter(pk=self.pk)
            .values_list('status', flat=True)
            .first()
        )

    def save(self, *args, **kwargs):
        """Refuse to rewrite a hold that is already terminal in the database.

        An active hold may still be transitioned by the service; a released or
        expired one is operational history and stays as written.
        """
        if self.pk is not None:
            stored = self._stored_status()
            if stored is not None and hold_is_terminal(stored):
                raise HoldImmutableError(
                    'A released or expired hold cannot be modified.'
                )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Holds are operational history and are not deleted."""
        raise HoldImmutableError(
            'Holds are financial operational history and cannot be deleted.'
        )


# ===========================================================================
# M4: the transaction engine
# ===========================================================================
# A transaction records what operation SpendWise is attempting and what it
# authoritatively knows about the outcome. It is not a transfer and not a
# provider attempt: there is no provider, recipient, bank, rail, account number
# or webhook field here, and there will not be one in M4.
#
# It also introduces no new balance. Spendability is still posted minus active
# holds; an in-flight operation is represented by a transaction row plus an
# active reservation, never by a "pending balance".


class FinancialTransactionQuerySet(models.QuerySet):
    """Refuses bulk writes that would rewrite settled transaction history."""

    def _resolved_rows_present(self) -> bool:
        return self.filter(status__in=sorted(TransactionStatus.TERMINAL)).exists()

    def update(self, **kwargs):
        if self._resolved_rows_present():
            raise TransactionIntentImmutableError(
                'Resolved transactions cannot be updated in bulk.'
            )
        return super().update(**kwargs)

    def delete(self):
        raise TransactionIntentImmutableError(
            'Financial transactions are permanent history and cannot be deleted.'
        )


class FinancialTransaction(models.Model):
    """One financial operation and its authoritative lifecycle state.

    The intent — wallet, direction, amount, currency, idempotency key — is fixed
    at creation and never edited afterwards. Status moves only through
    :mod:`moneycore.services.transactions`; the ``save()`` override below is a
    backstop, not the supported path.

    ``UNKNOWN`` is a persisted state, so an operation whose outcome is genuinely
    unresolved survives any process or worker restart still holding its
    reservation.
    """

    #: Fields that define what was asked for. Fixed once written.
    INTENT_FIELDS = ('wallet_id', 'direction', 'amount_minor', 'currency',
                     'idempotency_key')

    wallet = models.ForeignKey(
        Wallet,
        on_delete=models.PROTECT,
        related_name='transactions',
    )
    direction = models.CharField(
        max_length=20,
        choices=TransactionDirection.CHOICES,
    )
    # 64-bit signed, matching the ledger and holds. Positive magnitude only —
    # which way the money goes is `direction`, never a sign.
    amount_minor = models.BigIntegerField()
    currency = models.CharField(
        max_length=CURRENCY_CODE_LENGTH,
        validators=[currency_code_validator],
    )
    status = models.CharField(
        max_length=20,
        choices=TransactionStatus.CHOICES,
        default=TransactionStatus.CREATED,
    )
    # The reservation backing this operation. OneToOne: a hold funds at most one
    # transaction, so two operations can never spend the same reserved money.
    # Null for incoming transactions, which reserve nothing.
    hold = models.OneToOneField(
        FundsHold,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='transaction',
    )
    # The posted journal that makes success true. Written only by the success
    # path, and never rewritten afterwards.
    journal = models.OneToOneField(
        Journal,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='transaction',
    )
    # Caller-supplied, unique per wallet. Scoped to the wallet rather than
    # globally so two customers cannot collide, and so the uniqueness constraint
    # is the thing that actually serialises concurrent duplicate intents.
    idempotency_key = models.CharField(max_length=128)
    # A normalised internal code. The vocabulary is M6's to define when provider
    # errors are first mapped; no provider text, payload, status string, stack
    # trace or credential is ever stored here.
    failure_code = models.CharField(max_length=64, blank=True)
    processing_at = models.DateTimeField(null=True, blank=True)
    unknown_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = FinancialTransactionQuerySet.as_manager()

    class Meta:
        verbose_name = 'financial transaction'
        verbose_name_plural = 'financial transactions'
        constraints = [
            models.CheckConstraint(
                check=models.Q(amount_minor__gt=0),
                name='moneycore_txn_amount_positive',
            ),
            models.CheckConstraint(
                check=models.Q(direction__in=sorted(TransactionDirection.ALL)),
                name='moneycore_txn_direction_valid',
            ),
            models.CheckConstraint(
                check=models.Q(status__in=sorted(TransactionStatus.ALL)),
                name='moneycore_txn_status_valid',
            ),
            models.CheckConstraint(
                check=models.Q(currency__regex=r'^[A-Z]{3}$'),
                name='moneycore_txn_currency_format_valid',
            ),
            # One transaction per idempotency key per wallet. This is the
            # database-level guard that makes concurrent duplicate creation safe.
            models.UniqueConstraint(
                fields=['wallet', 'idempotency_key'],
                name='moneycore_txn_unique_idempotency_key_per_wallet',
            ),
            # Success means posted truth exists, and nothing else may carry a
            # success journal. Enforced by the database, not only by the service.
            models.CheckConstraint(
                check=(
                    models.Q(status=TransactionStatus.SUCCEEDED, journal__isnull=False)
                    | (
                        ~models.Q(status=TransactionStatus.SUCCEEDED)
                        & models.Q(journal__isnull=True)
                    )
                ),
                name='moneycore_txn_succeeded_requires_journal',
            ),
            # A resolved transaction records when; an unresolved one does not.
            models.CheckConstraint(
                check=(
                    models.Q(
                        status__in=sorted(TransactionStatus.TERMINAL),
                        resolved_at__isnull=False,
                    )
                    | (
                        ~models.Q(status__in=sorted(TransactionStatus.TERMINAL))
                        & models.Q(resolved_at__isnull=True)
                    )
                ),
                name='moneycore_txn_resolved_at_matches_status',
            ),
        ]
        indexes = [
            models.Index(fields=['wallet', 'status']),
            # Finding operations still awaiting the truth.
            models.Index(fields=['status', 'unknown_at']),
        ]

    def __str__(self) -> str:
        return (
            f'FinancialTransaction<{self.pk}: wallet {self.wallet_id} '
            f'{self.direction} {self.amount_minor} {self.currency} {self.status}>'
        )

    @property
    def amount(self) -> Money:
        return Money(self.amount_minor, self.currency)

    @property
    def is_terminal(self) -> bool:
        return transaction_is_terminal(self.status)

    @property
    def execution_may_have_started(self) -> bool:
        return execution_may_have_started(self.status)

    def _stored_intent(self):
        return (
            FinancialTransaction.objects.filter(pk=self.pk)
            .values(*self.INTENT_FIELDS)
            .first()
        )

    def save(self, *args, **kwargs):
        """Refuse to rewrite settled intent.

        What was asked for cannot change after the fact; only status and its
        associated bookkeeping move, and those move through the transaction
        services.
        """
        if self.pk is not None:
            stored = self._stored_intent()
            if stored is not None:
                current = {field: getattr(self, field) for field in self.INTENT_FIELDS}
                changed = [
                    field for field in self.INTENT_FIELDS
                    if stored[field] != current[field]
                ]
                if changed:
                    raise TransactionIntentImmutableError(
                        'A transaction\'s intent cannot be changed after creation.',
                        details={'fields': sorted(changed)},
                    )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TransactionIntentImmutableError(
            'Financial transactions are permanent history and cannot be deleted.'
        )
