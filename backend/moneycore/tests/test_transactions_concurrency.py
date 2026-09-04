"""Concurrency proofs for the transaction engine.

The engine's promise is that a financial operation happens **once**: one
transaction per intent, one resolution per transaction, one journal per success.
Every proof here is a real race — threads on independent connections released
together from a barrier — not a simulated one.

``SELECT ... FOR UPDATE`` is a no-op on SQLite, so these are PostgreSQL
guarantees and skip elsewhere with a stated reason rather than passing
vacuously. PostgreSQL remains authoritative for database semantics.
"""

import threading

import pytest
from django.contrib.auth.models import User
from django.db import connection, connections

from moneycore.domain.errors import (
    InvalidTransactionTransitionError,
    TransactionAlreadyResolvedError,
    TransactionHoldInvalidError,
)
from moneycore.domain.holds import HoldStatus
from moneycore.domain.money import Money
from moneycore.domain.transactions import TransactionDirection, TransactionStatus
from moneycore.models import FinancialTransaction, Journal
from moneycore.services.holds import create_hold, wallet_balance_projection
from moneycore.services.ledger import credit, debit
from moneycore.services.provisioning import provision_financial_account
from moneycore.services.transactions import (
    attach_hold,
    create_transaction,
    fail_transaction,
    mark_unknown,
    start_processing,
    succeed_transaction,
)

pytestmark = pytest.mark.django_db(transaction=True)

on_postgres = pytest.mark.skipif(
    connection.vendor != 'postgresql',
    reason='Row locking (SELECT ... FOR UPDATE) is a no-op on SQLite.',
)


def run_in_threads(target, count):
    """Run ``target`` in ``count`` threads, all released from one barrier."""
    results, errors = [], []
    barrier = threading.Barrier(count)

    def wrapped(index):
        try:
            barrier.wait(timeout=15)
            results.append(target(index))
        except Exception as exc:  # noqa: BLE001 - recorded for assertion
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=wrapped, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    return results, errors


def reload(txn):
    return FinancialTransaction.objects.get(pk=txn.pk)


@pytest.fixture
def processing(funded_wallet):
    """posted 10 000, hold 7 000, transaction PROCESSING."""
    txn = create_transaction(
        funded_wallet, TransactionDirection.OUTGOING, 7_000,
        idempotency_key='race',
    )
    return start_processing(attach_hold(txn, create_hold(funded_wallet, 7_000)))


def spend(wallet_account, counterpart_account, amount=7_000):
    return [debit(wallet_account, amount), credit(counterpart_account, amount)]


# --------------------------------------------------------------------------
# Creation
# --------------------------------------------------------------------------


@on_postgres
class TestConcurrentCreationWithTheSameKey:
    """Two callers, one key: exactly one transaction may exist."""

    def test_only_one_transaction_is_created(self, wallet):
        def create(index):
            return create_transaction(
                wallet, TransactionDirection.OUTGOING, 1_000,
                idempotency_key='same-key',
            ).pk

        results, errors = run_in_threads(create, 2)

        assert errors == []
        assert FinancialTransaction.objects.filter(wallet=wallet).count() == 1

    def test_both_callers_receive_that_same_transaction(self, wallet):
        def create(index):
            return create_transaction(
                wallet, TransactionDirection.OUTGOING, 1_000,
                idempotency_key='same-key-2',
            ).pk

        results, errors = run_in_threads(create, 2)

        assert errors == []
        assert len(set(results)) == 1

    def test_eight_simultaneous_callers_still_produce_one(self, wallet):
        def create(index):
            return create_transaction(
                wallet, TransactionDirection.OUTGOING, 1_000,
                idempotency_key='same-key-3',
            ).pk

        results, errors = run_in_threads(create, 8)

        assert errors == []
        assert len(set(results)) == 1
        assert FinancialTransaction.objects.filter(wallet=wallet).count() == 1

    def test_distinct_keys_race_without_interfering(self, wallet):
        def create(index):
            return create_transaction(
                wallet, TransactionDirection.OUTGOING, 1_000,
                idempotency_key=f'distinct-{index}',
            ).pk

        results, errors = run_in_threads(create, 5)

        assert errors == []
        assert len(set(results)) == 5

    def test_two_wallets_racing_on_one_key_get_one_each(self, wallet):
        other = provision_financial_account(
            User.objects.create_user('race-other-user')
        ).wallet
        wallets = [wallet, other]

        def create(index):
            return create_transaction(
                wallets[index % 2], TransactionDirection.OUTGOING, 1_000,
                idempotency_key='cross-wallet',
            ).pk

        results, errors = run_in_threads(create, 4)

        assert errors == []
        assert FinancialTransaction.objects.filter(
            idempotency_key='cross-wallet'
        ).count() == 2


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


