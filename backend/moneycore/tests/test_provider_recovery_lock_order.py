"""Recovery I/O happens outside transactions, and locks in the canonical order.

Both proofs need a connection pytest-django has **not** wrapped in its own
transaction: the ordinary ``django_db`` fixture runs each test inside an atomic
block, which would make ``in_atomic_block`` read True whatever the code does,
and would let one phase's locks mask another's ordering.
"""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from moneycore.domain.ledger import LedgerAccountType
from moneycore.domain.recovery import RecoveryOutcome
from moneycore.domain.transfers import VerifiedBankAccount
from moneycore.models import (
    FinancialTransaction,
    ProviderExecutionAttempt,
    ProviderRecoveryEvidence,
    ProviderWebhookEvent,
)
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
from moneycore.services.provider_recovery import (
    ingest_webhook,
    recover_provider_attempt,
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

WALLET_TABLE = 'moneycore_wallet'
TRANSACTION_TABLE = FinancialTransaction._meta.db_table
ATTEMPT_TABLE = ProviderExecutionAttempt._meta.db_table
EVIDENCE_TABLE = ProviderRecoveryEvidence._meta.db_table
WEBHOOK_TABLE = ProviderWebhookEvent._meta.db_table


@pytest.fixture
def settlement_account(db):
    return open_ledger_account(
        code='internal:m7-lockorder:NGN',
        name='Internal counterpart',
        account_type=LedgerAccountType.ASSET,
        currency='NGN',
    )


@pytest.fixture
def ambiguous(funded_wallet, settlement_account):
    transfer = prepare_transfer(
        funded_wallet, DESTINATION, 7_000, idempotency_key='m7-lock-1'
    )
    execute_transfer(
        transfer,
        provider=SimulatorTransferProvider(
            transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
        ),
        counterpart_account=settlement_account,
    )
    transfer.refresh_from_db()
    return transfer


def locking_order(statements):
    order = []
    for sql in statements:
        lowered = sql.lower()
        if 'for update' not in lowered:
            continue
        for table in (
            WALLET_TABLE, TRANSACTION_TABLE, ATTEMPT_TABLE,
            EVIDENCE_TABLE, WEBHOOK_TABLE,
        ):
            if f'"{table}"' in lowered or f' {table} ' in lowered:
                order.append(table)
                break
    return order


def first_acquisitions(order):
    seen = []
    for table in order:
        if table not in seen:
            seen.append(table)
    return seen


class TestTheAmbientConnectionIsNotWrapped:
    def test_the_test_itself_runs_outside_a_transaction(self):
        """Guard: without this, every proof below would pass vacuously."""
        assert connection.in_atomic_block is False


class TestProviderIoHappensOutsideTransactions:
    """The M6 invariant, extended to both recovery entry points."""

    def test_no_transaction_is_open_during_a_status_lookup(
        self, ambiguous, settlement_account
    ):
        observed = {}
        provider = SimulatorTransferProvider(
            status_scenario=StatusScenario.SUCCESS
        )
        original = provider.get_transfer_status

        def watched(**kwargs):
            observed['in_atomic_block'] = connection.in_atomic_block
            return original(**kwargs)

        provider.get_transfer_status = watched

        recover_provider_attempt(
            provider_attempt_for(ambiguous),
            provider=provider,
            counterpart_account=settlement_account,
        )

        assert observed['in_atomic_block'] is False

    def test_no_transaction_is_open_during_webhook_verification(
        self, ambiguous, settlement_account
    ):
        """Signature checking must never hold a wallet lock."""
        observed = {}
        provider = SimulatorTransferProvider()
        attempt = provider_attempt_for(ambiguous)
        original = provider.verify_and_parse_webhook

        def watched(**kwargs):
            observed['in_atomic_block'] = connection.in_atomic_block
            return original(**kwargs)

        provider.verify_and_parse_webhook = watched
        body, headers = provider.build_webhook(
            provider_event_id='evt-boundary',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference=attempt.client_reference,
        )

        ingest_webhook(
            provider=provider, body=body, headers=headers,
            counterpart_account=settlement_account,
        )

        assert observed['in_atomic_block'] is False

    @pytest.mark.parametrize(
        'scenario',
        [StatusScenario.SUCCESS, StatusScenario.FAILURE,
         StatusScenario.UNRESOLVED],
    )
    def test_the_boundary_holds_for_every_status_outcome(
        self, funded_wallet, settlement_account, scenario
    ):
        transfer = prepare_transfer(
            funded_wallet, DESTINATION, 7_000,
            idempotency_key=f'boundary-{scenario}',
        )
        execute_transfer(
            transfer,
            provider=SimulatorTransferProvider(
                transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
            ),
            counterpart_account=settlement_account,
        )
        observed = {}
        provider = SimulatorTransferProvider(status_scenario=scenario)
        original = provider.get_transfer_status

        def watched(**kwargs):
            observed['in_atomic_block'] = connection.in_atomic_block
            return original(**kwargs)

        provider.get_transfer_status = watched

        recover_provider_attempt(
            provider_attempt_for(transfer),
            provider=provider,
            counterpart_account=settlement_account,
        )

        assert observed['in_atomic_block'] is False, scenario

    def test_the_service_wraps_no_provider_call_in_atomic(self):
        import inspect

        from moneycore.services import provider_recovery

        for function in (
            provider_recovery.recover_provider_attempt,
            provider_recovery.ingest_webhook,
        ):
            source = inspect.getsource(function)
            assert 'atomic' not in source, function.__name__

    def test_neither_database_phase_calls_the_provider(self):
        import inspect

        from moneycore.services import provider_recovery

        for phase in (
            provider_recovery._apply_status_result,
            provider_recovery._apply_matched_webhook,
        ):
            source = inspect.getsource(phase)
            assert 'get_transfer_status' not in source, phase.__name__
            assert 'verify_and_parse_webhook' not in source, phase.__name__


@on_postgres
class TestCanonicalLockOrder:
    """Wallet -> FinancialTransaction -> ProviderExecutionAttempt -> evidence."""

    def test_status_recovery_locks_in_the_canonical_order(
        self, ambiguous, settlement_account
    ):
        with CaptureQueriesContext(connection) as captured:
            recover_provider_attempt(
                provider_attempt_for(ambiguous),
                provider=SimulatorTransferProvider(
                    status_scenario=StatusScenario.SUCCESS
                ),
                counterpart_account=settlement_account,
            )

        order = first_acquisitions(
            locking_order([q['sql'] for q in captured.captured_queries])
        )

        assert order[:3] == [WALLET_TABLE, TRANSACTION_TABLE, ATTEMPT_TABLE]

    def test_webhook_recovery_locks_in_the_canonical_order(
        self, ambiguous, settlement_account
    ):
        provider = SimulatorTransferProvider()
        attempt = provider_attempt_for(ambiguous)
        body, headers = provider.build_webhook(
            provider_event_id='evt-order',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference=attempt.client_reference,
        )

        with CaptureQueriesContext(connection) as captured:
            ingest_webhook(
                provider=provider, body=body, headers=headers,
                counterpart_account=settlement_account,
            )

        order = first_acquisitions(
            locking_order([q['sql'] for q in captured.captured_queries])
        )

        assert order[:3] == [WALLET_TABLE, TRANSACTION_TABLE, ATTEMPT_TABLE]

    def test_the_transaction_is_never_locked_after_the_attempt(
        self, ambiguous, settlement_account
    ):
        with CaptureQueriesContext(connection) as captured:
            recover_provider_attempt(
                provider_attempt_for(ambiguous),
                provider=SimulatorTransferProvider(
                    status_scenario=StatusScenario.SUCCESS
                ),
                counterpart_account=settlement_account,
            )

        order = locking_order([q['sql'] for q in captured.captured_queries])

        assert order.index(TRANSACTION_TABLE) < order.index(ATTEMPT_TABLE)

    def test_evidence_rows_are_never_locked_before_financial_rows(
        self, ambiguous, settlement_account
    ):
        """Prevents inversion against any path that locks financial rows first."""
        with CaptureQueriesContext(connection) as captured:
            recover_provider_attempt(
                provider_attempt_for(ambiguous),
                provider=SimulatorTransferProvider(
                    status_scenario=StatusScenario.SUCCESS
                ),
                counterpart_account=settlement_account,
            )

        order = locking_order([q['sql'] for q in captured.captured_queries])
        financial = {WALLET_TABLE, TRANSACTION_TABLE, ATTEMPT_TABLE}
        evidence = {EVIDENCE_TABLE, WEBHOOK_TABLE}

        seen_evidence = False
        for table in order:
            if table in evidence:
                seen_evidence = True
            elif table in financial:
                assert not seen_evidence, f'inverted order: {order}'

    def test_the_source_takes_the_locks_explicitly(self):
        import inspect

        from moneycore.services import provider_recovery

        source = inspect.getsource(provider_recovery._lock_financial_rows)

        assert source.index('_lock_wallets') < source.index('_lock_transaction')
        assert source.index('_lock_transaction') < source.index(
            'ProviderExecutionAttempt.objects.select_for_update'
        )
