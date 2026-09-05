"""The canonical lock order, proved rather than documented.

    Wallet rows (ascending PK) -> FinancialTransaction -> ProviderExecutionAttempt

The order matters beyond this module. A future recovery sweep may legitimately
lock a transaction and its attempt *without* needing the wallet at all — and if
outcome application took the attempt first, those two paths would form a cycle.
The wallet lock happens to serialise M6's own callers, so an inversion here
would not show up in M6's own concurrency tests; it would surface later, in M7,
as an intermittent deadlock. Hence these tests.
"""

import threading

import pytest
from django.db import connection, connections, transaction as db_transaction
from django.test.utils import CaptureQueriesContext

from moneycore.domain.ledger import LedgerAccountType
from moneycore.domain.money import Money
from moneycore.domain.providers import ProviderAttemptStatus
from moneycore.domain.transactions import TransactionStatus
from moneycore.domain.transfers import VerifiedBankAccount
from moneycore.models import (
    FinancialTransaction,
    ProviderExecutionAttempt,
    Transfer,
)
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
    reason='SELECT ... FOR UPDATE is a no-op on SQLite, so order is unobservable.',
)


DESTINATION = VerifiedBankAccount(
    account_number='0123456789',
    bank_code='SIM-001',
    bank_name='Simulated First Bank',
    account_name='Ada Okafor',
)

TRANSACTION_TABLE = FinancialTransaction._meta.db_table
ATTEMPT_TABLE = ProviderExecutionAttempt._meta.db_table
WALLET_TABLE = 'moneycore_wallet'


@pytest.fixture
def settlement_account(db):
    return open_ledger_account(
        code='internal:m6-lockorder-counterpart:NGN',
        name='Internal counterpart',
        account_type=LedgerAccountType.ASSET,
        currency='NGN',
    )


@pytest.fixture
def prepared(funded_wallet):
    return prepare_transfer(
        funded_wallet, DESTINATION, 7_000, idempotency_key='lockorder-1'
    )


def locking_order(statements):
    """The tables locked FOR UPDATE, in the order the statements were issued."""
    order = []
    for sql in statements:
        lowered = sql.lower()
        if 'for update' not in lowered:
            continue
        for table in (WALLET_TABLE, TRANSACTION_TABLE, ATTEMPT_TABLE):
            if f'"{table}"' in lowered or f' {table} ' in lowered:
                order.append(table)
                break
    return order


def execute_capturing_phases(transfer, provider, counterpart):
    """Run one execution, splitting the SQL by which database phase issued it.

    **Scoping matters.** The claim phase and the outcome phase are separate
    database transactions, so their locks are unrelated: the claim commits and
    releases everything before the provider is called. Capturing the whole call
    and looking at first acquisitions would let the claim phase's transaction
    lock mask an inversion in the outcome phase — which is exactly the defect
    being corrected here. The provider call is the boundary, so the simulator's
    own hook is where the split is taken.
    """
    marker = {}
    original = provider.on_submit

    def split(request):
        marker['at'] = len(captured.captured_queries)
        if original is not None:
            original(request)

    provider.on_submit = split

    with CaptureQueriesContext(connection) as captured:
        execute_transfer(
            transfer, provider=provider, counterpart_account=counterpart
        )

    statements = [query['sql'] for query in captured.captured_queries]
    split_at = marker['at']
    return statements[:split_at], statements[split_at:]


# ---------------------------------------------------------------------------
# Runtime proof
# ---------------------------------------------------------------------------