@on_postgres
class TestConcurrentResolutionOfAnUnknownTransaction:
    """The dangerous race: was it success or failure? Only one may land."""

    @pytest.fixture
    def unresolved(self, processing):
        return mark_unknown(processing)

    def test_success_and_failure_cannot_both_win(
        self, unresolved, wallet_account, counterpart_account
    ):
        def resolve(index):
            txn = reload(unresolved)
            if index == 0:
                return succeed_transaction(
                    txn, entries=spend(wallet_account, counterpart_account)
                ).status
            return fail_transaction(txn, failure_code='declined').status

        results, errors = run_in_threads(resolve, 2)

        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], TransactionAlreadyResolvedError)

    def test_the_final_state_matches_whichever_won(
        self, unresolved, wallet_account, counterpart_account
    ):
        def resolve(index):
            txn = reload(unresolved)
            if index == 0:
                return succeed_transaction(
                    txn, entries=spend(wallet_account, counterpart_account)
                ).status
            return fail_transaction(txn, failure_code='declined').status

        results, _ = run_in_threads(resolve, 2)
        winner = results[0]
        final = reload(unresolved)

        assert final.status == winner
        if winner == TransactionStatus.SUCCEEDED:
            assert final.journal_id is not None
            assert wallet_balance_projection(
                final.wallet
            ).posted == Money(3_000, 'NGN')
        else:
            assert final.journal_id is None
            assert wallet_balance_projection(
                final.wallet
            ).posted == Money(10_000, 'NGN')

    def test_the_reservation_ends_up_released_either_way(
        self, unresolved, wallet_account, counterpart_account
    ):
        def resolve(index):
            txn = reload(unresolved)
            if index == 0:
                return succeed_transaction(
                    txn, entries=spend(wallet_account, counterpart_account)
                ).status
            return fail_transaction(txn, failure_code='declined').status

        run_in_threads(resolve, 2)

        unresolved.hold.refresh_from_db()
        assert unresolved.hold.status == HoldStatus.RELEASED
        assert wallet_balance_projection(unresolved.wallet).held == Money(0, 'NGN')

    def test_two_concurrent_failures_resolve_once(self, unresolved):
        def fail(index):
            return fail_transaction(
                reload(unresolved), failure_code=f'code-{index}'
            ).failure_code

        results, errors = run_in_threads(fail, 2)

        assert len(results) == 1
        assert len(errors) == 1
        assert reload(unresolved).failure_code == results[0]


@on_postgres
class TestConcurrentUnknownAndSuccess:
    """PROCESSING raced by "we don't know" and "it worked".

    Both orderings are legitimate — UNKNOWN is not terminal, so a success may
    legally follow it. What must hold in every ordering is that the operation
    ends SUCCEEDED exactly once, with exactly one journal.
    """

    def _race(self, processing, wallet_account, counterpart_account):
        def act(index):
            txn = reload(processing)
            if index == 0:
                return succeed_transaction(
                    txn, entries=spend(wallet_account, counterpart_account)
                ).status
            return mark_unknown(txn).status

        return run_in_threads(act, 2)

    def test_the_operation_ends_succeeded(
        self, processing, wallet_account, counterpart_account
    ):
        self._race(processing, wallet_account, counterpart_account)

        assert reload(processing).status == TransactionStatus.SUCCEEDED

    def test_exactly_one_journal_is_posted(
        self, processing, wallet_account, counterpart_account
    ):
        before = Journal.objects.count()

        self._race(processing, wallet_account, counterpart_account)

        assert Journal.objects.count() == before + 1

    def test_the_balances_are_posted_exactly_once(
        self, processing, wallet_account, counterpart_account
    ):
        self._race(processing, wallet_account, counterpart_account)

        projection = wallet_balance_projection(processing.wallet)
        assert projection.posted == Money(3_000, 'NGN')
        assert projection.held == Money(0, 'NGN')
        assert projection.available == Money(3_000, 'NGN')

    def test_marking_unknown_after_success_is_refused(
        self, processing, wallet_account, counterpart_account
    ):
        """If success won the race, the ambiguity report must be rejected."""
        succeed_transaction(
            processing, entries=spend(wallet_account, counterpart_account)
        )

        with pytest.raises(TransactionAlreadyResolvedError):
            mark_unknown(reload(processing))


