"""The central M3 invariant: concurrent holds cannot oversubscribe a wallet.

Group F (creation concurrency) and G (release concurrency).

Real threads on real connections, released together from a barrier, so on
PostgreSQL these are genuine parallel transactions. SQLite's
``SELECT ... FOR UPDATE`` is a no-op, so the guarantee is a PostgreSQL guarantee
and these skip there with a stated reason rather than passing vacuously.
"""

import threading

import pytest
from django.db import connection, connections

from moneycore.domain.errors import InsufficientAvailableBalanceError
from moneycore.domain.holds import HoldStatus
from moneycore.domain.money import Money
from moneycore.models import FundsHold
from moneycore.services.holds import (
    create_hold,
    held_amount,
    release_hold,
    wallet_balance_projection,
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


@on_postgres
class TestOversubscriptionIsImpossible:
    def test_two_concurrent_holds_of_seven_thousand_against_ten_thousand(
        self, funded_wallet
    ):
        """The canonical race: both read 10 000 available, both want 7 000."""

        def hold(index):
            return create_hold(funded_wallet, 7_000).pk

        results, errors = run_in_threads(hold, 2)

        assert len(results) == 1, 'both holds committed — wallet oversubscribed'
        assert len(errors) == 1
        assert isinstance(errors[0], InsufficientAvailableBalanceError)

    def test_the_wallet_is_left_coherent_after_that_race(self, funded_wallet):
        def hold(index):
            return create_hold(funded_wallet, 7_000).pk

        run_in_threads(hold, 2)

        projection = wallet_balance_projection(funded_wallet)
        assert projection.posted == Money(10_000, 'NGN')
        assert projection.held == Money(7_000, 'NGN')
        assert projection.available == Money(3_000, 'NGN')

    def test_ten_concurrent_holds_of_one_thousand_all_fit_exactly(
        self, funded_wallet
    ):
        """Exactly consumes the balance: all ten must succeed."""

        def hold(index):
            return create_hold(funded_wallet, 1_000).pk

        results, errors = run_in_threads(hold, 10)

        assert errors == []
        assert len(results) == 10

        projection = wallet_balance_projection(funded_wallet)
        assert projection.held == Money(10_000, 'NGN')
        assert projection.available == Money(0, 'NGN')

    def test_ten_concurrent_holds_of_one_thousand_and_one_cannot_all_fit(
        self, funded_wallet
    ):
        """10 × 1001 = 10 010 > 10 000, so at most nine may succeed."""

        def hold(index):
            return create_hold(funded_wallet, 1_001).pk

        results, errors = run_in_threads(hold, 10)

        assert len(results) <= 9
        assert len(errors) == 10 - len(results)
        assert all(
            isinstance(e, InsufficientAvailableBalanceError) for e in errors
        )

    def test_the_invariant_is_never_exceeded_in_that_race(self, funded_wallet):
        def hold(index):
            return create_hold(funded_wallet, 1_001).pk

        run_in_threads(hold, 10)

        projection = wallet_balance_projection(funded_wallet)
        assert projection.held <= projection.posted
        assert projection.available.minor_units >= 0

    def test_a_large_contended_race_never_oversubscribes(self, funded_wallet):
        """20 threads competing for 10 000 in 1 500 chunks: at most six fit."""

        def hold(index):
            return create_hold(funded_wallet, 1_500).pk

        results, _ = run_in_threads(hold, 20)

        assert len(results) <= 6
        assert held_amount(funded_wallet) <= Money(10_000, 'NGN')

    def test_concurrent_creation_posts_no_journal(self, funded_wallet):
        from moneycore.models import Journal

        before = Journal.objects.count()

        run_in_threads(lambda index: create_hold(funded_wallet, 1_000).pk, 10)

        assert Journal.objects.count() == before


@on_postgres
class TestReleaseConcurrency:
    def test_only_one_of_many_parallel_releases_succeeds(self, funded_wallet):
        from moneycore.domain.errors import HoldNotActiveError

        hold = create_hold(funded_wallet, 5_000)

        def release(index):
            return release_hold(hold).pk

        results, errors = run_in_threads(release, 6)

        assert len(results) == 1
        assert all(isinstance(e, HoldNotActiveError) for e in errors)

    def test_a_released_hold_records_exactly_one_release_time(self, funded_wallet):
        hold = create_hold(funded_wallet, 5_000)

        run_in_threads(lambda index: release_hold(hold).pk, 6)

        hold.refresh_from_db()
        assert hold.status == HoldStatus.RELEASED
        assert hold.released_at is not None
        assert hold.expired_at is None

    def test_funds_freed_by_a_release_are_seen_by_a_later_creation(
        self, funded_wallet
    ):
        hold = create_hold(funded_wallet, 10_000)
        release_hold(hold)

        results, errors = run_in_threads(
            lambda index: create_hold(funded_wallet, 10_000).pk, 2
        )

        # The released funds are reservable again — but still only once.
        assert len(results) == 1
        assert len(errors) == 1


class TestConcurrencyReasoningOnEveryEngine:
    """Engine-independent facts about why the projection cannot be corrupted."""

    def test_no_mutable_balance_exists_to_race_for(self):
        from moneycore.models import FinancialAccount, LedgerAccount, Wallet

        for model in (Wallet, LedgerAccount, FinancialAccount, FundsHold):
            names = {f.name for f in model._meta.get_fields()}
            assert not any('balance' in name for name in names)

    def test_the_projection_is_recomputed_from_rows_on_every_read(
        self, funded_wallet
    ):
        assert wallet_balance_projection(funded_wallet).held == Money(0, 'NGN')

        create_hold(funded_wallet, 1)

        # No cache to invalidate: the next read simply sees the new row.
        assert wallet_balance_projection(funded_wallet).held == Money(1, 'NGN')

    def test_hold_creation_locks_the_wallet_row(self, funded_wallet, monkeypatch):
        """The lock is the gate; assert it is actually taken."""
        from moneycore.models import Wallet

        observed = {}
        real_manager = Wallet.objects.select_for_update

        def spy(*args, **kwargs):
            observed['locked'] = True
            return real_manager(*args, **kwargs)

        monkeypatch.setattr(Wallet.objects, 'select_for_update', spy)

        create_hold(funded_wallet, 100)

        assert observed.get('locked') is True

    def test_the_lock_is_not_a_balance_row(self):
        """Nothing was invented to have something to lock."""
        from moneycore.models import Wallet

        concrete = {f.name for f in Wallet._meta.get_fields() if f.concrete}

        assert concrete == {
            'id', 'financial_account', 'currency', 'status',
            'created_at', 'updated_at',
        }


@on_postgres
class TestLedgerPostingRacesAgainstHoldCreation:
    """The invariant that spans M2 and M3: held may never exceed posted.

    A journal can *decrease* a wallet-backed account's balance — a debit against
    a credit-normal wallet account does exactly that — so posting and reserving
    contend for the same funds. Both operations lock the wallet row, so whichever
    reaches it first wins and the other sees the committed reality.
    """

    def _spend_journal(self, wallet_account, counterpart_account, amount):
        """A balanced journal that reduces the wallet account's balance."""
        from moneycore.services.ledger import credit, debit, post_journal

        return post_journal(
            currency='NGN',
            entries=[
                debit(wallet_account, amount),
                credit(counterpart_account, amount),
            ],
            description='Outbound',
        )

    def test_a_hold_and_a_conflicting_debit_cannot_both_commit(
        self, funded_wallet, wallet_account, counterpart_account
    ):
        """posted=10 000, held=0. Hold 7 000 races a debit of 7 000."""
        from moneycore.domain.errors import (
            InsufficientAvailableBalanceError,
            LedgerPostingConflictsWithHoldsError,
        )

        def contend(index):
            if index == 0:
                return ('hold', create_hold(funded_wallet, 7_000).pk)
            return (
                'debit',
                self._spend_journal(wallet_account, counterpart_account, 7_000).pk,
            )

        results, errors = run_in_threads(contend, 2)

        assert len(results) == 1, 'both operations committed — invalid state'
        assert len(errors) == 1
        assert isinstance(
            errors[0],
            (InsufficientAvailableBalanceError, LedgerPostingConflictsWithHoldsError),
        )

    def test_the_forbidden_state_never_results(
        self, funded_wallet, wallet_account, counterpart_account
    ):
        """Never posted=3 000 with held=7 000, whichever operation wins."""

        def contend(index):
            if index == 0:
                return create_hold(funded_wallet, 7_000).pk
            return self._spend_journal(
                wallet_account, counterpart_account, 7_000
            ).pk

        run_in_threads(contend, 2)

        projection = wallet_balance_projection(funded_wallet)
        assert projection.held <= projection.posted
        assert projection.available.minor_units >= 0
        assert (projection.posted, projection.held) in {
            (Money(10_000, 'NGN'), Money(7_000, 'NGN')),  # hold won
            (Money(3_000, 'NGN'), Money(0, 'NGN')),       # debit won
        }

    def test_repeated_races_never_break_the_invariant(
        self, wallet, wallet_account, counterpart_account, fund_wallet
    ):
        fund_wallet(wallet_account, 10_000)

        def contend(index):
            if index % 2 == 0:
                return create_hold(wallet, 3_000).pk
            return self._spend_journal(wallet_account, counterpart_account, 3_000).pk

        run_in_threads(contend, 8)

        projection = wallet_balance_projection(wallet)
        assert projection.held <= projection.posted
        assert projection.available.minor_units >= 0

    def test_an_incoming_credit_racing_a_hold_never_creates_an_invalid_state(
        self, funded_wallet, wallet_account, counterpart_account
    ):
        """The benign direction: a credit only ever adds funds.

        The hold may be conservatively refused if it evaluates before the credit
        is visible; what must never happen is an incoherent projection.
        """
        from moneycore.services.ledger import credit, debit, post_journal

        def contend(index):
            if index % 2 == 0:
                return create_hold(funded_wallet, 5_000).pk
            return post_journal(
                currency='NGN',
                entries=[
                    debit(counterpart_account, 5_000),
                    credit(wallet_account, 5_000),
                ],
            ).pk

        run_in_threads(contend, 6)

        projection = wallet_balance_projection(funded_wallet)
        assert projection.held <= projection.posted
        assert projection.available.minor_units >= 0

    def test_a_debit_is_refused_once_funds_are_reserved(
        self, funded_wallet, wallet_account, counterpart_account
    ):
        """Sequential proof of the guard, independent of thread scheduling."""
        from moneycore.domain.errors import LedgerPostingConflictsWithHoldsError

        create_hold(funded_wallet, 7_000)

        with pytest.raises(LedgerPostingConflictsWithHoldsError):
            self._spend_journal(wallet_account, counterpart_account, 7_000)

    def test_a_debit_within_unreserved_funds_still_posts(
        self, funded_wallet, wallet_account, counterpart_account
    ):
        create_hold(funded_wallet, 7_000)

        self._spend_journal(wallet_account, counterpart_account, 3_000)

        projection = wallet_balance_projection(funded_wallet)
        assert projection.posted == Money(7_000, 'NGN')
        assert projection.held == Money(7_000, 'NGN')
        assert projection.available == Money(0, 'NGN')


class TestDeterministicMultiWalletLocking:
    """Several wallet rows are always locked in ascending primary-key order.

    Without a fixed order, two journals touching the same pair of wallets could
    lock them oppositely and deadlock.
    """

    def test_the_lock_helper_returns_wallets_in_ascending_key_order(
        self, wallet, fund_wallet, wallet_account
    ):
        """The contract itself: locks are taken lowest primary key first."""
        from django.contrib.auth.models import User
        from django.db import transaction

        from moneycore.services.ledger import _lock_wallets
        from moneycore.services.provisioning import provision_financial_account

        others = [
            provision_financial_account(
                User.objects.create_user(f'order-owner-{i}')
            ).wallet
            for i in range(3)
        ]
        ids = [w.pk for w in others] + [wallet.pk]

        with transaction.atomic():
            # Deliberately shuffled input; the helper must still order it.
            locked = _lock_wallets(list(reversed(ids)))

        assert [w.pk for w in locked] == sorted(ids)

    def test_the_locking_query_orders_by_key_and_takes_a_row_lock(
        self, wallet, wallet_account, fund_wallet
    ):
        """On PostgreSQL, the emitted SQL must carry ORDER BY and FOR UPDATE."""
        if connection.vendor != 'postgresql':
            pytest.skip('FOR UPDATE is a no-op on SQLite.')

        from django.db import transaction
        from django.test.utils import CaptureQueriesContext

        from moneycore.services.ledger import _lock_wallets

        with CaptureQueriesContext(connection) as captured:
            with transaction.atomic():
                _lock_wallets([wallet.pk])

        locking = [q['sql'] for q in captured.captured_queries if 'FOR UPDATE' in q['sql']]
        assert locking, 'no row lock was taken'
        assert 'ORDER BY' in locking[0]

    def test_a_journal_touching_two_wallets_respects_both_reservations(
        self, wallet, wallet_account, fund_wallet
    ):
        from django.contrib.auth.models import User

        from moneycore.domain.errors import LedgerPostingConflictsWithHoldsError
        from moneycore.domain.ledger import LedgerAccountType
        from moneycore.services.ledger import (
            credit,
            debit,
            open_wallet_ledger_account,
            post_journal,
        )
        from moneycore.services.provisioning import provision_financial_account

        second_wallet = provision_financial_account(
            User.objects.create_user('second-owner')
        ).wallet
        second_account = open_wallet_ledger_account(
            second_wallet, account_type=LedgerAccountType.LIABILITY
        )
        fund_wallet(wallet_account, 5_000)
        create_hold(wallet, 5_000)

        # Debiting the fully-reserved first wallet must be refused, even though
        # the credit side would be perfectly fine.
        with pytest.raises(LedgerPostingConflictsWithHoldsError):
            post_journal(
                currency='NGN',
                entries=[debit(wallet_account, 1_000), credit(second_account, 1_000)],
            )

    def test_internal_accounts_are_not_locked_or_guarded(
        self, counterpart_account, make_ledger_account
    ):
        """An account with no wallet carries no customer reservations."""
        from moneycore.services.ledger import credit, debit, post_journal

        other = make_ledger_account('internal:unguarded')

        # Drives `other` negative in its normal direction: legitimate for an
        # internal account, and no reservation check applies.
        journal = post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 700), credit(other, 700)],
        )

        assert journal.entries.count() == 2


