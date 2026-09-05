"""Preparing a transfer: intent, transaction and reservation, all or none.

Preparation is the point at which a transfer is **accepted for execution** —
not the point at which a customer starts typing. Everything before acceptance
produces no financial transaction at all, which is what keeps the locked §8.5
"no cancellation" rule workable without a draft.
"""

import pytest

from moneycore.domain.errors import (
    InsufficientAvailableBalanceError,
    InvalidTransferAmountError,
    TransferCurrencyMismatchError,
    TransferIdempotencyConflictError,
    TransferTransactionMismatchError,
    WalletLedgerAccountNotFoundError,
)
from moneycore.domain.holds import HoldStatus
from moneycore.domain.money import Money
from moneycore.domain.transactions import TransactionDirection, TransactionStatus
from moneycore.domain.transfers import VerifiedBankAccount
from moneycore.models import FinancialTransaction, FundsHold, Journal, Transfer
from moneycore.services.holds import create_hold, wallet_balance_projection
from moneycore.services.transfers import prepare_transfer

pytestmark = pytest.mark.django_db


DESTINATION = VerifiedBankAccount(
    account_number='0123456789',
    bank_code='NG-058',
    bank_name='Example Bank',
    account_name='Ada Okafor',
)
OTHER_DESTINATION = VerifiedBankAccount(
    account_number='9876543210',
    bank_code='NG-011',
    bank_name='Other Bank',
    account_name='Bola Adeyemi',
)


def prepare(wallet, amount=7_000, key='prepare-1', destination=DESTINATION, **kwargs):
    return prepare_transfer(
        wallet, destination, amount, idempotency_key=key, **kwargs
    )


class TestSuccessfulPreparation:
    """posted 10 000, available 10 000, prepare 7 000."""

    def test_one_transfer_is_created(self, funded_wallet):
        transfer = prepare(funded_wallet)

        assert Transfer.objects.count() == 1
        assert transfer.pk is not None

    def test_one_financial_transaction_backs_it(self, funded_wallet):
        transfer = prepare(funded_wallet)

        assert FinancialTransaction.objects.count() == 1
        assert transfer.financial_transaction.pk is not None

    def test_the_transaction_is_created_and_outgoing(self, funded_wallet):
        transfer = prepare(funded_wallet)
        txn = transfer.financial_transaction

        assert txn.status == TransactionStatus.CREATED
        assert txn.direction == TransactionDirection.OUTGOING

    def test_one_active_hold_reserves_the_principal(self, funded_wallet):
        transfer = prepare(funded_wallet)

        assert FundsHold.objects.count() == 1
        assert transfer.hold.status == HoldStatus.ACTIVE
        assert transfer.hold.amount_minor == 7_000

    def test_the_hold_is_attached_to_the_transaction(self, funded_wallet):
        transfer = prepare(funded_wallet)

        assert transfer.financial_transaction.hold_id == transfer.hold.pk

    def test_the_balance_picture_is_exact(self, funded_wallet):
        prepare(funded_wallet)

        projection = wallet_balance_projection(funded_wallet)
        assert projection.posted == Money(10_000, 'NGN')
        assert projection.held == Money(7_000, 'NGN')
        assert projection.available == Money(3_000, 'NGN')

    def test_nothing_is_posted(self, funded_wallet):
        before = Journal.objects.count()

        prepare(funded_wallet)

        assert Journal.objects.count() == before

    def test_the_transfer_and_transaction_agree_on_the_money(self, funded_wallet):
        transfer = prepare(funded_wallet)

        assert transfer.amount_minor == transfer.financial_transaction.amount_minor
        assert transfer.currency == transfer.financial_transaction.currency
        assert transfer.wallet.pk == transfer.financial_transaction.wallet_id

    def test_the_destination_snapshot_is_stored(self, funded_wallet):
        transfer = prepare(funded_wallet)

        assert transfer.recipient_name == 'Ada Okafor'
        assert transfer.destination_account_number == '0123456789'
        assert transfer.destination_bank_code == 'NG-058'
        assert transfer.destination_bank_name == 'Example Bank'

    def test_a_narration_is_stored_and_normalised(self, funded_wallet):
        transfer = prepare(funded_wallet, narration='  Rent for June  ')

        assert transfer.narration == 'Rent for June'

    def test_narration_is_optional(self, funded_wallet):
        assert prepare(funded_wallet).narration == ''

    def test_the_reservation_records_why_it_exists(self, funded_wallet):
        transfer = prepare(funded_wallet, key='reason-probe')

        assert transfer.hold.reason == 'transfer:reason-probe'

    def test_the_hold_has_no_expiry(self, funded_wallet):
        """O-17 is open: no timeout window is known, so none is guessed."""
        assert prepare(funded_wallet).hold.expires_at is None

    def test_the_reservation_is_principal_only(self, funded_wallet):
        """Fees do not exist yet (O-25), so nothing extra is reserved."""
        transfer = prepare(funded_wallet)

        assert transfer.hold.amount_minor == transfer.amount_minor

    def test_exactly_the_whole_balance_can_be_sent(self, funded_wallet):
        transfer = prepare(funded_wallet, amount=10_000)

        projection = wallet_balance_projection(funded_wallet)
        assert transfer.amount_minor == 10_000
        assert projection.available == Money(0, 'NGN')


