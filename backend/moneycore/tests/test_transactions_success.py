"""Success orchestration: release + post + mark, all or nothing.

The happy path here is the critical M4 proof — and so is its rollback.
"""

import pytest

from moneycore.domain.errors import (
    LedgerUnbalancedError,
    TransactionAlreadyResolvedError,
    TransactionSuccessRequiresJournalError,
)
from moneycore.domain.holds import HoldStatus
from moneycore.domain.ledger import JournalStatus
from moneycore.domain.money import Money
from moneycore.domain.transactions import TransactionDirection, TransactionStatus
from moneycore.models import FinancialTransaction, Journal, JournalEntry
from moneycore.services.holds import create_hold, wallet_balance_projection
from moneycore.services.ledger import credit, debit
from moneycore.services.transactions import (
    attach_hold,
    create_transaction,
    mark_unknown,
    start_processing,
    succeed_transaction,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def processing(funded_wallet):
    """posted 10 000, hold 7 000, transaction PROCESSING."""
    txn = create_transaction(
        funded_wallet, TransactionDirection.OUTGOING, 7_000,
        idempotency_key='success-1',
    )
    hold = create_hold(funded_wallet, 7_000)
    return start_processing(attach_hold(txn, hold))


def spend_entries(wallet_account, counterpart_account, amount=7_000):
    """Entries whose net effect reduces the wallet's posted balance."""
    return [debit(wallet_account, amount), credit(counterpart_account, amount)]


class TestSuccessHappyPath:
    """The critical proof: posted 10 000 − 7 000 held → 3 000, atomically."""

    def test_the_transaction_succeeds(self, processing, wallet_account, counterpart_account):
        succeeded = succeed_transaction(
            processing, entries=spend_entries(wallet_account, counterpart_account)
        )

        assert succeeded.status == TransactionStatus.SUCCEEDED
        assert succeeded.resolved_at is not None

    def test_a_posted_journal_is_created_and_linked(
        self, processing, wallet_account, counterpart_account
    ):
        succeeded = succeed_transaction(
            processing, entries=spend_entries(wallet_account, counterpart_account)
        )

        assert succeeded.journal is not None
        assert succeeded.journal.status == JournalStatus.POSTED
        assert Journal.objects.filter(pk=succeeded.journal_id).exists()

    def test_the_reservation_is_released(
        self, processing, wallet_account, counterpart_account
    ):
        succeeded = succeed_transaction(
            processing, entries=spend_entries(wallet_account, counterpart_account)
        )

        succeeded.hold.refresh_from_db()
        assert succeeded.hold.status == HoldStatus.RELEASED

    def test_the_final_balance_picture_is_exact(
        self, processing, wallet_account, counterpart_account
    ):
        succeed_transaction(
            processing, entries=spend_entries(wallet_account, counterpart_account)
        )

        projection = wallet_balance_projection(processing.wallet)
        assert projection.posted == Money(3_000, 'NGN')
        assert projection.held == Money(0, 'NGN')
        assert projection.available == Money(3_000, 'NGN')

    def test_exactly_one_journal_exists_for_the_success(
        self, processing, wallet_account, counterpart_account
    ):
        before = Journal.objects.count()

        succeed_transaction(
            processing, entries=spend_entries(wallet_account, counterpart_account)
        )

        assert Journal.objects.count() == before + 1

    def test_success_from_unknown_behaves_identically(
        self, processing, wallet_account, counterpart_account
    ):
        unresolved = mark_unknown(processing)

        succeeded = succeed_transaction(
            unresolved, entries=spend_entries(wallet_account, counterpart_account)
        )

        succeeded.hold.refresh_from_db()
        assert succeeded.status == TransactionStatus.SUCCEEDED
        assert succeeded.hold.status == HoldStatus.RELEASED
        assert wallet_balance_projection(
            processing.wallet
        ).available == Money(3_000, 'NGN')

    def test_an_incoming_transaction_succeeds_without_a_reservation(
        self, funded_wallet, wallet_account, counterpart_account
    ):
        txn = create_transaction(
            funded_wallet, TransactionDirection.INCOMING, 2_000,
            idempotency_key='incoming-success',
        )
        started = start_processing(txn)

        succeeded = succeed_transaction(
            started,
            entries=[debit(counterpart_account, 2_000), credit(wallet_account, 2_000)],
        )

        assert succeeded.status == TransactionStatus.SUCCEEDED
        assert wallet_balance_projection(
            funded_wallet
        ).posted == Money(12_000, 'NGN')

    def test_a_custom_description_and_reference_reach_the_journal(
        self, processing, wallet_account, counterpart_account
    ):
        succeeded = succeed_transaction(
            processing,
            entries=spend_entries(wallet_account, counterpart_account),
            description='Outbound settlement',
            reference='ref-42',
        )

        assert succeeded.journal.description == 'Outbound settlement'
        assert succeeded.journal.reference == 'ref-42'


class TestSuccessNeedsTheReservationReleasedFirst:
    """Proof that the ordering inside the atomic block actually matters.

    While the hold is active, M3's posting guard refuses a journal that would
    take the wallet below its reserved funds. Success works only because the
    release happens first, inside the same transaction.
    """

    def test_the_same_posting_is_refused_while_the_hold_is_active(
        self, processing, wallet_account, counterpart_account
    ):
        from moneycore.domain.errors import LedgerPostingConflictsWithHoldsError
        from moneycore.services.ledger import post_journal

        with pytest.raises(LedgerPostingConflictsWithHoldsError):
            post_journal(
                currency='NGN',
                entries=spend_entries(wallet_account, counterpart_account),
            )

    def test_but_succeeds_through_the_transaction_engine(
        self, processing, wallet_account, counterpart_account
    ):
        succeeded = succeed_transaction(
            processing, entries=spend_entries(wallet_account, counterpart_account)
        )

        assert succeeded.status == TransactionStatus.SUCCEEDED


class TestSuccessRollback:
    """All or nothing: a rejected posting must leave everything as it was."""

    def _unbalanced(self, wallet_account, counterpart_account):
        return [debit(wallet_account, 7_000), credit(counterpart_account, 6_999)]

    def test_an_unbalanced_journal_is_refused(
        self, processing, wallet_account, counterpart_account
    ):
        with pytest.raises(LedgerUnbalancedError):
            succeed_transaction(
                processing,
                entries=self._unbalanced(wallet_account, counterpart_account),
            )

    def test_the_transaction_stays_in_its_prior_state(
        self, processing, wallet_account, counterpart_account
    ):
        with pytest.raises(LedgerUnbalancedError):
            succeed_transaction(
                processing,
                entries=self._unbalanced(wallet_account, counterpart_account),
            )

        processing.refresh_from_db()
        assert processing.status == TransactionStatus.PROCESSING
        assert processing.journal_id is None
        assert processing.resolved_at is None

    def test_the_reservation_stays_active(
        self, processing, wallet_account, counterpart_account
    ):
        """The release must roll back with the posting."""
        with pytest.raises(LedgerUnbalancedError):
            succeed_transaction(
                processing,
                entries=self._unbalanced(wallet_account, counterpart_account),
            )

        processing.hold.refresh_from_db()
        assert processing.hold.status == HoldStatus.ACTIVE
        assert processing.hold.released_at is None

    def test_no_journal_survives(
        self, processing, wallet_account, counterpart_account
    ):
        before = (Journal.objects.count(), JournalEntry.objects.count())

        with pytest.raises(LedgerUnbalancedError):
            succeed_transaction(
                processing,
                entries=self._unbalanced(wallet_account, counterpart_account),
            )

        assert (Journal.objects.count(), JournalEntry.objects.count()) == before

    def test_the_balance_picture_is_unchanged(
        self, processing, wallet_account, counterpart_account
    ):
        before = wallet_balance_projection(processing.wallet)

        with pytest.raises(LedgerUnbalancedError):
            succeed_transaction(
                processing,
                entries=self._unbalanced(wallet_account, counterpart_account),
            )

        assert wallet_balance_projection(processing.wallet) == before

    def test_a_single_entry_journal_also_rolls_everything_back(
        self, processing, wallet_account
    ):
        from moneycore.domain.errors import InvalidLedgerEntryError

        with pytest.raises(InvalidLedgerEntryError):
            succeed_transaction(processing, entries=[debit(wallet_account, 7_000)])

        processing.refresh_from_db()
        processing.hold.refresh_from_db()
        assert processing.status == TransactionStatus.PROCESSING
        assert processing.hold.status == HoldStatus.ACTIVE

    def test_a_currency_mismatched_entry_rolls_everything_back(
        self, processing, wallet_account, make_ledger_account
    ):
        from moneycore.domain.errors import LedgerCurrencyMismatchError

        usd = make_ledger_account('internal:usd-success', currency='USD')

        with pytest.raises(LedgerCurrencyMismatchError):
            succeed_transaction(
                processing,
                entries=[debit(wallet_account, 7_000), credit(usd, 7_000)],
            )

        processing.refresh_from_db()
        processing.hold.refresh_from_db()
        assert processing.status == TransactionStatus.PROCESSING
        assert processing.hold.status == HoldStatus.ACTIVE

    def test_the_transaction_can_still_succeed_after_a_failed_attempt(
        self, processing, wallet_account, counterpart_account
    ):
        """Rollback leaves it genuinely usable, not half-broken."""
        with pytest.raises(LedgerUnbalancedError):
            succeed_transaction(
                processing,
                entries=self._unbalanced(wallet_account, counterpart_account),
            )

        succeeded = succeed_transaction(
            processing, entries=spend_entries(wallet_account, counterpart_account)
        )

        assert succeeded.status == TransactionStatus.SUCCEEDED
        assert wallet_balance_projection(
            processing.wallet
        ).available == Money(3_000, 'NGN')


class TestSuccessRequiresPostedTruth:
    def test_a_succeeded_transaction_always_has_a_journal(
        self, processing, wallet_account, counterpart_account
    ):
        succeeded = succeed_transaction(
            processing, entries=spend_entries(wallet_account, counterpart_account)
        )

        assert succeeded.journal_id is not None

    def test_the_journal_uses_the_transaction_currency(
        self, processing, wallet_account, counterpart_account
    ):
        succeeded = succeed_transaction(
            processing, entries=spend_entries(wallet_account, counterpart_account)
        )

        assert succeeded.journal.currency == succeeded.currency

    def test_the_currency_guard_carries_a_stable_code(self):
        """Guarded in the service as well as by the ledger's own rules."""
        assert TransactionSuccessRequiresJournalError.code == (
            'transaction_success_requires_journal'
        )

    def test_a_resolved_transaction_cannot_succeed_again(
        self, processing, wallet_account, counterpart_account
    ):
        succeed_transaction(
            processing, entries=spend_entries(wallet_account, counterpart_account)
        )

        with pytest.raises(TransactionAlreadyResolvedError):
            succeed_transaction(
                processing,
                entries=spend_entries(wallet_account, counterpart_account),
            )

    def test_a_second_success_creates_no_second_journal(
        self, processing, wallet_account, counterpart_account
    ):
        succeed_transaction(
            processing, entries=spend_entries(wallet_account, counterpart_account)
        )
        count = Journal.objects.count()

        with pytest.raises(TransactionAlreadyResolvedError):
            succeed_transaction(
                processing,
                entries=spend_entries(wallet_account, counterpart_account),
            )

        assert Journal.objects.count() == count

    def test_a_created_transaction_cannot_succeed_directly(
        self, funded_wallet, wallet_account, counterpart_account
    ):
        from moneycore.domain.errors import InvalidTransactionTransitionError

        txn = create_transaction(
            funded_wallet, TransactionDirection.OUTGOING, 1_000,
            idempotency_key='no-jump',
        )

        with pytest.raises(InvalidTransactionTransitionError):
            succeed_transaction(
                txn,
                entries=[debit(wallet_account, 1_000), credit(counterpart_account, 1_000)],
            )

    def test_the_journal_is_immutable_afterwards(
        self, processing, wallet_account, counterpart_account
    ):
        from moneycore.domain.errors import JournalImmutableError

        succeeded = succeed_transaction(
            processing, entries=spend_entries(wallet_account, counterpart_account)
        )

        succeeded.journal.description = 'rewritten'
        with pytest.raises(JournalImmutableError):
            succeeded.journal.save()

    def test_one_journal_serves_at_most_one_transaction(
        self, processing, funded_wallet, wallet_account, counterpart_account
    ):
        from django.db import IntegrityError, transaction as db_transaction

        succeeded = succeed_transaction(
            processing, entries=spend_entries(wallet_account, counterpart_account)
        )
        other = create_transaction(
            funded_wallet, TransactionDirection.OUTGOING, 1_000,
            idempotency_key='journal-steal',
        )

        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                FinancialTransaction.objects.filter(pk=other.pk).update(
                    journal=succeeded.journal
                )
