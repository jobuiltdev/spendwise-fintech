"""Provisioning the initial financial relationship.

Provisioning is **explicit**. No signal watches ``User``, so creating an account
in the existing SpendWise app does not give anyone a financial relationship —
every existing user stays Tier 0 until something deliberately calls in here.

Provisioning is also **separate from activation**. It leaves the account
``PENDING_ACTIVATION``: the rows exist, but the account is not usable. Making
it usable is
a lifecycle transition (see :mod:`moneycore.services.lifecycle`), and at M1 that
transition asserts nothing about verification — KYC is M10.

Idempotence here is ordinary domain idempotence: unique constraints plus
``get_or_create`` inside one transaction. This is **not** the M4 idempotency-key
framework, and nothing here reserves a key or records a request fingerprint.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.contrib.auth.models import User
from django.db import transaction

from moneycore.domain.lifecycle import AccountStatus, CustomerStatus, WalletStatus
from moneycore.models import FinancialAccount, FinancialCustomer, Wallet

# Banking V1 is NGN-oriented. Named here, at the provisioning policy boundary,
# rather than baked into the Wallet schema — adding another currency later is a
# change to this constant and a new row, not a migration.
DEFAULT_WALLET_CURRENCY = 'NGN'


@dataclass(frozen=True)
class ProvisioningResult:
    """What provisioning found or created.

    The ``*_created`` flags let a caller tell a first provisioning from a repeat
    without comparing timestamps. They are reporting only — the rows are the
    same either way.
    """

    customer: FinancialCustomer
    account: FinancialAccount
    wallet: Wallet
    customer_created: bool
    account_created: bool
    wallet_created: bool

    @property
    def newly_provisioned(self) -> bool:
        return self.customer_created or self.account_created or self.wallet_created


@transaction.atomic
def provision_financial_account(
    user: User, *, currency: str = DEFAULT_WALLET_CURRENCY
) -> ProvisioningResult:
    """Ensure ``user`` has a financial customer, account and initial wallet.

    Result-idempotent: calling it repeatedly converges on exactly one customer,
    one account and one wallet per currency. Concurrent callers are serialised
    by the unique constraints on those tables rather than by application
    locking.

    Nothing external is contacted, no account number is generated, and no
    balance is created — a wallet is a container, and the ledger (M2) owns what
    money exists.
    """
    customer, customer_created = FinancialCustomer.objects.get_or_create(
        user=user,
        defaults={'status': CustomerStatus.ACTIVE},
    )

    account, account_created = FinancialAccount.objects.get_or_create(
        customer=customer,
        defaults={'status': AccountStatus.PENDING_ACTIVATION},
    )

    wallet, wallet_created = Wallet.objects.get_or_create(
        financial_account=account,
        currency=currency,
        defaults={'status': WalletStatus.ACTIVE},
    )

    return ProvisioningResult(
        customer=customer,
        account=account,
        wallet=wallet,
        customer_created=customer_created,
        account_created=account_created,
        wallet_created=wallet_created,
    )


def get_financial_relationship(user: User) -> FinancialCustomer | None:
    """The user's financial customer, or None when they have no relationship.

    Absence is a normal state, not an error: a Tier 0 user is entitled to use
    every legacy SpendWise feature without one. Callers that genuinely require
    an account raise
    :class:`~moneycore.domain.errors.FinancialAccountNotFoundError` themselves.
    """
    return (
        FinancialCustomer.objects
        .select_related('financial_account')
        .prefetch_related('financial_account__wallets')
        .filter(user=user)
        .first()
    )
