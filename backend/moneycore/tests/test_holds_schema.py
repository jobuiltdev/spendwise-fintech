"""Hold schema invariants, and what must still not exist. Group A."""

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from moneycore.domain.holds import HoldStatus
from moneycore.domain.ledger import MAX_AMOUNT_MINOR
from moneycore.models import (
    FinancialAccount,
    FundsHold,
    LedgerAccount,
    Wallet,
)

pytestmark = pytest.mark.django_db


class TestNoPersistedBalanceAnywhere:
    """M3 adds a reservation row, never a stored balance."""

    FORBIDDEN = [
        'balance', 'available_balance', 'held_balance', 'reserved_balance',
        'spendable_balance', 'projected_balance', 'ledger_balance',
        'pending_balance', 'total_balance', 'opening_balance',
    ]

    @pytest.mark.parametrize(
        'model', [Wallet, LedgerAccount, FinancialAccount, FundsHold]
    )
    def test_no_balance_field_exists(self, model):
        names = {f.name for f in model._meta.get_fields()}

        assert names.isdisjoint(self.FORBIDDEN)

    def test_the_wallet_field_set_is_unchanged_by_m3(self):
        concrete = {f.name for f in Wallet._meta.get_fields() if f.concrete}

        assert concrete == {
            'id', 'financial_account', 'currency', 'status',
            'created_at', 'updated_at',
        }

    def test_the_ledger_account_field_set_is_unchanged_by_m3(self):
        concrete = {f.name for f in LedgerAccount._meta.get_fields() if f.concrete}

        assert concrete == {
            'id', 'code', 'name', 'account_type', 'currency', 'wallet',
            'status', 'created_at', 'updated_at',
        }

    def test_the_hold_field_set_is_exactly_what_m3_specified(self):
        concrete = {f.name for f in FundsHold._meta.get_fields() if f.concrete}

        assert concrete == {
            'id', 'wallet', 'currency', 'amount_minor', 'status',
            'expires_at', 'released_at', 'expired_at', 'reason',
            'created_at', 'updated_at',
        }

    @pytest.mark.parametrize('model', [Wallet, LedgerAccount, FundsHold])
    def test_no_money_mutating_helper_exists(self, model):
        for forbidden in (
            'credit', 'debit', 'set_balance', 'adjust_balance',
            'reserve', 'capture', 'settle',
        ):
            assert not hasattr(model, forbidden)


class TestAmountRepresentation:
    def test_the_amount_is_a_64_bit_integer(self):
        from django.db import models as dj

        assert isinstance(
            FundsHold._meta.get_field('amount_minor'), dj.BigIntegerField
        )

    def test_no_float_or_decimal_field_exists_on_the_hold(self):
        from django.db import models as dj

        for field in FundsHold._meta.get_fields():
            assert not isinstance(field, (dj.FloatField, dj.DecimalField))

    def test_the_largest_supported_amount_is_storable(self, funded_wallet):
        hold = FundsHold.objects.create(
            wallet=funded_wallet, currency='NGN',
            amount_minor=MAX_AMOUNT_MINOR, status=HoldStatus.ACTIVE,
        )
        hold.refresh_from_db()

        assert hold.amount_minor == MAX_AMOUNT_MINOR

    @pytest.mark.parametrize('amount', [0, -1, -100])
    def test_a_non_positive_amount_is_refused_by_the_database(
        self, funded_wallet, amount
    ):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                FundsHold.objects.create(
                    wallet=funded_wallet, currency='NGN',
                    amount_minor=amount, status=HoldStatus.ACTIVE,
                )

    def test_the_amount_reads_back_as_a_money_value(self, funded_wallet):
        from moneycore.domain.money import Money

        hold = FundsHold.objects.create(
            wallet=funded_wallet, currency='NGN',
            amount_minor=2_500, status=HoldStatus.ACTIVE,
        )

        assert hold.amount == Money(2_500, 'NGN')


