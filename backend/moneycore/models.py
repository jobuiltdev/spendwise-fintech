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
