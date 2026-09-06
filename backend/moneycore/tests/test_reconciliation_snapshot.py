"""The consistency of what one reconciliation run observes.

M8 originally took no locks. Under ``READ COMMITTED`` — PostgreSQL's default,
and what this project runs — every statement inside a transaction gets its own
fresh snapshot, so reading a transaction's status in one statement and its hold
in the next can straddle a concurrent M6/M7 resolution. The result is a state
SpendWise was never actually in: ``UNKNOWN`` with a released hold, or
``SUCCEEDED`` with no journal behind it. Reconciliation would then report an
internal integrity failure against a system that is perfectly consistent — the
worst possible false alarm, because it accuses our own ledger.

These tests prove that a run observes a concurrent resolution **entirely
before** or **entirely after**, never as a mixture, and that the lock which
buys that consistency is the narrowest one that will do: the transaction row
then the attempt row, and no wallet.

The interleavings are deterministic. Rather than hoping a race lands in the
window, a ``connection.execute_wrapper`` stops the reconciling thread at the
exact statement where a torn read would have to begin, and the test then asks
whether the resolving thread can get past it. Under the correction it cannot,
because reconciliation is holding the row that resolution needs.
"""

import threading
from datetime import timedelta

import pytest
from django.db import connection, connections
from django.utils import timezone

from moneycore.domain.errors import ReconciliationInternalIntegrityError
from moneycore.domain.holds import HoldStatus
from moneycore.domain.ledger import LedgerAccountType
from moneycore.domain.reconciliation import (
    ProviderTransferRecord,
    ReconciliationOutcome,
    ReconciliationRunStatus,
    ReconciliationStatus,
    ReconciliationWindow,
)
from moneycore.domain.transactions import TransactionStatus
from moneycore.domain.transfers import VerifiedBankAccount
from moneycore.models import FinancialTransaction
from moneycore.providers.simulator import (
    SimulatorTransferProvider,
    StatusScenario,
    TransferScenario,
)
from moneycore.services.ledger import open_ledger_account
from moneycore.services.provider_execution import (
    execute_transfer,
    provider_attempt_for,
)
from moneycore.services.provider_recovery import recover_provider_attempt
from moneycore.services.reconciliation import items_for, reconcile_transfers
from moneycore.services.transfers import prepare_transfer

pytestmark = pytest.mark.django_db(transaction=True)

on_postgres = pytest.mark.skipif(
    connection.vendor != 'postgresql',
    reason='Row locks and real concurrency need PostgreSQL.',
)

TRANSACTION_TABLE = 'moneycore_financialtransaction'
ATTEMPT_TABLE = 'moneycore_providerexecutionattempt'
HOLD_TABLE = 'moneycore_fundshold'
WALLET_TABLE = 'moneycore_wallet'
JOURNAL_TABLE = 'moneycore_journal'
LEDGER_ACCOUNT_TABLE = 'moneycore_ledgeraccount'

DESTINATION = VerifiedBankAccount(
    account_number='0123456789',
    bank_code='SIM-001',
    bank_name='Simulated First Bank',
    account_name='Ada Okafor',
)


# ---------------------------------------------------------------------------
# Instruments
# ---------------------------------------------------------------------------


class PauseAt:
    """Stall a connection the first time it touches ``table``.

    A test-side instrument only. The service has no hook, no injected clock and
    no test seam, so nothing here can leak into production behaviour.
    """

    def __init__(self, table, reached, resume, timeout=3.0):
        self.table = table
        self.reached = reached
        self.resume = resume
        self.timeout = timeout
        self.fired = False

    def __call__(self, execute, sql, params, many, context):
        if not self.fired and self.table in sql:
            self.fired = True
            self.reached.set()
            self.resume.wait(self.timeout)
        return execute(sql, params, many, context)