class TestAmountAndCurrency:
    @pytest.mark.parametrize('amount', [0, -1, -7_000])
    def test_a_non_positive_amount_is_refused(self, funded_wallet, amount):
        with pytest.raises(InvalidTransferAmountError):
            prepare(funded_wallet, amount=amount)

    @pytest.mark.parametrize('amount', [70.0, '7000', True, None])
    def test_a_non_integer_amount_is_refused(self, funded_wallet, amount):
        with pytest.raises(InvalidTransferAmountError):
            prepare(funded_wallet, amount=amount)

    def test_a_decimal_amount_is_refused(self, funded_wallet):
        from decimal import Decimal

        with pytest.raises(InvalidTransferAmountError):
            prepare(funded_wallet, amount=Decimal('70.00'))

    def test_a_refused_amount_creates_nothing(self, funded_wallet):
        with pytest.raises(InvalidTransferAmountError):
            prepare(funded_wallet, amount=0)

        assert Transfer.objects.count() == 0
        assert FinancialTransaction.objects.count() == 0
        assert FundsHold.objects.count() == 0

    def test_a_foreign_currency_is_refused(self, funded_wallet):
        """No FX in M5: balancing across currencies needs a rate M5 lacks."""
        with pytest.raises(TransferCurrencyMismatchError):
            prepare(funded_wallet, currency='USD')

    def test_the_matching_currency_is_accepted_explicitly(self, funded_wallet):
        transfer = prepare(funded_wallet, currency='NGN')

        assert transfer.currency == 'NGN'


class TestDestinationIsRequired:
    @pytest.mark.parametrize(
        'destination',
        [
            None,
            '0123456789',
            {'account_number': '0123456789', 'bank_code': 'NG-058'},
        ],
    )
    def test_an_unverified_destination_is_refused(self, funded_wallet, destination):
        """The type is the assertion. A dict has asserted nothing."""
        with pytest.raises(TransferTransactionMismatchError):
            prepare(funded_wallet, destination=destination)

    def test_a_refused_destination_creates_nothing(self, funded_wallet):
        with pytest.raises(TransferTransactionMismatchError):
            prepare(funded_wallet, destination=None)

        assert Transfer.objects.count() == 0
        assert FinancialTransaction.objects.count() == 0


class TestWalletMustBeUsable:
    def test_an_unmapped_wallet_cannot_send(self, wallet):
        """Inherits M2's rule: no ledger relationship is not zero funds."""
        with pytest.raises(WalletLedgerAccountNotFoundError):
            prepare(wallet, amount=1_000)

    def test_an_unmapped_wallet_creates_no_debris(self, wallet):
        with pytest.raises(WalletLedgerAccountNotFoundError):
            prepare(wallet, amount=1_000)

        assert Transfer.objects.count() == 0
        assert FinancialTransaction.objects.count() == 0
        assert FundsHold.objects.count() == 0


class TestInsufficientBalance:
    """posted 10 000, existing hold 4 000, available 6 000, request 7 000."""

    @pytest.fixture
    def partly_reserved(self, funded_wallet):
        create_hold(funded_wallet, 4_000)
        return funded_wallet

    def test_the_transfer_is_refused(self, partly_reserved):
        with pytest.raises(InsufficientAvailableBalanceError):
            prepare(partly_reserved)

    def test_no_transfer_survives(self, partly_reserved):
        with pytest.raises(InsufficientAvailableBalanceError):
            prepare(partly_reserved)

        assert Transfer.objects.count() == 0

    def test_no_transaction_debris_survives(self, partly_reserved):
        """The transaction is created before the hold, so this proves rollback."""
        with pytest.raises(InsufficientAvailableBalanceError):
            prepare(partly_reserved)

        assert FinancialTransaction.objects.count() == 0

    def test_no_new_hold_survives(self, partly_reserved):
        with pytest.raises(InsufficientAvailableBalanceError):
            prepare(partly_reserved)

        assert FundsHold.objects.count() == 1

    def test_the_balance_picture_is_unchanged(self, partly_reserved):
        before = wallet_balance_projection(partly_reserved)

        with pytest.raises(InsufficientAvailableBalanceError):
            prepare(partly_reserved)

        assert wallet_balance_projection(partly_reserved) == before

    def test_what_does_fit_is_still_accepted_afterwards(self, partly_reserved):
        with pytest.raises(InsufficientAvailableBalanceError):
            prepare(partly_reserved)

        transfer = prepare(partly_reserved, amount=6_000, key='fits')

        assert transfer.amount_minor == 6_000
        assert wallet_balance_projection(
            partly_reserved
        ).available == Money(0, 'NGN')


