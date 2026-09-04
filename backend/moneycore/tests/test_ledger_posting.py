"""Balanced posting, atomicity, and derived balances.

Groups C (balanced posting) and D (posted balance).
"""

import pytest

from moneycore.domain.errors import (
    InvalidLedgerEntryError,
    LedgerAccountClosedError,
    LedgerCurrencyMismatchError,
    LedgerUnbalancedError,
)
from moneycore.domain.ledger import (
    EntryDirection,
    JournalStatus,
    LedgerAccountStatus,
    LedgerAccountType,
    MAX_AMOUNT_MINOR,
)
from moneycore.domain.money import Money
from moneycore.models import Journal, JournalEntry
from moneycore.services.ledger import (
    EntryDraft,
    credit,
    debit,
    post_journal,
    posted_balance,
    wallet_posted_balance,
)

pytestmark = pytest.mark.django_db

# A spread of magnitudes, including values a float could not hold exactly.
AMOUNTS = [1, 2, 3, 10, 99, 100, 101, 999_999, 12_345_678_901, 9_007_199_254_740_993]


# ---------------------------------------------------------------------------
# C. Balanced posting
# ---------------------------------------------------------------------------


class TestSuccessfulPosting:
    def test_a_two_entry_journal_posts(self, wallet_account, counterpart_account):
        journal = post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 50_000), credit(wallet_account, 50_000)],
            description='Funding',
        )

        assert journal.status == JournalStatus.POSTED
        assert journal.posted_at is not None
        assert journal.entries.count() == 2

    def test_a_multi_entry_journal_posts(
        self, wallet_account, counterpart_account, make_ledger_account
    ):
        fees = make_ledger_account('internal:fees', account_type=LedgerAccountType.REVENUE)

        journal = post_journal(
            currency='NGN',
            entries=[
                debit(counterpart_account, 10_000),
                credit(wallet_account, 9_500),
                credit(fees, 500),
            ],
        )

        assert journal.entries.count() == 3

    def test_entries_are_numbered_in_the_order_given(
        self, wallet_account, counterpart_account
    ):
        journal = post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 100), credit(wallet_account, 100)],
        )

        assert [e.sequence for e in journal.entries.order_by('sequence')] == [1, 2]

    def test_a_completed_posting_leaves_no_draft_behind(
        self, wallet_account, counterpart_account
    ):
        post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 100), credit(wallet_account, 100)],
        )

        assert not Journal.objects.filter(status=JournalStatus.DRAFT).exists()

    def test_the_magnitude_is_stored_positive_with_direction_separate(
        self, wallet_account, counterpart_account
    ):
        journal = post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 750), credit(wallet_account, 750)],
        )

        for entry in journal.entries.all():
            assert entry.amount_minor == 750
            assert entry.direction in EntryDirection.ALL

    @pytest.mark.parametrize('amount', AMOUNTS)
    def test_posting_is_exact_at_every_magnitude(
        self, wallet_account, counterpart_account, amount
    ):
        journal = post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, amount), credit(wallet_account, amount)],
        )

        stored = [e.amount_minor for e in journal.entries.all()]
        assert stored == [amount, amount]
        assert all(isinstance(v, int) for v in stored)

    def test_the_largest_supported_amount_posts(self, wallet_account, counterpart_account):
        journal = post_journal(
            currency='NGN',
            entries=[
                debit(counterpart_account, MAX_AMOUNT_MINOR),
                credit(wallet_account, MAX_AMOUNT_MINOR),
            ],
        )

        assert journal.entries.first().amount_minor == MAX_AMOUNT_MINOR