class RecordSql:
    """Capture every statement a connection runs, in order."""

    def __init__(self):
        self.statements = []

    def __call__(self, execute, sql, params, many, context):
        self.statements.append(sql)
        return execute(sql, params, many, context)

    def locked_tables(self):
        """Tables named by a ``FOR UPDATE`` statement, in the order taken."""
        seen = []
        for sql in self.statements:
            if 'FOR UPDATE' not in sql.upper():
                continue
            for table in (
                TRANSACTION_TABLE, ATTEMPT_TABLE, HOLD_TABLE, WALLET_TABLE,
                JOURNAL_TABLE, LEDGER_ACCOUNT_TABLE,
            ):
                if table in sql:
                    seen.append(table)
        return seen


def in_thread(target):
    """Start ``target`` on its own connection; returns (thread, result box)."""
    box = {}

    def wrapped():
        try:
            box['result'] = target()
        except Exception as exc:  # noqa: BLE001 - asserted by the caller
            box['error'] = exc
        finally:
            connections.close_all()

    thread = threading.Thread(target=wrapped)
    thread.start()
    return thread, box


# ---------------------------------------------------------------------------
# Fixtures and scenario builders
# ---------------------------------------------------------------------------


@pytest.fixture
def settlement_account(db):
    return open_ledger_account(
        code='internal:m8-snapshot:NGN',
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


def ambiguous_transfer(wallet, counterpart, key='m8-snapshot', amount_minor=1_000):
    """A transfer left ``UNKNOWN``: active hold, no journal, nothing settled."""
    transfer = prepare_transfer(
        wallet, DESTINATION, amount_minor, idempotency_key=key
    )
    execute_transfer(
        transfer,
        provider=SimulatorTransferProvider(
            transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
        ),
        counterpart_account=counterpart,
    )
    transfer.refresh_from_db()
    assert transfer.status == TransactionStatus.UNKNOWN
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


def reconcile(records, window, wrapper=None):
    """A callable that runs one reconciliation, optionally instrumented."""
    provider = SimulatorTransferProvider(reconciliation_records=list(records))

    def act():
        if wrapper is None:
            return reconcile_transfers(provider=provider, window=window)
        with connection.execute_wrapper(wrapper):
            return reconcile_transfers(provider=provider, window=window)

    return act


def resolve(attempt, counterpart):
    """A callable that settles an ambiguous attempt through M7 recovery."""

    def act():
        return recover_provider_attempt(
            attempt,
            provider=SimulatorTransferProvider(
                status_scenario=StatusScenario.SUCCESS
            ),
            counterpart_account=counterpart,
        )

    return act


def sole_item(run):
    return items_for(run).get()


# ---------------------------------------------------------------------------
# The two legal observations
# ---------------------------------------------------------------------------


@on_postgres
class TestAnObservationIsWholeOrNotAtAll:
    """Entirely before the resolution, or entirely after. Never a mixture."""

    def test_a_resolution_cannot_land_between_our_reads(
        self, funded_wallet, settlement_account, window
    ):
        """The heart of the correction, as a deterministic race.

        The reconciling thread is stopped at the first statement that reads the
        hold — precisely where a torn read would begin. A resolution is then
        attempted. It must not get through: reconciliation is holding the
        transaction row that M4's ``_lock_for_transition`` needs, so the
        resolution waits, and the reads either side of the pause describe one
        moment.

        Without the observation lock this is the failure: the recovery commits
        during the pause, the hold read that follows returns a *released* hold
        against a status already read as ``UNKNOWN``, and reconciliation
        reports our ledger as corrupt.
        """
        transfer = ambiguous_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)
        reached, resume = threading.Event(), threading.Event()

        reconciler, reconcile_box = in_thread(
            reconcile(
                [record_for(attempt)], window,
                wrapper=PauseAt(HOLD_TABLE, reached, resume, timeout=8.0),
            )
        )
        assert reached.wait(30), 'reconciliation never reached the hold read'

        resolver, resolve_box = in_thread(resolve(attempt, settlement_account))
        resolver.join(2.0)

        assert resolver.is_alive(), (
            'the resolution committed while reconciliation was mid-read; '
            'the observation lock is not being taken'
        )

        resume.set()
        reconciler.join(timeout=60)
        resolver.join(timeout=60)
        assert not reconciler.is_alive() and not resolver.is_alive()
        assert 'error' not in reconcile_box, reconcile_box.get('error')
        assert 'error' not in resolve_box, resolve_box.get('error')

        # It read one consistent state — never an integrity complaint about
        # our own books.
        run = reconcile_box['result']
        assert run.status == ReconciliationRunStatus.COMPLETED
        assert sole_item(run).internal_outcome == (
            ReconciliationOutcome.UNRESOLVED
        )

    def test_entirely_before_reads_unresolved_against_a_success(
        self, funded_wallet, settlement_account, window
    ):
        """The 'before' half alone: unresolved, held, unjournalled."""
        transfer = ambiguous_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)

        run = reconcile([record_for(attempt)], window)()

        item = sole_item(run)
        transfer.refresh_from_db()
        assert item.internal_outcome == ReconciliationOutcome.UNRESOLVED
        assert item.overall_status == ReconciliationStatus.DISCREPANCY
        assert 'outcome_mismatch' in item.discrepancy_codes
        assert transfer.hold.status == HoldStatus.ACTIVE
        assert transfer.financial_transaction.journal_id is None

    def test_entirely_after_reads_matched(
        self, funded_wallet, settlement_account, window
    ):
        """The 'after' half: succeeded, released, journalled — and matched."""
        transfer = ambiguous_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)
        resolve(attempt, settlement_account)()
        transfer.refresh_from_db()

        run = reconcile([record_for(attempt)], window)()

        item = sole_item(run)
        assert item.internal_outcome == ReconciliationOutcome.SUCCEEDED
        assert item.overall_status == ReconciliationStatus.MATCHED
        assert transfer.status == TransactionStatus.SUCCEEDED
        assert transfer.hold.status == HoldStatus.RELEASED
        assert transfer.financial_transaction.journal_id is not None

    def test_a_resolution_already_committed_is_seen_whole(
        self, funded_wallet, settlement_account, window
    ):
        """Pausing at the hold read changes nothing once the answer is in."""
        transfer = ambiguous_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)
        resolve(attempt, settlement_account)()
        reached, resume = threading.Event(), threading.Event()

        reconciler, box = in_thread(
            reconcile(
                [record_for(attempt)], window,
                wrapper=PauseAt(HOLD_TABLE, reached, resume, timeout=1.0),
            )
        )
        assert reached.wait(30)
        resume.set()
        reconciler.join(timeout=60)

        assert 'error' not in box, box.get('error')
        assert sole_item(box['result']).overall_status == (
            ReconciliationStatus.MATCHED
        )