class TestPreparationRollback:
    """All or none — proved by breaking each step in turn."""

    def test_a_failure_creating_the_transfer_rolls_everything_back(
        self, funded_wallet, monkeypatch
    ):
        """Force a failure after the transaction and hold exist."""
        from moneycore.services import transfers as service

        def explode(*args, **kwargs):
            raise RuntimeError('transfer save failed')

        monkeypatch.setattr(service.Transfer.objects, 'create', explode)

        with pytest.raises(RuntimeError):
            prepare(funded_wallet)

        assert Transfer.objects.count() == 0
        assert FinancialTransaction.objects.count() == 0
        assert FundsHold.objects.count() == 0

    def test_the_balances_are_untouched_after_that_rollback(
        self, funded_wallet, monkeypatch
    ):
        from moneycore.services import transfers as service

        before = wallet_balance_projection(funded_wallet)

        def explode(*args, **kwargs):
            raise RuntimeError('transfer save failed')

        monkeypatch.setattr(service.Transfer.objects, 'create', explode)

        with pytest.raises(RuntimeError):
            prepare(funded_wallet)

        assert wallet_balance_projection(funded_wallet) == before
        assert wallet_balance_projection(
            funded_wallet
        ).available == Money(10_000, 'NGN')

    def test_a_failure_attaching_the_hold_rolls_everything_back(
        self, funded_wallet, monkeypatch
    ):
        from moneycore.services import transfers as service

        def explode(*args, **kwargs):
            raise RuntimeError('attach failed')

        monkeypatch.setattr(service, 'attach_hold', explode)

        with pytest.raises(RuntimeError):
            prepare(funded_wallet)

        assert Transfer.objects.count() == 0
        assert FinancialTransaction.objects.count() == 0
        assert FundsHold.objects.count() == 0

    def test_preparation_can_still_succeed_after_a_rollback(
        self, funded_wallet, monkeypatch
    ):
        from moneycore.services import transfers as service

        def explode(*args, **kwargs):
            raise RuntimeError('transfer save failed')

        monkeypatch.setattr(service.Transfer.objects, 'create', explode)
        with pytest.raises(RuntimeError):
            prepare(funded_wallet)
        monkeypatch.undo()

        transfer = prepare(funded_wallet)

        assert transfer.pk is not None
        assert wallet_balance_projection(
            funded_wallet
        ).available == Money(3_000, 'NGN')

    def test_there_is_never_a_transaction_without_its_reservation(
        self, funded_wallet
    ):
        """The invariant the rollback tests exist to protect."""
        prepare(funded_wallet)

        for txn in FinancialTransaction.objects.all():
            assert txn.hold_id is not None
            assert hasattr(txn, 'transfer')


class TestIdempotentPreparation:
    def test_the_same_request_returns_the_same_transfer(self, funded_wallet):
        first = prepare(funded_wallet, key='idem-1')
        second = prepare(funded_wallet, key='idem-1')

        assert second.pk == first.pk

    def test_it_creates_no_second_transaction_or_hold(self, funded_wallet):
        prepare(funded_wallet, key='idem-2')
        prepare(funded_wallet, key='idem-2')
        prepare(funded_wallet, key='idem-2')

        assert Transfer.objects.count() == 1
        assert FinancialTransaction.objects.count() == 1
        assert FundsHold.objects.count() == 1

    def test_funds_are_reserved_exactly_once(self, funded_wallet):
        prepare(funded_wallet, key='idem-3')
        prepare(funded_wallet, key='idem-3')

        projection = wallet_balance_projection(funded_wallet)
        assert projection.held == Money(7_000, 'NGN')
        assert projection.available == Money(3_000, 'NGN')

    def test_an_equal_destination_object_is_the_same_intent(self, funded_wallet):
        """Value equality, not identity."""
        twin = VerifiedBankAccount(
            account_number='0123456789',
            bank_code='NG-058',
            bank_name='Example Bank',
            account_name='Ada Okafor',
        )

        first = prepare(funded_wallet, key='idem-4')
        second = prepare(funded_wallet, key='idem-4', destination=twin)

        assert second.pk == first.pk

    def test_a_matching_narration_is_the_same_intent(self, funded_wallet):
        first = prepare(funded_wallet, key='idem-5', narration='Rent')
        second = prepare(funded_wallet, key='idem-5', narration='Rent')

        assert second.pk == first.pk

    def test_different_keys_are_different_transfers(self, funded_wallet):
        first = prepare(funded_wallet, amount=3_000, key='key-a')
        second = prepare(funded_wallet, amount=3_000, key='key-b')

        assert first.pk != second.pk
        assert wallet_balance_projection(
            funded_wallet
        ).held == Money(6_000, 'NGN')


