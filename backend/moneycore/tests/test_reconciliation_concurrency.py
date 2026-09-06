"""The provider fetch happens outside transactions, and races end safely.

Two proofs here need a connection pytest-django has **not** wrapped in its own
transaction: the ordinary ``django_db`` fixture runs each test inside an atomic
block, which would make ``in_atomic_block`` read True whatever the code does.

The races matter because reconciliation reads financial rows while other
milestones are free to change them. Reconciliation takes no locks at all, so
what must be shown is that both interleavings produce an honest observation and
neither deadlocks nor corrupts anything.
"""

import threading
from datetime import timedelta

import pytest
from django.db import connection, connections
from django.utils import timezone

from moneycore.domain.holds import HoldStatus
from moneycore.domain.ledger import LedgerAccountType
from moneycore.domain.money import Money
from moneycore.domain.reconciliation import (
    ProviderTransferRecord,
    ReconciliationOutcome,
    ReconciliationRunStatus,
    ReconciliationStatus,
    ReconciliationWindow,
)
from moneycore.domain.transactions import TransactionStatus
from moneycore.domain.transfers import VerifiedBankAccount
from moneycore.models import (
    Journal,
    ProviderExecutionAttempt,
    ProviderReconciliationItem,
    ProviderReconciliationRun,
    Transfer,
)
from moneycore.providers.simulator import (
    SimulatorTransferProvider,
    StatusScenario,
    TransferScenario,
)
from moneycore.services.holds import wallet_balance_projection
from moneycore.services.ledger import open_ledger_account
from moneycore.services.provider_execution import (
    execute_transfer,
    provider_attempt_for,
)
from moneycore.services.provider_recovery import recover_provider_attempt
from moneycore.services.reconciliation import (
    items_for,
    reconcile_transfers,
)
from moneycore.services.transfers import prepare_transfer

pytestmark = pytest.mark.django_db(transaction=True)

on_postgres = pytest.mark.skipif(
    connection.vendor != 'postgresql',
    reason='Real concurrency needs PostgreSQL; SQLite serialises writers.',
)


DESTINATION = VerifiedBankAccount(
    account_number='0123456789',
    bank_code='SIM-001',
    bank_name='Simulated First Bank',
    account_name='Ada Okafor',
)


def run_in_threads(target, count):
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

    assert not any(thread.is_alive() for thread in threads), 'a thread hung'
    return results, errors


@pytest.fixture
def settlement_account(db):
    return open_ledger_account(
        code='internal:m8-concurrency:NGN',
        name='Internal counterpart',
        account_type=LedgerAccountType.ASSET,
        currency='NGN',
    )


@pytest.fixture
def window():
    now = timezone.now()
    return ReconciliationWindow(
        start=now - timedelta(hours=1), end=now + timedelta(hours=1)
    )


def ambiguous_transfer(wallet, counterpart, key='m8-race'):
    transfer = prepare_transfer(wallet, DESTINATION, 7_000, idempotency_key=key)
    execute_transfer(
        transfer,
        provider=SimulatorTransferProvider(
            transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
        ),
        counterpart_account=counterpart,
    )
    transfer.refresh_from_db()
    return transfer


def record_for(attempt, **overrides):
    values = {
        'provider_key': 'simulator',
        'provider_record_id': f'rec-{attempt.pk}',
        'client_reference': attempt.client_reference,
        'outcome': ReconciliationOutcome.SUCCEEDED,
        'amount_minor': attempt.financial_transaction.amount_minor,
        'currency': attempt.financial_transaction.currency,
        'observed_at': timezone.now(),
    }
    values.update(overrides)
    return ProviderTransferRecord(**values)


class TestTheAmbientConnectionIsNotWrapped:
    def test_the_test_itself_runs_outside_a_transaction(self):
        """Guard: without this, the proof below would pass vacuously."""
        assert connection.in_atomic_block is False


