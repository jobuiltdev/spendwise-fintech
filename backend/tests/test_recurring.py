"""Characterization: recurring expenses.

Recurring V1 is **planning and intelligence only — never autopay**. The
architecture is explicit that existing generation may remain precisely because
it creates *expense records*, not payments. So the invariant most worth
protecting is that generating produces an Expense row and moves a date forward,
and nothing else: no money moves.

Deliberately not characterized: any customer-facing copy that could read as
automated debiting (see the report).
"""

from datetime import date
from decimal import Decimal

import pytest

from expenses.models import Expense
from recurring.models import RecurringExpense, RecurringExpenseLog

pytestmark = pytest.mark.django_db


@pytest.fixture
def make_recurring(db):
    def _make(owner, cat, frequency="monthly", start=date(2024, 1, 1), **extra):
        return RecurringExpense.objects.create(
            user=owner,
            category=cat,
            amount=Decimal(extra.pop("amount", "50.00")),
            description=extra.pop("description", "Gym"),
            frequency=frequency,
            start_date=start,
            next_occurrence=extra.pop("next_occurrence", start),
            **extra,
        )

    return _make


class TestCadenceMath:
    @pytest.mark.parametrize(
        "frequency,expected",
        [
            ("daily", date(2024, 1, 2)),
            ("weekly", date(2024, 1, 8)),
            ("biweekly", date(2024, 1, 15)),
            ("monthly", date(2024, 2, 1)),
            ("quarterly", date(2024, 4, 1)),
            ("yearly", date(2025, 1, 1)),
        ],
    )
    def test_each_frequency_advances_by_one_of_its_units(
        self, user, category, make_recurring, frequency, expected
    ):
        recurring = make_recurring(user, category, frequency=frequency)

        assert recurring.calculate_next_date(date(2024, 1, 1)) == expected

    @pytest.mark.parametrize(
        "frequency,interval,expected",
        [
            ("daily", 3, date(2024, 1, 4)),
            ("weekly", 2, date(2024, 1, 15)),
            ("biweekly", 2, date(2024, 1, 29)),
            ("monthly", 2, date(2024, 3, 1)),
            ("quarterly", 2, date(2024, 7, 1)),
            ("yearly", 2, date(2026, 1, 1)),
        ],
    )
    def test_interval_multiplies_the_step(
        self, user, category, make_recurring, frequency, interval, expected
    ):
        recurring = make_recurring(user, category, frequency=frequency, interval=interval)

        assert recurring.calculate_next_date(date(2024, 1, 1)) == expected

    def test_monthly_uses_calendar_months_not_thirty_day_blocks(
        self, user, category, make_recurring
    ):
        recurring = make_recurring(user, category, frequency="monthly")

        assert recurring.calculate_next_date(date(2024, 1, 31)) == date(2024, 2, 29)

    def test_a_custom_frequency_falls_back_to_monthly(
        self, user, category, make_recurring
    ):
        recurring = make_recurring(user, category, frequency="custom")

        assert recurring.calculate_next_date(date(2024, 1, 1)) == date(2024, 2, 1)

    def test_defaults_to_the_next_occurrence_when_no_date_is_given(
        self, user, category, make_recurring
    ):
        recurring = make_recurring(
            user, category, frequency="monthly", next_occurrence=date(2024, 5, 10)
        )

        assert recurring.calculate_next_date() == date(2024, 6, 10)


class TestUpcomingDates:
    def test_returns_the_requested_number_of_future_dates(
        self, user, category, make_recurring
    ):
        recurring = make_recurring(user, category, frequency="monthly")

        assert recurring.get_upcoming_dates(3) == [
            date(2024, 1, 1),
            date(2024, 2, 1),
            date(2024, 3, 1),
        ]

    def test_stops_at_the_end_date(self, user, category, make_recurring):
        recurring = make_recurring(
            user, category, frequency="monthly", end_date=date(2024, 2, 15)
        )

        assert recurring.get_upcoming_dates(5) == [date(2024, 1, 1), date(2024, 2, 1)]

    def test_does_not_generate_anything(self, user, category, make_recurring):
        """Previewing upcoming dates must never create expense records."""
        recurring = make_recurring(user, category)

        recurring.get_upcoming_dates(5)

        assert Expense.objects.count() == 0