class TestDestinationAwareIdempotency:
    """The critical M5 invariant.

    M4's key identifies wallet, direction, amount, currency and key. A
    transfer's intent also includes **where the money is going** — so the same
    key with the same amount and a different destination must never quietly
    return the earlier transfer, because that would send money to the wrong
    person.
    """

    def test_a_different_account_number_conflicts(self, funded_wallet):
        prepare(funded_wallet, key='dest-1')

        elsewhere = VerifiedBankAccount(
            account_number='9876543210',
            bank_code='NG-058',
            bank_name='Example Bank',
            account_name='Ada Okafor',
        )
        with pytest.raises(TransferIdempotencyConflictError):
            prepare(funded_wallet, key='dest-1', destination=elsewhere)

    def test_a_different_bank_conflicts(self, funded_wallet):
        prepare(funded_wallet, key='dest-2')

        other_bank = VerifiedBankAccount(
            account_number='0123456789',
            bank_code='NG-011',
            bank_name='Other Bank',
            account_name='Ada Okafor',
        )
        with pytest.raises(TransferIdempotencyConflictError):
            prepare(funded_wallet, key='dest-2', destination=other_bank)

    def test_a_different_bank_display_name_conflicts(self, funded_wallet):
        """The snapshot is intent. It is never quietly refreshed on replay."""
        prepare(funded_wallet, key='dest-3')

        renamed = VerifiedBankAccount(
            account_number='0123456789',
            bank_code='NG-058',
            bank_name='Example Bank Plc',
            account_name='Ada Okafor',
        )
        with pytest.raises(TransferIdempotencyConflictError):
            prepare(funded_wallet, key='dest-3', destination=renamed)

    def test_a_different_recipient_name_conflicts(self, funded_wallet):
        prepare(funded_wallet, key='dest-4')

        renamed = VerifiedBankAccount(
            account_number='0123456789',
            bank_code='NG-058',
            bank_name='Example Bank',
            account_name='Someone Else',
        )
        with pytest.raises(TransferIdempotencyConflictError):
            prepare(funded_wallet, key='dest-4', destination=renamed)

    def test_a_different_narration_conflicts(self, funded_wallet):
        prepare(funded_wallet, key='dest-5', narration='Rent')

        with pytest.raises(TransferIdempotencyConflictError):
            prepare(funded_wallet, key='dest-5', narration='Something else')

    def test_a_different_amount_conflicts_through_m4(self, funded_wallet):
        """M4 already owns this half of the intent, and M5 does not re-do it."""
        from moneycore.domain.errors import TransactionIdempotencyConflictError

        prepare(funded_wallet, key='dest-6', amount=3_000)

        with pytest.raises(TransactionIdempotencyConflictError):
            prepare(funded_wallet, key='dest-6', amount=4_000)

    def test_the_original_transfer_is_left_untouched(self, funded_wallet):
        original = prepare(funded_wallet, key='dest-7')

        with pytest.raises(TransferIdempotencyConflictError):
            prepare(funded_wallet, key='dest-7', destination=OTHER_DESTINATION)

        original.refresh_from_db()
        assert original.destination_account_number == '0123456789'
        assert original.recipient_name == 'Ada Okafor'

    def test_the_conflict_creates_nothing(self, funded_wallet):
        prepare(funded_wallet, key='dest-8')

        with pytest.raises(TransferIdempotencyConflictError):
            prepare(funded_wallet, key='dest-8', destination=OTHER_DESTINATION)

        assert Transfer.objects.count() == 1
        assert FinancialTransaction.objects.count() == 1
        assert FundsHold.objects.count() == 1

    def test_funds_are_not_reserved_twice_by_a_conflict(self, funded_wallet):
        prepare(funded_wallet, key='dest-9')

        with pytest.raises(TransferIdempotencyConflictError):
            prepare(funded_wallet, key='dest-9', destination=OTHER_DESTINATION)

        assert wallet_balance_projection(
            funded_wallet
        ).held == Money(7_000, 'NGN')

    def test_the_conflict_names_the_fields_but_not_the_destination(
        self, funded_wallet
    ):
        """A caller replaying someone's key is not told where that money went."""
        prepare(funded_wallet, key='dest-10')

        with pytest.raises(TransferIdempotencyConflictError) as raised:
            prepare(funded_wallet, key='dest-10', destination=OTHER_DESTINATION)

        details = raised.value.details
        assert 'destination_account_number' in details['conflicting_fields']
        assert '9876543210' not in str(details)
        assert '0123456789' not in str(details)

    def test_the_conflict_carries_a_stable_code(self, funded_wallet):
        prepare(funded_wallet, key='dest-11')

        with pytest.raises(TransferIdempotencyConflictError) as raised:
            prepare(funded_wallet, key='dest-11', destination=OTHER_DESTINATION)

        assert raised.value.code == 'transfer_idempotency_conflict'

    def test_a_new_key_sends_to_the_new_destination_normally(self, funded_wallet):
        prepare(funded_wallet, amount=3_000, key='dest-12')

        second = prepare(
            funded_wallet, amount=3_000, key='dest-13', destination=OTHER_DESTINATION
        )

        assert second.destination_account_number == '9876543210'
        assert Transfer.objects.count() == 2


