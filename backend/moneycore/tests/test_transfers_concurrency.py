"""Concurrency proofs for transfer orchestration.

M4 and M3 already prove their own pieces. What must be proved here is that
wrapping them in transfer preparation does not weaken any of it: one transfer
per key, one destination per key, no oversubscription, and money that moves
exactly once.

Real threads on independent connections, released together from a barrier.
``SELECT ... FOR UPDATE`` is a no-op on SQLite, so these are PostgreSQL
guarantees and skip elsewhere with a stated reason rather than passing
vacuously. PostgreSQL remains authoritative for database semantics.
"""

import threading

import pytest
from django.contrib.auth.models import User
from django.db import connection, connections

from moneycore.domain.errors import (
    InsufficientAvailableBalanceError,
    TransactionAlreadyResolvedError,
    TransferIdempotencyConflictError,
)
from moneycore.domain.holds import HoldStatus
from moneycore.domain.ledger import LedgerAccountType
from moneycore.domain.money import Money
from moneycore.domain.transactions import TransactionStatus
from moneycore.domain.transfers import VerifiedBankAccount
from moneycore.models import FinancialTransaction, FundsHold, Journal, Transfer
from moneycore.services.holds import wallet_balance_projection
from moneycore.services.ledger import open_ledger_account
from moneycore.services.provisioning import provision_financial_account
from moneycore.services.transactions import start_processing
from moneycore.services.transfers import prepare_transfer, succeed_transfer

pytestmark = pytest.mark.django_db(transaction=True)

on_postgres = pytest.mark.skipif(
    connection.vendor != 'postgresql',
    reason='Row locking (SELECT ... FOR UPDATE) is a no-op on SQLite.',
)


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


def run_in_threads(target, count):
    """Run ``target`` in ``count`` threads, all released from one barrier."""
    results, errors = [], []
    barrier = threading.Barrier(count)

    def wrapped(index):
        try:
            barrier.wait(timeout=15)
            results.append(target(index))
        except Exception as exc:  # noqa: BLE001 - recorded for assertion
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=wrapped, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    return results, errors


@pytest.fixture
def settlement_account(db):
    return open_ledger_account(
        code='internal:concurrency-counterpart:NGN',
        name='Internal counterpart',
        account_type=LedgerAccountType.ASSET,
        currency='NGN',
    )


# --------------------------------------------------------------------------
# 1. Duplicate preparation
# --------------------------------------------------------------------------


@on_postgres
class TestDuplicatePreparation:
    """Two callers, one key, one identical transfer."""

    def test_only_one_transfer_is_created(self, funded_wallet):
        def prepare(index):
            return prepare_transfer(
                funded_wallet, DESTINATION, 7_000, idempotency_key='dup-1'
            ).pk

        results, errors = run_in_threads(prepare, 2)

        assert errors == []
        assert Transfer.objects.count() == 1

    def test_both_callers_receive_the_same_transfer(self, funded_wallet):
        def prepare(index):
            return prepare_transfer(
                funded_wallet, DESTINATION, 7_000, idempotency_key='dup-2'
            ).pk

        results, errors = run_in_threads(prepare, 2)

        assert errors == []
        assert len(set(results)) == 1

    def test_one_transaction_and_one_hold_exist(self, funded_wallet):
        def prepare(index):
            return prepare_transfer(
                funded_wallet, DESTINATION, 7_000, idempotency_key='dup-3'
            ).pk

        run_in_threads(prepare, 2)

        assert FinancialTransaction.objects.count() == 1
        assert FundsHold.objects.count() == 1

    def test_funds_are_reserved_exactly_once(self, funded_wallet):
        def prepare(index):
            return prepare_transfer(
                funded_wallet, DESTINATION, 7_000, idempotency_key='dup-4'
            ).pk

        run_in_threads(prepare, 2)

        projection = wallet_balance_projection(funded_wallet)
        assert projection.held == Money(7_000, 'NGN')
        assert projection.available == Money(3_000, 'NGN')

    def test_six_simultaneous_callers_still_produce_one(self, funded_wallet):
        def prepare(index):
            return prepare_transfer(
                funded_wallet, DESTINATION, 7_000, idempotency_key='dup-5'
            ).pk

        results, errors = run_in_threads(prepare, 6)

        assert errors == []
        assert len(set(results)) == 1
        assert Transfer.objects.count() == 1
        assert FundsHold.objects.count() == 1
        assert wallet_balance_projection(
            funded_wallet
        ).held == Money(7_000, 'NGN')

    def test_nothing_is_posted_by_preparation(self, funded_wallet):
        def prepare(index):
            return prepare_transfer(
                funded_wallet, DESTINATION, 7_000, idempotency_key='dup-6'
            ).pk

        run_in_threads(prepare, 4)

        assert Journal.objects.count() == 1  # only the fixture funding


# --------------------------------------------------------------------------
# 2. Destination conflict
# --------------------------------------------------------------------------


