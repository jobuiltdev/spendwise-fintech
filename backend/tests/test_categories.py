"""Characterization: categories.

Categories are a KEEP area, including their curated colour and icon values —
the fintech design system explicitly preserves the existing palette rather than
replacing it. What matters here is per-user uniqueness, ownership, and the
soft-delete (archive/restore) behaviour the app relies on.
"""

import pytest

from categories.models import Category

pytestmark = pytest.mark.django_db

URL = "/api/categories/"


class TestCreate:
    def test_creates_a_category_owned_by_the_caller(self, auth_client, user):
        response = auth_client.post(URL, {"name": "Transport"}, format="json")

        assert response.status_code == 201
        assert Category.objects.get(name="Transport").user == user

    def test_defaults_to_an_expense_category_that_is_active(self, auth_client):
        auth_client.post(URL, {"name": "Transport"}, format="json")

        category = Category.objects.get()
        assert category.category_type == "expense"
        assert category.is_active is True

    def test_colour_and_icon_are_stored_as_given(self, auth_client):
        """Curated colours are shipped functionality; the API stores what the
        client picks rather than constraining it to a fixed set."""
        response = auth_client.post(
            URL, {"name": "Transport", "color": "#4F46E5", "icon": "car"}, format="json"
        )

        assert response.status_code == 201
        category = Category.objects.get()
        assert category.color == "#4F46E5"
        assert category.icon == "car"

    def test_there_is_a_default_colour_when_none_is_given(self, auth_client):
        auth_client.post(URL, {"name": "Transport"}, format="json")

        assert Category.objects.get().color == "#007bff"

    def test_is_default_cannot_be_set_by_the_client(self, auth_client):
        auth_client.post(URL, {"name": "Transport", "is_default": True}, format="json")

        assert Category.objects.get().is_default is False

    def test_a_duplicate_name_is_rejected_case_insensitively(self, auth_client, category):
        response = auth_client.post(URL, {"name": "FOOD"}, format="json")

        assert response.status_code == 400
        assert Category.objects.filter(user=category.user).count() == 1

    def test_two_users_may_each_have_the_same_category_name(
        self, auth_client, other_client, user, other_user
    ):
        assert auth_client.post(URL, {"name": "Food"}, format="json").status_code == 201
        assert other_client.post(URL, {"name": "Food"}, format="json").status_code == 201

        assert Category.objects.filter(name="Food").count() == 2

    def test_renaming_to_its_own_current_name_is_allowed(self, auth_client, category):
        response = auth_client.patch(
            f"{URL}{category.id}/", {"name": "Food", "description": "updated"}, format="json"
        )

        assert response.status_code == 200


class TestArchiveAndRestore:
    def test_archive_soft_deletes_rather_than_removing_the_row(
        self, auth_client, category
    ):
        response = auth_client.post(f"{URL}{category.id}/archive/")

        assert response.status_code == 200
        category.refresh_from_db()
        assert category.is_active is False
        assert Category.objects.filter(id=category.id).exists()

    def test_restore_reactivates_an_archived_category(self, auth_client, category):
        auth_client.post(f"{URL}{category.id}/archive/")

        auth_client.post(f"{URL}{category.id}/restore/")

        category.refresh_from_db()
        assert category.is_active is True

    def test_archiving_keeps_the_expenses_that_reference_it(
        self, auth_client, user, category, make_expense
    ):
        expense = make_expense(user, category=category)

        auth_client.post(f"{URL}{category.id}/archive/")

        expense.refresh_from_db()
        assert expense.category == category


class TestTypeFilters:
    def test_expense_categories_action_returns_only_expense_types(
        self, auth_client, user, make_category
    ):
        make_category(user, "Food", category_type="expense")
        make_category(user, "Salary", category_type="income")

        rows = auth_client.get(f"{URL}expense_categories/").json()

        assert [row["name"] for row in rows] == ["Food"]

    def test_income_categories_action_returns_only_income_types(
        self, auth_client, user, make_category
    ):
        make_category(user, "Food", category_type="expense")
        make_category(user, "Salary", category_type="income")

        rows = auth_client.get(f"{URL}income_categories/").json()

        assert [row["name"] for row in rows] == ["Salary"]


class TestOwnershipIsolation:
    def test_the_list_shows_only_the_callers_categories(
        self, auth_client, user, other_user, make_category
    ):
        mine = make_category(user, "Mine")
        make_category(other_user, "Theirs")

        body = auth_client.get(URL).json()
        results = body["results"] if isinstance(body, dict) else body

        assert [row["id"] for row in results] == [mine.id]

    def test_another_users_category_is_not_retrievable(
        self, auth_client, other_user, make_category
    ):
        theirs = make_category(other_user, "Theirs")

        assert auth_client.get(f"{URL}{theirs.id}/").status_code == 404

    def test_another_users_category_cannot_be_archived(
        self, auth_client, other_user, make_category
    ):
        theirs = make_category(other_user, "Theirs")

        assert auth_client.post(f"{URL}{theirs.id}/archive/").status_code == 404
        theirs.refresh_from_db()
        assert theirs.is_active is True


class TestSummary:
    def test_reports_spend_per_category_for_the_caller(
        self, auth_client, user, make_category, make_expense
    ):
        food = make_category(user, "Food")
        make_expense(user, "10.00", category=food)
        make_expense(user, "5.00", category=food)

        rows = auth_client.get(f"{URL}summary/").json()
        row = next(r for r in rows if r["id"] == food.id)

        assert str(row["total_spent"]) in {"15.00", "15.0", "15"}
        assert row["expense_count"] == 2

    def test_a_category_with_no_expenses_reports_zero(
        self, auth_client, user, make_category
    ):
        empty = make_category(user, "Unused")

        rows = auth_client.get(f"{URL}summary/").json()
        row = next(r for r in rows if r["id"] == empty.id)

        assert row["expense_count"] == 0
