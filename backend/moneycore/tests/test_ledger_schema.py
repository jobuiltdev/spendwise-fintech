"""Ledger schema invariants: accounts, entries, and what must not exist.

Group A (ledger account) and group B (journal/entry constraints).
"""

import pytest
from django.db import IntegrityError, transaction

from moneycore.domain.errors import (
    InvalidLedgerEntryError,
    LedgerCurrencyMismatchError,
)
from moneycore.domain.ledger import (
    EntryDirection,
    JournalStatus,
    LedgerAccountStatus,
    LedgerAccountType,
    MAX_AMOUNT_MINOR,
    is_debit_normal,
)
from moneycore.models import Journal, JournalEntry, LedgerAccount, Wallet
from moneycore.services.ledger import open_ledger_account, open_wallet_ledger_account

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# A. Ledger account
# ---------------------------------------------------------------------------


class TestNoBalanceAnywhereInTheLedger:
    """Posted entries are the only source of truth; nothing caches a total."""

    FORBIDDEN = [
        'balance', 'available_balance', 'ledger_balance', 'spendable_balance',
        'pending_balance', 'reserved_balance', 'held_balance', 'total_balance',
        'opening_balance', 'closing_balance', 'current_balance',
    ]

    @pytest.mark.parametrize('model', [LedgerAccount, Journal, JournalEntry, Wallet])
    def test_no_balance_field_exists(self, model):
        names = {f.name for f in model._meta.get_fields()}

        assert names.isdisjoint(self.FORBIDDEN)

    def test_the_ledger_account_field_set_is_exactly_what_m2_specified(self):
        concrete = {f.name for f in LedgerAccount._meta.get_fields() if f.concrete}

        assert concrete == {
            'id', 'code', 'name', 'account_type', 'currency', 'wallet',
            'status', 'created_at', 'updated_at',
        }

    def test_the_wallet_still_has_no_balance_after_m2(self):
        concrete = {f.name for f in Wallet._meta.get_fields() if f.concrete}

        assert concrete == {
            'id', 'financial_account', 'currency', 'status',
            'created_at', 'updated_at',
        }

    @pytest.mark.parametrize('model', [LedgerAccount, Wallet])
    def test_no_money_mutating_helper_exists(self, model):
        for forbidden in (
            'credit', 'debit', 'adjust_balance', 'set_balance',
            'deposit', 'withdraw', 'increment_balance',
        ):
            assert not hasattr(model, forbidden)

    def test_no_ledger_model_carries_a_float_or_decimal_amount(self):
        from django.db import models as dj

        for model in (LedgerAccount, Journal, JournalEntry):
            for field in model._meta.get_fields():
                assert not isinstance(field, (dj.FloatField, dj.DecimalField)), (
                    f'{model.__name__}.{field.name} is a float/decimal amount'
                )

    def test_the_entry_amount_is_a_64_bit_integer(self):
        from django.db import models as dj

        field = JournalEntry._meta.get_field('amount_minor')

        assert isinstance(field, dj.BigIntegerField)


class TestLedgerAccountCreation:
    def test_it_creates_an_active_account(self, make_ledger_account):
        account = make_ledger_account('internal:a')

        assert account.status == LedgerAccountStatus.ACTIVE
        assert account.currency == 'NGN'

    def test_the_code_is_unique(self, make_ledger_account):
        make_ledger_account('internal:a')

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                LedgerAccount.objects.create(
                    code='internal:a', name='dup',
                    account_type=LedgerAccountType.ASSET, currency='NGN',
                )

    @pytest.mark.parametrize('account_type', sorted(LedgerAccountType.ALL))
    def test_every_conventional_type_is_accepted(self, make_ledger_account, account_type):
        account = make_ledger_account(f'internal:{account_type}', account_type=account_type)

        assert account.account_type == account_type

    def test_an_unknown_account_type_is_refused_by_the_service(self, make_ledger_account):
        with pytest.raises(InvalidLedgerEntryError):
            make_ledger_account('internal:x', account_type='food')

    def test_an_unknown_account_type_is_refused_by_the_database(self):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                LedgerAccount.objects.create(
                    code='internal:x', name='x',
                    account_type='food', currency='NGN',
                )

    def test_the_taxonomy_holds_no_product_categories(self):
        """Food and Transport are expense intelligence, not bookkeeping."""
        assert LedgerAccountType.ALL == {
            'asset', 'liability', 'equity', 'revenue', 'expense'
        }

    @pytest.mark.parametrize('currency', ['ngn', 'NG', 'N1G', ''])
    def test_a_malformed_currency_is_refused_by_the_service(
        self, make_ledger_account, currency
    ):
        with pytest.raises(InvalidLedgerEntryError):
            make_ledger_account('internal:bad', currency=currency)

    def test_a_malformed_currency_is_refused_by_the_database(self):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                LedgerAccount.objects.create(
                    code='internal:bad', name='bad',
                    account_type=LedgerAccountType.ASSET, currency='ngn',
                )

    def test_an_invalid_status_is_refused_by_the_database(self, make_ledger_account):
        account = make_ledger_account('internal:a')

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                LedgerAccount.objects.filter(pk=account.pk).update(status='frozen')