@on_postgres
class TestDestinationConflictRace:
    """One key, two destinations. Exactly one may win.

    The invariant that matters most in M5: a single financial transaction must
    never end up shared between two destination intents, because that is how
    money reaches the wrong person.
    """

    def _race(self, wallet, key):
        destinations = [DESTINATION, OTHER_DESTINATION]

        def prepare(index):
            return prepare_transfer(
                wallet, destinations[index], 7_000, idempotency_key=key
            ).destination_account_number

        return run_in_threads(prepare, 2)

    def test_exactly_one_destination_wins(self, funded_wallet):
        results, errors = self._race(funded_wallet, 'conflict-1')

        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], TransferIdempotencyConflictError)

    def test_one_coherent_transfer_survives(self, funded_wallet):
        results, _ = self._race(funded_wallet, 'conflict-2')

        assert Transfer.objects.count() == 1
        assert Transfer.objects.get().destination_account_number == results[0]

    def test_the_transaction_is_not_shared_between_intents(self, funded_wallet):
        self._race(funded_wallet, 'conflict-3')

        assert FinancialTransaction.objects.count() == 1
        assert Transfer.objects.filter(
            financial_transaction=FinancialTransaction.objects.get()
        ).count() == 1

    def test_funds_are_reserved_once(self, funded_wallet):
        self._race(funded_wallet, 'conflict-4')

        assert FundsHold.objects.count() == 1
        assert wallet_balance_projection(
            funded_wallet
        ).held == Money(7_000, 'NGN')

    def test_four_racing_destinations_still_leave_one(self, funded_wallet):
        destinations = [
            VerifiedBankAccount(
                account_number=f'012345678{index}',
                bank_code='NG-058',
                bank_name='Example Bank',
                account_name=f'Person {index}',
            )
            for index in range(4)
        ]

        def prepare(index):
            return prepare_transfer(
                funded_wallet, destinations[index], 7_000,
                idempotency_key='conflict-5',
            ).pk

        results, errors = run_in_threads(prepare, 4)

        assert len(results) == 1
        assert len(errors) == 3
        assert all(
            isinstance(error, TransferIdempotencyConflictError) for error in errors
        )
        assert Transfer.objects.count() == 1

    def test_the_losers_reserve_nothing(self, funded_wallet):
        self._race(funded_wallet, 'conflict-6')

        assert FundsHold.objects.count() == 1
        assert wallet_balance_projection(
            funded_wallet
        ).available == Money(3_000, 'NGN')


# --------------------------------------------------------------------------
# 3 & 4. Oversubscription
# --------------------------------------------------------------------------


@on_postgres
class TestNoOversubscription:
    """M3's guarantee, re-proved through the transfer orchestration."""

    def test_two_different_keys_cannot_both_reserve_seven_thousand(
        self, funded_wallet
    ):
        """The canonical race: both want 7 000 against 10 000."""
        def prepare(index):
            return prepare_transfer(
                funded_wallet, DESTINATION, 7_000,
                idempotency_key=f'oversub-{index}',
            ).pk

        results, errors = run_in_threads(prepare, 2)

        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], InsufficientAvailableBalanceError)

    def test_the_wallet_is_left_coherent_after_that_race(self, funded_wallet):
        def prepare(index):
            return prepare_transfer(
                funded_wallet, DESTINATION, 7_000,
                idempotency_key=f'oversub-b-{index}',
            ).pk

        run_in_threads(prepare, 2)

        projection = wallet_balance_projection(funded_wallet)
        assert projection.posted == Money(10_000, 'NGN')
        assert projection.held == Money(7_000, 'NGN')
        assert projection.available == Money(3_000, 'NGN')

    def test_the_loser_leaves_no_debris(self, funded_wallet):
        def prepare(index):
            return prepare_transfer(
                funded_wallet, DESTINATION, 7_000,
                idempotency_key=f'oversub-c-{index}',
            ).pk

        run_in_threads(prepare, 2)

        assert Transfer.objects.count() == 1
        assert FinancialTransaction.objects.count() == 1
        assert FundsHold.objects.count() == 1

    def test_ten_transfers_of_one_thousand_fit_exactly(self, funded_wallet):
        """Exactly consumes the balance: all ten must succeed."""
        def prepare(index):
            return prepare_transfer(
                funded_wallet, DESTINATION, 1_000,
                idempotency_key=f'exact-{index}',
            ).pk

        results, errors = run_in_threads(prepare, 10)

        assert errors == []
        assert len(set(results)) == 10
        assert wallet_balance_projection(
            funded_wallet
        ).available == Money(0, 'NGN')

    def test_eleven_transfers_of_one_thousand_do_not(self, funded_wallet):
        """One too many for the balance: exactly one must be refused."""
        def prepare(index):
            return prepare_transfer(
                funded_wallet, DESTINATION, 1_000,
                idempotency_key=f'over-{index}',
            ).pk

        results, errors = run_in_threads(prepare, 11)

        assert len(results) == 10
        assert len(errors) == 1
        assert isinstance(errors[0], InsufficientAvailableBalanceError)
        assert wallet_balance_projection(
            funded_wallet
        ).available == Money(0, 'NGN')

    def test_the_held_total_never_exceeds_the_posted_balance(self, funded_wallet):
        def prepare(index):
            return prepare_transfer(
                funded_wallet, DESTINATION, 3_000,
                idempotency_key=f'floor-{index}',
            ).pk

        run_in_threads(prepare, 8)

        projection = wallet_balance_projection(funded_wallet)
        assert projection.held <= projection.posted
        assert projection.available >= Money(0, 'NGN')


