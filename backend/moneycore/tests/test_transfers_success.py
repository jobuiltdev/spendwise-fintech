"""Transfer success: the accounting M5 owns, on top of M4's orchestration.

The headline proof is that the wallet's posted **normal** balance falls by
exactly the transfer principal — and that this holds identically whether the
wallet's ledger account is debit-normal or credit-normal, because how a
customer wallet is classified is still open (O-13).
"""

import pytest

from moneycore.domain.errors import (
    LedgerUnbalancedError,
    TransactionAlreadyResolvedError,
    TransferAccountingInvalidError,
    TransferCurrencyMismatchError,
    TransferTransactionMismatchError,
)
from moneycore.domain.holds import HoldStatus
from moneycore.domain.ledger import (
    EntryDirection,
    JournalStatus,
    LedgerAccountType,
    is_debit_normal,
)
from moneycore.domain.money import Money
from moneycore.domain.transactions import TransactionDirection, TransactionStatus
from moneycore.domain.transfers import VerifiedBankAccount
from moneycore.models import Journal, JournalEntry, LedgerAccount, Transfer
from moneycore.services.holds import wallet_balance_projection
from moneycore.services.ledger import (
    credit,
    debit,
    open_ledger_account,
    open_wallet_ledger_account,
    posted_balance,
    post_journal,
)
from moneycore.services.provisioning import provision_financial_account
from moneycore.services.transactions import start_processing
from moneycore.services.transfers import (
    prepare_transfer,
    succeed_transfer,
    transfer_principal_entries,
)

pytestmark = pytest.mark.django_db


DESTINATION = VerifiedBankAccount(
    account_number='0123456789',
    bank_code='NG-058',
    bank_name='Example Bank',
    account_name='Ada Okafor',
)


@pytest.fixture
def settlement_account(db):
    """A neutral internal counterpart, supplied by the caller.

    Deliberately generic: it is not a bank, provider, trust or settlement
    account in any production sense. Which internal account a real transfer
    faces depends on the custody and settlement design, which is still open
    (O-29), so this is explicit test setup rather than production config.
    """
    return open_ledger_account(
        code='internal:test-transfer-counterpart:NGN',
        name='Internal transfer counterpart',
        account_type=LedgerAccountType.ASSET,
        currency='NGN',
    )


@pytest.fixture
def processing_transfer(funded_wallet):
    """posted 10 000, hold 7 000, transaction PROCESSING."""
    transfer = prepare_transfer(
        funded_wallet, DESTINATION, 7_000, idempotency_key='success-1'
    )
    # M5 deliberately offers no start_transfer: beginning execution is M6's.
    start_processing(transfer.financial_transaction)
    transfer.refresh_from_db()
    return transfer