@on_postgres
class TestExecutionCannotTearAnObservation:
    """The same guarantee against M6 finishing a submission mid-run."""

    def test_an_execution_outcome_cannot_land_between_our_reads(
        self, funded_wallet, settlement_account, window
    ):
        transfer = ambiguous_transfer(
            funded_wallet, settlement_account, key='m8-snapshot-exec'
        )
        attempt = provider_attempt_for(transfer)
        reached, resume = threading.Event(), threading.Event()

        reconciler, box = in_thread(
            reconcile(
                [record_for(attempt)], window,
                wrapper=PauseAt(HOLD_TABLE, reached, resume, timeout=8.0),
            )
        )
        assert reached.wait(30)

        settler, settle_box = in_thread(resolve(attempt, settlement_account))
        settler.join(2.0)
        assert settler.is_alive(), 'M6/M7 got past a row this run is holding'

        resume.set()
        reconciler.join(timeout=60)
        settler.join(timeout=60)

        assert 'error' not in box, box.get('error')
        assert 'error' not in settle_box, settle_box.get('error')
        transfer.refresh_from_db()
        assert transfer.status == TransactionStatus.SUCCEEDED

    def test_a_transfer_still_in_submission_reconciles_as_unresolved(
        self, funded_wallet, settlement_account, window
    ):
        """A live submission is in flight, not failed, and reads that way."""
        transfer = prepare_transfer(
            funded_wallet, DESTINATION, 2_000,
            idempotency_key='m8-snapshot-inflight',
        )
        started, release = threading.Event(), threading.Event()
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.SUCCESS
        )

        def on_submit(_request):
            started.set()
            release.wait(20)

        provider.on_submit = on_submit
        executor, exec_box = in_thread(
            lambda: execute_transfer(
                transfer, provider=provider,
                counterpart_account=settlement_account,
            )
        )
        assert started.wait(30), 'the submission never started'

        # Mid-submission: claimed, PROCESSING, hold standing, no journal —
        # and the provider fetch below must not be blocked by it.
        run = reconcile([], window)()

        item = sole_item(run)
        assert item.overall_status == ReconciliationStatus.INTERNAL_ONLY
        assert item.internal_outcome == ReconciliationOutcome.UNRESOLVED

        release.set()
        executor.join(timeout=60)
        assert 'error' not in exec_box, exec_box.get('error')


