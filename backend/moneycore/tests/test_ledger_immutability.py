"""Posted financial history cannot be rewritten. Group E.

Each test attacks a realistic ORM path rather than asserting a docstring. Where
a path is closed structurally, that is stated; where enforcement relies on the
model/queryset layer, the test exercises that layer directly.

Known limits are recorded in TestDocumentedEscapeHatches at the end — honestly,
rather than by omission.
"""

import pytest
from django.utils import timezone

from moneycore.domain.errors import JournalImmutableError
from moneycore.domain.ledger import EntryDirection, JournalStatus
from moneycore.models import Journal, JournalEntry
from moneycore.services.ledger import credit, debit, post_journal, posted_balance
from moneycore.domain.money import Money

pytestmark = pytest.mark.django_db


@pytest.fixture
def posted(wallet_account, counterpart_account):
    return post_journal(
        currency='NGN',
        entries=[debit(counterpart_account, 10_000), credit(wallet_account, 10_000)],
        description='Original',
    )


class TestJournalCannotBeMutated:
    def test_saving_a_posted_journal_is_refused(self, posted):
        posted.description = 'rewritten'

        with pytest.raises(JournalImmutableError):
            posted.save()

    def test_changing_the_currency_is_refused(self, posted):
        posted.currency = 'USD'

        with pytest.raises(JournalImmutableError):
            posted.save()

    def test_reverting_it_to_draft_is_refused(self, posted):
        posted.status = JournalStatus.DRAFT

        with pytest.raises(JournalImmutableError):
            posted.save()

    def test_a_refused_save_leaves_the_row_unchanged(self, posted):
        posted.description = 'rewritten'
        with pytest.raises(JournalImmutableError):
            posted.save()

        posted.refresh_from_db()
        assert posted.description == 'Original'

    def test_a_queryset_update_is_refused(self, posted):
        with pytest.raises(JournalImmutableError):
            Journal.objects.filter(pk=posted.pk).update(description='rewritten')

    def test_a_broad_queryset_update_touching_posted_rows_is_refused(self, posted):
        with pytest.raises(JournalImmutableError):
            Journal.objects.all().update(currency='USD')

    def test_an_update_over_only_draft_rows_still_works(self, posted):
        """Only posted truth is protected; drafts stay writable."""
        Journal.objects.create(status=JournalStatus.DRAFT, currency='NGN')

        updated = Journal.objects.filter(status=JournalStatus.DRAFT).update(
            description='still editable'
        )

        assert updated == 1


class TestJournalCannotBeDeleted:
    def test_deleting_a_posted_journal_is_refused(self, posted):
        with pytest.raises(JournalImmutableError):
            posted.delete()

    def test_a_queryset_delete_is_refused(self, posted):
        with pytest.raises(JournalImmutableError):
            Journal.objects.filter(pk=posted.pk).delete()

    def test_a_broad_queryset_delete_touching_posted_rows_is_refused(self, posted):
        with pytest.raises(JournalImmutableError):
            Journal.objects.all().delete()

    def test_the_journal_survives_a_refused_delete(self, posted):
        with pytest.raises(JournalImmutableError):
            posted.delete()

        assert Journal.objects.filter(pk=posted.pk).exists()


class TestEntriesCannotBeMutated:
    def test_changing_an_entry_amount_is_refused(self, posted):
        entry = posted.entries.first()
        entry.amount_minor = 99_999

        with pytest.raises(JournalImmutableError):
            entry.save()

    def test_flipping_an_entry_direction_is_refused(self, posted):
        entry = posted.entries.first()
        entry.direction = EntryDirection.CREDIT

        with pytest.raises(JournalImmutableError):
            entry.save()

    def test_moving_an_entry_to_another_account_is_refused(
        self, posted, make_ledger_account
    ):
        other = make_ledger_account('internal:elsewhere')
        entry = posted.entries.first()
        entry.ledger_account = other

        with pytest.raises(JournalImmutableError):
            entry.save()

    def test_an_entry_queryset_update_is_refused(self, posted):
        with pytest.raises(JournalImmutableError):
            JournalEntry.objects.filter(journal=posted).update(amount_minor=1)

    def test_a_broad_entry_update_touching_posted_rows_is_refused(self, posted):
        with pytest.raises(JournalImmutableError):
            JournalEntry.objects.all().update(amount_minor=1)

    def test_the_amounts_survive_a_refused_update(self, posted):
        with pytest.raises(JournalImmutableError):
            JournalEntry.objects.filter(journal=posted).update(amount_minor=1)

        assert {e.amount_minor for e in posted.entries.all()} == {10_000}