class TestTransferSuccess:
    """The critical proof: 10 000 − 7 000 principal → 3 000, atomically."""

    def test_the_transaction_succeeds(self, processing_transfer, settlement_account):
        transfer = succeed_transfer(
            processing_transfer, counterpart_account=settlement_account
        )

        assert transfer.status == TransactionStatus.SUCCEEDED
        assert transfer.financial_transaction.resolved_at is not None

    def test_one_posted_journal_is_linked(
        self, processing_transfer, settlement_account
    ):
        before = Journal.objects.count()

        transfer = succeed_transfer(
            processing_transfer, counterpart_account=settlement_account
        )

        assert Journal.objects.count() == before + 1
        assert transfer.journal is not None
        assert transfer.journal.status == JournalStatus.POSTED

    def test_the_reservation_is_released(
        self, processing_transfer, settlement_account
    ):
        transfer = succeed_transfer(
            processing_transfer, counterpart_account=settlement_account
        )

        transfer.hold.refresh_from_db()
        assert transfer.hold.status == HoldStatus.RELEASED

    def test_the_wallet_balance_picture_is_exact(
        self, processing_transfer, settlement_account
    ):
        succeed_transfer(
            processing_transfer, counterpart_account=settlement_account
        )

        projection = wallet_balance_projection(processing_transfer.wallet)
        assert projection.posted == Money(3_000, 'NGN')
        assert projection.held == Money(0, 'NGN')
        assert projection.available == Money(3_000, 'NGN')

    def test_the_counterpart_moves_by_exactly_the_principal(
        self, processing_transfer, settlement_account
    ):
        """Magnitude exactly the principal, in the balancing direction.

        The *sign* of the counterpart's own normal balance follows from how
        both accounts are classified, and is not something M5 fixes. Here a
        credit-normal wallet is debited (SpendWise owes the customer less) and
        the debit-normal counterpart is credited, which reduces its normal
        balance — cash leaving, in ordinary double-entry terms.
        """
        before = posted_balance(settlement_account)

        succeed_transfer(
            processing_transfer, counterpart_account=settlement_account
        )

        after = posted_balance(settlement_account)
        assert abs(after.minor_units - before.minor_units) == 7_000

    def test_the_counterpart_takes_the_balancing_entry_side(
        self, processing_transfer, settlement_account
    ):
        transfer = succeed_transfer(
            processing_transfer, counterpart_account=settlement_account
        )

        entries = JournalEntry.objects.filter(journal=transfer.journal)
        sides = {e.ledger_account_id: e.direction for e in entries}
        wallet_account = LedgerAccount.objects.get(wallet=transfer.wallet)

        assert sides[wallet_account.pk] != sides[settlement_account.pk]
        assert set(sides.values()) == {
            EntryDirection.DEBIT, EntryDirection.CREDIT
        }

    def test_the_journal_has_exactly_two_entries(
        self, processing_transfer, settlement_account
    ):
        transfer = succeed_transfer(
            processing_transfer, counterpart_account=settlement_account
        )

        assert JournalEntry.objects.filter(journal=transfer.journal).count() == 2

    def test_the_transfer_itself_is_unchanged_historical_intent(
        self, processing_transfer, settlement_account
    ):
        snapshot = (
            processing_transfer.recipient_name,
            processing_transfer.destination_account_number,
            processing_transfer.destination_bank_code,
            processing_transfer.destination_bank_name,
        )

        transfer = succeed_transfer(
            processing_transfer, counterpart_account=settlement_account
        )

        assert (
            transfer.recipient_name,
            transfer.destination_account_number,
            transfer.destination_bank_code,
            transfer.destination_bank_name,
        ) == snapshot

    def test_a_description_and_reference_reach_the_journal(
        self, processing_transfer, settlement_account
    ):
        transfer = succeed_transfer(
            processing_transfer,
            counterpart_account=settlement_account,
            description='Outbound bank transfer',
            reference='ref-7',
        )

        assert transfer.journal.description == 'Outbound bank transfer'
        assert transfer.journal.reference == 'ref-7'

    def test_the_default_description_names_the_transfer(
        self, processing_transfer, settlement_account
    ):
        transfer = succeed_transfer(
            processing_transfer, counterpart_account=settlement_account
        )

        assert str(transfer.pk) in transfer.journal.description

    def test_success_from_unknown_behaves_identically(
        self, processing_transfer, settlement_account
    ):
        from moneycore.services.transfers import mark_transfer_unknown

        mark_transfer_unknown(processing_transfer)

        transfer = succeed_transfer(
            processing_transfer, counterpart_account=settlement_account
        )

        transfer.hold.refresh_from_db()
        assert transfer.status == TransactionStatus.SUCCEEDED
        assert transfer.hold.status == HoldStatus.RELEASED
        assert wallet_balance_projection(
            transfer.wallet
        ).available == Money(3_000, 'NGN')

    def test_it_reuses_m4_orchestration_rather_than_repeating_it(self):
        """No hand-rolled release, no hand-rolled journal, no second atomic block."""
        import inspect

        from moneycore.services import transfers

        source = inspect.getsource(transfers.succeed_transfer)
        assert 'release_hold' not in source
        assert 'post_journal' not in source
        assert 'Journal.objects' not in source
        assert 'succeed_transaction' in source


