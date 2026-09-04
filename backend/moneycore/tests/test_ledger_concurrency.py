"""Concurrency. Group I.

These use real threads against real connections, so on PostgreSQL they exercise
genuine parallel transactions rather than two sequential calls wearing the word
"concurrency".

SQLite cannot support this honestly — pytest-django runs it in-memory per
connection, and its write locking is not the engine's target behaviour — so the
threaded tests skip there and say so, rather than passing vacuously.
"""

import threading

import pytest
from django.db import connection, connections, transaction

from moneycore.domain.errors import JournalAlreadyReversedError
from moneycore.domain.money import Money
from moneycore.models import Journal, JournalEntry
from moneycore.services.ledger import (
    credit,
    debit,
    post_journal,
    posted_balance,
    reverse_journal,
)

pytestmark = pytest.mark.django_db(transaction=True)

on_postgres = pytest.mark.skipif(
    connection.vendor != 'postgresql',
    reason='Threaded transaction behaviour is only meaningful on PostgreSQL.',
)


def run_in_threads(target, count):
    """Run ``target`` in ``count`` threads, collecting results and errors."""
    results, errors = [], []
    barrier = threading.Barrier(count)

    def wrapped(index):
        try:
            barrier.wait(timeout=10)  # release all threads together
            results.append(target(index))
        except Exception as exc:  # noqa: BLE001 - recorded for assertion
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=wrapped, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    return results, errors


@on_postgres
class TestConcurrentPosting:
    def test_parallel_postings_all_commit_completely(
        self, wallet_account, counterpart_account
    ):
        """Each journal is whole, or absent. Never half."""

        def post(index):
            return post_journal(
                currency='NGN',
                entries=[
                    debit(counterpart_account, 1_000),
                    credit(wallet_account, 1_000),
                ],
                description=f'concurrent-{index}',
            ).pk

        results, errors = run_in_threads(post, 8)

        assert errors == []
        assert len(results) == 8
        # Every committed journal has exactly its two entries — no half-journals.
        for journal in Journal.objects.all():
            assert journal.entries.count() == 2

    def test_the_derived_balance_is_exact_after_parallel_posting(
        self, wallet_account, counterpart_account
    ):
        """No mutable balance means no lost update to race for."""

        def post(index):
            return post_journal(
                currency='NGN',
                entries=[
                    debit(counterpart_account, 1_000),
                    credit(wallet_account, 1_000),
                ],
            ).pk

        _, errors = run_in_threads(post, 8)

        assert errors == []
        assert posted_balance(wallet_account) == Money(8_000, 'NGN')

    def test_a_failing_posting_alongside_successful_ones_leaves_no_debris(
        self, wallet_account, counterpart_account
    ):
        from moneycore.domain.errors import LedgerUnbalancedError

        def post(index):
            # Every third attempt is unbalanced and must write nothing.
            credit_amount = 999 if index % 3 == 0 else 1_000
            return post_journal(
                currency='NGN',
                entries=[
                    debit(counterpart_account, 1_000),
                    credit(wallet_account, credit_amount),
                ],
            ).pk

        results, errors = run_in_threads(post, 9)

        assert all(isinstance(e, LedgerUnbalancedError) for e in errors)
        assert Journal.objects.count() == len(results)
        assert JournalEntry.objects.count() == len(results) * 2


@on_postgres
class TestConcurrentReversal:
    def test_only_one_of_many_parallel_reversals_succeeds(
        self, wallet_account, counterpart_account
    ):
        """select_for_update plus the OneToOne make double-reversal impossible."""
        original = post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 5_000), credit(wallet_account, 5_000)],
        )

        def reverse(index):
            return reverse_journal(original).pk

        results, errors = run_in_threads(reverse, 6)

        assert len(results) == 1, 'more than one reversal committed'
        assert len(errors) == 5
        assert Journal.objects.filter(reverses=original).count() == 1

    def test_the_balance_returns_to_zero_exactly_once(
        self, wallet_account, counterpart_account
    ):
        original = post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 5_000), credit(wallet_account, 5_000)],
        )

        run_in_threads(lambda index: reverse_journal(original).pk, 6)

        # Not -5000, which is what a second reversal would have produced.
        assert posted_balance(wallet_account) == Money(0, 'NGN')


class TestConcurrencyReasoningThatHoldsOnEveryEngine:
    """Engine-independent facts about why races cannot corrupt a balance."""

    def test_no_mutable_balance_exists_to_race_for(self):
        from moneycore.models import LedgerAccount, Wallet

        for model in (Wallet, LedgerAccount):
            names = {f.name for f in model._meta.get_fields()}
            assert not any('balance' in name for name in names)

    def test_the_balance_is_recomputed_from_entries_on_every_read(
        self, wallet_account, counterpart_account
    ):
        assert posted_balance(wallet_account) == Money(0, 'NGN')

        post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 1), credit(wallet_account, 1)],
        )

        # No cache to invalidate: the next read simply sees the new entry.
        assert posted_balance(wallet_account) == Money(1, 'NGN')

    def test_posting_opens_a_transaction_even_when_called_outside_one(
        self, wallet_account, counterpart_account, monkeypatch
    ):
        """Atomicity is what makes a partial journal impossible.

        Asserted behaviourally: the service body observes itself running inside
        an atomic block despite the caller not opening one.
        """
        assert transaction.get_connection().in_atomic_block is False

        observed = {}
        real_create = Journal.objects.create

        def spy(*args, **kwargs):
            observed['in_atomic_block'] = transaction.get_connection().in_atomic_block
            return real_create(*args, **kwargs)

        monkeypatch.setattr(Journal.objects, 'create', spy)

        post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 1), credit(wallet_account, 1)],
        )

        assert observed['in_atomic_block'] is True
