"""The ambiguous outcome, which is the reason this milestone exists.

A rail that stops answering after the request went out tells us nothing about
whether money moved. Recording that as failure is how a system tells a customer
their money did not move when it did — so it must map to UNKNOWN, keep the
reservation, post nothing, and above all resubmit nothing.
"""

import pytest

from moneycore.domain.errors import ProviderExecutionAlreadyStartedError
from moneycore.domain.holds import HoldStatus
from moneycore.domain.ledger import LedgerAccountType
from moneycore.domain.money import Money
from moneycore.domain.providers import (
    ProviderAttemptStatus,
    ProviderFailureCode,
)
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

pytestmark = pytest.mark.django_db


DESTINATION = VerifiedBankAccount(
    account_number='0123456789',
    bank_code='SIM-001',
    bank_name='Simulated First Bank',
    account_name='Ada Okafor',
)


@pytest.fixture
def settlement_account(db):
    return open_ledger_account(
        code='internal:m6-unknown-counterpart:NGN',
        name='Internal counterpart',
        account_type=LedgerAccountType.ASSET,
        currency='NGN',
    )


@pytest.fixture
def ambiguous(funded_wallet, settlement_account):
    """posted 10 000, transfer 7 000, executed into an ambiguous answer."""
    transfer = prepare_transfer(
        funded_wallet, DESTINATION, 7_000, idempotency_key='unknown-1'
    )
    provider = SimulatorTransferProvider(
        transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
    )
    executed = execute_transfer(
        transfer, provider=provider, counterpart_account=settlement_account
    )
    return executed, provider


class TestTheAmbiguousOutcome:
    def test_the_attempt_records_unknown(self, ambiguous):
        transfer, _ = ambiguous
        attempt = provider_attempt_for(transfer)

        assert attempt.status == ProviderAttemptStatus.UNKNOWN
        assert attempt.finished_at is not None

    def test_the_transaction_is_unknown(self, ambiguous):
        transfer, _ = ambiguous

        assert transfer.status == TransactionStatus.UNKNOWN

    def test_the_reservation_stays_active(self, ambiguous):
        """The single most important assertion in the milestone."""
        transfer, _ = ambiguous

        transfer.hold.refresh_from_db()
        assert transfer.hold.status == HoldStatus.ACTIVE
        assert transfer.hold.released_at is None

    def test_nothing_is_posted(self, ambiguous):
        transfer, _ = ambiguous

        assert transfer.journal is None
        assert Journal.objects.count() == 1  # only the fixture funding

    def test_the_balance_picture_is_unchanged(self, ambiguous):
        transfer, _ = ambiguous

        projection = wallet_balance_projection(transfer.wallet)
        assert projection.posted == Money(10_000, 'NGN')
        assert projection.held == Money(7_000, 'NGN')
        assert projection.available == Money(3_000, 'NGN')

    def test_the_transfer_is_preserved(self, ambiguous):
        transfer, _ = ambiguous

        assert Transfer.objects.filter(pk=transfer.pk).exists()
        assert transfer.destination_account_number == '0123456789'

    def test_the_transaction_is_not_terminal(self, ambiguous):
        """Unresolved, not concluded. It is still waiting for the truth."""
        transfer, _ = ambiguous

        assert not transfer.financial_transaction.is_terminal
        assert transfer.financial_transaction.resolved_at is None