class TestNormalBalanceConvention:
    @pytest.mark.parametrize('account_type', ['asset', 'expense'])
    def test_asset_and_expense_are_debit_normal(self, account_type):
        assert is_debit_normal(account_type)

    @pytest.mark.parametrize('account_type', ['liability', 'equity', 'revenue'])
    def test_liability_equity_revenue_are_credit_normal(self, account_type):
        assert not is_debit_normal(account_type)

    def test_the_two_sets_partition_the_taxonomy(self):
        assert (
            LedgerAccountType.DEBIT_NORMAL | LedgerAccountType.CREDIT_NORMAL
            == LedgerAccountType.ALL
        )
        assert not (
            LedgerAccountType.DEBIT_NORMAL & LedgerAccountType.CREDIT_NORMAL
        )


class TestWalletLedgerMapping:
    def test_a_wallet_maps_to_exactly_one_ledger_account(self, wallet):
        first = open_wallet_ledger_account(
            wallet, account_type=LedgerAccountType.LIABILITY
        )
        second = open_wallet_ledger_account(
            wallet, account_type=LedgerAccountType.ASSET
        )

        assert first.pk == second.pk
        assert LedgerAccount.objects.filter(wallet=wallet).count() == 1

    def test_a_second_account_for_the_same_wallet_is_refused(self, wallet, wallet_account):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                LedgerAccount.objects.create(
                    code='internal:dup-wallet', name='dup',
                    account_type=LedgerAccountType.LIABILITY,
                    currency=wallet.currency, wallet=wallet,
                )

    def test_the_account_shares_the_wallet_currency(self, wallet, wallet_account):
        assert wallet_account.currency == wallet.currency

    def test_a_mismatched_currency_is_refused(self, wallet):
        with pytest.raises(LedgerCurrencyMismatchError):
            open_ledger_account(
                code='internal:mismatch', name='mismatch',
                account_type=LedgerAccountType.LIABILITY,
                currency='USD', wallet=wallet,
            )

    @pytest.mark.parametrize(
        'account_type',
        [LedgerAccountType.LIABILITY, LedgerAccountType.ASSET, LedgerAccountType.EQUITY],
    )
    def test_a_wallet_may_map_to_any_conventional_classification(
        self, ledger_user, account_type
    ):
        """M2 imposes no accounting classification on a customer wallet.

        Which class represents customer funds follows from a custody, provider
        and accounting design that is still open, so the ledger accepts whatever
        the caller states rather than deciding on their behalf.
        """
        from django.contrib.auth.models import User

        from moneycore.services.provisioning import provision_financial_account

        owner = User.objects.create_user(f'owner-{account_type}')
        wallet = provision_financial_account(owner).wallet

        account = open_wallet_ledger_account(wallet, account_type=account_type)

        assert account.account_type == account_type
        assert account.wallet_id == wallet.pk

    def test_an_internal_account_has_no_wallet(self, counterpart_account):
        assert counterpart_account.wallet_id is None

    def test_a_wallet_cannot_be_deleted_while_its_ledger_account_exists(
        self, wallet, wallet_account
    ):
        """PROTECT: financial history must not vanish with a container."""
        from django.db.models import ProtectedError

        with pytest.raises(ProtectedError):
            wallet.delete()


# ---------------------------------------------------------------------------
# B. Journal / entry constraints
# ---------------------------------------------------------------------------


