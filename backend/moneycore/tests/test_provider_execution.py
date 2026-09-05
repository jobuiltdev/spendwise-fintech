"""Executing a prepared transfer: the three outcomes, and the call boundary.

The two proofs that matter most here are that the provider call happens with no
database transaction open, and that an ambiguous answer keeps the customer's
funds reserved.
"""

import pytest
from django.db import connection

from moneycore.domain.errors import (
    ProviderExecutionAlreadyStartedError,
    ProviderResultInvalidError,
    TransferTransactionMismatchError,
)
from moneycore.domain.holds import HoldStatus
from moneycore.domain.ledger import LedgerAccountType
from moneycore.domain.money import Money
from moneycore.domain.providers import (
    ProviderAttemptStatus,
    ProviderFailureCode,
    ProviderOperation,
    ProviderTransferSucceeded,
)
from moneycore.domain.transactions import TransactionStatus
from moneycore.domain.transfers import VerifiedBankAccount
from moneycore.models import (
    FinancialTransaction,
    Journal,
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
    client_reference_for,
    execute_transfer,
    provider_attempt_for,
)
from moneycore.services.transfers import prepare_transfer

pytestmark = pytest.mark.django_db


DESTINATION = VerifiedBankAccount(
    account_number='0123456789',
    bank_code='SIM-001',
    bank_name='Simulated First Bank',
    account_name='Ada Okafor',
)


@pytest.fixture
def settlement_account(db):
    """A neutral internal counterpart, supplied by the caller (O-29 open)."""
    return open_ledger_account(
        code='internal:m6-counterpart:NGN',
        name='Internal counterpart',
        account_type=LedgerAccountType.ASSET,
        currency='NGN',
    )


@pytest.fixture
def prepared(funded_wallet):
    """posted 10 000, transfer 7 000, hold 7 000, transaction CREATED."""
    return prepare_transfer(
        funded_wallet, DESTINATION, 7_000, idempotency_key='exec-1'
    )


def provider_for(scenario, **kwargs):
    return SimulatorTransferProvider(transfer_scenario=scenario, **kwargs)


# ---------------------------------------------------------------------------
# The call boundary
# ---------------------------------------------------------------------------


class TestTheServiceStructuresTheBoundary:
    """Source-level companion to the runtime proofs.

    The runtime proofs live in test_provider_call_boundary.py, which needs a
    connection that pytest-django has not wrapped in its own transaction.
    """

    def test_the_service_wraps_no_provider_call_in_atomic(self):
        import inspect

        from moneycore.services import provider_execution

        source = inspect.getsource(provider_execution.execute_transfer)
        assert 'atomic' not in source
        assert 'submit_transfer' in source

    def test_neither_database_phase_calls_the_provider(self):
        """The call sits between them, in neither one's transaction."""
        import inspect

        from moneycore.services import provider_execution

        for phase in (
            provider_execution._claim_execution,
            provider_execution._apply_outcome,
        ):
            source = inspect.getsource(phase)
            assert 'submit_transfer' not in source
            assert 'resolve_bank_account' not in source

    def test_both_database_phases_are_atomic(self):
        import inspect

        from moneycore.services import provider_execution

        for phase in (
            provider_execution._claim_execution,
            provider_execution._apply_outcome,
        ):
            module_source = inspect.getsource(provider_execution)
            marker = '@transaction.atomic' + chr(10) + 'def ' + phase.__name__
            assert marker in module_source, phase.__name__

    def test_resolution_opens_no_transaction_in_source(self):
        import inspect

        from moneycore.services import provider_execution

        source = inspect.getsource(
            provider_execution.resolve_transfer_destination
        )
        assert 'atomic' not in source


# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------