class TestAccountTypeNeutrality:
    """The same principal reduction, whichever way the wallet is classified.

    O-13 is open, so "transfer out = debit the wallet" is not a rule M5 is
    entitled to write down. What is fixed is the *effect*.
    """

    def _wallet_with(self, username, account_type):
        from django.contrib.auth.models import User

        wallet = provision_financial_account(
            User.objects.create_user(username)
        ).wallet
        account = open_wallet_ledger_account(wallet, account_type=account_type)
        return wallet, account

    def _fund(self, wallet_account, counterpart, amount=10_000):
        """Fund the wallet in its own normal direction, whichever that is."""
        if is_debit_normal(wallet_account.account_type):
            entries = [debit(wallet_account, amount), credit(counterpart, amount)]
        else:
            entries = [debit(counterpart, amount), credit(wallet_account, amount)]
        post_journal(currency='NGN', entries=entries, description='funding')

    @pytest.fixture
    def funding_source(self, db):
        return open_ledger_account(
            code='internal:neutrality-funding:NGN',
            name='Funding source',
            account_type=LedgerAccountType.EQUITY,
            currency='NGN',
        )

    @pytest.mark.parametrize(
        'account_type',
        [LedgerAccountType.ASSET, LedgerAccountType.LIABILITY],
        ids=['debit-normal-wallet', 'credit-normal-wallet'],
    )
    def test_the_wallet_normal_balance_falls_by_exactly_the_principal(
        self, account_type, funding_source, settlement_account
    ):
        wallet, wallet_account = self._wallet_with(
            f'neutral-{account_type}', account_type
        )
        self._fund(wallet_account, funding_source)

        transfer = prepare_transfer(
            wallet, DESTINATION, 7_000, idempotency_key=f'neutral-{account_type}'
        )
        start_processing(transfer.financial_transaction)
        transfer.refresh_from_db()

        succeed_transfer(transfer, counterpart_account=settlement_account)

        assert posted_balance(wallet_account) == Money(3_000, 'NGN')

    @pytest.mark.parametrize(
        'account_type',
        [LedgerAccountType.ASSET, LedgerAccountType.LIABILITY],
        ids=['debit-normal-wallet', 'credit-normal-wallet'],
    )
    def test_the_projection_matches_in_both_classifications(
        self, account_type, funding_source, settlement_account
    ):
        wallet, wallet_account = self._wallet_with(
            f'neutral-proj-{account_type}', account_type
        )
        self._fund(wallet_account, funding_source)

        transfer = prepare_transfer(
            wallet, DESTINATION, 7_000, idempotency_key=f'neutral-proj-{account_type}'
        )
        start_processing(transfer.financial_transaction)
        transfer.refresh_from_db()

        succeed_transfer(transfer, counterpart_account=settlement_account)

        projection = wallet_balance_projection(wallet)
        assert projection.posted == Money(3_000, 'NGN')
        assert projection.held == Money(0, 'NGN')
        assert projection.available == Money(3_000, 'NGN')

    def test_a_debit_normal_wallet_is_reduced_by_a_credit(
        self, funding_source, settlement_account
    ):
        """The entry side is derived, and the derivation is the opposite here."""
        wallet, wallet_account = self._wallet_with(
            'side-debit-normal', LedgerAccountType.ASSET
        )
        self._fund(wallet_account, funding_source)
        transfer = prepare_transfer(
            wallet, DESTINATION, 7_000, idempotency_key='side-debit'
        )

        entries = transfer_principal_entries(transfer, settlement_account)
        wallet_entry = next(
            e for e in entries if e.ledger_account.pk == wallet_account.pk
        )

        assert wallet_entry.direction == EntryDirection.CREDIT

    def test_a_credit_normal_wallet_is_reduced_by_a_debit(
        self, funding_source, settlement_account
    ):
        wallet, wallet_account = self._wallet_with(
            'side-credit-normal', LedgerAccountType.LIABILITY
        )
        self._fund(wallet_account, funding_source)
        transfer = prepare_transfer(
            wallet, DESTINATION, 7_000, idempotency_key='side-credit'
        )

        entries = transfer_principal_entries(transfer, settlement_account)
        wallet_entry = next(
            e for e in entries if e.ledger_account.pk == wallet_account.pk
        )

        assert wallet_entry.direction == EntryDirection.DEBIT

    def test_the_service_hardcodes_no_direction_for_the_wallet(self):
        """A grep-proof that the rule really is derived."""
        import inspect

        from moneycore.services import transfers

        source = inspect.getsource(transfers.transfer_principal_entries)
        assert 'is_debit_normal' in source

    def test_no_default_wallet_classification_was_introduced(self):
        """O-13 stays open: M5 takes no position either."""
        from moneycore.services import transfers

        for forbidden in (
            'WALLET_LEDGER_ACCOUNT_TYPE', 'DEFAULT_WALLET_ACCOUNT_TYPE',
            'WALLET_ACCOUNT_TYPE',
        ):
            assert not hasattr(transfers, forbidden)