class TestAmbiguityIsNeverAFailure:
    def test_the_attempt_carries_no_failure_code(self, ambiguous):
        transfer, _ = ambiguous

        assert provider_attempt_for(transfer).failure_code == ''

    def test_the_transaction_carries_no_failure_code(self, ambiguous):
        transfer, _ = ambiguous

        assert transfer.financial_transaction.failure_code == ''

    def test_the_ambiguity_reason_is_a_separate_column(self, ambiguous):
        transfer, _ = ambiguous
        attempt = provider_attempt_for(transfer)

        assert attempt.ambiguity_reason == 'response_not_received'
        assert attempt.ambiguity_reason not in ProviderFailureCode.ALL

    def test_the_orm_refuses_an_ambiguous_row_a_failure_code(self, ambiguous):
        from moneycore.domain.errors import ProviderAttemptImmutableError

        transfer, _ = ambiguous
        attempt = provider_attempt_for(transfer)

        with pytest.raises(ProviderAttemptImmutableError):
            ProviderExecutionAttempt.objects.filter(pk=attempt.pk).update(
                failure_code='request_rejected'
            )

    def test_the_database_itself_refuses_it_too(self, ambiguous):
        """Past the ORM guard entirely: the check constraint is the real one."""
        from django.db import IntegrityError, connection
        from django.db import transaction as db_transaction

        transfer, _ = ambiguous
        attempt = provider_attempt_for(transfer)

        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        'UPDATE moneycore_providerexecutionattempt '
                        'SET failure_code = %s WHERE id = %s',
                        ['request_rejected', attempt.pk],
                    )

    def test_no_code_path_maps_a_timeout_to_failure(self):
        """There is no failure code that could express one."""
        assert 'timeout' not in ProviderFailureCode.ALL
        assert not any(
            'timeout' in code or 'unknown_response' in code
            for code in ProviderFailureCode.ALL
        )

    def test_the_service_never_calls_fail_for_an_unknown_result(self):
        import inspect

        from moneycore.services import provider_execution

        source = inspect.getsource(provider_execution._apply_outcome)
        unknown_branch = source.split('else:')[-1]

        assert 'mark_transfer_unknown' in unknown_branch
        assert 'fail_transfer' not in unknown_branch


class TestNoRetryAfterAmbiguity:
    def test_the_rail_was_asked_exactly_once(self, ambiguous):
        _, provider = ambiguous

        assert provider.submit_call_count == 1

    def test_no_second_attempt_was_created(self, ambiguous):
        assert ProviderExecutionAttempt.objects.count() == 1

    def test_a_second_execute_is_refused_not_resubmitted(
        self, ambiguous, settlement_account
    ):
        transfer, provider = ambiguous

        with pytest.raises(ProviderExecutionAlreadyStartedError):
            execute_transfer(
                transfer, provider=provider,
                counterpart_account=settlement_account,
            )

        assert provider.submit_call_count == 1
        assert ProviderExecutionAttempt.objects.count() == 1

    def test_the_refusal_changes_nothing(self, ambiguous, settlement_account):
        transfer, provider = ambiguous
        before = wallet_balance_projection(transfer.wallet)

        with pytest.raises(ProviderExecutionAlreadyStartedError):
            execute_transfer(
                transfer, provider=provider,
                counterpart_account=settlement_account,
            )

        transfer.refresh_from_db()
        transfer.hold.refresh_from_db()
        assert wallet_balance_projection(transfer.wallet) == before
        assert transfer.status == TransactionStatus.UNKNOWN
        assert transfer.hold.status == HoldStatus.ACTIVE

    def test_the_service_submits_from_exactly_one_place(self):
        """A second submission path is what a retry would need, and there is none.

        Counted against executable code only — the module docstring draws the
        call in its diagram of the three phases.
        """
        import ast
        from pathlib import Path

        from moneycore.services import provider_execution

        tree = ast.parse(
            Path(provider_execution.__file__).read_text(encoding='utf-8')
        )
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'submit_transfer'
        ]

        assert len(calls) == 1

    def test_the_service_contains_no_looping_or_sleeping_construct(self):
        """Checked against code, not prose: the docstring says "no retry"."""
        from pathlib import Path

        from moneycore.services import provider_execution

        lines = [
            line for line in
            Path(provider_execution.__file__)
            .read_text(encoding='utf-8').splitlines()
            if line.strip() and not line.strip().startswith('#')
        ]
        code = chr(10).join(lines)

        for forbidden in (
            'while ', 'time.sleep', 'for attempt in range', 'reschedule(',
        ):
            assert forbidden not in code, forbidden

    def test_no_resolution_or_polling_entry_point_exists(self):
        from moneycore.services import provider_execution

        for forbidden in (
            'resolve_unknown', 'retry_unknown', 'check_transfer_status',
            'poll_provider', 'sweep_unknown', 'recover',
        ):
            assert not hasattr(provider_execution, forbidden), forbidden


