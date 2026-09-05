"""Proof that provider I/O never happens with a database transaction open.

This is a non-negotiable architecture invariant, and it needs a connection that
pytest-django has **not** wrapped in its own transaction: the ordinary
``django_db`` fixture runs each test inside an atomic block that is rolled back
afterwards, which would make ``in_atomic_block`` read True no matter what the
code under test does. ``transaction=True`` gives real commits, so the flag
reflects only the service's own transactions.

Why it matters: M3 made the wallet row the serialisation gate for all
spendability. A provider call inside ``transaction.atomic`` would hold that lock
for the duration of somebody else's outage, stalling every other operation on
that customer's money behind a hung socket.
"""

import pytest
from django.db import connection

from moneycore.domain.providers import ProviderAttemptStatus
from moneycore.domain.transactions import TransactionStatus
from moneycore.domain.ledger import LedgerAccountType
from moneycore.domain.transfers import VerifiedBankAccount
from moneycore.models import FinancialTransaction, ProviderExecutionAttempt
from moneycore.providers.simulator import (
    SimulatorTransferProvider,
    TransferScenario,
)
from moneycore.services.ledger import open_ledger_account
from moneycore.services.provider_execution import (
    execute_transfer,
    resolve_transfer_destination,
)
from moneycore.services.transfers import prepare_transfer

pytestmark = pytest.mark.django_db(transaction=True)


DESTINATION = VerifiedBankAccount(
    account_number='0123456789',
    bank_code='SIM-001',
    bank_name='Simulated First Bank',
    account_name='Ada Okafor',
)


@pytest.fixture
def settlement_account(db):
    return open_ledger_account(
        code='internal:m6-boundary-counterpart:NGN',
        name='Internal counterpart',
        account_type=LedgerAccountType.ASSET,
        currency='NGN',
    )


@pytest.fixture
def prepared(funded_wallet):
    return prepare_transfer(
        funded_wallet, DESTINATION, 7_000, idempotency_key='boundary-1'
    )


def provider_for(scenario, **kwargs):
    return SimulatorTransferProvider(transfer_scenario=scenario, **kwargs)


class TestTheAmbientConnectionIsNotWrapped:
    def test_the_test_itself_runs_outside_a_transaction(self):
        """Guard: without this, every proof below would pass vacuously."""
        assert connection.in_atomic_block is False


class TestProviderCallHappensOutsideAnyTransaction:
    """Non-negotiable: no row lock is held across a provider call."""

    def test_no_database_transaction_is_open_during_submit(
        self, prepared, settlement_account
    ):
        observed = {}
        provider = provider_for(TransferScenario.SUCCESS)
        provider.on_submit = lambda request: observed.update(
            in_atomic_block=connection.in_atomic_block
        )

        execute_transfer(
            prepared, provider=provider, counterpart_account=settlement_account
        )

        assert observed['in_atomic_block'] is False

    def test_the_transaction_is_already_committed_as_processing(
        self, prepared, settlement_account
    ):
        """The claim is durable before the call, not merely in flight."""
        observed = {}
        provider = provider_for(TransferScenario.SUCCESS)

        def look(request):
            txn = FinancialTransaction.objects.get(
                pk=prepared.financial_transaction_id
            )
            attempt = ProviderExecutionAttempt.objects.filter(
                financial_transaction=txn
            ).first()
            observed['status'] = txn.status
            observed['attempt_status'] = attempt.status if attempt else None

        provider.on_submit = look

        execute_transfer(
            prepared, provider=provider, counterpart_account=settlement_account
        )

        assert observed['status'] == TransactionStatus.PROCESSING
        assert observed['attempt_status'] == ProviderAttemptStatus.STARTED

    def test_the_boundary_holds_for_every_outcome(
        self, funded_wallet, settlement_account
    ):
        for index, scenario in enumerate((
            TransferScenario.SUCCESS,
            TransferScenario.KNOWN_FAILURE,
            TransferScenario.AMBIGUOUS_AFTER_SUBMISSION,
        )):
            transfer = prepare_transfer(
                funded_wallet, DESTINATION, 1_000,
                idempotency_key=f'boundary-{index}',
            )
            observed = {}
            provider = provider_for(scenario)
            provider.on_submit = lambda request: observed.update(
                in_atomic_block=connection.in_atomic_block
            )

            execute_transfer(
                transfer, provider=provider, counterpart_account=settlement_account
            )

            assert observed['in_atomic_block'] is False, scenario

    def test_account_resolution_opens_no_transaction_either(self):
        from moneycore.services.provider_execution import (
            resolve_transfer_destination,
        )

        observed = {}
        provider = SimulatorTransferProvider()
        original = provider.resolve_bank_account

        def watched(**kwargs):
            observed['in_atomic_block'] = connection.in_atomic_block
            return original(**kwargs)

        provider.resolve_bank_account = watched

        resolve_transfer_destination(
            provider, bank_code='SIM-001', account_number='0000000001'
        )

        assert observed['in_atomic_block'] is False

    def test_the_service_wraps_no_provider_call_in_atomic(self):
        """Source-level companion to the runtime proof above."""
        import inspect

        from moneycore.services import provider_execution

        source = inspect.getsource(provider_execution.execute_transfer)
        assert 'atomic' not in source
        assert 'submit_transfer' in source


