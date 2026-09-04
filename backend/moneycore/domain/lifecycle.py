"""Lifecycle state machines for the financial account and wallet.

Pure domain rules: which states exist and which moves between them are legal.
Nothing here touches the database — the service layer applies these decisions.

The two machines are deliberately separate. A financial account and a wallet
answer different questions, and copying one enum onto the other would invent
states that mean nothing.
"""

from __future__ import annotations

from typing import Final

# --------------------------------------------------------------------------
# Financial account
# --------------------------------------------------------------------------
# The smallest set that captures a real, distinct operational situation:
#
#   PENDING_ACTIVATION  provisioned, but not usable yet — the "activation
#                       path" state. Named explicitly because SpendWise will
#                       later have pending KYC, provider, compliance and
#                       transaction states, and a bare PENDING would not say
#                       which of them it meant.
#   ACTIVE     usable
#   SUSPENDED  temporarily disabled; reversible
#   CLOSED     terminal
#
# RESTRICTED is deliberately NOT a state here. The architecture requires
# restrictions to be capability-driven (canSend, canReceive, ...) rather than a
# generic "account restricted", and those capabilities arrive with M9. Adding a
# RESTRICTED state at M1 — where there are no operations to restrict — would
# create a state with no behavioural distinction from SUSPENDED.


class AccountStatus:
    PENDING_ACTIVATION: Final = 'pending_activation'
    ACTIVE: Final = 'active'
    SUSPENDED: Final = 'suspended'
    CLOSED: Final = 'closed'

    CHOICES: Final = [
        (PENDING_ACTIVATION, 'Pending activation'),
        (ACTIVE, 'Active'),
        (SUSPENDED, 'Suspended'),
        (CLOSED, 'Closed'),
    ]

    ALL: Final = frozenset({PENDING_ACTIVATION, ACTIVE, SUSPENDED, CLOSED})
    TERMINAL: Final = frozenset({CLOSED})


# Every legal move. Anything absent is rejected, including no-op self
# transitions: re-activating an already active account is a caller mistake, not
# a silent success.
ACCOUNT_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    AccountStatus.PENDING_ACTIVATION: frozenset(
        {AccountStatus.ACTIVE, AccountStatus.CLOSED}
    ),
    AccountStatus.ACTIVE: frozenset({AccountStatus.SUSPENDED, AccountStatus.CLOSED}),
    AccountStatus.SUSPENDED: frozenset({AccountStatus.ACTIVE, AccountStatus.CLOSED}),
    AccountStatus.CLOSED: frozenset(),
}


def can_transition_account(current: str, target: str) -> bool:
    """Whether an account may move from ``current`` to ``target``."""
    return target in ACCOUNT_TRANSITIONS.get(current, frozenset())


def account_is_terminal(status: str) -> bool:
    return status in AccountStatus.TERMINAL


# --------------------------------------------------------------------------
# Wallet
# --------------------------------------------------------------------------
# A wallet is a currency container. It is not independently suspendable in the
# current architecture — suspending money movement is an account-level or
# capability-level concern — so it has only the two states that carry a real
# distinction: it is usable, or it is permanently closed.


class WalletStatus:
    ACTIVE: Final = 'active'
    CLOSED: Final = 'closed'

    CHOICES: Final = [
        (ACTIVE, 'Active'),
        (CLOSED, 'Closed'),
    ]

    ALL: Final = frozenset({ACTIVE, CLOSED})
    TERMINAL: Final = frozenset({CLOSED})


WALLET_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    WalletStatus.ACTIVE: frozenset({WalletStatus.CLOSED}),
    WalletStatus.CLOSED: frozenset(),
}


def can_transition_wallet(current: str, target: str) -> bool:
    """Whether a wallet may move from ``current`` to ``target``."""
    return target in WALLET_TRANSITIONS.get(current, frozenset())


def wallet_is_terminal(status: str) -> bool:
    return status in WalletStatus.TERMINAL


# --------------------------------------------------------------------------
# Financial customer
# --------------------------------------------------------------------------
# The customer relationship is either live or ended. Operational nuance belongs
# to the account, not to the relationship.


class CustomerStatus:
    ACTIVE: Final = 'active'
    CLOSED: Final = 'closed'

    CHOICES: Final = [
        (ACTIVE, 'Active'),
        (CLOSED, 'Closed'),
    ]

    ALL: Final = frozenset({ACTIVE, CLOSED})