class TestTheAttemptRecordsAnObservation:
    def test_a_finished_unknown_attempt_is_not_reopened(self, ambiguous):
        """Later evidence is M7's business, not an edit of this row."""
        from moneycore.domain.errors import ProviderAttemptImmutableError

        transfer, _ = ambiguous
        attempt = provider_attempt_for(transfer)

        attempt.status = ProviderAttemptStatus.SUCCEEDED
        with pytest.raises(ProviderAttemptImmutableError):
            attempt.save()

    def test_the_stored_observation_is_unchanged_after_a_refused_edit(
        self, ambiguous
    ):
        from moneycore.domain.errors import ProviderAttemptImmutableError

        transfer, _ = ambiguous
        attempt = provider_attempt_for(transfer)

        attempt.status = ProviderAttemptStatus.SUCCEEDED
        with pytest.raises(ProviderAttemptImmutableError):
            attempt.save()

        attempt.refresh_from_db()
        assert attempt.status == ProviderAttemptStatus.UNKNOWN

    def test_unknown_is_terminal_for_the_attempt_but_not_the_transaction(
        self, ambiguous
    ):
        transfer, _ = ambiguous
        attempt = provider_attempt_for(transfer)

        assert attempt.is_finished
        assert not transfer.financial_transaction.is_terminal

    def test_the_attempt_says_it_may_have_been_submitted(self, ambiguous):
        transfer, _ = ambiguous

        assert provider_attempt_for(transfer).may_have_been_submitted


class TestTheDemoStory:
    """The three deterministic demonstrations M6 must make possible."""

    def _run(self, wallet, counterpart, scenario, key, amount=7_000):
        transfer = prepare_transfer(
            wallet, DESTINATION, amount, idempotency_key=key
        )
        provider = SimulatorTransferProvider(transfer_scenario=scenario)
        return execute_transfer(
            transfer, provider=provider, counterpart_account=counterpart
        )

    def test_success_reduces_the_balance(self, funded_wallet, settlement_account):
        transfer = self._run(
            funded_wallet, settlement_account, TransferScenario.SUCCESS, 'demo-a'
        )

        assert transfer.status == TransactionStatus.SUCCEEDED
        assert transfer.journal is not None
        assert wallet_balance_projection(
            funded_wallet
        ).available == Money(3_000, 'NGN')

    def test_known_failure_restores_the_balance(
        self, funded_wallet, settlement_account
    ):
        transfer = self._run(
            funded_wallet, settlement_account,
            TransferScenario.KNOWN_FAILURE, 'demo-b',
        )

        transfer.hold.refresh_from_db()
        assert transfer.status == TransactionStatus.FAILED
        assert transfer.hold.status == HoldStatus.RELEASED
        assert wallet_balance_projection(
            funded_wallet
        ).available == Money(10_000, 'NGN')

    def test_ambiguity_keeps_the_funds_reserved(
        self, funded_wallet, settlement_account
    ):
        transfer = self._run(
            funded_wallet, settlement_account,
            TransferScenario.AMBIGUOUS_AFTER_SUBMISSION, 'demo-c',
        )

        transfer.hold.refresh_from_db()
        assert transfer.status == TransactionStatus.UNKNOWN
        assert transfer.hold.status == HoldStatus.ACTIVE
        assert transfer.journal is None
        assert wallet_balance_projection(
            funded_wallet
        ).available == Money(3_000, 'NGN')

    def test_each_demo_is_reproducible(self, funded_wallet, settlement_account):
        """Same scenario, same outcome, every time."""
        first = self._run(
            funded_wallet, settlement_account, TransferScenario.SUCCESS,
            'repeat-1', amount=3_000,
        )
        second = self._run(
            funded_wallet, settlement_account, TransferScenario.SUCCESS,
            'repeat-2', amount=3_000,
        )

        assert first.status == second.status == TransactionStatus.SUCCEEDED
        assert provider_attempt_for(first).status == (
            provider_attempt_for(second).status
        )
