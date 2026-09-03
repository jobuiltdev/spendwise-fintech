"""Characterization: budgets.

Budgets are advisory. The fintech architecture is explicit that budgets never
block a banking transaction and that banking limits are a separate system, so
the most valuable thing to pin down here is that budgets only ever *observe*
spending — they never gate it — plus the cached-total bookkeeping the signals
maintain.
"""

from datetime import date
from decimal import Decimal

import pytest

from budgets.models import Budget

pytestmark = pytest.mark.django_db

URL = "/api/budgets/"


def payload(category_id, **overrides):
    body = {
        "category": category_id,
        "amount": "100.00",
        "period": "monthly",
        "start_date": "2024-01-01",
    }
    body.update(overrides)
    return body


class TestCreate:
    def test_creates_a_budget_owned_by_the_caller(self, auth_client, user, category):
        response = auth_client.post(URL, payload(category.id), format="json")

        assert response.status_code == 201
        assert Budget.objects.get().user == user

    def test_an_end_date_must_follow_the_start_date_on_update(
        self, auth_client, user, category, make_budget
    ):
        budget = make_budget(user, category, "100.00", start=date(2024, 2, 1))

        response = auth_client.patch(
            f"{URL}{budget.id}/",
            {"start_date": "2024-02-01", "end_date": "2024-01-01"},
            format="json",
        )

        assert response.status_code == 400

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "KNOWN BUG: create uses BudgetCreateSerializer, which does not carry "
            "BudgetSerializer.validate()'s end_date > start_date check, so an "
            "inverted date range is accepted on create but rejected on update. "
            "Asserted as it should behave; delete this marker when fixed."
        ),
    )
    def test_an_end_date_must_follow_the_start_date_on_create(self, auth_client, category):
        response = auth_client.post(
            URL,
            payload(category.id, start_date="2024-02-01", end_date="2024-01-01"),
            format="json",
        )

        assert response.status_code == 400

    def test_the_alert_threshold_defaults_to_eighty_percent(self, auth_client, category):
        auth_client.post(URL, payload(category.id), format="json")

        assert Budget.objects.get().alert_threshold == 80

    def test_cached_totals_are_not_writable_by_the_client(self, auth_client, category):
        auth_client.post(
            URL, payload(category.id, spent_amount="999.00"), format="json"
        )

        assert Budget.objects.get().spent_amount == Decimal("0")


class TestSpentAmountCalculation:
    def test_counts_expenses_inside_the_window(self, user, category, make_budget, make_expense):
        budget = make_budget(user, category, "100.00", start=date(2024, 1, 1))
        budget.end_date = date(2024, 1, 31)
        budget.save()
        make_expense(user, "30.00", when=date(2024, 1, 15), category=category)

        budget.update_spent_amount()

        assert budget.spent_amount == Decimal("30.00")
        assert budget.remaining_amount == Decimal("70.00")
        assert budget.percentage_used == pytest.approx(30.0)

    def test_ignores_expenses_outside_the_window(
        self, user, category, make_budget, make_expense
    ):
        budget = make_budget(user, category, "100.00", start=date(2024, 1, 1))
        budget.end_date = date(2024, 1, 31)
        budget.save()
        make_expense(user, "30.00", when=date(2024, 2, 15), category=category)

        budget.update_spent_amount()

        assert budget.spent_amount == Decimal("0")

    def test_ignores_other_categories(
        self, user, category, make_category, make_budget, make_expense
    ):
        other = make_category(user, "Transport")
        budget = make_budget(user, category, "100.00", start=date(2024, 1, 1))
        budget.end_date = date(2024, 1, 31)
        budget.save()
        make_expense(user, "30.00", when=date(2024, 1, 15), category=other)

        budget.update_spent_amount()

        assert budget.spent_amount == Decimal("0")

    def test_ignores_another_users_spending(
        self, user, other_user, category, make_category, make_budget, make_expense
    ):
        theirs = make_category(other_user, "Food")
        budget = make_budget(user, category, "100.00", start=date(2024, 1, 1))
        budget.end_date = date(2024, 1, 31)
        budget.save()
        make_expense(other_user, "30.00", when=date(2024, 1, 15), category=theirs)

        budget.update_spent_amount()

        assert budget.spent_amount == Decimal("0")

    def test_overspending_produces_a_negative_remainder_rather_than_clamping(
        self, user, category, make_budget, make_expense
    ):
        budget = make_budget(user, category, "100.00", start=date(2024, 1, 1))
        budget.end_date = date(2024, 1, 31)
        budget.save()
        make_expense(user, "150.00", when=date(2024, 1, 15), category=category)

        budget.update_spent_amount()

        assert budget.spent_amount == Decimal("150.00")
        assert budget.remaining_amount == Decimal("-50.00")
        assert budget.percentage_used == pytest.approx(150.0)