# ---------------------------------------------------------------------------
# What is locked, and in what order
# ---------------------------------------------------------------------------


@on_postgres
class TestTheLocksTakenAtRuntime:
    """Proved from the SQL actually issued, not from the source."""

    @pytest.fixture
    def spy(self, funded_wallet, settlement_account, window):
        transfer = ambiguous_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)
        recorder = RecordSql()
        thread, box = in_thread(
            reconcile([record_for(attempt)], window, wrapper=recorder)
        )
        thread.join(timeout=60)
        assert 'error' not in box, box.get('error')
        return recorder

    def test_it_locks_something(self, spy):
        """Guards every assertion below from passing vacuously."""
        assert spy.locked_tables(), 'no FOR UPDATE was issued at all'

    def test_it_locks_the_transaction_before_the_attempt(self, spy):
        taken = spy.locked_tables()

        assert taken.index(TRANSACTION_TABLE) < taken.index(ATTEMPT_TABLE)

    def test_it_never_locks_a_wallet(self, spy):
        assert WALLET_TABLE not in spy.locked_tables()

    def test_it_never_locks_a_hold(self, spy):
        assert HOLD_TABLE not in spy.locked_tables()

    def test_it_never_locks_a_journal_or_ledger_account(self, spy):
        taken = spy.locked_tables()

        assert JOURNAL_TABLE not in taken
        assert LEDGER_ACCOUNT_TABLE not in taken

    def test_it_locks_exactly_two_kinds_of_row(self, spy):
        assert set(spy.locked_tables()) == {TRANSACTION_TABLE, ATTEMPT_TABLE}

    def test_it_still_reads_the_hold(self, spy):
        """The hold is read, only never locked — else the above proves little."""
        assert any(
            HOLD_TABLE in sql and 'FOR UPDATE' not in sql.upper()
            for sql in spy.statements
        )

    def test_no_locking_statement_joins_another_table(self, spy):
        """A joined ``FOR UPDATE`` would silently lock the joined rows too."""
        for sql in spy.statements:
            if 'FOR UPDATE' in sql.upper():
                assert ' JOIN ' not in sql.upper(), sql

    def test_locking_is_ordered_by_primary_key_not_by_provider_data(
        self, funded_wallet, settlement_account, window
    ):
        """Deterministic across the run, whatever order the export arrived in."""
        attempts = [
            provider_attempt_for(
                ambiguous_transfer(
                    funded_wallet, settlement_account, key=f'm8-order-{index}'
                )
            )
            for index in range(3)
        ]
        recorder = RecordSql()
        # Records deliberately handed over in reverse.
        thread, box = in_thread(
            reconcile(
                [record_for(a) for a in reversed(attempts)], window,
                wrapper=recorder,
            )
        )
        thread.join(timeout=60)
        assert 'error' not in box, box.get('error')

        locking = [
            sql for sql in recorder.statements
            if 'FOR UPDATE' in sql.upper()
        ]
        assert len(locking) == 2, locking
        for sql in locking:
            assert 'ORDER BY' in sql.upper(), sql
            assert '"id" ASC' in sql, sql


