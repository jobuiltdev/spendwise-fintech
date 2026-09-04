"""Reversal semantics. Group F.

A correction is a new posted journal, never an edit. The original survives
untouched, the reversal points back at it, and one reversal is the limit.
"""

import pytest

from moneycore.domain.errors import (
    JournalAlreadyReversedError,
    JournalNotPostedError,
    LedgerUnbalancedError,
)
from moneycore.domain.ledger import EntryDirection, JournalStatus, opposite_direction
from moneycore.domain.money import Money
from moneycore.models import Journal, JournalEntry
from moneycore.services.ledger import (
    credit,
    debit,
    post_journal,
    posted_balance,
    reverse_journal,
)

pytestmark = pytest.mark.django_db

AMOUNTS = [1, 2, 3, 10, 99, 100, 101, 999_999, 12_345_678_901, 9_007_199_254_740_993]


@pytest.fixture
def original(wallet_account, counterpart_account):
    return post_journal(
        currency='NGN',
        entries=[debit(counterpart_account, 10_000), credit(wallet_account, 10_000)],
        description='Original',
        reference='ref-1',
    )


class TestReversalCreatesAnOppositeJournal:
    def test_it_posts_a_new_journal(self, original):
        reversal = reverse_journal(original)

        assert reversal.pk != original.pk
        assert reversal.status == JournalStatus.POSTED
        assert Journal.objects.count() == 2

    def test_it_references_the_original(self, original):
        reversal = reverse_journal(original)

        assert reversal.reverses_id == original.pk
        assert original.reversed_by.pk == reversal.pk

    def test_every_entry_is_the_exact_opposite(self, original):
        reversal = reverse_journal(original)

        originals = list(original.entries.order_by('sequence'))
        mirrored = list(reversal.entries.order_by('sequence'))

        assert len(mirrored) == len(originals)
        for source, mirror in zip(originals, mirrored):
            assert mirror.ledger_account_id == source.ledger_account_id
            assert mirror.amount_minor == source.amount_minor
            assert mirror.direction == opposite_direction(source.direction)

    def test_it_preserves_the_currency(self, original):
        assert reverse_journal(original).currency == original.currency

    def test_it_carries_a_describing_default(self, original):
        assert str(original.pk) in reverse_journal(original).description

    def test_a_custom_description_is_honoured(self, original):
        reversal = reverse_journal(original, description='Duplicate funding backed out')

        assert reversal.description == 'Duplicate funding backed out'

    def test_a_multi_entry_journal_reverses_entry_for_entry(
        self, wallet_account, counterpart_account, make_ledger_account
    ):
        fees = make_ledger_account('internal:fees-rev', account_type='revenue')
        journal = post_journal(
            currency='NGN',
            entries=[
                debit(counterpart_account, 10_000),
                credit(wallet_account, 9_500),
                credit(fees, 500),
            ],
        )

        reversal = reverse_journal(journal)

        assert reversal.entries.count() == 3
        assert {e.direction for e in reversal.entries.all()} == {
            EntryDirection.CREDIT, EntryDirection.DEBIT
        }


class TestReversalNetsToZero:
    def test_the_wallet_balance_returns_to_where_it_started(
        self, original, wallet_account
    ):
        assert posted_balance(wallet_account) == Money(10_000, 'NGN')

        reverse_journal(original)

        assert posted_balance(wallet_account) == Money(0, 'NGN')

    def test_the_counterpart_balance_also_returns(self, original, counterpart_account):
        reverse_journal(original)

        assert posted_balance(counterpart_account) == Money(0, 'NGN')

    @pytest.mark.parametrize('amount', AMOUNTS)
    def test_it_nets_to_zero_at_every_magnitude(
        self, wallet_account, counterpart_account, amount
    ):
        journal = post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, amount), credit(wallet_account, amount)],
        )

        reverse_journal(journal)

        assert posted_balance(wallet_account) == Money(0, 'NGN')
        assert posted_balance(counterpart_account) == Money(0, 'NGN')

    def test_only_the_reversed_journal_is_undone(
        self, wallet_account, counterpart_account
    ):
        first = post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 1_000), credit(wallet_account, 1_000)],
        )
        post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 2_500), credit(wallet_account, 2_500)],
        )

        reverse_journal(first)

        assert posted_balance(wallet_account) == Money(2_500, 'NGN')