class TestEntryDatabaseConstraints:
    """These run on whichever engine the suite is pointed at."""

    @pytest.fixture
    def draft_journal(self, db):
        return Journal.objects.create(status=JournalStatus.DRAFT, currency='NGN')

    @pytest.mark.parametrize('amount', [0, -1, -100])
    def test_a_non_positive_amount_is_refused_by_the_database(
        self, draft_journal, counterpart_account, amount
    ):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                JournalEntry.objects.create(
                    journal=draft_journal, ledger_account=counterpart_account,
                    direction=EntryDirection.DEBIT, amount_minor=amount, sequence=1,
                )

    def test_an_invalid_direction_is_refused_by_the_database(
        self, draft_journal, counterpart_account
    ):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                JournalEntry.objects.create(
                    journal=draft_journal, ledger_account=counterpart_account,
                    direction='sideways', amount_minor=100, sequence=1,
                )

    def test_a_duplicate_sequence_within_a_journal_is_refused(
        self, draft_journal, counterpart_account
    ):
        JournalEntry.objects.create(
            journal=draft_journal, ledger_account=counterpart_account,
            direction=EntryDirection.DEBIT, amount_minor=100, sequence=1,
        )

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                JournalEntry.objects.create(
                    journal=draft_journal, ledger_account=counterpart_account,
                    direction=EntryDirection.CREDIT, amount_minor=100, sequence=1,
                )

    def test_the_maximum_supported_amount_is_storable(
        self, draft_journal, counterpart_account
    ):
        entry = JournalEntry.objects.create(
            journal=draft_journal, ledger_account=counterpart_account,
            direction=EntryDirection.DEBIT, amount_minor=MAX_AMOUNT_MINOR, sequence=1,
        )
        entry.refresh_from_db()

        assert entry.amount_minor == MAX_AMOUNT_MINOR

    def test_an_entry_inherits_its_account_currency(
        self, draft_journal, counterpart_account
    ):
        entry = JournalEntry.objects.create(
            journal=draft_journal, ledger_account=counterpart_account,
            direction=EntryDirection.DEBIT, amount_minor=100, sequence=1,
        )

        assert entry.currency == counterpart_account.currency

    def test_the_entry_has_no_currency_column_of_its_own(self):
        concrete = {f.name for f in JournalEntry._meta.get_fields() if f.concrete}

        assert 'currency' not in concrete


class TestJournalDatabaseConstraints:
    def test_an_invalid_status_is_refused(self):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Journal.objects.create(status='half-posted', currency='NGN')

    def test_a_malformed_currency_is_refused(self):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Journal.objects.create(status=JournalStatus.DRAFT, currency='ngn')

    def test_a_posted_journal_must_carry_a_posting_time(self):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Journal.objects.create(
                    status=JournalStatus.POSTED, currency='NGN', posted_at=None
                )

    def test_a_draft_must_not_carry_a_posting_time(self):
        from django.utils import timezone

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Journal.objects.create(
                    status=JournalStatus.DRAFT, currency='NGN',
                    posted_at=timezone.now(),
                )


class TestTheDatabaseCannotEnforceBalancing:
    """A cross-row invariant is not something a CHECK constraint can express.

    The database guards single-row facts — positive amounts, valid directions.
    "Debits equal credits across all rows of this journal" is enforced by the
    posting service, and this test states that division of responsibility
    explicitly rather than leaving it implied.
    """

    def test_the_database_will_accept_an_unbalanced_draft(self, counterpart_account):
        journal = Journal.objects.create(status=JournalStatus.DRAFT, currency='NGN')

        JournalEntry.objects.create(
            journal=journal, ledger_account=counterpart_account,
            direction=EntryDirection.DEBIT, amount_minor=100, sequence=1,
        )

        # No constraint fires: the row itself is valid in isolation.
        assert journal.entries.count() == 1

    def test_but_such_a_journal_can_never_become_posted_through_the_service(
        self, counterpart_account, wallet_account
    ):
        from moneycore.domain.errors import LedgerUnbalancedError
        from moneycore.services.ledger import credit, debit, post_journal

        with pytest.raises(LedgerUnbalancedError):
            post_journal(
                currency='NGN',
                entries=[
                    debit(counterpart_account, 100),
                    credit(wallet_account, 99),
                ],
            )
