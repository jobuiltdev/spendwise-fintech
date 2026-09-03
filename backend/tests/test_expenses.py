"""Characterization: manual expenses.

An Expense is a **manual, user-authored record of spending** — the thing the
fintech architecture calls a manual expense and explicitly separates from a
financial transaction. The invariants worth protecting here are that it stays
freely editable and deletable by its owner, that it never leaks across users,
and that it carries no ledger semantics.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from expenses.models import Expense

pytestmark = pytest.mark.django_db

URL = "/api/expenses/"


def create_payload(**overrides):
    payload = {"amount": "25.50", "description": "Lunch", "date": "2024-01-15"}
    payload.update(overrides)
    return payload


class TestCreate:
    def test_creates_an_expense_owned_by_the_caller(self, auth_client, user):
        response = auth_client.post(URL, create_payload(), format="json")

        assert response.status_code == 201
        expense = Expense.objects.get()
        assert expense.user == user
        assert expense.amount == Decimal("25.50")

    def test_the_create_response_carries_the_full_record_not_just_the_input(
        self, auth_client
    ):
        """ReadSerializerResponseMixin: writes answer with the read
        representation, so the client gets an id and computed fields back."""
        body = auth_client.post(URL, create_payload(), format="json").json()

        assert "id" in body
        assert body["description"] == "Lunch"
        assert "category_details" in body

    def test_ownership_cannot_be_forged_through_the_payload(
        self, auth_client, user, other_user
    ):
        auth_client.post(URL, create_payload(user=other_user.id), format="json")

        assert Expense.objects.get().user == user

    def test_a_new_expense_is_completed_by_default(self, auth_client):
        auth_client.post(URL, create_payload(), format="json")

        assert Expense.objects.get().payment_status == "completed"

    def test_a_new_expense_is_not_a_group_expense(self, auth_client):
        auth_client.post(URL, create_payload(), format="json")

        assert Expense.objects.get().is_group_expense is False

    @pytest.mark.parametrize("amount", ["0", "0.00", "-5.00"])
    def test_non_positive_amounts_are_rejected(self, auth_client, amount):
        response = auth_client.post(URL, create_payload(amount=amount), format="json")

        assert response.status_code == 400
        assert Expense.objects.count() == 0

    def test_description_is_required(self, auth_client):
        response = auth_client.post(
            URL, {"amount": "10.00", "date": "2024-01-15"}, format="json"
        )

        assert response.status_code == 400

    def test_a_future_date_is_currently_accepted(self, auth_client):
        """Current behaviour: future-dating is allowed (the validator is
        commented out in the serializer). Recorded, not endorsed."""
        response = auth_client.post(URL, create_payload(date="2099-12-31"), format="json")

        assert response.status_code == 201

    def test_can_be_linked_to_a_category_and_payment_method(
        self, auth_client, user, category, make_payment_method
    ):
        method = make_payment_method(user, "Cash")

        response = auth_client.post(
            URL,
            create_payload(category=category.id, payment_method=method.id),
            format="json",
        )

        assert response.status_code == 201
        expense = Expense.objects.get()
        assert expense.category == category
        assert expense.payment_method == method


class TestManualExpenseLifecycle:
    """Manual expenses remain fully mutable — this is what separates them from
    a financial transaction, which must never be customer-editable."""

    def test_the_owner_can_edit_the_amount(self, auth_client, user, make_expense):
        expense = make_expense(user, "10.00")

        response = auth_client.patch(
            f"{URL}{expense.id}/", {"amount": "42.00"}, format="json"
        )

        assert response.status_code == 200
        expense.refresh_from_db()
        assert expense.amount == Decimal("42.00")

    def test_the_owner_can_delete_it(self, auth_client, user, make_expense):
        expense = make_expense(user)

        assert auth_client.delete(f"{URL}{expense.id}/").status_code == 204
        assert not Expense.objects.filter(id=expense.id).exists()

    def test_an_edit_cannot_reassign_ownership(
        self, auth_client, user, other_user, make_expense
    ):
        expense = make_expense(user)

        auth_client.patch(f"{URL}{expense.id}/", {"user": other_user.id}, format="json")

        expense.refresh_from_db()
        assert expense.user == user


class TestOwnershipIsolation:
    def test_the_list_shows_only_the_callers_expenses(
        self, auth_client, user, other_user, make_expense
    ):
        mine = make_expense(user, description="Mine")
        make_expense(other_user, description="Theirs")

        body = auth_client.get(URL).json()
        results = body["results"] if isinstance(body, dict) else body

        assert [row["id"] for row in results] == [mine.id]

    def test_another_users_expense_is_not_retrievable(
        self, auth_client, other_user, make_expense
    ):
        theirs = make_expense(other_user)

        assert auth_client.get(f"{URL}{theirs.id}/").status_code == 404

    def test_another_users_expense_cannot_be_edited(
        self, auth_client, other_user, make_expense
    ):
        theirs = make_expense(other_user, "10.00")

        assert auth_client.patch(
            f"{URL}{theirs.id}/", {"amount": "999.00"}, format="json"
        ).status_code == 404
        theirs.refresh_from_db()
        assert theirs.amount == Decimal("10.00")

    def test_another_users_expense_cannot_be_deleted(
        self, auth_client, other_user, make_expense
    ):
        theirs = make_expense(other_user)

        assert auth_client.delete(f"{URL}{theirs.id}/").status_code == 404
        assert Expense.objects.filter(id=theirs.id).exists()


class TestFiltering:
    def test_filters_by_date_range(self, auth_client, user, make_expense):
        make_expense(user, when=date(2024, 1, 10), description="in")
        make_expense(user, when=date(2024, 3, 10), description="out")

        body = auth_client.get(
            URL, {"start_date": "2024-01-01", "end_date": "2024-01-31"}
        ).json()
        results = body["results"] if isinstance(body, dict) else body

        assert [row["description"] for row in results] == ["in"]

    def test_filters_by_year_and_month(self, auth_client, user, make_expense):
        make_expense(user, when=date(2024, 1, 10), description="jan")
        make_expense(user, when=date(2024, 2, 10), description="feb")

        body = auth_client.get(URL, {"year": "2024", "month": "2"}).json()
        results = body["results"] if isinstance(body, dict) else body

        assert [row["description"] for row in results] == ["feb"]

    def test_filters_by_category(self, auth_client, user, make_expense, make_category):
        food = make_category(user, "Food")
        make_expense(user, category=food, description="with-category")
        make_expense(user, description="without-category")

        body = auth_client.get(URL, {"category": food.id}).json()
        results = body["results"] if isinstance(body, dict) else body

        assert [row["description"] for row in results] == ["with-category"]

    def test_default_ordering_is_newest_first(self, auth_client, user, make_expense):
        make_expense(user, when=date(2024, 1, 1), description="older")
        make_expense(user, when=date(2024, 6, 1), description="newer")

        body = auth_client.get(URL).json()
        results = body["results"] if isinstance(body, dict) else body

        assert [row["description"] for row in results] == ["newer", "older"]


class TestSummary:
    def test_totals_count_and_average_come_from_the_callers_expenses_only(
        self, auth_client, user, other_user, make_expense
    ):
        make_expense(user, "10.00")
        make_expense(user, "30.00")
        make_expense(other_user, "999.00")

        body = auth_client.get(f"{URL}summary/").json()

        assert Decimal(str(body["total_amount"])) == Decimal("40.00")
        assert body["expense_count"] == 2
        assert Decimal(str(body["average_amount"])) == Decimal("20.00")

    def test_an_empty_account_reports_zeroes_rather_than_failing(self, auth_client):
        body = auth_client.get(f"{URL}summary/").json()

        assert body["expense_count"] == 0
        assert Decimal(str(body["total_amount"])) == Decimal("0")
        assert Decimal(str(body["average_amount"])) == Decimal("0")

    def test_category_breakdown_groups_by_category(
        self, auth_client, user, make_expense, make_category
    ):
        food = make_category(user, "Food")
        make_expense(user, "10.00", category=food)
        make_expense(user, "15.00", category=food)

        body = auth_client.get(f"{URL}summary/").json()
        rows = [row for row in body["category_breakdown"] if row["category__id"] == food.id]

        assert len(rows) == 1
        assert Decimal(str(rows[0]["total"])) == Decimal("25.00")
        assert rows[0]["count"] == 2


class TestRecent:
    def test_returns_only_the_last_thirty_days(self, auth_client, user, make_expense):
        from django.utils import timezone

        today = timezone.now().date()
        make_expense(user, when=today, description="recent")
        make_expense(user, when=today - timedelta(days=45), description="old")

        rows = auth_client.get(f"{URL}recent/").json()

        assert [row["description"] for row in rows] == ["recent"]
