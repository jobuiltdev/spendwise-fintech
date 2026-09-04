"""The financial transaction: what operation SpendWise is currently attempting.

Pure domain: direction, lifecycle states, and the transitions between them.
Nothing here touches the database or knows anything about a provider.

Three concepts, kept apart
--------------------------
* **Ledger** — what money has posted (M2).
* **Hold** — what posted money is temporarily reserved (M3).
* **Transaction** — what operation is happening, and where it has got to (M4).

A transaction is not a transfer, not a provider attempt and not a payment. It
carries no bank, no recipient, no rail and no provider vocabulary; M5 owns
transfers and M6 the provider abstraction. What lives here is only the
question *"what are we attempting, and what do we authoritatively know about
it?"*.

Nor is it a draft
-----------------
**Backing out before execution is accepted does not create a financial
transaction.** There is no cancellation state and no abandonment transition,
because a `FinancialTransaction` is not a persisted draft of a UI flow — it
exists because SpendWise is attempting something with money. If persisted
drafts are ever needed, they belong to the transfer/product layer, not here.
This follows the locked architecture §8.5.

Why UNKNOWN is first class
--------------------------
The single most important state in this module is ``UNKNOWN``. Once an
operation may have been executed, an absent or ambiguous answer is **not a
failure** — it is an admission that SpendWise cannot yet say. Treating that as
failure is how a system tells a customer their money did not move when it did.

So the state machine has no path from ambiguity to ``FAILED``: reaching
``FAILED`` requires *authoritative knowledge* that nothing moved. ``UNKNOWN`` is
persisted, survives any restart, and is deliberately **not terminal** — it is
waiting for the truth, not concluding. (Customer-facing wording for this state
is "Confirming"; that word belongs to the product layer, never here.)
"""

from __future__ import annotations

from typing import Final

# --------------------------------------------------------------------------
# Direction
# --------------------------------------------------------------------------
# Which way money moves relative to the customer's wallet. Deliberately just
# two values: business operation types (card payment, refund, cash-out, fee)
# describe *what kind* of operation it is, which is M5's concern, not this
# module's.


class TransactionDirection:
    OUTGOING: Final = 'outgoing'
    INCOMING: Final = 'incoming'

    CHOICES: Final = [
        (OUTGOING, 'Outgoing'),
        (INCOMING, 'Incoming'),
    ]

    ALL: Final = frozenset({OUTGOING, INCOMING})


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


class TransactionStatus:
    #: Intent exists; nothing external has been attempted.
    CREATED: Final = 'created'
    #: Execution has started; no definitive outcome recorded yet.
    PROCESSING: Final = 'processing'
    #: Execution may have happened. SpendWise cannot yet truthfully say which.
    UNKNOWN: Final = 'unknown'
    #: Authoritative success, with the corresponding financial truth posted.
    SUCCEEDED: Final = 'succeeded'
    #: Authoritative failure. The money is known not to have moved.
    FAILED: Final = 'failed'

    CHOICES: Final = [
        (CREATED, 'Created'),
        (PROCESSING, 'Processing'),
        (UNKNOWN, 'Unknown'),
        (SUCCEEDED, 'Succeeded'),
        (FAILED, 'Failed'),
    ]

    ALL: Final = frozenset({CREATED, PROCESSING, UNKNOWN, SUCCEEDED, FAILED})

    #: Reached a final answer. UNKNOWN is deliberately absent — it is unresolved,
    #: not concluded.
    TERMINAL: Final = frozenset({SUCCEEDED, FAILED})

    #: States where execution may already have happened, so the outcome cannot
    #: be assumed.
    EXECUTION_MAY_HAVE_STARTED: Final = frozenset({PROCESSING, UNKNOWN})


# Every legal move. Anything absent is refused, including no-op self
# transitions: re-marking a PROCESSING transaction as PROCESSING is a caller
# mistake, not a silent success.
TRANSACTION_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    # Nothing has been attempted yet, so the only outcome reachable without
    # execution is an authoritative failure on a pre-execution check that
    # genuinely failed.
    TransactionStatus.CREATED: frozenset({
        TransactionStatus.PROCESSING,
        TransactionStatus.FAILED,
    }),
    # In flight. Any of the three outcomes is reachable — including the honest
    # "we do not know".
    TransactionStatus.PROCESSING: frozenset({
        TransactionStatus.SUCCEEDED,
        TransactionStatus.FAILED,
        TransactionStatus.UNKNOWN,
    }),
    # Waiting for the truth. It can only resolve to an authoritative answer;
    # notably it can NOT go back to PROCESSING, because "try again" would
    # discard the fact that an execution may already have happened.
    TransactionStatus.UNKNOWN: frozenset({
        TransactionStatus.SUCCEEDED,
        TransactionStatus.FAILED,
    }),
    TransactionStatus.SUCCEEDED: frozenset(),
    TransactionStatus.FAILED: frozenset(),
}


def can_transition(current: str, target: str) -> bool:
    """Whether a transaction may move from ``current`` to ``target``."""
    return target in TRANSACTION_TRANSITIONS.get(current, frozenset())


def is_terminal(status: str) -> bool:
    """Whether the outcome is settled. UNKNOWN is not."""
    return status in TransactionStatus.TERMINAL


def execution_may_have_started(status: str) -> bool:
    """Whether the operation might already have executed externally."""
    return status in TransactionStatus.EXECUTION_MAY_HAVE_STARTED


def requires_reservation(direction: str) -> bool:
    """Whether starting execution needs funds reserved first.

    Outgoing money leaves the customer's wallet, so it must be reserved before
    execution begins or the same funds could be spent twice. Incoming money adds
    to the wallet and reserves nothing.
    """
    return direction == TransactionDirection.OUTGOING