@on_postgres
class TestTheOrderIsWhatTheSqlSays:
    """Spied on the actual statements, scoped to the phase that issued them."""

    def test_the_outcome_phase_locks_wallet_then_transaction_then_attempt(
        self, prepared, settlement_account
    ):
        """The correction. Previously the attempt was locked before the
        transaction, which was only reached later inside M4's nested service."""
        _, outcome = execute_capturing_phases(
            prepared, SimulatorTransferProvider(), settlement_account
        )

        order = locking_order(outcome)
        first_seen = []
        for table in order:
            if table not in first_seen:
                first_seen.append(table)

        assert first_seen == [WALLET_TABLE, TRANSACTION_TABLE, ATTEMPT_TABLE]

    def test_the_outcome_phase_never_takes_the_attempt_first(
        self, prepared, settlement_account
    ):
        _, outcome = execute_capturing_phases(
            prepared, SimulatorTransferProvider(), settlement_account
        )

        order = locking_order(outcome)
        assert ATTEMPT_TABLE in order, 'the attempt was never locked'
        assert order.index(TRANSACTION_TABLE) < order.index(ATTEMPT_TABLE)

    def test_the_claim_phase_locks_wallet_then_transaction(
        self, prepared, settlement_account
    ):
        claim, _ = execute_capturing_phases(
            prepared, SimulatorTransferProvider(), settlement_account
        )

        order = locking_order(claim)
        first_seen = []
        for table in order:
            if table not in first_seen:
                first_seen.append(table)

        assert first_seen[:2] == [WALLET_TABLE, TRANSACTION_TABLE]

    def test_the_claim_phase_locks_the_transaction_before_inserting_the_attempt(
        self, prepared, settlement_account
    ):
        claim, _ = execute_capturing_phases(
            prepared, SimulatorTransferProvider(), settlement_account
        )

        lowered = [sql.lower() for sql in claim]
        insert_at = next(
            index for index, sql in enumerate(lowered)
            if sql.startswith('insert into') and ATTEMPT_TABLE in sql
        )
        lock_at = next(
            index for index, sql in enumerate(lowered)
            if 'for update' in sql and TRANSACTION_TABLE in sql
        )

        assert lock_at < insert_at

    def test_nothing_new_is_locked_after_the_attempt(
        self, prepared, settlement_account
    ):
        """So the attempt is always the tail of any ordering chain."""
        _, outcome = execute_capturing_phases(
            prepared, SimulatorTransferProvider(), settlement_account
        )

        order = locking_order(outcome)
        after_attempt = order[order.index(ATTEMPT_TABLE) + 1:]

        assert set(after_attempt) <= {WALLET_TABLE, TRANSACTION_TABLE}

    def test_the_nested_m4_service_only_relocks_rows_already_held(
        self, prepared, settlement_account
    ):
        """The interaction with M4/M5, stated as a property rather than assumed.

        Every FOR UPDATE issued after the attempt lock targets the same
        transaction row this phase already holds, so PostgreSQL grants it
        immediately and no new ordering edge is created.
        """
        transaction_id = prepared.financial_transaction_id

        _, outcome = execute_capturing_phases(
            prepared, SimulatorTransferProvider(), settlement_account
        )

        locking_statements = [
            sql for sql in outcome if 'FOR UPDATE' in sql.upper()
        ]
        attempt_at = next(
            index for index, sql in enumerate(locking_statements)
            if ATTEMPT_TABLE in sql
        )

        for sql in locking_statements[attempt_at + 1:]:
            if TRANSACTION_TABLE in sql:
                assert str(transaction_id) in sql, sql

    @pytest.mark.parametrize(
        'scenario',
        [
            TransferScenario.SUCCESS,
            TransferScenario.KNOWN_FAILURE,
            TransferScenario.AMBIGUOUS_AFTER_SUBMISSION,
        ],
    )
    def test_the_order_holds_for_every_outcome(
        self, funded_wallet, settlement_account, scenario
    ):
        transfer = prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key=f'order-{scenario}'
        )

        _, outcome = execute_capturing_phases(
            transfer,
            SimulatorTransferProvider(transfer_scenario=scenario),
            settlement_account,
        )

        order = locking_order(outcome)
        assert order.index(WALLET_TABLE) < order.index(TRANSACTION_TABLE)
        assert order.index(TRANSACTION_TABLE) < order.index(ATTEMPT_TABLE)


@on_postgres
class TestTheSourceTakesBothLocksExplicitly:
    """Engine-independent companion: neither lock is left to a nested call."""

    def test_both_phases_lock_the_transaction_themselves(self):
        import inspect

        from moneycore.services import provider_execution

        for phase in (
            provider_execution._claim_execution,
            provider_execution._apply_outcome,
        ):
            source = inspect.getsource(phase)
            assert '_lock_wallets' in source, phase.__name__
            assert '_lock_transaction' in source, phase.__name__

    def test_the_outcome_phase_locks_the_transaction_before_the_attempt(self):
        import inspect

        from moneycore.services import provider_execution

        source = inspect.getsource(provider_execution._apply_outcome)

        assert source.index('_lock_wallets') < source.index('_lock_transaction')
        assert source.index('_lock_transaction') < source.index(
            'ProviderExecutionAttempt.objects.select_for_update'
        )


# ---------------------------------------------------------------------------
# Deadlock regression against a recovery-shaped path
# ---------------------------------------------------------------------------