class TestProviderFetchHappensOutsideTransactions:
    def test_no_transaction_is_open_while_listing_records(
        self, funded_wallet, settlement_account, window
    ):
        observed = {}
        provider = SimulatorTransferProvider(reconciliation_records=())
        provider.on_list_records = lambda w: observed.update(
            in_atomic_block=connection.in_atomic_block
        )

        reconcile_transfers(provider=provider, window=window)

        assert observed['in_atomic_block'] is False

    def test_the_run_is_already_committed_when_the_fetch_happens(
        self, funded_wallet, settlement_account, window
    ):
        """The claim is durable before anything external is asked."""
        observed = {}
        provider = SimulatorTransferProvider(reconciliation_records=())

        def look(w):
            run = ProviderReconciliationRun.objects.first()
            observed['status'] = run.status if run else None

        provider.on_list_records = look

        reconcile_transfers(provider=provider, window=window)

        assert observed['status'] == ReconciliationRunStatus.STARTED

    def test_the_service_wraps_no_fetch_in_atomic(self):
        import inspect

        from moneycore.services import reconciliation

        source = inspect.getsource(reconciliation.reconcile_transfers)
        assert 'atomic' not in source

    def test_neither_database_phase_calls_the_provider(self):
        import inspect

        from moneycore.services import reconciliation

        for phase in (
            reconciliation._open_run,
            reconciliation._classify_and_record,
            reconciliation._fail_run,
        ):
            source = inspect.getsource(phase)
            assert 'list_transfer_records' not in source, phase.__name__


@on_postgres
class TestRecoveryRacesReconciliation:
    """M7 resolving while M8 is comparing.

    Both orderings are valid historical observations — the point is that
    neither deadlocks, neither duplicates a journal, and reconciliation never
    changes what recovery decided.
    """

    def _race(self, transfer, settlement_account, window):
        attempt = provider_attempt_for(transfer)
        record = record_for(attempt)

        def act(index):
            if index == 0:
                fresh = ProviderExecutionAttempt.objects.get(pk=attempt.pk)
                return recover_provider_attempt(
                    fresh,
                    provider=SimulatorTransferProvider(
                        status_scenario=StatusScenario.SUCCESS
                    ),
                    counterpart_account=settlement_account,
                ).outcome
            run = reconcile_transfers(
                provider=SimulatorTransferProvider(
                    reconciliation_records=[record]
                ),
                window=window,
            )
            return run.pk

        return run_in_threads(act, 2)

    def test_neither_thread_fails(
        self, funded_wallet, settlement_account, window
    ):
        transfer = ambiguous_transfer(funded_wallet, settlement_account)

        _, errors = self._race(transfer, settlement_account, window)

        assert errors == [], errors

    def test_exactly_one_journal_is_posted(
        self, funded_wallet, settlement_account, window
    ):
        transfer = ambiguous_transfer(funded_wallet, settlement_account)
        before = Journal.objects.count()

        self._race(transfer, settlement_account, window)

        assert Journal.objects.count() == before + 1

    def test_recovery_still_settles_the_transfer(
        self, funded_wallet, settlement_account, window
    ):
        transfer = ambiguous_transfer(funded_wallet, settlement_account)

        self._race(transfer, settlement_account, window)

        final = Transfer.objects.get(pk=transfer.pk)
        final.hold.refresh_from_db()
        assert final.status == TransactionStatus.SUCCEEDED
        assert final.hold.status == HoldStatus.RELEASED
        assert wallet_balance_projection(
            funded_wallet
        ).posted == Money(3_000, 'NGN')

    def test_the_reconciliation_finding_is_one_of_two_honest_answers(
        self, funded_wallet, settlement_account, window
    ):
        """Snapshot before recovery -> discrepancy. After -> matched."""
        transfer = ambiguous_transfer(funded_wallet, settlement_account)

        self._race(transfer, settlement_account, window)

        run = ProviderReconciliationRun.objects.get()
        item = items_for(run).get()
        assert item.overall_status in {
            ReconciliationStatus.MATCHED, ReconciliationStatus.DISCREPANCY
        }
        if item.overall_status == ReconciliationStatus.DISCREPANCY:
            assert item.discrepancy_codes == ('outcome_mismatch',)

    def test_no_partial_item_is_written(
        self, funded_wallet, settlement_account, window
    ):
        transfer = ambiguous_transfer(funded_wallet, settlement_account)

        self._race(transfer, settlement_account, window)

        run = ProviderReconciliationRun.objects.get()
        assert run.status == ReconciliationRunStatus.COMPLETED
        assert items_for(run).count() == 1

    def test_a_later_run_matches(
        self, funded_wallet, settlement_account, window
    ):
        """Whatever the race decided, reconciling again agrees with truth."""
        transfer = ambiguous_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)

        self._race(transfer, settlement_account, window)

        later = reconcile_transfers(
            provider=SimulatorTransferProvider(
                reconciliation_records=[record_for(attempt)]
            ),
            window=window,
        )

        assert items_for(later).get().overall_status == (
            ReconciliationStatus.MATCHED
        )


