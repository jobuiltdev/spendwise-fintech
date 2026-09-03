"""Characterization: groups, splits and balances.

Groups are a KEEP area and the balance sheet is the most valuable reusable math
in the backend: splits always sum to their expense, so `paid - owed` cancels to
zero across the group, and recorded settlements move value between members
without inventing any. Settlement here stays *recorded, not executed* — no money
moves through SpendWise.
"""

from datetime import date
from decimal import Decimal

import pytest

from groups.models import Group, GroupExpense, GroupExpenseSplit, GroupMembership, GroupSettlement

pytestmark = pytest.mark.django_db

GROUPS_URL = "/api/groups/"
GROUP_EXPENSES_URL = "/api/group-expenses/"


@pytest.fixture
def make_group(db):
    def _make(creator, name="Flatmates", members=()):
        group = Group.objects.create(name=name, created_by=creator)
        group.generate_invite_code()
        group.save()
        GroupMembership.objects.create(group=group, user=creator, role="admin")
        for member in members:
            GroupMembership.objects.create(group=group, user=member, role="member")
        return group

    return _make


def balances_by_username(payload):
    return {row["username"]: Decimal(str(row["balance"])) for row in payload["balances"]}


class TestGroupCreation:
    def test_the_creator_becomes_an_admin_member(self, auth_client, user):
        response = auth_client.post(GROUPS_URL, {"name": "Trip"}, format="json")

        assert response.status_code == 201
        group = Group.objects.get()
        membership = GroupMembership.objects.get(group=group, user=user)
        assert membership.role == "admin"

    def test_an_invite_code_is_generated(self, auth_client):
        auth_client.post(GROUPS_URL, {"name": "Trip"}, format="json")

        group = Group.objects.get()
        assert group.invite_code
        assert len(group.invite_code) == 8

    def test_invite_codes_are_not_client_supplied(self, auth_client):
        auth_client.post(
            GROUPS_URL, {"name": "Trip", "invite_code": "CHOSEN"}, format="json"
        )

        assert Group.objects.get().invite_code != "CHOSEN"

    def test_a_new_group_is_active(self, auth_client):
        auth_client.post(GROUPS_URL, {"name": "Trip"}, format="json")

        assert Group.objects.get().status == "active"


class TestEqualSplits:
    def test_an_equal_split_covers_every_member(
        self, auth_client, user, other_user, make_group
    ):
        group = make_group(user, members=[other_user])

        response = auth_client.post(
            GROUP_EXPENSES_URL,
            {
                "group": group.id,
                "paid_by": user.id,
                "amount": "100.00",
                "description": "Dinner",
                "date": "2024-01-15",
                "split_type": "equal",
            },
            format="json",
        )

        assert response.status_code == 201
        splits = GroupExpenseSplit.objects.all()
        assert splits.count() == 2
        assert {s.amount for s in splits} == {Decimal("50.00")}

    def test_equal_splits_sum_to_the_expense_total(
        self, auth_client, user, other_user, make_user, make_group
    ):
        third = make_user("carol")
        group = make_group(user, members=[other_user, third])

        auth_client.post(
            GROUP_EXPENSES_URL,
            {
                "group": group.id,
                "paid_by": user.id,
                "amount": "90.00",
                "description": "Dinner",
                "date": "2024-01-15",
                "split_type": "equal",
            },
            format="json",
        )

        total = sum(s.amount for s in GroupExpenseSplit.objects.all())
        assert total == Decimal("90.00")