class TestPrincipalAccountingInvariant:
    """The wallet effect must equal the principal — no more, no less."""

    def test_the_derived_entries_balance(
        self, processing_transfer, settlement_account
    ):
        entries = transfer_principal_entries(
            processing_transfer, settlement_account
        )

        debits = sum(
            e.amount_minor for e in entries if e.direction == EntryDirection.DEBIT
        )
        credits = sum(
            e.amount_minor for e in entries if e.direction == EntryDirection.CREDIT
        )
        assert debits == credits == 7_000

    def test_both_entries_carry_the_principal(
        self, processing_transfer, settlement_account
    ):
        entries = transfer_principal_entries(
            processing_transfer, settlement_account
        )

        assert [e.amount_minor for e in entries] == [7_000, 7_000]

    def test_the_wallet_delta_is_exactly_minus_the_principal(
        self, processing_transfer, settlement_account
    ):
        from moneycore.services.ledger import _wallet_balance_deltas

        entries = transfer_principal_entries(
            processing_transfer, settlement_account
        )

        deltas = _wallet_balance_deltas(entries)
        assert deltas == {processing_transfer.wallet.pk: -7_000}

    def test_a_plan_moving_the_wrong_amount_is_refused(
        self, processing_transfer, settlement_account, monkeypatch
    ):
        """Closes the M4 limitation: success cannot post unrelated accounting."""
        from moneycore.services import transfers as service

        def wrong_amount(transfer, counterpart_account):
            wallet_account = LedgerAccount.objects.get(wallet=transfer.wallet)
            return [debit(wallet_account, 1_000), credit(counterpart_account, 1_000)]

        monkeypatch.setattr(service, 'transfer_principal_entries', wrong_amount)

        with pytest.raises(TransferAccountingInvalidError):
            succeed_transfer(
                processing_transfer, counterpart_account=settlement_account
            )

    def test_a_plan_moving_the_wallet_the_wrong_way_is_refused(
        self, processing_transfer, settlement_account, monkeypatch
    ):
        from moneycore.services import transfers as service

        def backwards(transfer, counterpart_account):
            wallet_account = LedgerAccount.objects.get(wallet=transfer.wallet)
            # Credit-normal wallet: crediting it *increases* the balance.
            return [
                credit(wallet_account, 7_000),
                debit(counterpart_account, 7_000),
            ]

        monkeypatch.setattr(service, 'transfer_principal_entries', backwards)

        with pytest.raises(TransferAccountingInvalidError):
            succeed_transfer(
                processing_transfer, counterpart_account=settlement_account
            )

    def test_a_plan_touching_another_wallet_is_refused(
        self, processing_transfer, settlement_account, monkeypatch
    ):
        """A bank transfer leaves the wallet system; it does not move a neighbour."""
        from django.contrib.auth.models import User

        from moneycore.services import transfers as service

        other_wallet = provision_financial_account(
            User.objects.create_user('bystander')
        ).wallet
        other_account = open_wallet_ledger_account(
            other_wallet, account_type=LedgerAccountType.LIABILITY
        )

        def steals(transfer, counterpart_account):
            wallet_account = LedgerAccount.objects.get(wallet=transfer.wallet)
            return [debit(wallet_account, 7_000), credit(other_account, 7_000)]

        monkeypatch.setattr(service, 'transfer_principal_entries', steals)

        with pytest.raises(TransferAccountingInvalidError):
            succeed_transfer(
                processing_transfer, counterpart_account=settlement_account
            )

    def test_the_invariant_error_reports_the_numbers(
        self, processing_transfer, settlement_account, monkeypatch
    ):
        from moneycore.services import transfers as service

        def wrong_amount(transfer, counterpart_account):
            wallet_account = LedgerAccount.objects.get(wallet=transfer.wallet)
            return [debit(wallet_account, 1_000), credit(counterpart_account, 1_000)]

        monkeypatch.setattr(service, 'transfer_principal_entries', wrong_amount)

        with pytest.raises(TransferAccountingInvalidError) as raised:
            succeed_transfer(
                processing_transfer, counterpart_account=settlement_account
            )

        details = raised.value.details
        assert details['principal_minor'] == 7_000
        assert details['wallet_delta_minor'] == -1_000
        assert details['expected_delta_minor'] == -7_000