@on_postgres
class TestNoInversionAgainstARecoveryShapedPath:
    """The scenario M7 will actually create.

    A recovery sweep finds an attempt by status and locks the transaction and
    the attempt — it does not need the wallet. Raced against outcome
    application, the old order (wallet -> attempt -> transaction) could
    deadlock: outcome holds the attempt and wants the transaction, while
    recovery holds the transaction and wants the attempt.

    PostgreSQL detects such a cycle and raises, so "no exception" is a real
    result rather than an absence of evidence.
    """

    def _recovery_shaped_lock(self, transaction_id, attempt_id, hold_for):
        """Transaction, then attempt — the canonical order, minus the wallet."""
        with db_transaction.atomic():
            FinancialTransaction.objects.select_for_update().get(pk=transaction_id)
            hold_for.wait(timeout=20)
            ProviderExecutionAttempt.objects.select_for_update().get(pk=attempt_id)

    def test_outcome_application_does_not_deadlock_with_recovery(
        self, prepared, settlement_account
    ):
        in_call = threading.Event()
        recovery_holds_transaction = threading.Event()
        errors = []

        provider = SimulatorTransferProvider()

        def announce(request):
            # The attempt row is committed and no lock is held at this point.
            in_call.set()
            recovery_holds_transaction.wait(timeout=20)

        provider.on_submit = announce

        def run_execution():
            try:
                execute_transfer(
                    Transfer.objects.get(pk=prepared.pk),
                    provider=provider,
                    counterpart_account=settlement_account,
                )
            except Exception as exc:  # noqa: BLE001 - recorded for assertion
                errors.append(('execution', exc))
            finally:
                connections.close_all()

        def run_recovery():
            try:
                in_call.wait(timeout=20)
                attempt = ProviderExecutionAttempt.objects.get(
                    financial_transaction=prepared.financial_transaction_id
                )
                with db_transaction.atomic():
                    FinancialTransaction.objects.select_for_update().get(
                        pk=prepared.financial_transaction_id
                    )
                    # Let execution proceed while we hold the transaction.
                    recovery_holds_transaction.set()
                    ProviderExecutionAttempt.objects.select_for_update().get(
                        pk=attempt.pk
                    )
            except Exception as exc:  # noqa: BLE001 - recorded for assertion
                errors.append(('recovery', exc))
            finally:
                connections.close_all()

        threads = [
            threading.Thread(target=run_execution),
            threading.Thread(target=run_recovery),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert not any(thread.is_alive() for thread in threads), 'a thread hung'
        assert errors == [], errors

    def test_the_transfer_still_completed_correctly(
        self, prepared, settlement_account
    ):
        """The race must not merely avoid deadlock — it must still be right."""
        in_call = threading.Event()
        recovery_done = threading.Event()
        provider = SimulatorTransferProvider()

        def announce(request):
            in_call.set()
            recovery_done.wait(timeout=20)

        provider.on_submit = announce

        def run_execution():
            try:
                execute_transfer(
                    Transfer.objects.get(pk=prepared.pk),
                    provider=provider,
                    counterpart_account=settlement_account,
                )
            finally:
                connections.close_all()

        def run_recovery():
            try:
                in_call.wait(timeout=20)
                attempt = ProviderExecutionAttempt.objects.get(
                    financial_transaction=prepared.financial_transaction_id
                )
                with db_transaction.atomic():
                    FinancialTransaction.objects.select_for_update().get(
                        pk=prepared.financial_transaction_id
                    )
                    ProviderExecutionAttempt.objects.select_for_update().get(
                        pk=attempt.pk
                    )
                recovery_done.set()
            finally:
                connections.close_all()

        threads = [
            threading.Thread(target=run_execution),
            threading.Thread(target=run_recovery),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        final = Transfer.objects.get(pk=prepared.pk)
        assert final.status == TransactionStatus.SUCCEEDED
        assert provider_attempt_for(final).status == (
            ProviderAttemptStatus.SUCCEEDED
        )
        assert provider.submit_call_count == 1
        assert wallet_balance_projection(
            final.wallet
        ).posted == Money(3_000, 'NGN')

    @pytest.mark.parametrize(
        'scenario',
        [
            TransferScenario.KNOWN_FAILURE,
            TransferScenario.AMBIGUOUS_AFTER_SUBMISSION,
        ],
    )
    def test_the_other_outcomes_survive_the_same_race(
        self, funded_wallet, settlement_account, scenario
    ):
        transfer = prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key=f'race-{scenario}'
        )
        in_call = threading.Event()
        recovery_done = threading.Event()
        errors = []
        provider = SimulatorTransferProvider(transfer_scenario=scenario)

        def announce(request):
            in_call.set()
            recovery_done.wait(timeout=20)

        provider.on_submit = announce

        def run_execution():
            try:
                execute_transfer(
                    Transfer.objects.get(pk=transfer.pk),
                    provider=provider,
                    counterpart_account=settlement_account,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connections.close_all()

        def run_recovery():
            try:
                in_call.wait(timeout=20)
                attempt = ProviderExecutionAttempt.objects.get(
                    financial_transaction=transfer.financial_transaction_id
                )
                with db_transaction.atomic():
                    FinancialTransaction.objects.select_for_update().get(
                        pk=transfer.financial_transaction_id
                    )
                    ProviderExecutionAttempt.objects.select_for_update().get(
                        pk=attempt.pk
                    )
                recovery_done.set()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [
            threading.Thread(target=run_execution),
            threading.Thread(target=run_recovery),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert errors == [], errors
        assert provider.submit_call_count == 1