class TestSplitValidation:
    def test_custom_splits_must_sum_to_the_expense_total(
        self, auth_client, user, other_user, make_group
    ):
        group = make_group(user, members=[other_user])

        response = auth_client.post(
            GROUP_EXPENSES_URL,
            {
                "group": group.id,
                "paid_by": user.id,
                "amount": "100.00",
                "description": "Dinner",
                "date": "2024-01-15",
                "split_type": "custom",
                "splits": [
                    {"user": user.id, "amount": "40.00"},
                    {"user": other_user.id, "amount": "40.00"},
                ],
            },
            format="json",
        )

        assert response.status_code == 400

    def test_matching_custom_splits_are_accepted(
        self, auth_client, user, other_user, make_group
    ):
        group = make_group(user, members=[other_user])

        response = auth_client.post(
            GROUP_EXPENSES_URL,
            {
                "group": group.id,
                "paid_by": user.id,
                "amount": "100.00",
                "description": "Dinner",
                "date": "2024-01-15",
                "split_type": "custom",
                "splits": [
                    {"user": user.id, "amount": "70.00"},
                    {"user": other_user.id, "amount": "30.00"},
                ],
            },
            format="json",
        )

        assert response.status_code == 201

    def test_custom_splits_require_split_details(
        self, auth_client, user, other_user, make_group
    ):
        group = make_group(user, members=[other_user])

        response = auth_client.post(
            GROUP_EXPENSES_URL,
            {
                "group": group.id,
                "paid_by": user.id,
                "amount": "100.00",
                "description": "Dinner",
                "date": "2024-01-15",
                "split_type": "custom",
            },
            format="json",
        )

        assert response.status_code == 400

    def test_the_payer_must_be_a_member_of_the_group(
        self, auth_client, user, other_user, make_group
    ):
        group = make_group(user)  # other_user deliberately not a member

        response = auth_client.post(
            GROUP_EXPENSES_URL,
            {
                "group": group.id,
                "paid_by": other_user.id,
                "amount": "100.00",
                "description": "Dinner",
                "date": "2024-01-15",
                "split_type": "equal",
            },
            format="json",
        )

        assert response.status_code == 400


class TestBalanceSheet:
    """The balance sheet's defining property: it sums to zero by construction."""

    def _add_expense(self, group, payer, amount, members):
        expense = GroupExpense.objects.create(
            group=group,
            paid_by=payer,
            amount=Decimal(amount),
            description="Dinner",
            date=date(2024, 1, 15),
            split_type="equal",
        )
        share = Decimal(amount) / len(members)
        for member in members:
            GroupExpenseSplit.objects.create(
                group_expense=expense, user=member, amount=share
            )
        return expense

    def test_the_payer_is_owed_and_the_others_owe(
        self, auth_client, user, other_user, make_group
    ):
        group = make_group(user, members=[other_user])
        self._add_expense(group, user, "100.00", [user, other_user])

        body = auth_client.get(f"{GROUPS_URL}{group.id}/balance/").json()
        balances = balances_by_username(body)

        assert balances["alice"] == Decimal("50.00")
        assert balances["bob"] == Decimal("-50.00")

    def test_balances_sum_to_zero(
        self, auth_client, user, other_user, make_user, make_group
    ):
        third = make_user("carol")
        group = make_group(user, members=[other_user, third])
        self._add_expense(group, user, "90.00", [user, other_user, third])
        self._add_expense(group, other_user, "30.00", [user, other_user, third])

        body = auth_client.get(f"{GROUPS_URL}{group.id}/balance/").json()

        assert sum(balances_by_username(body).values()) == Decimal("0")

    def test_a_recorded_settlement_moves_the_balance(
        self, auth_client, user, other_user, make_group
    ):
        group = make_group(user, members=[other_user])
        self._add_expense(group, user, "100.00", [user, other_user])
        GroupSettlement.objects.create(
            group=group, from_user=other_user, to_user=user, amount=Decimal("50.00")
        )

        body = auth_client.get(f"{GROUPS_URL}{group.id}/balance/").json()
        balances = balances_by_username(body)

        assert balances["alice"] == Decimal("0.00")
        assert balances["bob"] == Decimal("0.00")

    def test_settlements_keep_the_sheet_summing_to_zero(
        self, auth_client, user, other_user, make_group
    ):
        group = make_group(user, members=[other_user])
        self._add_expense(group, user, "100.00", [user, other_user])
        GroupSettlement.objects.create(
            group=group, from_user=other_user, to_user=user, amount=Decimal("20.00")
        )

        body = auth_client.get(f"{GROUPS_URL}{group.id}/balance/").json()

        assert sum(balances_by_username(body).values()) == Decimal("0")

    def test_marking_a_split_paid_does_not_double_count_the_payment(
        self, auth_client, user, other_user, make_group
    ):
        """A split's is_paid flag is deliberately excluded from the balance —
        settlement is tracked by GroupSettlement rows instead."""
        group = make_group(user, members=[other_user])
        expense = self._add_expense(group, user, "100.00", [user, other_user])
        split = GroupExpenseSplit.objects.get(group_expense=expense, user=other_user)
        split.is_paid = True
        split.save()

        body = auth_client.get(f"{GROUPS_URL}{group.id}/balance/").json()
        balances = balances_by_username(body)

        assert balances["alice"] == Decimal("50.00")
        assert balances["bob"] == Decimal("-50.00")

    def test_an_empty_group_reports_zero_for_its_member(
        self, auth_client, user, make_group
    ):
        group = make_group(user)

        body = auth_client.get(f"{GROUPS_URL}{group.id}/balance/").json()

        assert balances_by_username(body) == {"alice": Decimal("0")}
        assert Decimal(str(body["total_group_expenses"])) == Decimal("0")