class TestCounterpartValidation:
    def test_the_sending_wallet_account_cannot_be_its_own_counterpart(
        self, processing_transfer
    ):
        wallet_account = LedgerAccount.objects.get(
            wallet=processing_transfer.wallet
        )

        with pytest.raises(TransferAccountingInvalidError):
            succeed_transfer(
                processing_transfer, counterpart_account=wallet_account
            )

    def test_another_customer_wallet_cannot_be_the_counterpart(
        self, processing_transfer
    ):
        """Internal wallet-to-wallet is a different product operation."""
        from django.contrib.auth.models import User

        other_wallet = provision_financial_account(
            User.objects.create_user('counterpart-wallet')
        ).wallet
        other_account = open_wallet_ledger_account(
            other_wallet, account_type=LedgerAccountType.LIABILITY
        )

        with pytest.raises(TransferAccountingInvalidError):
            succeed_transfer(
                processing_transfer, counterpart_account=other_account
            )

    def test_a_counterpart_in_another_currency_is_refused(
        self, processing_transfer
    ):
        usd = open_ledger_account(
            code='internal:usd-counterpart',
            name='USD counterpart',
            account_type=LedgerAccountType.ASSET,
            currency='USD',
        )

        with pytest.raises(TransferCurrencyMismatchError):
            succeed_transfer(processing_transfer, counterpart_account=usd)

    def test_a_closed_counterpart_is_refused_by_the_ledger(
        self, processing_transfer, settlement_account
    ):
        from moneycore.domain.errors import LedgerAccountClosedError
        from moneycore.domain.ledger import LedgerAccountStatus

        LedgerAccount.objects.filter(pk=settlement_account.pk).update(
            status=LedgerAccountStatus.CLOSED
        )
        settlement_account.refresh_from_db()

        with pytest.raises(LedgerAccountClosedError):
            succeed_transfer(
                processing_transfer, counterpart_account=settlement_account
            )

    def test_no_settlement_account_is_created_or_assumed(self):
        """M5 invents no settlement taxonomy — the caller supplies one."""
        from moneycore.services import transfers

        for forbidden in (
            'settlement_account', 'SETTLEMENT_ACCOUNT', 'get_settlement_account',
            'default_counterpart', 'DEFAULT_COUNTERPART', 'bank_account',
        ):
            assert not hasattr(transfers, forbidden), f'{forbidden} exists'

    def test_no_ledger_account_is_seeded_by_the_migration(self, db):
        """Nothing in M5 creates a production account behind the scenes."""
        assert LedgerAccount.objects.count() == 0


