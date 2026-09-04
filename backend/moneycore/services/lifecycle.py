"""Lifecycle transitions for financial accounts and wallets.

Every status change goes through here. Assigning ``account.status`` directly
bypasses the rules in :mod:`moneycore.domain.lifecycle` and is not a supported
way to move an account — these functions are the boundary M2–M8 will extend
rather than a convenience wrapper.

Nothing in this module asserts that verification happened. Activating an account
at M1 is a domain transition, not a claim about KYC, and it is deliberately not
exposed to customers (see :mod:`moneycore.api.views`).
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from moneycore.domain.errors import (
    InvalidAccountTransitionError,
    InvalidWalletTransitionError,
    WalletAlreadyExistsError,
)
from moneycore.domain.lifecycle import (
    AccountStatus,
    WalletStatus,
    can_transition_account,
    can_transition_wallet,
)
from moneycore.models import FinancialAccount, Wallet


@transaction.atomic
def transition_account(account: FinancialAccount, target_status: str) -> FinancialAccount:
    """Move ``account`` to ``target_status``, or refuse.

    Refuses unknown targets, illegal moves, and no-op self transitions — asking
    to activate an already-active account is a caller mistake worth surfacing,
    not a silent success.
    """
    if target_status not in AccountStatus.ALL:
        raise InvalidAccountTransitionError(
            f'{target_status!r} is not a financial account status.',
            details={'current': account.status, 'requested': target_status},
        )

    if not can_transition_account(account.status, target_status):
        raise InvalidAccountTransitionError(
            f'A {account.status} account cannot become {target_status}.',
            details={'current': account.status, 'requested': target_status},
        )

    updated_fields = ['status', 'updated_at']
    account.status = target_status

    if target_status == AccountStatus.ACTIVE and account.activated_at is None:
        # Set once, on first activation: it records when the account first
        # became usable, not the most recent un-suspension.
        account.activated_at = timezone.now()
        updated_fields.append('activated_at')

    if target_status == AccountStatus.CLOSED:
        account.closed_at = timezone.now()
        updated_fields.append('closed_at')

    account.save(update_fields=updated_fields)
    return account


@transaction.atomic
def transition_wallet(wallet: Wallet, target_status: str) -> Wallet:
    """Move ``wallet`` to ``target_status``, or refuse."""
    if target_status not in WalletStatus.ALL:
        raise InvalidWalletTransitionError(
            f'{target_status!r} is not a wallet status.',
            details={'current': wallet.status, 'requested': target_status},
        )

    if not can_transition_wallet(wallet.status, target_status):
        raise InvalidWalletTransitionError(
            f'A {wallet.status} wallet cannot become {target_status}.',
            details={'current': wallet.status, 'requested': target_status},
        )

    wallet.status = target_status
    wallet.save(update_fields=['status', 'updated_at'])
    return wallet


@transaction.atomic
def open_wallet(account: FinancialAccount, currency: str) -> Wallet:
    """Open an additional currency wallet on ``account``.

    Refuses a duplicate currency, and refuses to open a wallet under a closed
    account — a container on a terminated account could never be used.
    """
    if account.status == AccountStatus.CLOSED:
        raise InvalidAccountTransitionError(
            'A wallet cannot be opened on a closed account.',
            details={'current': account.status},
        )

    if Wallet.objects.filter(financial_account=account, currency=currency).exists():
        raise WalletAlreadyExistsError(
            f'This account already has a {currency} wallet.',
            details={'currency': currency},
        )

    wallet = Wallet(
        financial_account=account,
        currency=currency,
        status=WalletStatus.ACTIVE,
    )
    # Runs the currency-format validator before the row reaches the database, so
    # a bad code is a domain-shaped failure rather than an IntegrityError.
    wallet.full_clean(exclude=['financial_account'])
    wallet.save()
    return wallet