class TestSuggestedSettlements:
    def _add_expense(self, group, payer, amount, members):
        expense = GroupExpense.objects.create(
            group=group,
            paid_by=payer,
            amount=Decimal(amount),
            description="Dinner",
            date=date(2024, 1, 15),
            split_type="equal",
        )
        share = Decimal(amount) / len(members)
        for member in members:
            GroupExpenseSplit.objects.create(
                group_expense=expense, user=member, amount=share
            )

    def test_suggests_the_debtor_pays_the_creditor(
        self, auth_client, user, other_user, make_group
    ):
        group = make_group(user, members=[other_user])
        self._add_expense(group, user, "100.00", [user, other_user])

        body = auth_client.get(f"{GROUPS_URL}{group.id}/balance/").json()

        assert body["suggested_settlements"] == [
            {
                "from": "bob",
                "from_id": other_user.id,
                "to": "alice",
                "to_id": user.id,
                "amount": Decimal("50.00"),
            }
        ]

    def test_suggested_transfers_cover_the_whole_debt(
        self, auth_client, user, other_user, make_user, make_group
    ):
        third = make_user("carol")
        group = make_group(user, members=[other_user, third])
        self._add_expense(group, user, "90.00", [user, other_user, third])

        body = auth_client.get(f"{GROUPS_URL}{group.id}/balance/").json()
        suggested = body["suggested_settlements"]

        assert sum(Decimal(str(s["amount"])) for s in suggested) == Decimal("60.00")
        assert {s["to"] for s in suggested} == {"alice"}

    def test_a_settled_group_needs_no_transfers(
        self, auth_client, user, other_user, make_group
    ):
        group = make_group(user, members=[other_user])

        body = auth_client.get(f"{GROUPS_URL}{group.id}/balance/").json()

        assert body["suggested_settlements"] == []

    def test_the_returned_balances_are_not_mutated_by_the_suggestion_pass(
        self, auth_client, user, other_user, make_group
    ):
        group = make_group(user, members=[other_user])
        self._add_expense(group, user, "100.00", [user, other_user])

        body = auth_client.get(f"{GROUPS_URL}{group.id}/balance/").json()

        # Creditor's balance still reads 50, not paid down to 0.
        assert balances_by_username(body)["alice"] == Decimal("50.00")


class TestSettlementIsRecordedNotExecuted:
    def test_recording_a_settlement_creates_no_expense_or_money_movement(
        self, user, other_user, make_group
    ):
        from expenses.models import Expense

        group = make_group(user, members=[other_user])

        GroupSettlement.objects.create(
            group=group, from_user=other_user, to_user=user, amount=Decimal("25.00")
        )

        assert Expense.objects.count() == 0


class TestGroupAccessBoundaries:
    def test_a_non_member_cannot_read_the_group(
        self, other_client, user, make_group
    ):
        group = make_group(user)

        assert other_client.get(f"{GROUPS_URL}{group.id}/").status_code == 404

    def test_a_non_member_cannot_read_the_balance_sheet(
        self, other_client, user, make_group
    ):
        group = make_group(user)

        assert other_client.get(f"{GROUPS_URL}{group.id}/balance/").status_code == 404

    def test_the_group_list_shows_only_groups_the_caller_belongs_to(
        self, auth_client, user, other_user, make_group
    ):
        mine = make_group(user, name="Mine")
        make_group(other_user, name="Theirs")

        body = auth_client.get(GROUPS_URL).json()
        results = body["results"] if isinstance(body, dict) else body

        assert [row["id"] for row in results] == [mine.id]

    def test_a_member_added_to_a_group_can_then_see_it(
        self, other_client, user, other_user, make_group
    ):
        group = make_group(user, members=[other_user])

        assert other_client.get(f"{GROUPS_URL}{group.id}/").status_code == 200
