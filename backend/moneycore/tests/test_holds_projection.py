"""Hold creation and the posted / held / available projection.

Groups B (creation) and C (projection).
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from moneycore.domain.errors import (
    BalanceProjectionInvalidError,
    InsufficientAvailableBalanceError,
    InvalidHoldAmountError,
    WalletLedgerAccountNotFoundError,
    WalletNotHoldableError,
)
from moneycore.domain.holds import BalanceProjection, HoldStatus
from moneycore.domain.ledger import LedgerAccountStatus, MAX_AMOUNT_MINOR
from moneycore.domain.lifecycle import WalletStatus
from moneycore.domain.money import Money
from moneycore.domain.ledger import EntryDirection
from moneycore.models import FundsHold, Journal, JournalEntry
from moneycore.services.holds import (
    create_hold,
    held_amount,
    release_hold,
    wallet_balance_projection,
)

pytestmark = pytest.mark.django_db

AMOUNTS = [1, 2, 3, 10, 99, 100, 101, 1_000, 1_001, 999_999, 12_345_678_901]


# ---------------------------------------------------------------------------
# B. Hold creation
# ---------------------------------------------------------------------------


class TestSuccessfulCreation:
    def test_a_hold_within_available_funds_succeeds(self, funded_wallet):
        hold = create_hold(funded_wallet, 3_000)

        assert hold.status == HoldStatus.ACTIVE
        assert hold.amount_minor == 3_000
        assert hold.currency == 'NGN'
        assert hold.wallet_id == funded_wallet.pk

    def test_a_hold_for_the_exact_available_balance_succeeds(self, funded_wallet):
        hold = create_hold(funded_wallet, 10_000)

        assert hold.amount_minor == 10_000
        assert wallet_balance_projection(funded_wallet).available == Money(0, 'NGN')

    def test_a_new_hold_records_no_terminal_timestamps(self, funded_wallet):
        hold = create_hold(funded_wallet, 100)

        assert hold.released_at is None
        assert hold.expired_at is None

    def test_an_expiry_may_be_set(self, funded_wallet):
        expires = timezone.now() + timedelta(hours=1)

        hold = create_hold(funded_wallet, 100, expires_at=expires)

        assert hold.expires_at == expires

    def test_a_reason_may_be_recorded(self, funded_wallet):
        hold = create_hold(funded_wallet, 100, reason='internal test')

        assert hold.reason == 'internal test'

    def test_several_holds_may_coexist(self, funded_wallet):
        create_hold(funded_wallet, 3_000)
        create_hold(funded_wallet, 2_000)

        assert FundsHold.objects.filter(wallet=funded_wallet).count() == 2

    @pytest.mark.parametrize('amount', AMOUNTS)
    def test_creation_is_exact_at_every_magnitude(
        self, wallet, wallet_account, fund_wallet, amount
    ):
        fund_wallet(wallet_account, amount)

        hold = create_hold(wallet, amount)

        assert hold.amount_minor == amount
        assert isinstance(hold.amount_minor, int)


class TestRejectedCreation:
    def test_more_than_the_available_balance_is_refused(self, funded_wallet):
        with pytest.raises(InsufficientAvailableBalanceError) as excinfo:
            create_hold(funded_wallet, 10_001)

        assert excinfo.value.details['requested_minor'] == 10_001
        assert excinfo.value.details['available_minor'] == 10_000

    def test_a_prior_hold_reduces_what_can_be_reserved(self, funded_wallet):
        create_hold(funded_wallet, 7_000)

        with pytest.raises(InsufficientAvailableBalanceError):
            create_hold(funded_wallet, 4_000)

    def test_the_remainder_after_a_prior_hold_can_still_be_reserved(
        self, funded_wallet
    ):
        create_hold(funded_wallet, 7_000)

        hold = create_hold(funded_wallet, 3_000)

        assert hold.amount_minor == 3_000

    @pytest.mark.parametrize('amount', [0, -1, -1_000])
    def test_a_non_positive_amount_is_refused(self, funded_wallet, amount):
        with pytest.raises(InvalidHoldAmountError):
            create_hold(funded_wallet, amount)

    @pytest.mark.parametrize('amount', [100.0, 100.5, '100', None, True])
    def test_a_non_integer_amount_is_refused(self, funded_wallet, amount):
        with pytest.raises(InvalidHoldAmountError):
            create_hold(funded_wallet, amount)

    def test_a_decimal_amount_is_refused(self, funded_wallet):
        from decimal import Decimal

        with pytest.raises(InvalidHoldAmountError):
            create_hold(funded_wallet, Decimal('100.00'))

    def test_an_amount_beyond_the_supported_range_is_refused(self, funded_wallet):
        with pytest.raises(InvalidHoldAmountError):
            create_hold(funded_wallet, MAX_AMOUNT_MINOR + 1)

    def test_a_wallet_with_no_ledger_mapping_is_refused(self, wallet):
        """Inherits the M2 rule: no ledger relationship is not zero funds."""
        with pytest.raises(WalletLedgerAccountNotFoundError):
            create_hold(wallet, 100)

    def test_a_closed_wallet_is_refused(self, funded_wallet):
        funded_wallet.status = WalletStatus.CLOSED
        funded_wallet.save(update_fields=['status'])

        with pytest.raises(WalletNotHoldableError):
            create_hold(funded_wallet, 100)

    def test_a_closed_ledger_account_is_refused(self, funded_wallet, wallet_account):
        wallet_account.status = LedgerAccountStatus.CLOSED
        wallet_account.save(update_fields=['status'])

        with pytest.raises(WalletNotHoldableError):
            create_hold(funded_wallet, 100)

    def test_a_wallet_with_no_posted_funds_cannot_reserve(self, unfunded_wallet):
        with pytest.raises(InsufficientAvailableBalanceError):
            create_hold(unfunded_wallet, 1)

    def test_a_refused_creation_writes_no_hold(self, funded_wallet):
        with pytest.raises(InsufficientAvailableBalanceError):
            create_hold(funded_wallet, 99_999)

        assert FundsHold.objects.count() == 0


class TestCreationTouchesNoLedger:
    def test_creating_a_hold_posts_no_journal(self, funded_wallet):
        before = Journal.objects.count()

        create_hold(funded_wallet, 3_000)

        assert Journal.objects.count() == before

    def test_creating_a_hold_writes_no_journal_entry(self, funded_wallet):
        before = JournalEntry.objects.count()

        create_hold(funded_wallet, 3_000)

        assert JournalEntry.objects.count() == before

    def test_the_posted_balance_is_unchanged_by_a_hold(self, funded_wallet):
        from moneycore.services.ledger import wallet_posted_balance

        create_hold(funded_wallet, 3_000)

        assert wallet_posted_balance(funded_wallet) == Money(10_000, 'NGN')


# ---------------------------------------------------------------------------
# C. Balance projection
# ---------------------------------------------------------------------------


class TestProjection:
    def test_a_mapped_wallet_with_no_entries_reads_all_zero(self, unfunded_wallet):
        projection = wallet_balance_projection(unfunded_wallet)

        assert projection.posted == Money(0, 'NGN')
        assert projection.held == Money(0, 'NGN')
        assert projection.available == Money(0, 'NGN')

    def test_funds_with_no_holds_are_fully_available(self, funded_wallet):
        projection = wallet_balance_projection(funded_wallet)

        assert projection.posted == Money(10_000, 'NGN')
        assert projection.held == Money(0, 'NGN')
        assert projection.available == Money(10_000, 'NGN')

    def test_a_hold_reduces_available_but_not_posted(self, funded_wallet):
        create_hold(funded_wallet, 3_000)

        projection = wallet_balance_projection(funded_wallet)

        assert projection.posted == Money(10_000, 'NGN')
        assert projection.held == Money(3_000, 'NGN')
        assert projection.available == Money(7_000, 'NGN')

    def test_multiple_active_holds_aggregate(self, funded_wallet):
        create_hold(funded_wallet, 3_000)
        create_hold(funded_wallet, 2_500)
        create_hold(funded_wallet, 500)

        projection = wallet_balance_projection(funded_wallet)

        assert projection.held == Money(6_000, 'NGN')
        assert projection.available == Money(4_000, 'NGN')

    def test_a_released_hold_is_excluded(self, funded_wallet):
        hold = create_hold(funded_wallet, 3_000)

        release_hold(hold)

        assert wallet_balance_projection(funded_wallet).held == Money(0, 'NGN')

    def test_an_expired_hold_is_excluded(self, funded_wallet):
        create_hold(
            funded_wallet, 3_000, expires_at=timezone.now() + timedelta(seconds=1)
        )

        later = timezone.now() + timedelta(hours=1)
        projection = wallet_balance_projection(funded_wallet, at=later)

        assert projection.held == Money(0, 'NGN')
        assert projection.available == Money(10_000, 'NGN')

    def test_a_mixed_lifecycle_aggregates_only_the_effective_holds(
        self, funded_wallet
    ):
        create_hold(funded_wallet, 1_000)  # active, no expiry
        create_hold(
            funded_wallet, 2_000, expires_at=timezone.now() + timedelta(days=1)
        )  # active, future expiry
        released = create_hold(funded_wallet, 3_000)
        release_hold(released)
        create_hold(
            funded_wallet, 4_000, expires_at=timezone.now() + timedelta(seconds=1)
        )  # will have expired

        later = timezone.now() + timedelta(hours=1)
        projection = wallet_balance_projection(funded_wallet, at=later)

        assert projection.held == Money(3_000, 'NGN')
        assert projection.available == Money(7_000, 'NGN')

    def test_the_projection_is_single_currency(self, funded_wallet):
        projection = wallet_balance_projection(funded_wallet)

        assert projection.currency == 'NGN'
        assert projection.posted.currency == 'NGN'
        assert projection.held.currency == 'NGN'
        assert projection.available.currency == 'NGN'

    def test_the_invariant_holds(self, funded_wallet):
        create_hold(funded_wallet, 2_345)

        projection = wallet_balance_projection(funded_wallet)

        assert projection.available == projection.posted - projection.held

    @pytest.mark.parametrize('amount', AMOUNTS)
    def test_the_projection_is_exact_at_every_magnitude(
        self, wallet, wallet_account, fund_wallet, amount
    ):
        fund_wallet(wallet_account, amount)
        create_hold(wallet, amount)

        projection = wallet_balance_projection(wallet)

        assert projection.posted == Money(amount, 'NGN')
        assert projection.held == Money(amount, 'NGN')
        assert projection.available == Money(0, 'NGN')

    def test_the_projection_performs_no_writes(self, funded_wallet):
        # Written directly so the row is already overdue: create_hold would
        # reject a past expiry as pointless.
        hold = FundsHold.objects.create(
            wallet=funded_wallet, currency='NGN', amount_minor=1_000,
            status=HoldStatus.ACTIVE,
            expires_at=timezone.now() - timedelta(hours=1),
        )

        wallet_balance_projection(funded_wallet)

        hold.refresh_from_db()
        # Still ACTIVE in the database, even though it no longer reserves funds.
        assert hold.status == HoldStatus.ACTIVE


class TestNoFakeZeroForUnmappedWallet:
    def test_an_unmapped_wallet_has_no_projection(self, wallet):
        with pytest.raises(WalletLedgerAccountNotFoundError):
            wallet_balance_projection(wallet)

    def test_it_does_not_report_zero_zero_zero(self, wallet):
        """Absence of a ledger relationship is not a balance of all zeroes."""
        try:
            projection = wallet_balance_projection(wallet)
        except WalletLedgerAccountNotFoundError:
            return
        pytest.fail(f'unmapped wallet fabricated a projection: {projection}')

    def test_held_amount_alone_does_not_imply_a_balance(self, wallet):
        """held_amount is answerable without a ledger; the projection is not."""
        assert held_amount(wallet) == Money(0, wallet.currency)

        with pytest.raises(WalletLedgerAccountNotFoundError):
            wallet_balance_projection(wallet)

    def test_reading_a_projection_provisions_no_ledger_account(self, wallet):
        from moneycore.models import LedgerAccount

        with pytest.raises(WalletLedgerAccountNotFoundError):
            wallet_balance_projection(wallet)

        assert not LedgerAccount.objects.filter(wallet=wallet).exists()


class TestIncoherentStateIsRaisedNotClamped:
    def test_held_exceeding_posted_raises(self, funded_wallet):
        """Only reachable by writing rows directly, which is the point."""
        FundsHold.objects.create(
            wallet=funded_wallet, currency='NGN',
            amount_minor=99_999, status=HoldStatus.ACTIVE,
        )

        with pytest.raises(BalanceProjectionInvalidError):
            wallet_balance_projection(funded_wallet)

    def test_available_is_never_silently_clamped_to_zero(self, funded_wallet):
        FundsHold.objects.create(
            wallet=funded_wallet, currency='NGN',
            amount_minor=99_999, status=HoldStatus.ACTIVE,
        )

        try:
            projection = wallet_balance_projection(funded_wallet)
        except BalanceProjectionInvalidError:
            return
        pytest.fail(f'impossible state was concealed as {projection}')

    def test_the_service_refuses_to_drive_a_wallet_negative(
        self, wallet, wallet_account
    ):
        """The supported path cannot create the invalid state at all.

        Since the M3 reservation guard, a posting that would leave a wallet
        below its reserved funds — zero here — is refused outright.
        """
        from moneycore.domain.errors import LedgerPostingConflictsWithHoldsError
        from moneycore.services.ledger import credit, debit, post_journal

        with pytest.raises(LedgerPostingConflictsWithHoldsError):
            post_journal(
                currency='NGN',
                entries=[
                    debit(wallet_account, 500),
                    credit(_counterpart_of(wallet_account), 500),
                ],
            )

    def test_a_negative_posted_balance_raises_if_it_somehow_exists(
        self, wallet, wallet_account
    ):
        """M3 invents no overdraft, so this is reported rather than absorbed.

        Written directly, bypassing the posting service, because the service now
        refuses to produce it. That is the point of the guard: this state should
        only be reachable by corruption, and if it ever is, it is surfaced.
        """
        _write_raw_posting(wallet_account, EntryDirection.DEBIT, 500)

        with pytest.raises(BalanceProjectionInvalidError):
            wallet_balance_projection(wallet)

    def test_a_hold_cannot_be_created_against_a_negative_balance(
        self, wallet, wallet_account
    ):
        _write_raw_posting(wallet_account, EntryDirection.DEBIT, 500)

        with pytest.raises(BalanceProjectionInvalidError):
            create_hold(wallet, 1)


def _counterpart_of(wallet_account):
    """A second internal account in the same currency, for signed-balance tests."""
    from moneycore.domain.ledger import LedgerAccountType
    from moneycore.services.ledger import open_ledger_account

    return open_ledger_account(
        code=f'internal:negative-probe:{wallet_account.pk}',
        name='Negative probe counterpart',
        account_type=LedgerAccountType.ASSET,
        currency=wallet_account.currency,
    )


def _write_raw_posting(account, direction, amount_minor):
    """Write a posted journal directly, bypassing every service guard.

    Simulates corruption. No application code does this — the ledger service is
    the only supported writer — and it exists here purely to prove that an
    impossible state is reported rather than concealed if it ever arises.
    """
    from django.utils import timezone

    from moneycore.domain.ledger import JournalStatus

    journal = Journal.objects.create(
        status=JournalStatus.DRAFT, currency=account.currency
    )
    counterpart = _counterpart_of(account)
    JournalEntry.objects.create(
        journal=journal, ledger_account=account,
        direction=direction, amount_minor=amount_minor, sequence=1,
    )
    JournalEntry.objects.create(
        journal=journal, ledger_account=counterpart,
        direction=(
            EntryDirection.CREDIT
            if direction == EntryDirection.DEBIT
            else EntryDirection.DEBIT
        ),
        amount_minor=amount_minor, sequence=2,
    )
    journal.status = JournalStatus.POSTED
    journal.posted_at = timezone.now()
    journal.save(update_fields=['status', 'posted_at'])
    return journal


class TestBalanceProjectionValueObject:
    def test_it_derives_available_from_posted_and_held(self):
        projection = BalanceProjection.build(Money(10_000, 'NGN'), Money(3_000, 'NGN'))

        assert projection.available == Money(7_000, 'NGN')

    def test_it_is_frozen(self):
        projection = BalanceProjection.build(Money(1, 'NGN'), Money(0, 'NGN'))

        with pytest.raises(Exception):
            projection.posted = Money(2, 'NGN')

    def test_it_refuses_mixed_currencies(self):
        with pytest.raises(ValueError, match='single-currency'):
            BalanceProjection(
                posted=Money(10, 'NGN'), held=Money(0, 'USD'), available=Money(10, 'NGN')
            )

    def test_it_refuses_an_inconsistent_invariant(self):
        with pytest.raises(ValueError, match='available = posted - held'):
            BalanceProjection(
                posted=Money(10, 'NGN'), held=Money(3, 'NGN'), available=Money(99, 'NGN')
            )

    def test_it_exposes_no_float(self):
        projection = BalanceProjection.build(Money(10_000, 'NGN'), Money(1, 'NGN'))

        for value in (projection.posted, projection.held, projection.available):
            assert isinstance(value.minor_units, int)
            assert not isinstance(value.minor_units, bool)