class TestGeneration:
    def test_creates_a_plain_expense_record(self, user, category, make_recurring):
        recurring = make_recurring(user, category, amount="50.00")

        expense = recurring.generate_expense()

        assert isinstance(expense, Expense)
        assert expense.user == user
        assert expense.category == category
        assert expense.amount == Decimal("50.00")
        assert expense.date == date(2024, 1, 1)

    def test_the_generated_record_is_an_ordinary_editable_expense(
        self, user, category, make_recurring
    ):
        """Generation produces a manual-expense row — not a payment, not a
        ledger entry. It carries no money-movement state of its own."""
        recurring = make_recurring(user, category)

        expense = recurring.generate_expense()

        assert expense.is_group_expense is False
        assert expense.group is None
        assert expense.payment_status == "completed"

    def test_advances_the_schedule(self, user, category, make_recurring):
        recurring = make_recurring(user, category, frequency="monthly")

        recurring.generate_expense()

        recurring.refresh_from_db()
        assert recurring.last_occurrence == date(2024, 1, 1)
        assert recurring.next_occurrence == date(2024, 2, 1)

    def test_writes_a_log_entry_linked_to_the_expense(
        self, user, category, make_recurring
    ):
        recurring = make_recurring(user, category)

        expense = recurring.generate_expense()

        log = RecurringExpenseLog.objects.get()
        assert log.recurring_expense == recurring
        assert log.expense == expense
        assert log.scheduled_date == date(2024, 1, 1)
        assert log.status == "created"

    def test_deactivates_once_the_schedule_passes_the_end_date(
        self, user, category, make_recurring
    ):
        recurring = make_recurring(
            user, category, frequency="monthly", end_date=date(2024, 1, 15)
        )

        recurring.generate_expense()

        recurring.refresh_from_db()
        assert recurring.is_active is False

    def test_stays_active_while_the_end_date_is_still_ahead(
        self, user, category, make_recurring
    ):
        recurring = make_recurring(
            user, category, frequency="monthly", end_date=date(2024, 12, 31)
        )

        recurring.generate_expense()

        recurring.refresh_from_db()
        assert recurring.is_active is True

    def test_an_explicit_date_overrides_the_schedule(
        self, user, category, make_recurring
    ):
        recurring = make_recurring(user, category, frequency="monthly")

        expense = recurring.generate_expense(expense_date=date(2024, 6, 10))

        assert expense.date == date(2024, 6, 10)
        recurring.refresh_from_db()
        assert recurring.next_occurrence == date(2024, 7, 10)

    def test_generating_twice_produces_two_distinct_dated_expenses(
        self, user, category, make_recurring
    ):
        """There is no duplicate-generation guard on the model itself; each
        call advances the schedule, so repeats land on different dates."""
        recurring = make_recurring(user, category, frequency="monthly")

        first = recurring.generate_expense()
        second = recurring.generate_expense()

        assert first.date == date(2024, 1, 1)
        assert second.date == date(2024, 2, 1)
        assert Expense.objects.count() == 2


class TestAutoCreateDefault:
    def test_generation_is_manual_by_default(self, user, category, make_recurring):
        """auto_create defaults to False — nothing happens without an explicit
        request. Relevant to the no-autopay rule."""
        recurring = make_recurring(user, category)

        assert recurring.auto_create is False


class TestRecurrencePatternStorage:
    def test_round_trips_a_pattern_dictionary(self, user, category, make_recurring):
        recurring = make_recurring(user, category)

        recurring.set_recurrence_pattern({"weekday": "monday"})
        recurring.save()

        recurring.refresh_from_db()
        assert recurring.get_recurrence_pattern() == {"weekday": "monday"}

    def test_returns_none_when_unset(self, user, category, make_recurring):
        recurring = make_recurring(user, category)

        assert recurring.get_recurrence_pattern() is None

    def test_returns_none_rather_than_raising_on_corrupt_data(
        self, user, category, make_recurring
    ):
        recurring = make_recurring(user, category)
        recurring.recurrence_pattern = "{not json"
        recurring.save()

        assert recurring.get_recurrence_pattern() is None


class TestOwnershipIsolation:
    URL = "/api/recurring-expenses/"

    def test_the_list_shows_only_the_callers_schedules(
        self, auth_client, user, other_user, category, make_category, make_recurring
    ):
        mine = make_recurring(user, category)
        make_recurring(other_user, make_category(other_user, "Food"))

        body = auth_client.get(self.URL).json()
        results = body["results"] if isinstance(body, dict) else body

        assert [row["id"] for row in results] == [mine.id]

    def test_another_users_schedule_is_not_retrievable(
        self, auth_client, other_user, make_category, make_recurring
    ):
        theirs = make_recurring(other_user, make_category(other_user, "Food"))

        assert auth_client.get(f"{self.URL}{theirs.id}/").status_code == 404