class TestRejectedPostings:
    def test_an_unbalanced_journal_is_refused(self, wallet_account, counterpart_account):
        with pytest.raises(LedgerUnbalancedError) as excinfo:
            post_journal(
                currency='NGN',
                entries=[debit(counterpart_account, 100), credit(wallet_account, 99)],
            )

        assert excinfo.value.details == {'debits': 100, 'credits': 99}

    def test_a_journal_with_no_entries_is_refused(self):
        with pytest.raises(InvalidLedgerEntryError):
            post_journal(currency='NGN', entries=[])

    def test_a_single_entry_journal_is_refused(self, wallet_account):
        """There is no single-sided balance adjustment, by construction."""
        with pytest.raises(InvalidLedgerEntryError):
            post_journal(currency='NGN', entries=[credit(wallet_account, 100)])

    @pytest.mark.parametrize('amount', [0, -1, -100])
    def test_a_non_positive_amount_is_refused(
        self, wallet_account, counterpart_account, amount
    ):
        with pytest.raises(InvalidLedgerEntryError):
            post_journal(
                currency='NGN',
                entries=[
                    debit(counterpart_account, amount),
                    credit(wallet_account, amount),
                ],
            )

    @pytest.mark.parametrize('amount', [100.0, 100.5, '100', None, True])
    def test_a_non_integer_amount_is_refused(
        self, wallet_account, counterpart_account, amount
    ):
        """Float, Decimal, str and bool never reach the ledger."""
        with pytest.raises(InvalidLedgerEntryError):
            post_journal(
                currency='NGN',
                entries=[
                    debit(counterpart_account, amount),
                    credit(wallet_account, amount),
                ],
            )

    def test_a_decimal_amount_is_refused(self, wallet_account, counterpart_account):
        from decimal import Decimal

        with pytest.raises(InvalidLedgerEntryError):
            post_journal(
                currency='NGN',
                entries=[
                    debit(counterpart_account, Decimal('100.00')),
                    credit(wallet_account, Decimal('100.00')),
                ],
            )

    def test_an_amount_beyond_the_supported_range_is_refused(
        self, wallet_account, counterpart_account
    ):
        with pytest.raises(InvalidLedgerEntryError):
            post_journal(
                currency='NGN',
                entries=[
                    debit(counterpart_account, MAX_AMOUNT_MINOR + 1),
                    credit(wallet_account, MAX_AMOUNT_MINOR + 1),
                ],
            )

    def test_an_invalid_direction_is_refused(self, wallet_account, counterpart_account):
        with pytest.raises(InvalidLedgerEntryError):
            post_journal(
                currency='NGN',
                entries=[
                    EntryDraft(counterpart_account, 'sideways', 100),
                    credit(wallet_account, 100),
                ],
            )

    def test_a_malformed_journal_currency_is_refused(
        self, wallet_account, counterpart_account
    ):
        with pytest.raises(InvalidLedgerEntryError):
            post_journal(
                currency='ngn',
                entries=[debit(counterpart_account, 100), credit(wallet_account, 100)],
            )

    def test_an_account_currency_mismatch_is_refused(
        self, wallet_account, counterpart_account, make_ledger_account
    ):
        usd = make_ledger_account('internal:usd', currency='USD')

        with pytest.raises(LedgerCurrencyMismatchError):
            post_journal(
                currency='NGN',
                entries=[debit(usd, 100), credit(wallet_account, 100)],
            )

    def test_cross_currency_balancing_is_impossible(
        self, wallet_account, make_ledger_account
    ):
        """No conversion path exists, so FX cannot be faked into balancing."""
        usd = make_ledger_account('internal:usd2', currency='USD')

        with pytest.raises(LedgerCurrencyMismatchError):
            post_journal(
                currency='NGN',
                entries=[debit(usd, 100), credit(wallet_account, 100)],
            )

    def test_a_closed_account_cannot_take_a_posting(
        self, wallet_account, counterpart_account
    ):
        counterpart_account.status = LedgerAccountStatus.CLOSED
        counterpart_account.save(update_fields=['status'])

        with pytest.raises(LedgerAccountClosedError):
            post_journal(
                currency='NGN',
                entries=[debit(counterpart_account, 100), credit(wallet_account, 100)],
            )

    def test_an_unsaved_account_is_refused(self, wallet_account):
        from moneycore.models import LedgerAccount

        unsaved = LedgerAccount(
            code='internal:unsaved', name='unsaved',
            account_type=LedgerAccountType.ASSET, currency='NGN',
        )

        with pytest.raises(InvalidLedgerEntryError):
            post_journal(
                currency='NGN',
                entries=[debit(unsaved, 100), credit(wallet_account, 100)],
            )