@on_postgres
class TestExecutionRacesReconciliation:
    """M6 applying an outcome while M8 is comparing."""

    def test_neither_deadlocks(
        self, funded_wallet, settlement_account, window
    ):
        transfer = prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key='exec-race'
        )
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.SUCCESS
        )

        def act(index):
            if index == 0:
                return execute_transfer(
                    Transfer.objects.get(pk=transfer.pk),
                    provider=provider,
                    counterpart_account=settlement_account,
                ).pk
            return reconcile_transfers(
                provider=SimulatorTransferProvider(reconciliation_records=()),
                window=window,
            ).pk

        results, errors = run_in_threads(act, 2)

        assert errors == [], errors
        assert len(results) == 2

    def test_execution_still_completes_correctly(
        self, funded_wallet, settlement_account, window
    ):
        transfer = prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key='exec-race-2'
        )
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.SUCCESS
        )

        def act(index):
            if index == 0:
                return execute_transfer(
                    Transfer.objects.get(pk=transfer.pk),
                    provider=provider,
                    counterpart_account=settlement_account,
                ).pk
            return reconcile_transfers(
                provider=SimulatorTransferProvider(reconciliation_records=()),
                window=window,
            ).pk

        run_in_threads(act, 2)

        final = Transfer.objects.get(pk=transfer.pk)
        assert final.status == TransactionStatus.SUCCEEDED
        assert provider.submit_call_count == 1
        assert wallet_balance_projection(
            funded_wallet
        ).posted == Money(3_000, 'NGN')

    def test_reconciliation_reports_whatever_it_saw(
        self, funded_wallet, settlement_account, window
    ):
        """An in-flight execution reads as unresolved, never as failed."""
        transfer = prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key='exec-race-3'
        )

        def act(index):
            if index == 0:
                return execute_transfer(
                    Transfer.objects.get(pk=transfer.pk),
                    provider=SimulatorTransferProvider(
                        transfer_scenario=TransferScenario.SUCCESS
                    ),
                    counterpart_account=settlement_account,
                ).pk
            return reconcile_transfers(
                provider=SimulatorTransferProvider(reconciliation_records=()),
                window=window,
            ).pk

        run_in_threads(act, 2)

        run = ProviderReconciliationRun.objects.get()
        for item in items_for(run):
            assert item.internal_outcome in {
                ReconciliationOutcome.SUCCEEDED,
                ReconciliationOutcome.UNRESOLVED,
            }


@on_postgres
class TestConcurrentReconciliationRuns:
    def test_two_simultaneous_runs_both_complete(
        self, funded_wallet, settlement_account, window
    ):
        transfer = ambiguous_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)
        observed = timezone.now()

        def act(index):
            return reconcile_transfers(
                provider=SimulatorTransferProvider(
                    reconciliation_records=[
                        record_for(attempt, observed_at=observed)
                    ]
                ),
                window=window,
            ).pk

        results, errors = run_in_threads(act, 2)

        assert errors == [], errors
        assert len(set(results)) == 2
        assert ProviderReconciliationRun.objects.filter(
            status=ReconciliationRunStatus.COMPLETED
        ).count() == 2

    def test_they_reach_the_same_conclusion(
        self, funded_wallet, settlement_account, window
    ):
        transfer = ambiguous_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)
        observed = timezone.now()

        def act(index):
            return reconcile_transfers(
                provider=SimulatorTransferProvider(
                    reconciliation_records=[
                        record_for(attempt, observed_at=observed)
                    ]
                ),
                window=window,
            ).pk

        results, _ = run_in_threads(act, 2)

        conclusions = {
            tuple(i.overall_status for i in items_for(
                ProviderReconciliationRun.objects.get(pk=pk)
            ))
            for pk in results
        }
        assert len(conclusions) == 1

    def test_neither_run_mutates_financial_state(
        self, funded_wallet, settlement_account, window
    ):
        transfer = ambiguous_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)
        before = wallet_balance_projection(funded_wallet)
        journals = Journal.objects.count()

        def act(index):
            return reconcile_transfers(
                provider=SimulatorTransferProvider(
                    reconciliation_records=[record_for(attempt)]
                ),
                window=window,
            ).pk

        run_in_threads(act, 4)

        transfer.refresh_from_db()
        transfer.hold.refresh_from_db()
        assert wallet_balance_projection(funded_wallet) == before
        assert Journal.objects.count() == journals
        assert transfer.status == TransactionStatus.UNKNOWN
        assert transfer.hold.status == HoldStatus.ACTIVE