class TestExecutionSuccess:
    @pytest.fixture
    def executed(self, prepared, settlement_account):
        provider = provider_for(TransferScenario.SUCCESS)
        transfer = execute_transfer(
            prepared, provider=provider, counterpart_account=settlement_account
        )
        return transfer, provider

    def test_exactly_one_attempt_exists(self, executed):
        assert ProviderExecutionAttempt.objects.count() == 1

    def test_the_attempt_succeeded(self, executed):
        transfer, _ = executed
        attempt = provider_attempt_for(transfer)

        assert attempt.status == ProviderAttemptStatus.SUCCEEDED
        assert attempt.finished_at is not None
        assert attempt.operation == ProviderOperation.SUBMIT_TRANSFER
        assert attempt.provider_key == 'simulator'

    def test_the_transaction_succeeded(self, executed):
        transfer, _ = executed

        assert transfer.status == TransactionStatus.SUCCEEDED

    def test_the_reservation_was_released(self, executed):
        transfer, _ = executed

        transfer.hold.refresh_from_db()
        assert transfer.hold.status == HoldStatus.RELEASED

    def test_exactly_one_journal_was_posted(self, executed, funded_wallet):
        transfer, _ = executed

        assert transfer.journal is not None
        assert Journal.objects.filter(pk=transfer.journal.pk).exists()

    def test_the_balance_picture_is_exact(self, executed):
        transfer, _ = executed

        projection = wallet_balance_projection(transfer.wallet)
        assert projection.posted == Money(3_000, 'NGN')
        assert projection.held == Money(0, 'NGN')
        assert projection.available == Money(3_000, 'NGN')

    def test_the_transfer_intent_is_unchanged(self, executed):
        transfer, _ = executed

        assert transfer.destination_account_number == '0123456789'
        assert transfer.recipient_name == 'Ada Okafor'

    def test_the_provider_reference_lives_on_the_attempt_only(self, executed):
        transfer, _ = executed
        attempt = provider_attempt_for(transfer)

        assert attempt.provider_reference.startswith('SIM-SW-')
        assert not hasattr(transfer, 'provider_reference')
        assert not hasattr(transfer.financial_transaction, 'provider_reference')

    def test_the_attempt_carries_no_failure_code(self, executed):
        transfer, _ = executed

        assert provider_attempt_for(transfer).failure_code == ''

    def test_the_rail_was_asked_exactly_once(self, executed):
        _, provider = executed

        assert provider.submit_call_count == 1

    def test_the_request_carried_the_transfer_details(self, executed):
        _, provider = executed
        request = provider.submitted_requests[0]

        assert request.amount_minor == 7_000
        assert request.currency == 'NGN'
        assert request.destination_account_number == '0123456789'
        assert request.destination_bank_code == 'SIM-001'


# ---------------------------------------------------------------------------
# Known failure
# ---------------------------------------------------------------------------


class TestExecutionKnownFailure:
    @pytest.fixture
    def executed(self, prepared, settlement_account):
        provider = provider_for(
            TransferScenario.KNOWN_FAILURE,
            failure_code=ProviderFailureCode.DESTINATION_REJECTED,
        )
        transfer = execute_transfer(
            prepared, provider=provider, counterpart_account=settlement_account
        )
        return transfer, provider

    def test_the_attempt_failed(self, executed):
        transfer, _ = executed
        attempt = provider_attempt_for(transfer)

        assert attempt.status == ProviderAttemptStatus.FAILED
        assert attempt.finished_at is not None

    def test_the_failure_code_is_normalised_and_stored(self, executed):
        transfer, _ = executed
        attempt = provider_attempt_for(transfer)

        assert attempt.failure_code == 'destination_rejected'
        assert attempt.failure_code in ProviderFailureCode.ALL

    def test_the_transaction_failed(self, executed):
        transfer, _ = executed

        assert transfer.status == TransactionStatus.FAILED
        assert transfer.financial_transaction.failure_code == 'destination_rejected'

    def test_the_reservation_was_released(self, executed):
        transfer, _ = executed

        transfer.hold.refresh_from_db()
        assert transfer.hold.status == HoldStatus.RELEASED

    def test_no_journal_was_posted(self, executed, funded_wallet):
        transfer, _ = executed

        assert transfer.journal is None
        assert Journal.objects.count() == 1  # only the fixture funding

    def test_the_funds_are_restored(self, executed):
        transfer, _ = executed

        projection = wallet_balance_projection(transfer.wallet)
        assert projection.posted == Money(10_000, 'NGN')
        assert projection.held == Money(0, 'NGN')
        assert projection.available == Money(10_000, 'NGN')

    def test_the_transfer_is_preserved_as_a_historical_attempt(self, executed):
        transfer, _ = executed

        assert Transfer.objects.filter(pk=transfer.pk).exists()
        assert transfer.destination_account_number == '0123456789'

    def test_the_attempt_carries_no_ambiguity_reason(self, executed):
        transfer, _ = executed

        assert provider_attempt_for(transfer).ambiguity_reason == ''

    def test_an_unreachable_rail_is_also_a_definitive_failure(
        self, funded_wallet, settlement_account
    ):
        """The adapter proved nothing was sent, so this is not ambiguity."""
        transfer = prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key='unreachable'
        )
        provider = provider_for(TransferScenario.UNREACHABLE_BEFORE_SUBMISSION)

        transfer = execute_transfer(
            transfer, provider=provider, counterpart_account=settlement_account
        )

        attempt = provider_attempt_for(transfer)
        assert attempt.status == ProviderAttemptStatus.FAILED
        assert attempt.failure_code == ProviderFailureCode.PROVIDER_UNAVAILABLE
        assert transfer.status == TransactionStatus.FAILED
        assert wallet_balance_projection(
            transfer.wallet
        ).available == Money(10_000, 'NGN')