class TestAtomicity:
    """A rejection must leave no financial rows at all."""

    def _assert_ledger_is_empty(self):
        assert Journal.objects.count() == 0
        assert JournalEntry.objects.count() == 0

    def test_an_unbalanced_posting_writes_nothing(
        self, wallet_account, counterpart_account
    ):
        with pytest.raises(LedgerUnbalancedError):
            post_journal(
                currency='NGN',
                entries=[debit(counterpart_account, 100), credit(wallet_account, 99)],
            )

        self._assert_ledger_is_empty()

    def test_one_bad_entry_among_several_rolls_back_everything(
        self, wallet_account, counterpart_account, make_ledger_account
    ):
        fees = make_ledger_account('internal:fees2', account_type=LedgerAccountType.REVENUE)

        with pytest.raises(InvalidLedgerEntryError):
            post_journal(
                currency='NGN',
                entries=[
                    debit(counterpart_account, 10_000),
                    credit(wallet_account, 9_500),
                    credit(fees, 0),  # invalid
                ],
            )

        self._assert_ledger_is_empty()

    def test_a_currency_mismatch_deep_in_the_list_writes_nothing(
        self, wallet_account, counterpart_account, make_ledger_account
    ):
        usd = make_ledger_account('internal:usd3', currency='USD')

        with pytest.raises(LedgerCurrencyMismatchError):
            post_journal(
                currency='NGN',
                entries=[
                    debit(counterpart_account, 100),
                    credit(wallet_account, 50),
                    credit(usd, 50),
                ],
            )

        self._assert_ledger_is_empty()

    def test_a_failure_after_an_earlier_success_leaves_the_earlier_intact(
        self, wallet_account, counterpart_account
    ):
        good = post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 100), credit(wallet_account, 100)],
        )

        with pytest.raises(LedgerUnbalancedError):
            post_journal(
                currency='NGN',
                entries=[debit(counterpart_account, 100), credit(wallet_account, 1)],
            )

        assert Journal.objects.count() == 1
        assert Journal.objects.get().pk == good.pk
        assert JournalEntry.objects.count() == 2


# ---------------------------------------------------------------------------
# D. Posted balance
# ---------------------------------------------------------------------------


class TestPostedBalance:
    def test_an_account_with_no_entries_reads_zero(self, wallet_account):
        assert posted_balance(wallet_account) == Money(0, 'NGN')

    def test_a_credit_normal_account_increases_on_credit(
        self, wallet_account, counterpart_account
    ):
        post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 50_000), credit(wallet_account, 50_000)],
        )

        assert posted_balance(wallet_account) == Money(50_000, 'NGN')

    def test_a_debit_normal_account_increases_on_debit(
        self, wallet_account, counterpart_account
    ):
        post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 50_000), credit(wallet_account, 50_000)],
        )

        assert posted_balance(counterpart_account) == Money(50_000, 'NGN')

    def test_multiple_journals_aggregate(self, wallet_account, counterpart_account):
        for amount in (1_000, 2_500, 400):
            post_journal(
                currency='NGN',
                entries=[debit(counterpart_account, amount), credit(wallet_account, amount)],
            )

        assert posted_balance(wallet_account) == Money(3_900, 'NGN')

    def test_opposite_postings_offset_exactly(self, wallet_account, counterpart_account):
        post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 5_000), credit(wallet_account, 5_000)],
        )
        post_journal(
            currency='NGN',
            entries=[debit(wallet_account, 2_000), credit(counterpart_account, 2_000)],
        )

        assert posted_balance(wallet_account) == Money(3_000, 'NGN')
        assert posted_balance(counterpart_account) == Money(3_000, 'NGN')

    def test_an_internal_balance_may_go_negative_in_its_normal_direction(
        self, counterpart_account, make_ledger_account
    ):
        """The ledger records what happened; it does not enforce policy.

        Shown on internal accounts, which carry signed balances legitimately. A
        wallet-backed account is different: M3's reservation guard refuses a
        posting that would drive a customer wallet below its reserved funds, so
        that case is covered in the holds tests rather than here.
        """
        other = make_ledger_account('internal:signed-probe')

        post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 700), credit(other, 700)],
        )

        # Credit-normal would read +700; this account is debit-normal, so the
        # credited side reads negative.
        assert posted_balance(other) == Money(-700, 'NGN')

    def test_a_draft_journal_is_excluded(self, wallet_account, counterpart_account):
        """Constructed directly, bypassing the service, to prove the filter."""
        draft = Journal.objects.create(status=JournalStatus.DRAFT, currency='NGN')
        JournalEntry.objects.create(
            journal=draft, ledger_account=wallet_account,
            direction=EntryDirection.CREDIT, amount_minor=999_999, sequence=1,
        )
        JournalEntry.objects.create(
            journal=draft, ledger_account=counterpart_account,
            direction=EntryDirection.DEBIT, amount_minor=999_999, sequence=2,
        )

        assert posted_balance(wallet_account) == Money(0, 'NGN')

    def test_another_accounts_entries_are_excluded(
        self, wallet_account, counterpart_account, make_ledger_account
    ):
        other = make_ledger_account('internal:other', account_type=LedgerAccountType.LIABILITY)
        post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 800), credit(other, 800)],
        )

        assert posted_balance(wallet_account) == Money(0, 'NGN')

    def test_the_balance_carries_the_account_currency(self, wallet_account):
        assert posted_balance(wallet_account).currency == 'NGN'

    def test_the_balance_is_a_money_value_with_integer_minor_units(
        self, wallet_account, counterpart_account
    ):
        post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 1), credit(wallet_account, 1)],
        )

        balance = posted_balance(wallet_account)
        assert isinstance(balance, Money)
        assert isinstance(balance.minor_units, int)
        assert not isinstance(balance.minor_units, bool)

    @pytest.mark.parametrize('amount', AMOUNTS)
    def test_the_balance_is_exact_at_every_magnitude(
        self, wallet_account, counterpart_account, amount
    ):
        post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, amount), credit(wallet_account, amount)],
        )

        assert posted_balance(wallet_account) == Money(amount, 'NGN')

    def test_the_whole_ledger_sums_to_zero_across_accounts(
        self, wallet_account, counterpart_account, make_ledger_account
    ):
        """Every posting is balanced, so signed totals must cancel."""
        fees = make_ledger_account('internal:fees3', account_type=LedgerAccountType.REVENUE)
        post_journal(
            currency='NGN',
            entries=[
                debit(counterpart_account, 10_000),
                credit(wallet_account, 9_500),
                credit(fees, 500),
            ],
        )

        def signed(account):
            value = posted_balance(account).minor_units
            return value if account.is_debit_normal else -value

        assert signed(counterpart_account) + signed(wallet_account) + signed(fees) == 0