class TestKeyCollisionWithNonTransferTransactions:
    """An M4 transaction already in flight must not be adopted by a transfer."""

    def test_a_transaction_already_backed_by_a_hold_is_refused(self, funded_wallet):
        """M4 was used directly under this key. Adopting it would attach a
        transfer to an operation that never described one."""
        from moneycore.services.transactions import attach_hold, create_transaction

        txn = create_transaction(
            funded_wallet, TransactionDirection.OUTGOING, 7_000,
            idempotency_key='collide-1',
        )
        attach_hold(txn, create_hold(funded_wallet, 7_000))

        with pytest.raises(TransferTransactionMismatchError):
            prepare(funded_wallet, key='collide-1')

        assert Transfer.objects.count() == 0

    def test_a_started_transaction_under_the_same_key_is_refused(
        self, funded_wallet
    ):
        from moneycore.services.transactions import (
            attach_hold,
            create_transaction,
            start_processing,
        )

        txn = create_transaction(
            funded_wallet, TransactionDirection.OUTGOING, 7_000,
            idempotency_key='collide-2',
        )
        start_processing(attach_hold(txn, create_hold(funded_wallet, 7_000)))

        with pytest.raises(TransferTransactionMismatchError):
            prepare(funded_wallet, key='collide-2')

    def test_a_resolved_transaction_under_the_same_key_is_refused(
        self, funded_wallet
    ):
        from moneycore.services.transactions import create_transaction, fail_transaction

        txn = create_transaction(
            funded_wallet, TransactionDirection.OUTGOING, 7_000,
            idempotency_key='collide-3',
        )
        fail_transaction(txn, failure_code='limit_exceeded')

        with pytest.raises(TransferTransactionMismatchError):
            prepare(funded_wallet, key='collide-3')

    def test_the_refusal_reserves_nothing_extra(self, funded_wallet):
        from moneycore.services.transactions import attach_hold, create_transaction

        txn = create_transaction(
            funded_wallet, TransactionDirection.OUTGOING, 7_000,
            idempotency_key='collide-4',
        )
        attach_hold(txn, create_hold(funded_wallet, 7_000))
        before = wallet_balance_projection(funded_wallet)

        with pytest.raises(TransferTransactionMismatchError):
            prepare(funded_wallet, key='collide-4')

        assert wallet_balance_projection(funded_wallet) == before

    def test_an_untouched_matching_intent_is_adopted_deliberately(
        self, funded_wallet
    ):
        """Documented behaviour, not an accident.

        A CREATED transaction with no reservation, whose wallet, direction,
        amount and currency all match, describes exactly this transfer and
        nothing else. Backing it is what M4 idempotency is for; refusing it
        would strand the key with no way to complete.
        """
        from moneycore.services.transactions import create_transaction

        txn = create_transaction(
            funded_wallet, TransactionDirection.OUTGOING, 7_000,
            idempotency_key='adopt-1',
        )

        transfer = prepare(funded_wallet, key='adopt-1')

        assert transfer.financial_transaction_id == txn.pk
        assert transfer.hold.amount_minor == 7_000
        assert FinancialTransaction.objects.count() == 1