class TestTheOriginalIsUntouched:
    def test_its_status_is_unchanged(self, original):
        reverse_journal(original)

        original.refresh_from_db()
        assert original.status == JournalStatus.POSTED

    def test_its_description_and_entries_are_unchanged(self, original):
        before = [
            (e.ledger_account_id, e.direction, e.amount_minor)
            for e in original.entries.order_by('sequence')
        ]

        reverse_journal(original)

        original.refresh_from_db()
        after = [
            (e.ledger_account_id, e.direction, e.amount_minor)
            for e in original.entries.order_by('sequence')
        ]
        assert after == before
        assert original.description == 'Original'

    def test_it_is_not_deleted(self, original):
        reverse_journal(original)

        assert Journal.objects.filter(pk=original.pk).exists()

    def test_it_gains_no_reversed_marker_field(self):
        """One reversal is enforced by the OneToOne, not by editing the original."""
        concrete = {f.name for f in Journal._meta.get_fields() if f.concrete}

        assert 'reversed' not in concrete
        assert 'is_reversed' not in concrete


class TestReversalIsLimited:
    def test_a_second_reversal_is_refused(self, original):
        reverse_journal(original)

        with pytest.raises(JournalAlreadyReversedError):
            reverse_journal(original)

    def test_the_refused_second_reversal_writes_nothing(self, original):
        reverse_journal(original)

        with pytest.raises(JournalAlreadyReversedError):
            reverse_journal(original)

        assert Journal.objects.count() == 2

    def test_a_reversal_cannot_itself_be_reversed(self, original):
        """Reversal-of-reversal is not something the architecture defines."""
        reversal = reverse_journal(original)

        with pytest.raises(JournalAlreadyReversedError):
            reverse_journal(reversal)

    def test_the_database_also_allows_at_most_one_reversal(self, original):
        from django.db import IntegrityError, transaction
        from django.utils import timezone

        reverse_journal(original)

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Journal.objects.create(
                    status=JournalStatus.POSTED, currency='NGN',
                    posted_at=timezone.now(), reverses=original,
                )

    def test_an_unposted_journal_cannot_be_reversed(self):
        draft = Journal.objects.create(status=JournalStatus.DRAFT, currency='NGN')

        with pytest.raises(JournalNotPostedError):
            reverse_journal(draft)

    def test_a_refused_reversal_of_a_draft_writes_nothing(self):
        draft = Journal.objects.create(status=JournalStatus.DRAFT, currency='NGN')

        with pytest.raises(JournalNotPostedError):
            reverse_journal(draft)

        assert Journal.objects.count() == 1


class TestReversalIsAtomic:
    def test_a_reversal_that_cannot_post_leaves_nothing_behind(
        self, original, counterpart_account
    ):
        """Closing an account mid-life makes the mirrored posting invalid."""
        from moneycore.domain.errors import LedgerAccountClosedError
        from moneycore.domain.ledger import LedgerAccountStatus

        counterpart_account.status = LedgerAccountStatus.CLOSED
        counterpart_account.save(update_fields=['status'])

        with pytest.raises(LedgerAccountClosedError):
            reverse_journal(original)

        assert Journal.objects.count() == 1
        assert JournalEntry.objects.count() == 2
        assert not Journal.objects.filter(reverses=original).exists()

    def test_the_original_survives_a_failed_reversal(self, original, counterpart_account):
        from moneycore.domain.errors import LedgerAccountClosedError
        from moneycore.domain.ledger import LedgerAccountStatus

        counterpart_account.status = LedgerAccountStatus.CLOSED
        counterpart_account.save(update_fields=['status'])

        with pytest.raises(LedgerAccountClosedError):
            reverse_journal(original)

        original.refresh_from_db()
        assert original.status == JournalStatus.POSTED


class TestReversalIsAccountingOnly:
    def test_it_introduces_no_transaction_or_refund_vocabulary(self):
        """M2 reverses bookkeeping. Failed, refunded, cancelled are later domains."""
        concrete = {f.name for f in Journal._meta.get_fields() if f.concrete}

        assert concrete.isdisjoint(
            {'failed', 'refunded', 'cancelled', 'transaction', 'transfer'}
        )

    def test_the_journal_status_set_has_only_draft_and_posted(self):
        assert JournalStatus.ALL == {'draft', 'posted'}
