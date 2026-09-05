"""Concurrency proofs for provider execution.

The single most important thing to be able to prove about a payment
integration: **the rail was asked exactly once.** Everything else here follows
from that.

The provider call deliberately happens outside the database transaction, so the
durable attempt row — committed before the call, unique per transaction — is
what serialises execution. These tests exist because that design has to be
proved, not asserted.

Real threads on independent connections, released together from a barrier.
``SELECT ... FOR UPDATE`` is a no-op on SQLite, so these are PostgreSQL
guarantees and skip elsewhere with a stated reason.
"""

import threading

import pytest
from django.db import connection, connections

from moneycore.domain.errors import ProviderExecutionAlreadyStartedError
from moneycore.domain.holds import HoldStatus
from moneycore.domain.ledger import LedgerAccountType
from moneycore.domain.money import Money
from moneycore.domain.providers import ProviderAttemptStatus
from moneycore.domain.transactions import TransactionStatus
from moneycore.domain.transfers import VerifiedBankAccount
from moneycore.models import Journal, ProviderExecutionAttempt, Transfer
from moneycore.providers.simulator import (
    SimulatorTransferProvider,
    TransferScenario,
)
from moneycore.services.holds import wallet_balance_projection
from moneycore.services.ledger import open_ledger_account
from moneycore.services.provider_execution import (
    execute_transfer,
    provider_attempt_for,
)
from moneycore.services.transfers import prepare_transfer

pytestmark = pytest.mark.django_db(transaction=True)

on_postgres = pytest.mark.skipif(
    connection.vendor != 'postgresql',
    reason='Row locking (SELECT ... FOR UPDATE) is a no-op on SQLite.',
)


DESTINATION = VerifiedBankAccount(
    account_number='0123456789',
    bank_code='SIM-001',
    bank_name='Simulated First Bank',
    account_name='Ada Okafor',
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
        code='internal:m6-concurrency-counterpart:NGN',
        name='Internal counterpart',
        account_type=LedgerAccountType.ASSET,
        currency='NGN',
    )


@pytest.fixture
def prepared(funded_wallet):
    return prepare_transfer(
        funded_wallet, DESTINATION, 7_000, idempotency_key='concurrent-1'
    )


def race_execution(transfer, provider, counterpart, count):
    """Fire ``count`` simultaneous execute calls at one transfer."""
    def execute(index):
        fresh = Transfer.objects.get(pk=transfer.pk)
        return execute_transfer(
            fresh, provider=provider, counterpart_account=counterpart
        )

    return run_in_threads(execute, count)


@on_postgres
class TestTheRailIsAskedExactlyOnce:
    """Whatever the outcome, and however many callers arrive together."""

    @pytest.mark.parametrize(
        'scenario',
        [
            TransferScenario.SUCCESS,
            TransferScenario.KNOWN_FAILURE,
            TransferScenario.AMBIGUOUS_AFTER_SUBMISSION,
        ],
    )
    def test_two_simultaneous_executions_submit_once(
        self, funded_wallet, settlement_account, scenario
    ):
        transfer = prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key=f'once-{scenario}'
        )
        provider = SimulatorTransferProvider(transfer_scenario=scenario)

        results, errors = race_execution(
            transfer, provider, settlement_account, 2
        )

        assert provider.submit_call_count == 1
        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], ProviderExecutionAlreadyStartedError)

    @pytest.mark.parametrize(
        'scenario',
        [
            TransferScenario.SUCCESS,
            TransferScenario.KNOWN_FAILURE,
            TransferScenario.AMBIGUOUS_AFTER_SUBMISSION,
        ],
    )
    def test_only_one_attempt_row_exists(
        self, funded_wallet, settlement_account, scenario
    ):
        transfer = prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key=f'row-{scenario}'
        )
        provider = SimulatorTransferProvider(transfer_scenario=scenario)

        race_execution(transfer, provider, settlement_account, 2)

        assert ProviderExecutionAttempt.objects.count() == 1

    def test_five_simultaneous_executions_still_submit_once(
        self, prepared, settlement_account
    ):
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.SUCCESS
        )

        results, errors = race_execution(
            prepared, provider, settlement_account, 5
        )

        assert provider.submit_call_count == 1
        assert len(results) == 1
        assert len(errors) == 4
        assert all(
            isinstance(error, ProviderExecutionAlreadyStartedError)
            for error in errors
        )
        assert ProviderExecutionAttempt.objects.count() == 1

    def test_the_recorded_requests_match_the_call_count(
        self, prepared, settlement_account
    ):
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.SUCCESS
        )

        race_execution(prepared, provider, settlement_account, 4)

        assert len(provider.submitted_requests) == 1


