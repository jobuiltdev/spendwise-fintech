"""Characterization: deterministic analytics and date helpers.

Only the stable, reusable calculations are pinned here: the month-boundary
helpers several apps share, the spending-trend series, and the trend-line slope.

Deliberately not characterized: AnalyticsService.calculate_financial_health()
and its sub-scores. The numeric Financial Health score is being removed from the
customer experience, so locking its arithmetic would work against that decision
(see the report).
"""

from datetime import date
from decimal import Decimal

import pytest

from analytics.services import AnalyticsService
from spendwise.date_ranges import (
    month_bounds,
    month_offset,
    month_start,
    previous_month_bounds,
)

pytestmark = pytest.mark.django_db


class TestMonthBoundaryHelpers:
    """Shared by expenses, budgets, analytics and reports."""

    def test_month_bounds_covers_the_whole_month(self):
        assert month_bounds(2024, 1) == (date(2024, 1, 1), date(2024, 1, 31))

    def test_month_bounds_handles_a_leap_february(self):
        assert month_bounds(2024, 2) == (date(2024, 2, 1), date(2024, 2, 29))

    def test_month_bounds_handles_a_non_leap_february(self):
        assert month_bounds(2023, 2) == (date(2023, 2, 1), date(2023, 2, 28))

    def test_month_start_snaps_to_the_first(self):
        assert month_start(date(2024, 3, 17)) == date(2024, 3, 1)

    def test_previous_month_bounds_crosses_a_year_boundary(self):
        assert previous_month_bounds(date(2024, 1, 15)) == (
            date(2023, 12, 1),
            date(2023, 12, 31),
        )

    def test_previous_month_of_march_is_february_not_thirty_days_back(self):
        assert previous_month_bounds(date(2024, 3, 1)) == (
            date(2024, 2, 1),
            date(2024, 2, 29),
        )

    @pytest.mark.parametrize(
        "months_back,expected",
        [
            (0, (date(2024, 3, 1), date(2024, 3, 31))),
            (1, (date(2024, 2, 1), date(2024, 2, 29))),
            (3, (date(2023, 12, 1), date(2023, 12, 31))),
            (12, (date(2023, 3, 1), date(2023, 3, 31))),
        ],
    )
    def test_month_offset_counts_calendar_months(self, months_back, expected):
        assert month_offset(date(2024, 3, 15), months_back) == expected

    def test_month_offset_never_skips_a_short_month(self):
        """Six steps back from March must land on six distinct months."""
        months = [month_offset(date(2024, 3, 15), i)[0].month for i in range(6)]

        assert months == [3, 2, 1, 12, 11, 10]


class TestSpendingTrends:
    def test_returns_one_bucket_per_requested_month(self, user):
        trends = AnalyticsService(user).get_spending_trends(months=6)

        assert len(trends) == 6

    def test_buckets_are_distinct_consecutive_months(self, user):
        trends = AnalyticsService(user).get_spending_trends(months=6)

        keys = [(row["year"], row["month"]) for row in trends]
        assert len(set(keys)) == 6

    def test_a_user_with_no_expenses_reports_zero_rather_than_failing(self, user):
        trends = AnalyticsService(user).get_spending_trends(months=3)

        assert all(row["total_spent"] == Decimal("0") for row in trends)

    def test_the_current_month_totals_the_users_expenses(
        self, user, make_expense
    ):
        from django.utils import timezone

        today = timezone.now().date()
        make_expense(user, "40.00", when=today)
        make_expense(user, "60.00", when=today)

        trends = AnalyticsService(user).get_spending_trends(months=1)

        assert trends[0]["total_spent"] == Decimal("100.00")

    def test_another_users_spending_is_excluded(
        self, user, other_user, make_expense
    ):
        from django.utils import timezone

        make_expense(other_user, "500.00", when=timezone.now().date())

        trends = AnalyticsService(user).get_spending_trends(months=1)

        assert trends[0]["total_spent"] == Decimal("0")

    def test_uncategorised_spending_is_labelled_rather_than_dropped(
        self, user, make_expense
    ):
        from django.utils import timezone

        make_expense(user, "25.00", when=timezone.now().date(), category=None)

        trends = AnalyticsService(user).get_spending_trends(months=1)

        assert trends[0]["category_breakdown"] == {"Uncategorized": 25.0}


class TestTrendSlope:
    def test_a_rising_series_has_a_positive_slope(self, user):
        slope = AnalyticsService(user)._calculate_slope([1, 2, 3], [10, 20, 30])

        assert slope == pytest.approx(10.0)

    def test_a_falling_series_has_a_negative_slope(self, user):
        slope = AnalyticsService(user)._calculate_slope([1, 2, 3], [30, 20, 10])

        assert slope == pytest.approx(-10.0)

    def test_a_flat_series_has_no_slope(self, user):
        slope = AnalyticsService(user)._calculate_slope([1, 2, 3], [10, 10, 10])

        assert slope == pytest.approx(0.0)

    def test_an_empty_series_returns_zero_rather_than_raising(self, user):
        assert AnalyticsService(user)._calculate_slope([], []) == 0

    def test_a_single_point_returns_zero_rather_than_dividing_by_zero(self, user):
        assert AnalyticsService(user)._calculate_slope([1], [10]) == 0