class TestReversalRespectsReservations:
    """Reversal posts through the same guarded path — no bypass."""

    def test_a_reversal_that_would_break_the_invariant_is_refused(
        self, funded_wallet, wallet_account, counterpart_account
    ):
        from moneycore.domain.errors import LedgerPostingConflictsWithHoldsError
        from moneycore.services.ledger import (
            credit,
            debit,
            post_journal,
            reverse_journal,
        )

        # Fund, then reserve everything.
        funding = post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 5_000), credit(wallet_account, 5_000)],
        )
        create_hold(funded_wallet, 15_000)

        # Reversing the funding would claw back 5 000 that is now reserved.
        with pytest.raises(LedgerPostingConflictsWithHoldsError):
            reverse_journal(funding)

    def test_the_refused_reversal_leaves_the_original_intact(
        self, funded_wallet, wallet_account, counterpart_account
    ):
        from moneycore.domain.errors import LedgerPostingConflictsWithHoldsError
        from moneycore.domain.ledger import JournalStatus
        from moneycore.models import Journal
        from moneycore.services.ledger import (
            credit,
            debit,
            post_journal,
            reverse_journal,
        )

        funding = post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 5_000), credit(wallet_account, 5_000)],
        )
        create_hold(funded_wallet, 15_000)

        with pytest.raises(LedgerPostingConflictsWithHoldsError):
            reverse_journal(funding)

        funding.refresh_from_db()
        assert funding.status == JournalStatus.POSTED
        assert not Journal.objects.filter(reverses=funding).exists()

    def test_a_reversal_within_unreserved_funds_still_posts(
        self, funded_wallet, wallet_account, counterpart_account
    ):
        from moneycore.services.ledger import (
            credit,
            debit,
            post_journal,
            reverse_journal,
        )

        funding = post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 5_000), credit(wallet_account, 5_000)],
        )
        create_hold(funded_wallet, 3_000)

        reversal = reverse_journal(funding)

        assert reversal.reverses_id == funding.pk
        projection = wallet_balance_projection(funded_wallet)
        assert projection.posted == Money(10_000, 'NGN')
        assert projection.held == Money(3_000, 'NGN')