class TestSignalsKeepCachedTotalsFresh:
    """budgets/signals.py recalculates the budgets an expense falls into on
    every save and delete, so the stored columns don't drift."""

    @pytest.fixture
    def budget(self, user, category, make_budget):
        budget = make_budget(user, category, "100.00", start=date(2024, 1, 1))
        budget.end_date = date(2024, 12, 31)
        budget.save()
        return budget

    def test_creating_an_expense_updates_the_covering_budget(
        self, budget, user, category, make_expense
    ):
        make_expense(user, "25.00", when=date(2024, 3, 1), category=category)

        budget.refresh_from_db()
        assert budget.spent_amount == Decimal("25.00")

    def test_deleting_an_expense_updates_the_covering_budget(
        self, budget, user, category, make_expense
    ):
        expense = make_expense(user, "25.00", when=date(2024, 3, 1), category=category)
        budget.refresh_from_db()
        assert budget.spent_amount == Decimal("25.00")

        expense.delete()

        budget.refresh_from_db()
        assert budget.spent_amount == Decimal("0")

    def test_editing_an_amount_updates_the_covering_budget(
        self, budget, user, category, make_expense
    ):
        expense = make_expense(user, "25.00", when=date(2024, 3, 1), category=category)

        expense.amount = Decimal("60.00")
        expense.save()

        budget.refresh_from_db()
        assert budget.spent_amount == Decimal("60.00")

    def test_moving_an_expense_between_categories_repairs_both_budgets(
        self, budget, user, category, make_category, make_budget, make_expense
    ):
        transport = make_category(user, "Transport")
        transport_budget = make_budget(user, transport, "100.00", start=date(2024, 1, 1))
        transport_budget.end_date = date(2024, 12, 31)
        transport_budget.save()
        expense = make_expense(user, "25.00", when=date(2024, 3, 1), category=category)

        expense.category = transport
        expense.save()

        budget.refresh_from_db()
        transport_budget.refresh_from_db()
        assert budget.spent_amount == Decimal("0")
        assert transport_budget.spent_amount == Decimal("25.00")

    def test_an_uncategorised_expense_touches_no_budget(
        self, budget, user, make_expense
    ):
        make_expense(user, "25.00", when=date(2024, 3, 1), category=None)

        budget.refresh_from_db()
        assert budget.spent_amount == Decimal("0")


class TestBudgetsAreAdvisoryOnly:
    def test_an_expense_far_over_budget_is_still_accepted(
        self, auth_client, user, category, make_budget
    ):
        """Budgets inform; they never block. This is a locked fintech
        invariant — banking limits are a separate system."""
        make_budget(user, category, "10.00", start=date(2024, 1, 1))

        response = auth_client.post(
            "/api/expenses/",
            {
                "amount": "5000.00",
                "description": "Way over budget",
                "date": "2024-01-15",
                "category": category.id,
            },
            format="json",
        )

        assert response.status_code == 201


class TestStatusReporting:
    @pytest.mark.parametrize(
        "spent,expected",
        [("10.00", "good"), ("85.00", "warning"), ("120.00", "exceeded")],
    )
    def test_status_reflects_usage_against_the_alert_threshold(
        self, auth_client, user, category, make_budget, make_expense, spent, expected
    ):
        budget = make_budget(user, category, "100.00", start=date(2024, 1, 1))
        budget.end_date = date(2024, 12, 31)
        budget.save()
        make_expense(user, spent, when=date(2024, 3, 1), category=category)

        body = auth_client.get(f"{URL}{budget.id}/").json()

        assert body["status"] == expected


class TestOwnershipIsolation:
    def test_the_list_shows_only_the_callers_budgets(
        self, auth_client, user, other_user, category, make_category, make_budget
    ):
        mine = make_budget(user, category, "100.00")
        make_budget(other_user, make_category(other_user, "Food"), "100.00")

        body = auth_client.get(URL).json()
        results = body["results"] if isinstance(body, dict) else body

        assert [row["id"] for row in results] == [mine.id]

    def test_another_users_budget_is_not_retrievable(
        self, auth_client, other_user, make_category, make_budget
    ):
        theirs = make_budget(other_user, make_category(other_user, "Food"), "100.00")

        assert auth_client.get(f"{URL}{theirs.id}/").status_code == 404

    def test_another_users_budget_cannot_be_edited(
        self, auth_client, other_user, make_category, make_budget
    ):
        theirs = make_budget(other_user, make_category(other_user, "Food"), "100.00")

        assert auth_client.patch(
            f"{URL}{theirs.id}/", {"amount": "1.00"}, format="json"
        ).status_code == 404