# --------------------------------------------------------------------------
# 5. Duplicate success
# --------------------------------------------------------------------------


@on_postgres
class TestDuplicateTransferSuccess:
    """The one that must never happen twice: the wallet debited twice."""

    @pytest.fixture
    def processing(self, funded_wallet):
        transfer = prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key='success-race'
        )
        start_processing(transfer.financial_transaction)
        transfer.refresh_from_db()
        return transfer

    def _reload(self, transfer):
        return Transfer.objects.get(pk=transfer.pk)

    def test_only_one_success_lands(self, processing, settlement_account):
        def succeed(index):
            return succeed_transfer(
                self._reload(processing), counterpart_account=settlement_account
            ).pk

        results, errors = run_in_threads(succeed, 2)

        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], TransactionAlreadyResolvedError)

    def test_exactly_one_journal_is_posted(self, processing, settlement_account):
        before = Journal.objects.count()

        def succeed(index):
            return succeed_transfer(
                self._reload(processing), counterpart_account=settlement_account
            ).pk

        run_in_threads(succeed, 2)

        assert Journal.objects.count() == before + 1

    def test_the_wallet_is_debited_once(self, processing, settlement_account):
        def succeed(index):
            return succeed_transfer(
                self._reload(processing), counterpart_account=settlement_account
            ).pk

        run_in_threads(succeed, 2)

        projection = wallet_balance_projection(processing.wallet)
        assert projection.posted == Money(3_000, 'NGN')
        assert projection.held == Money(0, 'NGN')
        assert projection.available == Money(3_000, 'NGN')

    def test_five_simultaneous_successes_still_post_once(
        self, processing, settlement_account
    ):
        before = Journal.objects.count()

        def succeed(index):
            return succeed_transfer(
                self._reload(processing), counterpart_account=settlement_account
            ).pk

        results, errors = run_in_threads(succeed, 5)

        assert len(results) == 1
        assert len(errors) == 4
        assert all(
            isinstance(error, TransactionAlreadyResolvedError) for error in errors
        )
        assert Journal.objects.count() == before + 1
        assert wallet_balance_projection(
            processing.wallet
        ).posted == Money(3_000, 'NGN')

    def test_the_reservation_is_released_exactly_once(
        self, processing, settlement_account
    ):
        def succeed(index):
            return succeed_transfer(
                self._reload(processing), counterpart_account=settlement_account
            ).pk

        run_in_threads(succeed, 3)

        processing.hold.refresh_from_db()
        assert processing.hold.status == HoldStatus.RELEASED
        assert FundsHold.objects.filter(status=HoldStatus.RELEASED).count() == 1

    def test_the_final_transfer_state_is_coherent(
        self, processing, settlement_account
    ):
        def succeed(index):
            return succeed_transfer(
                self._reload(processing), counterpart_account=settlement_account
            ).pk

        run_in_threads(succeed, 3)

        final = self._reload(processing)
        assert final.status == TransactionStatus.SUCCEEDED
        assert final.journal is not None
        assert final.destination_account_number == '0123456789'


# --------------------------------------------------------------------------
# Cross-wallet independence
# --------------------------------------------------------------------------


@on_postgres
class TestSeparateWalletsDoNotContend:
    def test_two_wallets_prepare_independently(self, funded_wallet, db):
        from moneycore.domain.ledger import LedgerAccountType
        from moneycore.services.ledger import (
            credit,
            debit,
            open_wallet_ledger_account,
            post_journal,
        )

        other_wallet = provision_financial_account(
            User.objects.create_user('transfer-other')
        ).wallet
        other_account = open_wallet_ledger_account(
            other_wallet, account_type=LedgerAccountType.LIABILITY
        )
        funding = open_ledger_account(
            code='internal:other-funding:NGN',
            name='Other funding',
            account_type=LedgerAccountType.ASSET,
            currency='NGN',
        )
        post_journal(
            currency='NGN',
            entries=[debit(funding, 10_000), credit(other_account, 10_000)],
        )

        wallets = [funded_wallet, other_wallet]

        def prepare(index):
            return prepare_transfer(
                wallets[index % 2], DESTINATION, 7_000,
                idempotency_key='cross-wallet',
            ).pk

        results, errors = run_in_threads(prepare, 4)

        assert errors == []
        assert Transfer.objects.count() == 2
        for wallet in wallets:
            assert wallet_balance_projection(
                wallet
            ).held == Money(7_000, 'NGN')