@on_postgres
class TestConcurrentSuccess:
    def test_exactly_one_journal_is_posted(self, prepared, settlement_account):
        before = Journal.objects.count()
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.SUCCESS
        )

        race_execution(prepared, provider, settlement_account, 4)

        assert Journal.objects.count() == before + 1

    def test_the_wallet_is_debited_once(self, prepared, settlement_account):
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.SUCCESS
        )

        race_execution(prepared, provider, settlement_account, 4)

        projection = wallet_balance_projection(prepared.wallet)
        assert projection.posted == Money(3_000, 'NGN')
        assert projection.held == Money(0, 'NGN')
        assert projection.available == Money(3_000, 'NGN')

    def test_the_reservation_is_released_once(self, prepared, settlement_account):
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.SUCCESS
        )

        race_execution(prepared, provider, settlement_account, 3)

        prepared.hold.refresh_from_db()
        assert prepared.hold.status == HoldStatus.RELEASED
        assert ProviderExecutionAttempt.objects.get().status == (
            ProviderAttemptStatus.SUCCEEDED
        )

    def test_the_final_state_is_coherent(self, prepared, settlement_account):
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.SUCCESS
        )

        race_execution(prepared, provider, settlement_account, 3)

        final = Transfer.objects.get(pk=prepared.pk)
        assert final.status == TransactionStatus.SUCCEEDED
        assert final.journal is not None
        assert provider_attempt_for(final).provider_reference.startswith('SIM-')


@on_postgres
class TestConcurrentKnownFailure:
    def test_no_journal_is_posted_and_the_hold_is_released_once(
        self, prepared, settlement_account
    ):
        before = Journal.objects.count()
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.KNOWN_FAILURE
        )

        race_execution(prepared, provider, settlement_account, 4)

        prepared.hold.refresh_from_db()
        assert Journal.objects.count() == before
        assert prepared.hold.status == HoldStatus.RELEASED
        assert wallet_balance_projection(
            prepared.wallet
        ).available == Money(10_000, 'NGN')

    def test_the_transaction_failed_exactly_once(
        self, prepared, settlement_account
    ):
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.KNOWN_FAILURE
        )

        race_execution(prepared, provider, settlement_account, 3)

        final = Transfer.objects.get(pk=prepared.pk)
        assert final.status == TransactionStatus.FAILED
        assert ProviderExecutionAttempt.objects.count() == 1
        assert provider.submit_call_count == 1


@on_postgres
class TestConcurrentAmbiguity:
    """The dangerous one: several callers, an answer nobody received."""

    def test_the_rail_is_still_asked_only_once(
        self, prepared, settlement_account
    ):
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
        )

        race_execution(prepared, provider, settlement_account, 5)

        assert provider.submit_call_count == 1

    def test_the_reservation_survives_the_race(self, prepared, settlement_account):
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
        )

        race_execution(prepared, provider, settlement_account, 5)

        prepared.hold.refresh_from_db()
        assert prepared.hold.status == HoldStatus.ACTIVE
        assert wallet_balance_projection(
            prepared.wallet
        ).held == Money(7_000, 'NGN')

    def test_nothing_is_posted(self, prepared, settlement_account):
        before = Journal.objects.count()
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
        )

        race_execution(prepared, provider, settlement_account, 5)

        assert Journal.objects.count() == before

    def test_the_transaction_is_unknown_not_failed(
        self, prepared, settlement_account
    ):
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
        )

        race_execution(prepared, provider, settlement_account, 5)

        final = Transfer.objects.get(pk=prepared.pk)
        assert final.status == TransactionStatus.UNKNOWN
        assert provider_attempt_for(final).status == ProviderAttemptStatus.UNKNOWN


@on_postgres
class TestSeparateTransfersDoNotContend:
    def test_two_transfers_execute_independently(
        self, funded_wallet, settlement_account
    ):
        transfers = [
            prepare_transfer(
                funded_wallet, DESTINATION, 3_000,
                idempotency_key=f'independent-{index}',
            )
            for index in range(2)
        ]
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.SUCCESS
        )

        def execute(index):
            fresh = Transfer.objects.get(pk=transfers[index].pk)
            return execute_transfer(
                fresh, provider=provider, counterpart_account=settlement_account
            ).pk

        results, errors = run_in_threads(execute, 2)

        assert errors == []
        assert provider.submit_call_count == 2
        assert ProviderExecutionAttempt.objects.count() == 2
        assert wallet_balance_projection(
            funded_wallet
        ).posted == Money(4_000, 'NGN')