# ---------------------------------------------------------------------------
# No new deadlock
# ---------------------------------------------------------------------------


@on_postgres
class TestTheNewLocksDoNotDeadlock:
    def test_two_runs_over_the_same_window_both_complete(
        self, funded_wallet, settlement_account, window
    ):
        """Same rows, opposite record order — ordering comes from the data."""
        attempts = [
            provider_attempt_for(
                ambiguous_transfer(
                    funded_wallet, settlement_account, key=f'm8-dead-{index}'
                )
            )
            for index in range(4)
        ]
        records = [record_for(a) for a in attempts]
        barrier = threading.Barrier(2, timeout=30)

        def act(ordering):
            def run():
                barrier.wait()
                return reconcile(ordering, window)()

            return run

        threads = [
            in_thread(act(records)),
            in_thread(act(list(reversed(records)))),
        ]
        for thread, _ in threads:
            thread.join(timeout=90)

        for thread, box in threads:
            assert not thread.is_alive(), 'a run hung'
            assert 'error' not in box, box.get('error')
            assert box['result'].status == ReconciliationRunStatus.COMPLETED

    def test_a_run_and_several_resolutions_never_deadlock(
        self, funded_wallet, settlement_account, window
    ):
        """Reconciliation starts one step inside the canonical order.

        M4 takes wallet then transaction; M8 takes transaction then attempt. A
        cycle would need somebody to take them the other way round, and after
        the M6 correction nobody does.
        """
        attempts = [
            provider_attempt_for(
                ambiguous_transfer(
                    funded_wallet, settlement_account, key=f'm8-cycle-{index}'
                )
            )
            for index in range(3)
        ]
        barrier = threading.Barrier(4, timeout=30)

        def reconciling():
            def run():
                barrier.wait()
                return reconcile([record_for(a) for a in attempts], window)()

            return run

        def resolving(attempt):
            def run():
                barrier.wait()
                return resolve(attempt, settlement_account)()

            return run

        threads = [in_thread(reconciling())]
        threads += [in_thread(resolving(a)) for a in attempts]
        for thread, _ in threads:
            thread.join(timeout=90)

        for thread, box in threads:
            assert not thread.is_alive(), 'a thread hung — suspect a deadlock'
            assert 'error' not in box, box.get('error')

    def test_a_run_holds_no_lock_while_the_provider_is_slow(
        self, funded_wallet, settlement_account, window
    ):
        """The fetch still holds nothing, so a slow rail stalls no payment."""
        transfer = ambiguous_transfer(
            funded_wallet, settlement_account, key='m8-slow'
        )
        attempt = provider_attempt_for(transfer)
        listing, release = threading.Event(), threading.Event()
        provider = SimulatorTransferProvider(
            reconciliation_records=[record_for(attempt)]
        )

        def on_list(_window):
            listing.set()
            release.wait(20)

        provider.on_list_records = on_list
        reconciler, box = in_thread(
            lambda: reconcile_transfers(provider=provider, window=window)
        )
        assert listing.wait(30), 'the fetch never started'

        # A resolution must sail straight past a run stuck in its fetch.
        resolver, resolve_box = in_thread(resolve(attempt, settlement_account))
        resolver.join(timeout=30)
        assert not resolver.is_alive(), 'the fetch was holding a row lock'
        assert 'error' not in resolve_box, resolve_box.get('error')

        release.set()
        reconciler.join(timeout=60)
        assert 'error' not in box, box.get('error')


# ---------------------------------------------------------------------------
# The false alarm this exists to prevent
# ---------------------------------------------------------------------------


