"""Release, expiry and terminal-hold immutability.

Groups D (release), E (expiry) and the immutability rules.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from moneycore.domain.errors import (
    HoldImmutableError,
    HoldNotActiveError,
    HoldNotDueForExpiryError,
)
from moneycore.domain.holds import HoldStatus
from moneycore.domain.money import Money
from moneycore.models import FundsHold, Journal, JournalEntry
from moneycore.services.holds import (
    create_hold,
    expire_due_holds,
    expire_hold,
    held_amount,
    is_hold_effective,
    release_hold,
    wallet_balance_projection,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# D. Release
# ---------------------------------------------------------------------------


class TestRelease:
    def test_an_active_hold_releases(self, funded_wallet):
        hold = create_hold(funded_wallet, 3_000)

        released = release_hold(hold)

        assert released.status == HoldStatus.RELEASED

    def test_it_records_when(self, funded_wallet):
        hold = create_hold(funded_wallet, 3_000)

        released = release_hold(hold)

        assert released.released_at is not None
        assert released.expired_at is None

    def test_the_projection_updates_immediately(self, funded_wallet):
        hold = create_hold(funded_wallet, 3_000)
        assert wallet_balance_projection(funded_wallet).available == Money(7_000, 'NGN')

        release_hold(hold)

        assert wallet_balance_projection(funded_wallet).available == Money(10_000, 'NGN')

    def test_released_funds_can_be_reserved_again(self, funded_wallet):
        first = create_hold(funded_wallet, 10_000)
        release_hold(first)

        second = create_hold(funded_wallet, 10_000)

        assert second.amount_minor == 10_000

    def test_it_posts_no_journal(self, funded_wallet):
        hold = create_hold(funded_wallet, 3_000)
        before = (Journal.objects.count(), JournalEntry.objects.count())

        release_hold(hold)

        assert (Journal.objects.count(), JournalEntry.objects.count()) == before

    def test_the_posted_balance_is_untouched(self, funded_wallet):
        from moneycore.services.ledger import wallet_posted_balance

        hold = create_hold(funded_wallet, 3_000)
        release_hold(hold)

        assert wallet_posted_balance(funded_wallet) == Money(10_000, 'NGN')

    def test_a_second_release_is_refused(self, funded_wallet):
        """Documented contract: release is NOT idempotent.

        A duplicate release is surfaced as a caller mistake rather than
        absorbed. Safe-retry semantics belong to M4's idempotency keys.
        """
        hold = create_hold(funded_wallet, 3_000)
        release_hold(hold)

        with pytest.raises(HoldNotActiveError):
            release_hold(hold)

    def test_an_expired_hold_cannot_be_released(self, funded_wallet):
        hold = _overdue_hold(funded_wallet)
        expire_hold(hold)

        with pytest.raises(HoldNotActiveError):
            release_hold(hold)

    def test_a_refused_release_leaves_the_row_unchanged(self, funded_wallet):
        hold = create_hold(funded_wallet, 3_000)
        released = release_hold(hold)
        first_time = released.released_at

        with pytest.raises(HoldNotActiveError):
            release_hold(hold)

        released.refresh_from_db()
        assert released.released_at == first_time


# ---------------------------------------------------------------------------
# E. Expiry
# ---------------------------------------------------------------------------


def _overdue_hold(wallet, amount=3_000):
    """A hold already past its expiry, written directly."""
    return FundsHold.objects.create(
        wallet=wallet, currency=wallet.currency, amount_minor=amount,
        status=HoldStatus.ACTIVE,
        expires_at=timezone.now() - timedelta(hours=1),
    )


class TestEffectiveExpiry:
    """Correctness must not depend on any cleanup process having run."""

    def test_a_future_expiry_still_reserves_funds(self, funded_wallet):
        create_hold(
            funded_wallet, 3_000, expires_at=timezone.now() + timedelta(days=1)
        )

        assert held_amount(funded_wallet) == Money(3_000, 'NGN')

    def test_a_hold_with_no_expiry_reserves_indefinitely(self, funded_wallet):
        create_hold(funded_wallet, 3_000)

        far_future = timezone.now() + timedelta(days=3650)
        assert held_amount(funded_wallet, at=far_future) == Money(3_000, 'NGN')

    def test_an_overdue_hold_stops_reserving_without_any_cleanup(self, funded_wallet):
        hold = _overdue_hold(funded_wallet)

        assert held_amount(funded_wallet) == Money(0, 'NGN')
        # Still stored as ACTIVE: nothing ran, and nothing needed to.
        hold.refresh_from_db()
        assert hold.status == HoldStatus.ACTIVE

    def test_available_funds_return_without_any_cleanup(self, funded_wallet):
        _overdue_hold(funded_wallet)

        assert wallet_balance_projection(funded_wallet).available == Money(10_000, 'NGN')

    def test_funds_freed_by_expiry_can_be_reserved_again(self, funded_wallet):
        _overdue_hold(funded_wallet, 10_000)

        hold = create_hold(funded_wallet, 10_000)

        assert hold.amount_minor == 10_000

    def test_expiry_is_evaluated_at_the_boundary_exclusively(self, funded_wallet):
        moment = timezone.now()
        FundsHold.objects.create(
            wallet=funded_wallet, currency='NGN', amount_minor=1_000,
            status=HoldStatus.ACTIVE, expires_at=moment,
        )

        # Expiring exactly at `moment` means it has expired.
        assert held_amount(funded_wallet, at=moment) == Money(0, 'NGN')
        assert held_amount(
            funded_wallet, at=moment - timedelta(seconds=1)
        ) == Money(1_000, 'NGN')

    def test_the_predicate_agrees_with_the_aggregate(self, funded_wallet):
        active = create_hold(funded_wallet, 1_000)
        overdue = _overdue_hold(funded_wallet, 2_000)

        assert is_hold_effective(active)
        assert not is_hold_effective(overdue)


class TestExplicitExpiry:
    def test_a_due_hold_transitions_to_expired(self, funded_wallet):
        hold = _overdue_hold(funded_wallet)

        expired = expire_hold(hold)

        assert expired.status == HoldStatus.EXPIRED
        assert expired.expired_at is not None
        assert expired.released_at is None

    def test_expiring_changes_no_balance(self, funded_wallet):
        hold = _overdue_hold(funded_wallet)
        before = wallet_balance_projection(funded_wallet)

        expire_hold(hold)

        assert wallet_balance_projection(funded_wallet) == before

    def test_it_posts_no_journal(self, funded_wallet):
        hold = _overdue_hold(funded_wallet)
        before = (Journal.objects.count(), JournalEntry.objects.count())

        expire_hold(hold)

        assert (Journal.objects.count(), JournalEntry.objects.count()) == before

    def test_a_hold_that_is_not_due_cannot_be_expired(self, funded_wallet):
        """Ending a hold early is a release, not an expiry."""
        hold = create_hold(
            funded_wallet, 3_000, expires_at=timezone.now() + timedelta(days=1)
        )

        with pytest.raises(HoldNotDueForExpiryError):
            expire_hold(hold)

    def test_a_hold_with_no_expiry_cannot_be_expired(self, funded_wallet):
        hold = create_hold(funded_wallet, 3_000)

        with pytest.raises(HoldNotDueForExpiryError):
            expire_hold(hold)

    def test_a_released_hold_cannot_be_expired(self, funded_wallet):
        hold = create_hold(funded_wallet, 3_000)
        release_hold(hold)

        with pytest.raises(HoldNotActiveError):
            expire_hold(hold)

    def test_expiring_twice_is_refused(self, funded_wallet):
        hold = _overdue_hold(funded_wallet)
        expire_hold(hold)

        with pytest.raises(HoldNotActiveError):
            expire_hold(hold)


class TestBulkExpiry:
    def test_it_marks_only_due_holds(self, funded_wallet):
        due = _overdue_hold(funded_wallet, 1_000)
        not_due = create_hold(
            funded_wallet, 2_000, expires_at=timezone.now() + timedelta(days=1)
        )
        no_expiry = create_hold(funded_wallet, 3_000)

        expire_due_holds(wallet=funded_wallet)

        due.refresh_from_db()
        not_due.refresh_from_db()
        no_expiry.refresh_from_db()
        assert due.status == HoldStatus.EXPIRED
        assert not_due.status == HoldStatus.ACTIVE
        assert no_expiry.status == HoldStatus.ACTIVE

    def test_it_changes_no_balance(self, funded_wallet):
        _overdue_hold(funded_wallet)
        before = wallet_balance_projection(funded_wallet)

        expire_due_holds(wallet=funded_wallet)

        assert wallet_balance_projection(funded_wallet) == before

    def test_it_posts_no_journal(self, funded_wallet):
        _overdue_hold(funded_wallet)
        before = Journal.objects.count()

        expire_due_holds(wallet=funded_wallet)

        assert Journal.objects.count() == before

    def test_no_scheduling_infrastructure_was_introduced(self):
        """M3 adds no worker: this is called deliberately or not at all."""
        from pathlib import Path

        import moneycore

        package = Path(moneycore.__file__).parent
        offenders = [
            str(path.relative_to(package))
            for path in package.rglob('*.py')
            if 'tests' not in path.parts
            and any(
                marker in path.read_text(encoding='utf-8')
                for marker in ('import celery', 'from celery', 'shared_task',
                               'import redis', 'from redis', 'periodic_task')
            )
        ]

        assert offenders == []


# ---------------------------------------------------------------------------
# Terminal hold immutability
# ---------------------------------------------------------------------------


class TestTerminalHoldsAreImmutable:
    @pytest.fixture
    def released(self, funded_wallet):
        return release_hold(create_hold(funded_wallet, 3_000))

    def test_a_released_hold_cannot_be_saved(self, released):
        released.amount_minor = 99

        with pytest.raises(HoldImmutableError):
            released.save()

    def test_a_released_hold_cannot_be_reactivated(self, released):
        released.status = HoldStatus.ACTIVE

        with pytest.raises(HoldImmutableError):
            released.save()

    def test_a_released_hold_cannot_be_reassigned_to_another_wallet(
        self, released, wallet
    ):
        released.wallet = wallet

        with pytest.raises(HoldImmutableError):
            released.save()

    def test_a_released_hold_cannot_have_its_expiry_rewritten(self, released):
        released.expires_at = timezone.now() + timedelta(days=1)

        with pytest.raises(HoldImmutableError):
            released.save()

    def test_a_queryset_update_touching_terminal_rows_is_refused(self, released):
        with pytest.raises(HoldImmutableError):
            FundsHold.objects.filter(pk=released.pk).update(amount_minor=1)

    def test_a_broad_update_touching_terminal_rows_is_refused(self, released):
        with pytest.raises(HoldImmutableError):
            FundsHold.objects.all().update(status=HoldStatus.ACTIVE)

    def test_an_update_over_only_active_rows_still_works(self, funded_wallet, released):
        create_hold(funded_wallet, 100)

        updated = FundsHold.objects.filter(status=HoldStatus.ACTIVE).update(
            reason='still editable'
        )

        assert updated == 1

    def test_the_row_survives_a_refused_write(self, released):
        released.amount_minor = 99
        with pytest.raises(HoldImmutableError):
            released.save()

        released.refresh_from_db()
        assert released.amount_minor == 3_000


class TestHoldsAreNotDeleted:
    def test_an_active_hold_cannot_be_deleted(self, funded_wallet):
        hold = create_hold(funded_wallet, 3_000)

        with pytest.raises(HoldImmutableError):
            hold.delete()

    def test_a_terminal_hold_cannot_be_deleted(self, funded_wallet):
        hold = release_hold(create_hold(funded_wallet, 3_000))

        with pytest.raises(HoldImmutableError):
            hold.delete()

    def test_a_queryset_delete_is_refused(self, funded_wallet):
        create_hold(funded_wallet, 3_000)

        with pytest.raises(HoldImmutableError):
            FundsHold.objects.all().delete()

    def test_the_history_survives(self, funded_wallet):
        hold = release_hold(create_hold(funded_wallet, 3_000))

        with pytest.raises(HoldImmutableError):
            hold.delete()

        assert FundsHold.objects.filter(pk=hold.pk).exists()