class TestDatabaseConstraints:
    def test_an_invalid_status_is_refused(self, funded_wallet):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                FundsHold.objects.create(
                    wallet=funded_wallet, currency='NGN',
                    amount_minor=100, status='captured',
                )

    def test_a_malformed_currency_is_refused(self, funded_wallet):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                FundsHold.objects.create(
                    wallet=funded_wallet, currency='ngn',
                    amount_minor=100, status=HoldStatus.ACTIVE,
                )

    def test_a_released_hold_must_record_when(self, funded_wallet):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                FundsHold.objects.create(
                    wallet=funded_wallet, currency='NGN', amount_minor=100,
                    status=HoldStatus.RELEASED, released_at=None,
                )

    def test_an_active_hold_must_not_record_a_release_time(self, funded_wallet):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                FundsHold.objects.create(
                    wallet=funded_wallet, currency='NGN', amount_minor=100,
                    status=HoldStatus.ACTIVE, released_at=timezone.now(),
                )

    def test_an_expired_hold_must_record_when(self, funded_wallet):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                FundsHold.objects.create(
                    wallet=funded_wallet, currency='NGN', amount_minor=100,
                    status=HoldStatus.EXPIRED, expired_at=None,
                )

    def test_an_active_hold_must_not_record_an_expiry_time(self, funded_wallet):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                FundsHold.objects.create(
                    wallet=funded_wallet, currency='NGN', amount_minor=100,
                    status=HoldStatus.ACTIVE, expired_at=timezone.now(),
                )

    def test_a_wallet_with_holds_cannot_be_deleted(self, funded_wallet):
        """PROTECT: reservation history must not vanish with a container."""
        from django.db.models import ProtectedError

        FundsHold.objects.create(
            wallet=funded_wallet, currency='NGN',
            amount_minor=100, status=HoldStatus.ACTIVE,
        )

        with pytest.raises(ProtectedError):
            funded_wallet.delete()


class TestLifecycleVocabulary:
    def test_the_states_are_exactly_the_three_m3_defined(self):
        assert HoldStatus.ALL == {'active', 'released', 'expired'}

    @pytest.mark.parametrize(
        'forbidden',
        ['captured', 'settled', 'completed', 'failed', 'pending', 'processing', 'confirming'],
    )
    def test_no_transaction_or_provider_state_exists(self, forbidden):
        """Those describe transactions and provider outcomes, not reservations."""
        assert forbidden not in HoldStatus.ALL

    def test_both_terminal_states_are_terminal(self):
        from moneycore.domain.holds import HOLD_TRANSITIONS

        assert HOLD_TRANSITIONS[HoldStatus.RELEASED] == frozenset()
        assert HOLD_TRANSITIONS[HoldStatus.EXPIRED] == frozenset()

    def test_an_active_hold_may_reach_either_terminal_state(self):
        from moneycore.domain.holds import can_transition_hold

        assert can_transition_hold(HoldStatus.ACTIVE, HoldStatus.RELEASED)
        assert can_transition_hold(HoldStatus.ACTIVE, HoldStatus.EXPIRED)

    @pytest.mark.parametrize('terminal', [HoldStatus.RELEASED, HoldStatus.EXPIRED])
    def test_a_terminal_hold_cannot_return_to_active(self, terminal):
        from moneycore.domain.holds import can_transition_hold

        assert not can_transition_hold(terminal, HoldStatus.ACTIVE)


class TestNoReservationDuplicateConcept:
    def test_hold_is_the_only_reservation_model(self):
        from django.apps import apps

        declared = {
            m.__name__.lower() for m in apps.get_app_config('moneycore').get_models()
        }

        assert 'fundshold' in declared
        assert 'reservation' not in declared
        assert 'hold' not in declared  # no second, duplicate concept

    def test_the_hold_has_no_transaction_or_provider_reference(self):
        names = {f.name for f in FundsHold._meta.get_fields()}

        assert names.isdisjoint({
            'transaction', 'transaction_id', 'transfer', 'transfer_id',
            'provider', 'provider_id', 'provider_reference', 'recipient',
            'account_number',
        })

    def test_the_hold_has_no_relation_to_a_legacy_expense(self):
        related = {
            f.related_model.__name__
            for f in FundsHold._meta.get_fields()
            if f.related_model is not None
        }

        assert related == {'Wallet'}
