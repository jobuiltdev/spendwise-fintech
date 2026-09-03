"""Tests for the Money value type.

No database access: Money is pure domain code.
"""

import pytest

from moneycore.domain.money import CurrencyMismatchError, Money


class TestConstruction:
    def test_holds_integer_minor_units_and_a_currency(self):
        amount = Money(150_00, 'NGN')

        assert amount.minor_units == 150_00
        assert amount.currency == 'NGN'

    def test_accepts_zero(self):
        assert Money(0, 'NGN').minor_units == 0

    def test_accepts_a_negative_amount(self):
        """Negative values are legitimate — a double-entry ledger needs them."""
        assert Money(-1, 'NGN').minor_units == -1

    def test_zero_helper_builds_a_zero_amount(self):
        assert Money.zero('USD') == Money(0, 'USD')

    def test_is_not_currency_specific(self):
        """Nothing about the type is tied to the NGN money-core V1."""
        for code in ('NGN', 'USD', 'EUR', 'GBP', 'KES'):
            assert Money(1, code).currency == code


class TestRejectsFloats:
    def test_rejects_a_float_amount(self):
        with pytest.raises(TypeError, match='minor_units must be an int'):
            Money(10.5, 'NGN')

    def test_rejects_a_whole_float(self):
        """10.0 is still a float, and a float is how precision gets lost."""
        with pytest.raises(TypeError):
            Money(10.0, 'NGN')

    def test_rejects_a_decimal(self):
        from decimal import Decimal

        with pytest.raises(TypeError):
            Money(Decimal('10.00'), 'NGN')

    def test_rejects_a_numeric_string(self):
        with pytest.raises(TypeError):
            Money('1000', 'NGN')

    def test_rejects_a_bool(self):
        """bool subclasses int; True would silently become one minor unit."""
        with pytest.raises(TypeError):
            Money(True, 'NGN')

    def test_rejects_none(self):
        with pytest.raises(TypeError):
            Money(None, 'NGN')


class TestRejectsInvalidCurrency:
    @pytest.mark.parametrize(
        'code', ['ngn', 'Ngn', 'NG', 'NGNN', '', '123', 'NG1', 'NG ', ' NGN']
    )
    def test_rejects_a_malformed_code(self, code):
        with pytest.raises(ValueError, match='ISO-4217'):
            Money(1, code)

    def test_rejects_a_non_string_code(self):
        with pytest.raises(ValueError):
            Money(1, 566)

    def test_does_not_silently_normalise_case(self):
        """Upper-casing a lowercase code would hide a caller bug."""
        with pytest.raises(ValueError):
            Money(1, 'ngn')


class TestImmutability:
    def test_cannot_reassign_the_amount(self):
        amount = Money(100, 'NGN')

        with pytest.raises(Exception):
            amount.minor_units = 200

    def test_cannot_reassign_the_currency(self):
        amount = Money(100, 'NGN')

        with pytest.raises(Exception):
            amount.currency = 'USD'

    def test_is_hashable_so_it_can_be_a_dict_key(self):
        assert {Money(100, 'NGN'): 'ok'}[Money(100, 'NGN')] == 'ok'


class TestEquality:
    def test_equal_amounts_in_the_same_currency_are_equal(self):
        assert Money(100, 'NGN') == Money(100, 'NGN')

    def test_different_amounts_are_not_equal(self):
        assert Money(100, 'NGN') != Money(101, 'NGN')

    def test_the_same_number_in_different_currencies_is_not_equal(self):
        assert Money(100, 'NGN') != Money(100, 'USD')

    def test_is_not_equal_to_a_bare_number(self):
        assert Money(100, 'NGN') != 100


class TestArithmetic:
    def test_adds_amounts_in_the_same_currency(self):
        assert Money(100, 'NGN') + Money(50, 'NGN') == Money(150, 'NGN')

    def test_subtracts_amounts_in_the_same_currency(self):
        assert Money(100, 'NGN') - Money(50, 'NGN') == Money(50, 'NGN')

    def test_subtraction_can_go_negative(self):
        assert Money(50, 'NGN') - Money(100, 'NGN') == Money(-50, 'NGN')

    def test_negation_flips_the_sign(self):
        assert -Money(100, 'NGN') == Money(-100, 'NGN')

    def test_addition_is_exact_at_scales_that_would_break_a_float(self):
        # 0.1 + 0.2 in minor units is exactly 0.3 — no float drift.
        assert Money(10, 'NGN') + Money(20, 'NGN') == Money(30, 'NGN')

    def test_addition_is_exact_for_very_large_amounts(self):
        big = 9_007_199_254_740_993  # beyond float's exact integer range
        assert (Money(big, 'NGN') + Money(1, 'NGN')).minor_units == big + 1

    def test_operands_are_unchanged_by_arithmetic(self):
        left, right = Money(100, 'NGN'), Money(50, 'NGN')

        left + right

        assert left == Money(100, 'NGN')
        assert right == Money(50, 'NGN')


class TestCurrencyMismatch:
    def test_addition_across_currencies_is_rejected(self):
        with pytest.raises(CurrencyMismatchError):
            Money(100, 'NGN') + Money(100, 'USD')

    def test_subtraction_across_currencies_is_rejected(self):
        with pytest.raises(CurrencyMismatchError):
            Money(100, 'NGN') - Money(100, 'USD')

    def test_comparison_across_currencies_is_rejected(self):
        with pytest.raises(CurrencyMismatchError):
            Money(100, 'NGN') < Money(100, 'USD')

    def test_the_error_names_both_currencies(self):
        with pytest.raises(CurrencyMismatchError) as excinfo:
            Money(100, 'NGN') + Money(100, 'USD')

        assert excinfo.value.left == 'NGN'
        assert excinfo.value.right == 'USD'


class TestArithmeticWithNonMoney:
    @pytest.mark.parametrize('other', [1, 1.5, '1', None])
    def test_cannot_add_a_non_money_value(self, other):
        with pytest.raises(TypeError):
            Money(100, 'NGN') + other

    @pytest.mark.parametrize('other', [1, 1.5, '1', None])
    def test_cannot_subtract_a_non_money_value(self, other):
        with pytest.raises(TypeError):
            Money(100, 'NGN') - other

    def test_cannot_compare_against_a_bare_number(self):
        with pytest.raises(TypeError):
            Money(100, 'NGN') < 100


class TestOrdering:
    def test_orders_by_amount_within_a_currency(self):
        assert Money(50, 'NGN') < Money(100, 'NGN')
        assert Money(100, 'NGN') > Money(50, 'NGN')
        assert Money(100, 'NGN') <= Money(100, 'NGN')
        assert Money(100, 'NGN') >= Money(100, 'NGN')

    def test_sorts_a_list_of_same_currency_amounts(self):
        amounts = [Money(300, 'NGN'), Money(100, 'NGN'), Money(200, 'NGN')]

        assert sorted(amounts) == [
            Money(100, 'NGN'),
            Money(200, 'NGN'),
            Money(300, 'NGN'),
        ]

    def test_negative_sorts_below_zero(self):
        assert Money(-1, 'NGN') < Money.zero('NGN')


class TestRepr:
    def test_shows_minor_units_and_currency(self):
        assert repr(Money(150_00, 'NGN')) == "Money(15000, 'NGN')"