class TestEntriesCannotBeRemovedOrAdded:
    def test_deleting_an_entry_is_refused(self, posted):
        with pytest.raises(JournalImmutableError):
            posted.entries.first().delete()

    def test_an_entry_queryset_delete_is_refused(self, posted):
        with pytest.raises(JournalImmutableError):
            JournalEntry.objects.filter(journal=posted).delete()

    def test_adding_an_entry_to_a_posted_journal_is_refused(
        self, posted, counterpart_account
    ):
        with pytest.raises(JournalImmutableError):
            JournalEntry.objects.create(
                journal=posted, ledger_account=counterpart_account,
                direction=EntryDirection.DEBIT, amount_minor=1, sequence=99,
            )

    def test_adding_through_the_related_manager_is_refused(
        self, posted, counterpart_account
    ):
        with pytest.raises(JournalImmutableError):
            posted.entries.create(
                ledger_account=counterpart_account,
                direction=EntryDirection.DEBIT, amount_minor=1, sequence=98,
            )

    def test_the_entry_count_is_unchanged_after_refused_attempts(
        self, posted, counterpart_account
    ):
        with pytest.raises(JournalImmutableError):
            posted.entries.create(
                ledger_account=counterpart_account,
                direction=EntryDirection.DEBIT, amount_minor=1, sequence=97,
            )

        assert posted.entries.count() == 2

    def test_an_entry_is_write_once_even_before_posting(self, counterpart_account):
        """Entries are never updated in place, posted or not."""
        draft = Journal.objects.create(status=JournalStatus.DRAFT, currency='NGN')
        entry = JournalEntry.objects.create(
            journal=draft, ledger_account=counterpart_account,
            direction=EntryDirection.DEBIT, amount_minor=100, sequence=1,
        )

        entry.memo = 'changed'
        with pytest.raises(JournalImmutableError):
            entry.save()


class TestBalanceIsUnaffectedByRefusedMutations:
    def test_the_derived_balance_holds_after_every_failed_attempt(
        self, posted, wallet_account
    ):
        for attempt in (
            lambda: posted.entries.first().delete(),
            lambda: JournalEntry.objects.filter(journal=posted).update(amount_minor=1),
            lambda: Journal.objects.filter(pk=posted.pk).delete(),
        ):
            with pytest.raises(JournalImmutableError):
                attempt()

        assert posted_balance(wallet_account) == Money(10_000, 'NGN')


class TestReferentialProtection:
    def test_a_ledger_account_with_entries_cannot_be_deleted(
        self, posted, counterpart_account
    ):
        """PROTECT: history must not disappear with an account row."""
        from django.db.models import ProtectedError

        with pytest.raises(ProtectedError):
            counterpart_account.delete()


class TestDocumentedEscapeHatches:
    """What is *not* structurally prevented, stated plainly.

    Enforcement lives in the model and queryset layer, which covers every
    ordinary application path. It does not cover paths that bypass the ORM's
    Python layer entirely. These are recorded so nobody mistakes the guarantee
    for a database-level one.
    """

    def test_bulk_create_bypasses_the_save_override(self, posted, counterpart_account):
        """bulk_create does not call save(), so it is an escape hatch.

        No application code uses it for ledger entries; the ledger service is
        the only writer. Closing this fully would need database triggers, which
        the architecture has not approved.
        """
        JournalEntry.objects.bulk_create([
            JournalEntry(
                journal=posted, ledger_account=counterpart_account,
                direction=EntryDirection.DEBIT, amount_minor=1, sequence=50,
            )
        ])

        # Documented, not endorsed: the row lands.
        assert posted.entries.count() == 3

    def test_raw_sql_is_likewise_outside_the_orm_guarantee(self):
        """Stated for completeness — raw SQL bypasses every Python-level check."""
        assert hasattr(JournalEntry.objects, 'raw')