class TestWalletPostedBalance:
    def test_it_derives_from_the_wallet_ledger_account(
        self, wallet, wallet_account, counterpart_account
    ):
        post_journal(
            currency='NGN',
            entries=[debit(counterpart_account, 12_345), credit(wallet_account, 12_345)],
        )

        assert wallet_posted_balance(wallet) == Money(12_345, 'NGN')

    def test_a_wallet_with_no_ledger_account_has_no_balance_at_all(self, wallet):
        """Absence of a ledger relationship is not a balance of zero.

        Answering zero here would state a financial fact the ledger has never
        established — the same fabrication as a fake zero on a screen.
        """
        from moneycore.domain.errors import WalletLedgerAccountNotFoundError

        with pytest.raises(WalletLedgerAccountNotFoundError):
            wallet_posted_balance(wallet)

    def test_a_mapped_account_with_no_entries_reads_exactly_zero(
        self, wallet, wallet_account
    ):
        """The other case: a real ledger relationship that has posted nothing."""
        assert wallet_posted_balance(wallet) == Money(0, wallet.currency)

    def test_the_two_cases_are_distinguishable(self, wallet):
        """Unmapped raises; mapped-but-empty returns zero. Same wallet."""
        from moneycore.domain.errors import WalletLedgerAccountNotFoundError
        from moneycore.domain.ledger import LedgerAccountType
        from moneycore.services.ledger import open_wallet_ledger_account

        with pytest.raises(WalletLedgerAccountNotFoundError):
            wallet_posted_balance(wallet)

        open_wallet_ledger_account(wallet, account_type=LedgerAccountType.LIABILITY)

        assert wallet_posted_balance(wallet) == Money(0, wallet.currency)

    def test_reading_a_balance_does_not_provision_a_ledger_account(self, wallet):
        """O-14 stays open: nothing is created as a side effect of a read."""
        from moneycore.domain.errors import WalletLedgerAccountNotFoundError
        from moneycore.models import LedgerAccount

        with pytest.raises(WalletLedgerAccountNotFoundError):
            wallet_posted_balance(wallet)

        assert not LedgerAccount.objects.filter(wallet=wallet).exists()

    def test_the_error_names_the_wallet_and_uses_the_domain_shape(self, wallet):
        from moneycore.domain.errors import WalletLedgerAccountNotFoundError

        with pytest.raises(WalletLedgerAccountNotFoundError) as excinfo:
            wallet_posted_balance(wallet)

        assert excinfo.value.code == 'wallet_ledger_account_not_found'
        assert excinfo.value.http_status == 404
        assert excinfo.value.details['wallet'] == wallet.pk
        assert excinfo.value.as_payload('c-1')['error']['correlation_id'] == 'c-1' 

    def test_provisioning_alone_creates_no_ledger_account_and_no_balance(
        self, ledger_user
    ):
        from moneycore.models import LedgerAccount
        from moneycore.services.provisioning import provision_financial_account

        result = provision_financial_account(ledger_user)

        assert not LedgerAccount.objects.filter(wallet=result.wallet).exists()
        assert Journal.objects.count() == 0