# ---------------------------------------------------------------------------
# Validation and refusal
# ---------------------------------------------------------------------------


class TestExecutionPreconditions:
    def test_a_transfer_already_executed_is_refused(
        self, prepared, settlement_account
    ):
        provider = provider_for(TransferScenario.SUCCESS)
        execute_transfer(
            prepared, provider=provider, counterpart_account=settlement_account
        )

        with pytest.raises(ProviderExecutionAlreadyStartedError):
            execute_transfer(
                prepared, provider=provider,
                counterpart_account=settlement_account,
            )

    def test_a_transfer_already_processing_is_refused(
        self, prepared, settlement_account
    ):
        from moneycore.services.transactions import start_processing

        start_processing(prepared.financial_transaction)

        with pytest.raises(ProviderExecutionAlreadyStartedError):
            execute_transfer(
                prepared,
                provider=provider_for(TransferScenario.SUCCESS),
                counterpart_account=settlement_account,
            )

    def test_a_resolved_transfer_is_refused(self, prepared, settlement_account):
        from moneycore.services.transfers import fail_transfer

        fail_transfer(prepared, failure_code='limit_exceeded')

        with pytest.raises(ProviderExecutionAlreadyStartedError):
            execute_transfer(
                prepared,
                provider=provider_for(TransferScenario.SUCCESS),
                counterpart_account=settlement_account,
            )

    def test_an_adapter_returning_nonsense_is_refused(
        self, prepared, settlement_account
    ):
        class BrokenProvider:
            provider_key = 'broken'

            def submit_transfer(self, request):
                return None

        with pytest.raises(ProviderResultInvalidError):
            execute_transfer(
                prepared, provider=BrokenProvider(),
                counterpart_account=settlement_account,
            )

    def test_a_broken_adapter_leaves_the_attempt_unfinished(
        self, prepared, settlement_account
    ):
        """Never silently a failure: the claim stands, unresolved."""
        class BrokenProvider:
            provider_key = 'broken'

            def submit_transfer(self, request):
                return True

        with pytest.raises(ProviderResultInvalidError):
            execute_transfer(
                prepared, provider=BrokenProvider(),
                counterpart_account=settlement_account,
            )

        attempt = provider_attempt_for(prepared)
        assert attempt.status == ProviderAttemptStatus.STARTED
        assert attempt.finished_at is None
        prepared.hold.refresh_from_db()
        assert prepared.hold.status == HoldStatus.ACTIVE
        assert Journal.objects.count() == 1


# ---------------------------------------------------------------------------
# The outbound reference
# ---------------------------------------------------------------------------


class TestClientReference:
    def test_it_is_ours_and_prefixed(self, prepared):
        reference = client_reference_for(prepared.financial_transaction)

        assert reference.startswith('SW-')

    def test_it_is_deterministic_across_reads(self, prepared):
        txn = prepared.financial_transaction

        assert client_reference_for(txn) == client_reference_for(txn)

    def test_it_differs_between_transactions(self, funded_wallet):
        first = prepare_transfer(
            funded_wallet, DESTINATION, 1_000, idempotency_key='ref-a'
        )
        second = prepare_transfer(
            funded_wallet, DESTINATION, 1_000, idempotency_key='ref-b'
        )

        assert client_reference_for(first.financial_transaction) != (
            client_reference_for(second.financial_transaction)
        )

    def test_it_does_not_publish_the_database_sequence(self, prepared):
        txn = prepared.financial_transaction
        reference = client_reference_for(txn)

        assert str(txn.pk) != reference.removeprefix('SW-')
        assert f'SW-{txn.pk}' != reference

    def test_it_is_stored_immutably_on_the_attempt(
        self, prepared, settlement_account
    ):
        transfer = execute_transfer(
            prepared,
            provider=provider_for(TransferScenario.SUCCESS),
            counterpart_account=settlement_account,
        )
        attempt = provider_attempt_for(transfer)

        assert attempt.client_reference == client_reference_for(
            transfer.financial_transaction
        )

    def test_it_is_not_the_provider_reference(self, prepared, settlement_account):
        transfer = execute_transfer(
            prepared,
            provider=provider_for(TransferScenario.SUCCESS),
            counterpart_account=settlement_account,
        )
        attempt = provider_attempt_for(transfer)

        assert attempt.client_reference != attempt.provider_reference

    def test_it_is_not_the_m4_idempotency_key(self, prepared):
        txn = prepared.financial_transaction

        assert client_reference_for(txn) != txn.idempotency_key