@on_postgres
class TestDuplicateSuccess:
    """The one that must never happen twice: money posted twice for one intent."""

    def test_only_one_success_lands(
        self, processing, wallet_account, counterpart_account
    ):
        def succeed(index):
            return succeed_transaction(
                reload(processing), entries=spend(wallet_account, counterpart_account)
            ).pk

        results, errors = run_in_threads(succeed, 2)

        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], TransactionAlreadyResolvedError)

    def test_only_one_journal_exists(
        self, processing, wallet_account, counterpart_account
    ):
        before = Journal.objects.count()

        def succeed(index):
            return succeed_transaction(
                reload(processing), entries=spend(wallet_account, counterpart_account)
            ).pk

        run_in_threads(succeed, 2)

        assert Journal.objects.count() == before + 1

    def test_the_money_moves_once(
        self, processing, wallet_account, counterpart_account
    ):
        def succeed(index):
            return succeed_transaction(
                reload(processing), entries=spend(wallet_account, counterpart_account)
            ).pk

        run_in_threads(succeed, 2)

        assert wallet_balance_projection(
            processing.wallet
        ).posted == Money(3_000, 'NGN')

    def test_five_simultaneous_successes_still_post_once(
        self, processing, wallet_account, counterpart_account
    ):
        before = Journal.objects.count()

        def succeed(index):
            return succeed_transaction(
                reload(processing), entries=spend(wallet_account, counterpart_account)
            ).pk

        results, errors = run_in_threads(succeed, 5)

        assert len(results) == 1
        assert len(errors) == 4
        assert all(
            isinstance(error, TransactionAlreadyResolvedError) for error in errors
        )
        assert Journal.objects.count() == before + 1
        assert wallet_balance_projection(
            processing.wallet
        ).posted == Money(3_000, 'NGN')


# --------------------------------------------------------------------------
# Earlier transitions
# --------------------------------------------------------------------------


@on_postgres
class TestConcurrentStarts:
    """Execution begins once, even when two callers race to begin it."""

    @pytest.fixture
    def created(self, funded_wallet):
        txn = create_transaction(
            funded_wallet, TransactionDirection.OUTGOING, 7_000,
            idempotency_key='start-race',
        )
        return attach_hold(txn, create_hold(funded_wallet, 7_000))

    def test_two_concurrent_starts_start_once(self, created):
        def start(index):
            return start_processing(reload(created)).pk

        results, errors = run_in_threads(start, 2)

        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], InvalidTransactionTransitionError)

    def test_the_transaction_records_one_start(self, created):
        def start(index):
            return start_processing(reload(created)).processing_at

        run_in_threads(start, 2)

        final = reload(created)
        assert final.status == TransactionStatus.PROCESSING
        assert final.processing_at is not None


@on_postgres
class TestConcurrentHoldAttachment:
    """One reservation can fund at most one operation, even under a race."""

    def test_two_transactions_cannot_share_a_hold(self, funded_wallet):
        hold = create_hold(funded_wallet, 3_000)
        transactions = [
            create_transaction(
                funded_wallet, TransactionDirection.OUTGOING, 3_000,
                idempotency_key=f'attach-{index}',
            )
            for index in range(2)
        ]

        def attach(index):
            return attach_hold(reload(transactions[index]), hold).pk

        results, errors = run_in_threads(attach, 2)

        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], TransactionHoldInvalidError)

    def test_the_losing_transaction_has_no_reservation(self, funded_wallet):
        hold = create_hold(funded_wallet, 3_000)
        transactions = [
            create_transaction(
                funded_wallet, TransactionDirection.OUTGOING, 3_000,
                idempotency_key=f'attach-loser-{index}',
            )
            for index in range(2)
        ]

        def attach(index):
            return attach_hold(reload(transactions[index]), hold).pk

        results, _ = run_in_threads(attach, 2)

        winner = results[0]
        loser = next(t for t in transactions if t.pk != winner)
        assert reload(loser).hold_id is None
        assert FinancialTransaction.objects.filter(hold=hold).count() == 1