@on_postgres
class TestLegitimateConcurrencyIsNeverCalledCorruption:
    def _race(self, wallet, counterpart, window, key):
        # Small amounts: a loop of these runs against one 10 000 wallet, and
        # every iteration either spends its hold or leaves it standing.
        attempt = provider_attempt_for(
            ambiguous_transfer(wallet, counterpart, key=key, amount_minor=300)
        )
        barrier = threading.Barrier(2, timeout=30)

        def reconciling():
            barrier.wait()
            return reconcile([record_for(attempt)], window)()

        def resolving():
            barrier.wait()
            return resolve(attempt, counterpart)()

        threads = [in_thread(reconciling), in_thread(resolving)]
        for thread, _ in threads:
            thread.join(timeout=90)
            assert not thread.is_alive(), 'a thread hung'
        return attempt, [box for _, box in threads]

    def test_repeated_races_never_report_an_integrity_failure(
        self, funded_wallet, settlement_account, window
    ):
        """The regression, run often enough that a torn read would surface."""
        errors = []

        for index in range(12):
            _, boxes = self._race(
                funded_wallet, settlement_account, window,
                key=f'm8-regress-{index}',
            )
            errors.extend(box['error'] for box in boxes if 'error' in box)

        integrity = [
            error for error in errors
            if isinstance(error, ReconciliationInternalIntegrityError)
        ]
        assert not integrity, [str(error) for error in integrity]
        assert not errors, [repr(error) for error in errors]

    def test_a_racing_run_records_one_of_exactly_two_answers(
        self, funded_wallet, settlement_account, window
    ):
        """Unresolved or succeeded. There is no third thing to see."""
        seen = set()

        for index in range(8):
            attempt, boxes = self._race(
                funded_wallet, settlement_account, window,
                key=f'm8-two-{index}',
            )
            run = boxes[0]['result']
            seen.add(
                items_for(run)
                .get(provider_record_id=f'rec-{attempt.pk}')
                .internal_outcome
            )

        assert seen <= {
            ReconciliationOutcome.UNRESOLVED, ReconciliationOutcome.SUCCEEDED
        }, seen

    def test_the_ledger_is_untouched_by_all_of_it(
        self, funded_wallet, settlement_account, window
    ):
        """A lock is not a licence: M8 still writes to no financial table."""
        transfer = ambiguous_transfer(
            funded_wallet, settlement_account, key='m8-untouched'
        )
        attempt = provider_attempt_for(transfer)
        txn_id = transfer.financial_transaction_id
        before = FinancialTransaction.objects.get(pk=txn_id)

        reconcile([record_for(attempt)], window)()

        after = FinancialTransaction.objects.get(pk=txn_id)
        assert (
            after.status, after.hold_id, after.journal_id, after.updated_at
        ) == (
            before.status, before.hold_id, before.journal_id, before.updated_at
        )
        assert after.hold.status == HoldStatus.ACTIVE


# ---------------------------------------------------------------------------
# SQLite still has to work
# ---------------------------------------------------------------------------


class TestTheCorrectionIsEngineNeutral:
    """SQLite serialises writers and emits no ``FOR UPDATE``; nothing breaks."""

    def test_a_run_still_completes(
        self, funded_wallet, settlement_account, window
    ):
        transfer = ambiguous_transfer(
            funded_wallet, settlement_account, key='m8-engine'
        )
        attempt = provider_attempt_for(transfer)

        run = reconcile([record_for(attempt)], window)()

        assert run.status == ReconciliationRunStatus.COMPLETED
        assert sole_item(run).internal_outcome == (
            ReconciliationOutcome.UNRESOLVED
        )

    def test_an_empty_window_locks_nothing_and_completes(
        self, funded_wallet, settlement_account
    ):
        """No candidates means no identities, so no locking query at all."""
        now = timezone.now()
        empty = ReconciliationWindow(
            start=now + timedelta(days=1), end=now + timedelta(days=2)
        )
        recorder = RecordSql()
        thread, box = in_thread(reconcile([], empty, wrapper=recorder))
        thread.join(timeout=60)

        assert 'error' not in box, box.get('error')
        assert box['result'].status == ReconciliationRunStatus.COMPLETED
        assert items_for(box['result']).count() == 0
        assert not recorder.locked_tables()