class TestTransactionRelationshipValidation:
    def test_the_transfer_and_transaction_cannot_disagree_about_the_money(
        self, processing_transfer
    ):
        """Stronger than validation: the disagreement is unrepresentable.

        Amount, currency and wallet are read-through properties, not stored
        columns, so there is only ever one copy of each fact. A transfer whose
        principal differs from its transaction's amount is not a state this
        model can be put into — including through raw ORM writes.
        """
        from moneycore.models import FinancialTransaction

        FinancialTransaction.objects.filter(
            pk=processing_transfer.financial_transaction_id
        ).update(amount_minor=5_000)
        processing_transfer.refresh_from_db()

        assert processing_transfer.amount_minor == 5_000
        assert processing_transfer.amount_minor == (
            processing_transfer.financial_transaction.amount_minor
        )

    def test_the_principal_check_follows_the_transaction(
        self, processing_transfer, settlement_account
    ):
        """And the accounting invariant tracks it, so it cannot be side-stepped."""
        from moneycore.models import FinancialTransaction

        FinancialTransaction.objects.filter(
            pk=processing_transfer.financial_transaction_id
        ).update(amount_minor=5_000)
        processing_transfer.refresh_from_db()

        succeed_transfer(
            processing_transfer, counterpart_account=settlement_account
        )

        assert wallet_balance_projection(
            processing_transfer.wallet
        ).posted == Money(5_000, 'NGN')

    def test_an_incoming_transaction_cannot_back_a_transfer(
        self, funded_wallet, settlement_account
    ):
        from moneycore.models import FinancialTransaction
        from moneycore.services.transactions import create_transaction

        txn = create_transaction(
            funded_wallet, TransactionDirection.INCOMING, 7_000,
            idempotency_key='incoming-probe',
        )
        transfer = Transfer.objects.create(
            financial_transaction=txn,
            recipient_name='Ada Okafor',
            destination_account_number='0123456789',
            destination_bank_code='NG-058',
            destination_bank_name='Example Bank',
        )

        with pytest.raises(TransferTransactionMismatchError):
            succeed_transfer(transfer, counterpart_account=settlement_account)

    def test_a_created_transfer_cannot_succeed_directly(
        self, funded_wallet, settlement_account
    ):
        from moneycore.domain.errors import InvalidTransactionTransitionError

        transfer = prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key='not-started'
        )

        with pytest.raises(InvalidTransactionTransitionError):
            succeed_transfer(transfer, counterpart_account=settlement_account)

    def test_a_resolved_transfer_cannot_succeed_again(
        self, processing_transfer, settlement_account
    ):
        succeed_transfer(
            processing_transfer, counterpart_account=settlement_account
        )

        with pytest.raises(TransactionAlreadyResolvedError):
            succeed_transfer(
                processing_transfer, counterpart_account=settlement_account
            )

    def test_a_second_success_posts_no_second_journal(
        self, processing_transfer, settlement_account
    ):
        succeed_transfer(
            processing_transfer, counterpart_account=settlement_account
        )
        count = Journal.objects.count()

        with pytest.raises(TransactionAlreadyResolvedError):
            succeed_transfer(
                processing_transfer, counterpart_account=settlement_account
            )

        assert Journal.objects.count() == count


class TestSuccessRollback:
    """All or nothing, inherited from M4 and proved again through M5."""

    def _break_posting(self, monkeypatch):
        from moneycore.services import transfers as service

        def unbalanced(transfer, counterpart_account):
            wallet_account = LedgerAccount.objects.get(wallet=transfer.wallet)
            # Passes M5's principal check, fails the ledger's balance check.
            return [
                debit(wallet_account, 7_000),
                credit(counterpart_account, 6_999),
            ]

        monkeypatch.setattr(service, 'transfer_principal_entries', unbalanced)

    def test_an_unbalanced_posting_is_refused(
        self, processing_transfer, settlement_account, monkeypatch
    ):
        self._break_posting(monkeypatch)

        with pytest.raises(LedgerUnbalancedError):
            succeed_transfer(
                processing_transfer, counterpart_account=settlement_account
            )

    def test_the_transaction_stays_in_its_prior_state(
        self, processing_transfer, settlement_account, monkeypatch
    ):
        self._break_posting(monkeypatch)

        with pytest.raises(LedgerUnbalancedError):
            succeed_transfer(
                processing_transfer, counterpart_account=settlement_account
            )

        processing_transfer.financial_transaction.refresh_from_db()
        txn = processing_transfer.financial_transaction
        assert txn.status == TransactionStatus.PROCESSING
        assert txn.journal_id is None
        assert txn.resolved_at is None

    def test_the_reservation_stays_active(
        self, processing_transfer, settlement_account, monkeypatch
    ):
        self._break_posting(monkeypatch)

        with pytest.raises(LedgerUnbalancedError):
            succeed_transfer(
                processing_transfer, counterpart_account=settlement_account
            )

        processing_transfer.hold.refresh_from_db()
        assert processing_transfer.hold.status == HoldStatus.ACTIVE
        assert processing_transfer.hold.released_at is None

    def test_no_journal_survives(
        self, processing_transfer, settlement_account, monkeypatch
    ):
        before = (Journal.objects.count(), JournalEntry.objects.count())
        self._break_posting(monkeypatch)

        with pytest.raises(LedgerUnbalancedError):
            succeed_transfer(
                processing_transfer, counterpart_account=settlement_account
            )

        assert (Journal.objects.count(), JournalEntry.objects.count()) == before

    def test_the_balances_are_unchanged(
        self, processing_transfer, settlement_account, monkeypatch
    ):
        before = wallet_balance_projection(processing_transfer.wallet)
        self._break_posting(monkeypatch)

        with pytest.raises(LedgerUnbalancedError):
            succeed_transfer(
                processing_transfer, counterpart_account=settlement_account
            )

        assert wallet_balance_projection(processing_transfer.wallet) == before

    def test_the_transfer_is_unchanged(
        self, processing_transfer, settlement_account, monkeypatch
    ):
        self._break_posting(monkeypatch)

        with pytest.raises(LedgerUnbalancedError):
            succeed_transfer(
                processing_transfer, counterpart_account=settlement_account
            )

        processing_transfer.refresh_from_db()
        assert processing_transfer.destination_account_number == '0123456789'
        assert processing_transfer.status == TransactionStatus.PROCESSING

    def test_an_accounting_rejection_also_leaves_everything_intact(
        self, processing_transfer, settlement_account, monkeypatch
    ):
        """M5's own check rejects before M4 is ever entered."""
        from moneycore.services import transfers as service

        def wrong_amount(transfer, counterpart_account):
            wallet_account = LedgerAccount.objects.get(wallet=transfer.wallet)
            return [debit(wallet_account, 1_000), credit(counterpart_account, 1_000)]

        before = Journal.objects.count()
        monkeypatch.setattr(service, 'transfer_principal_entries', wrong_amount)

        with pytest.raises(TransferAccountingInvalidError):
            succeed_transfer(
                processing_transfer, counterpart_account=settlement_account
            )

        processing_transfer.hold.refresh_from_db()
        processing_transfer.financial_transaction.refresh_from_db()
        assert processing_transfer.hold.status == HoldStatus.ACTIVE
        assert (
            processing_transfer.financial_transaction.status
            == TransactionStatus.PROCESSING
        )
        assert Journal.objects.count() == before

    def test_the_transfer_can_still_succeed_after_a_failed_attempt(
        self, processing_transfer, settlement_account, monkeypatch
    ):
        self._break_posting(monkeypatch)
        with pytest.raises(LedgerUnbalancedError):
            succeed_transfer(
                processing_transfer, counterpart_account=settlement_account
            )
        monkeypatch.undo()

        transfer = succeed_transfer(
            processing_transfer, counterpart_account=settlement_account
        )

        assert transfer.status == TransactionStatus.SUCCEEDED
        assert wallet_balance_projection(
            transfer.wallet
        ).available == Money(3_000, 'NGN')
