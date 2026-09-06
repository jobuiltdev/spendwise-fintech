"""Concurrent recovery resolves money exactly once.

Rails redeliver, and an operator may query at the same moment a webhook lands.
Whatever the interleaving, there must be one journal, one hold release and one
terminal outcome — and a contradiction must never mutate a settled result.

Real threads on independent connections, released together from a barrier.
PostgreSQL-only: ``SELECT ... FOR UPDATE`` is a no-op on SQLite.
"""

import threading

import pytest
from django.db import connection, connections

from moneycore.domain.errors import (
    ProviderRecoveryConflictError,
    ProviderWebhookConflictError,
    ProviderWebhookUnmatchedError,
)
from moneycore.domain.holds import HoldStatus
from moneycore.domain.ledger import LedgerAccountType
from moneycore.domain.money import Money
from moneycore.domain.providers import ProviderAttemptStatus, ProviderFailureCode
from moneycore.domain.recovery import RecoveryOutcome, WebhookProcessingStatus
from moneycore.domain.transactions import TransactionStatus
from moneycore.domain.transfers import VerifiedBankAccount
from moneycore.models import (
    Journal,
    ProviderExecutionAttempt,
    ProviderRecoveryEvidence,
    ProviderWebhookEvent,
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
from moneycore.services.provider_recovery import (
    ingest_webhook,
    recover_provider_attempt,
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
        code='internal:m7-concurrency:NGN',
        name='Internal counterpart',
        account_type=LedgerAccountType.ASSET,
        currency='NGN',
    )


@pytest.fixture
def ambiguous(funded_wallet, settlement_account):
    """posted 10 000, held 7 000, transaction UNKNOWN."""
    transfer = prepare_transfer(
        funded_wallet, DESTINATION, 7_000, idempotency_key='m7-race-1'
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


def deliver_webhook(provider, counterpart, **event):
    body, headers = provider.build_webhook(**event)
    return ingest_webhook(
        provider=provider, body=body, headers=headers,
        counterpart_account=counterpart,
    )


@on_postgres
class TestDuplicateWebhookDelivery:
    """The classic: a rail sends the same event several times at once."""

    @pytest.mark.parametrize('count', [2, 5])
    def test_concurrent_duplicates_resolve_once(
        self, ambiguous, settlement_account, count
    ):
        attempt = provider_attempt_for(ambiguous)
        provider = SimulatorTransferProvider()
        before = Journal.objects.count()

        def send(index):
            return deliver_webhook(
                provider, settlement_account,
                provider_event_id='evt-concurrent',
                outcome=RecoveryOutcome.SUCCEEDED,
                client_reference=attempt.client_reference,
            ).pk

        results, errors = run_in_threads(send, count)

        # Identical deliveries are idempotent whatever the timing: a conflict
        # here would mean concurrency had changed what the event meant.
        assert errors == [], errors
        assert Journal.objects.count() == before + 1
        assert ProviderWebhookEvent.objects.count() == 1
        assert ProviderRecoveryEvidence.objects.count() == 1
        assert len(set(results)) == 1

    def test_the_wallet_is_debited_once(self, ambiguous, settlement_account):
        attempt = provider_attempt_for(ambiguous)
        provider = SimulatorTransferProvider()

        def send(index):
            return deliver_webhook(
                provider, settlement_account,
                provider_event_id='evt-debit-once',
                outcome=RecoveryOutcome.SUCCEEDED,
                client_reference=attempt.client_reference,
            ).pk

        run_in_threads(send, 5)

        projection = wallet_balance_projection(ambiguous.wallet)
        assert projection.posted == Money(3_000, 'NGN')
        assert projection.held == Money(0, 'NGN')
        assert projection.available == Money(3_000, 'NGN')

    def test_the_hold_is_released_once(self, ambiguous, settlement_account):
        attempt = provider_attempt_for(ambiguous)
        provider = SimulatorTransferProvider()

        def send(index):
            return deliver_webhook(
                provider, settlement_account,
                provider_event_id='evt-hold-once',
                outcome=RecoveryOutcome.SUCCEEDED,
                client_reference=attempt.client_reference,
            ).pk

        run_in_threads(send, 4)

        ambiguous.hold.refresh_from_db()
        assert ambiguous.hold.status == HoldStatus.RELEASED

    def test_the_attempt_is_never_rewritten(self, ambiguous, settlement_account):
        attempt = provider_attempt_for(ambiguous)
        provider = SimulatorTransferProvider()

        def send(index):
            return deliver_webhook(
                provider, settlement_account,
                provider_event_id='evt-attempt-intact',
                outcome=RecoveryOutcome.SUCCEEDED,
                client_reference=attempt.client_reference,
            ).pk

        run_in_threads(send, 4)

        attempt.refresh_from_db()
        assert attempt.status == ProviderAttemptStatus.UNKNOWN


@on_postgres
class TestConcurrentStatusRecovery:
    def test_two_simultaneous_lookups_resolve_once(
        self, ambiguous, settlement_account
    ):
        provider = SimulatorTransferProvider(
            status_scenario=StatusScenario.SUCCESS
        )
        before = Journal.objects.count()

        def query(index):
            attempt = ProviderExecutionAttempt.objects.get(
                financial_transaction=ambiguous.financial_transaction_id
            )
            return recover_provider_attempt(
                attempt, provider=provider,
                counterpart_account=settlement_account,
            ).pk

        results, errors = run_in_threads(query, 2)

        assert errors == []
        assert Journal.objects.count() == before + 1
        assert wallet_balance_projection(
            ambiguous.wallet
        ).posted == Money(3_000, 'NGN')

    def test_five_simultaneous_lookups_still_post_once(
        self, ambiguous, settlement_account
    ):
        provider = SimulatorTransferProvider(
            status_scenario=StatusScenario.SUCCESS
        )
        before = Journal.objects.count()

        def query(index):
            attempt = ProviderExecutionAttempt.objects.get(
                financial_transaction=ambiguous.financial_transaction_id
            )
            return recover_provider_attempt(
                attempt, provider=provider,
                counterpart_account=settlement_account,
            ).pk

        run_in_threads(query, 5)

        assert Journal.objects.count() == before + 1
        assert Transfer.objects.get(pk=ambiguous.pk).status == (
            TransactionStatus.SUCCEEDED
        )

    def test_concurrent_failure_lookups_release_once(
        self, ambiguous, settlement_account
    ):
        provider = SimulatorTransferProvider(
            status_scenario=StatusScenario.FAILURE
        )
        before = Journal.objects.count()

        def query(index):
            attempt = ProviderExecutionAttempt.objects.get(
                financial_transaction=ambiguous.financial_transaction_id
            )
            return recover_provider_attempt(
                attempt, provider=provider,
                counterpart_account=settlement_account,
            ).pk

        run_in_threads(query, 4)

        ambiguous.hold.refresh_from_db()
        assert Journal.objects.count() == before
        assert ambiguous.hold.status == HoldStatus.RELEASED
        assert wallet_balance_projection(
            ambiguous.wallet
        ).available == Money(10_000, 'NGN')

    def test_repeated_unresolved_lookups_change_nothing(
        self, ambiguous, settlement_account
    ):
        provider = SimulatorTransferProvider(
            status_scenario=StatusScenario.UNRESOLVED
        )
        before = Journal.objects.count()

        def query(index):
            attempt = ProviderExecutionAttempt.objects.get(
                financial_transaction=ambiguous.financial_transaction_id
            )
            return recover_provider_attempt(
                attempt, provider=provider,
                counterpart_account=settlement_account,
            ).pk

        results, errors = run_in_threads(query, 4)

        assert errors == []
        assert Journal.objects.count() == before
        assert wallet_balance_projection(
            ambiguous.wallet
        ).held == Money(7_000, 'NGN')
        assert ProviderRecoveryEvidence.objects.count() == 4


@on_postgres
class TestWebhookRacesStatusQuery:
    """Two sources of truth arriving together."""

    def test_agreeing_success_resolves_once(self, ambiguous, settlement_account):
        attempt = provider_attempt_for(ambiguous)
        webhook_provider = SimulatorTransferProvider()
        status_provider = SimulatorTransferProvider(
            status_scenario=StatusScenario.SUCCESS
        )
        before = Journal.objects.count()

        def act(index):
            if index == 0:
                return deliver_webhook(
                    webhook_provider, settlement_account,
                    provider_event_id='evt-vs-status',
                    outcome=RecoveryOutcome.SUCCEEDED,
                    client_reference=attempt.client_reference,
                ).pk
            fresh = ProviderExecutionAttempt.objects.get(pk=attempt.pk)
            return recover_provider_attempt(
                fresh, provider=status_provider,
                counterpart_account=settlement_account,
            ).pk

        results, errors = run_in_threads(act, 2)

        assert errors == []
        assert Journal.objects.count() == before + 1
        assert Transfer.objects.get(pk=ambiguous.pk).status == (
            TransactionStatus.SUCCEEDED
        )
        assert wallet_balance_projection(
            ambiguous.wallet
        ).posted == Money(3_000, 'NGN')

    def test_agreeing_failure_resolves_once(self, ambiguous, settlement_account):
        attempt = provider_attempt_for(ambiguous)
        webhook_provider = SimulatorTransferProvider()
        status_provider = SimulatorTransferProvider(
            status_scenario=StatusScenario.FAILURE
        )
        before = Journal.objects.count()

        def act(index):
            if index == 0:
                return deliver_webhook(
                    webhook_provider, settlement_account,
                    provider_event_id='evt-fail-vs-status',
                    outcome=RecoveryOutcome.FAILED,
                    client_reference=attempt.client_reference,
                    failure_code=ProviderFailureCode.REQUEST_REJECTED,
                ).pk
            fresh = ProviderExecutionAttempt.objects.get(pk=attempt.pk)
            return recover_provider_attempt(
                fresh, provider=status_provider,
                counterpart_account=settlement_account,
            ).pk

        results, errors = run_in_threads(act, 2)

        ambiguous.hold.refresh_from_db()
        assert errors == []
        assert Journal.objects.count() == before
        assert Transfer.objects.get(pk=ambiguous.pk).status == (
            TransactionStatus.FAILED
        )
        assert ambiguous.hold.status == HoldStatus.RELEASED

    def test_contradictory_evidence_leaves_one_terminal_outcome(
        self, ambiguous, settlement_account
    ):
        """One wins, the other is flagged. Nothing is reversed either way."""
        attempt = provider_attempt_for(ambiguous)
        webhook_provider = SimulatorTransferProvider()
        status_provider = SimulatorTransferProvider(
            status_scenario=StatusScenario.FAILURE
        )
        before = Journal.objects.count()

        def act(index):
            if index == 0:
                return deliver_webhook(
                    webhook_provider, settlement_account,
                    provider_event_id='evt-contradict',
                    outcome=RecoveryOutcome.SUCCEEDED,
                    client_reference=attempt.client_reference,
                ).pk
            fresh = ProviderExecutionAttempt.objects.get(pk=attempt.pk)
            return recover_provider_attempt(
                fresh, provider=status_provider,
                counterpart_account=settlement_account,
            ).pk

        results, errors = run_in_threads(act, 2)

        final = Transfer.objects.get(pk=ambiguous.pk)
        assert final.status in {
            TransactionStatus.SUCCEEDED, TransactionStatus.FAILED
        }
        # At most one journal — a success posts one, a failure posts none.
        assert Journal.objects.count() in {before, before + 1}
        # Whichever lost is a recorded conflict, never a silent discard.
        assert len(errors) <= 1
        for error in errors:
            assert isinstance(error, ProviderRecoveryConflictError)

    def test_the_contradiction_is_retained(self, ambiguous, settlement_account):
        attempt = provider_attempt_for(ambiguous)
        webhook_provider = SimulatorTransferProvider()
        status_provider = SimulatorTransferProvider(
            status_scenario=StatusScenario.FAILURE
        )

        def act(index):
            if index == 0:
                return deliver_webhook(
                    webhook_provider, settlement_account,
                    provider_event_id='evt-contradict-kept',
                    outcome=RecoveryOutcome.SUCCEEDED,
                    client_reference=attempt.client_reference,
                ).pk
            fresh = ProviderExecutionAttempt.objects.get(pk=attempt.pk)
            return recover_provider_attempt(
                fresh, provider=status_provider,
                counterpart_account=settlement_account,
            ).pk

        run_in_threads(act, 2)

        outcomes = {
            e.outcome for e in ProviderRecoveryEvidence.objects.all()
        }
        # Both observations survive, whichever resolved first.
        assert outcomes == {RecoveryOutcome.SUCCEEDED, RecoveryOutcome.FAILED}

    def test_no_journal_is_ever_reversed(self, ambiguous, settlement_account):
        """M2 records a reversal as a *new* journal linked by ``reverses``.

        So the proof is that no such journal exists: recovery never claws back
        posted money on the strength of a later contradicting message.
        """
        attempt = provider_attempt_for(ambiguous)
        webhook_provider = SimulatorTransferProvider()
        status_provider = SimulatorTransferProvider(
            status_scenario=StatusScenario.FAILURE
        )

        def act(index):
            if index == 0:
                return deliver_webhook(
                    webhook_provider, settlement_account,
                    provider_event_id='evt-no-reversal',
                    outcome=RecoveryOutcome.SUCCEEDED,
                    client_reference=attempt.client_reference,
                ).pk
            fresh = ProviderExecutionAttempt.objects.get(pk=attempt.pk)
            return recover_provider_attempt(
                fresh, provider=status_provider,
                counterpart_account=settlement_account,
            ).pk

        run_in_threads(act, 2)

        assert not Journal.objects.exclude(reverses=None).exists()
        assert not Journal.objects.filter(reversed_by__isnull=False).exists()


@on_postgres
class TestInvalidWebhookRacingValidOne:
    def test_the_invalid_delivery_changes_nothing(
        self, ambiguous, settlement_account
    ):
        from moneycore.domain.errors import ProviderWebhookUnauthenticatedError

        attempt = provider_attempt_for(ambiguous)
        provider = SimulatorTransferProvider()
        before = Journal.objects.count()

        def act(index):
            if index == 0:
                return deliver_webhook(
                    provider, settlement_account,
                    provider_event_id='evt-valid',
                    outcome=RecoveryOutcome.SUCCEEDED,
                    client_reference=attempt.client_reference,
                ).pk
            body, _ = provider.build_webhook(
                provider_event_id='evt-forged',
                outcome=RecoveryOutcome.FAILED,
                client_reference=attempt.client_reference,
                failure_code=ProviderFailureCode.REQUEST_REJECTED,
            )
            return ingest_webhook(
                provider=provider, body=body,
                headers={'X-Simulator-Signature': 'forged'},
                counterpart_account=settlement_account,
            ).pk

        results, errors = run_in_threads(act, 2)

        assert len(errors) == 1
        assert isinstance(errors[0], ProviderWebhookUnauthenticatedError)
        assert Journal.objects.count() == before + 1
        assert Transfer.objects.get(pk=ambiguous.pk).status == (
            TransactionStatus.SUCCEEDED
        )
        assert not ProviderWebhookEvent.objects.filter(
            provider_event_id='evt-forged'
        ).exists()


@on_postgres
class TestStartedRecoveryConcurrency:
    def test_concurrent_recovery_of_a_started_attempt_posts_once(
        self, funded_wallet, settlement_account
    ):
        from moneycore.services.provider_execution import _claim_execution

        transfer = prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key='started-race'
        )
        _claim_execution(transfer, 'simulator')
        provider = SimulatorTransferProvider(
            status_scenario=StatusScenario.SUCCESS
        )
        before = Journal.objects.count()

        def query(index):
            attempt = ProviderExecutionAttempt.objects.get(
                financial_transaction=transfer.financial_transaction_id
            )
            return recover_provider_attempt(
                attempt, provider=provider,
                counterpart_account=settlement_account,
            ).pk

        results, errors = run_in_threads(query, 3)

        assert errors == []
        assert Journal.objects.count() == before + 1
        assert Transfer.objects.get(pk=transfer.pk).status == (
            TransactionStatus.SUCCEEDED
        )
        assert provider.submit_call_count == 0

    def test_the_started_attempt_is_never_rewritten(
        self, funded_wallet, settlement_account
    ):
        from moneycore.services.provider_execution import _claim_execution

        transfer = prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key='started-intact'
        )
        _claim_execution(transfer, 'simulator')
        provider = SimulatorTransferProvider(
            status_scenario=StatusScenario.SUCCESS
        )

        def query(index):
            attempt = ProviderExecutionAttempt.objects.get(
                financial_transaction=transfer.financial_transaction_id
            )
            return recover_provider_attempt(
                attempt, provider=provider,
                counterpart_account=settlement_account,
            ).pk

        run_in_threads(query, 3)

        attempt = ProviderExecutionAttempt.objects.get(
            financial_transaction=transfer.financial_transaction_id
        )
        assert attempt.status == ProviderAttemptStatus.STARTED


@on_postgres
class TestNothingIsEverResubmitted:
    def test_no_race_causes_a_submission(self, ambiguous, settlement_account):
        attempt = provider_attempt_for(ambiguous)
        provider = SimulatorTransferProvider(
            status_scenario=StatusScenario.UNRESOLVED
        )

        def act(index):
            if index % 2 == 0:
                return deliver_webhook(
                    provider, settlement_account,
                    provider_event_id=f'evt-noresubmit-{index}',
                    outcome=RecoveryOutcome.UNRESOLVED,
                    client_reference=attempt.client_reference,
                ).pk
            fresh = ProviderExecutionAttempt.objects.get(pk=attempt.pk)
            return recover_provider_attempt(
                fresh, provider=provider,
                counterpart_account=settlement_account,
            ).pk

        run_in_threads(act, 6)

        assert provider.submit_call_count == 0
        assert ProviderExecutionAttempt.objects.count() == 1


@on_postgres
class TestConcurrencyDoesNotChangeWhatAnEventMeans:
    """The correction: an identical delivery is idempotent at any timing.

    Rails redeliver, sometimes simultaneously. Whether two copies of the same
    event arrive one after the other or at the same instant is an accident of
    the network, and it must not change whether SpendWise treats them as the
    same event.

    A conflict is reserved for the genuinely different thing: one event
    identity carrying different contents.
    """

    def _identical(self, attempt, provider, counterpart, count, **overrides):
        event = {
            'provider_event_id': 'evt-identical',
            'outcome': RecoveryOutcome.SUCCEEDED,
            'client_reference': attempt.client_reference,
        }
        event.update(overrides)

        def send(index):
            return deliver_webhook(provider, counterpart, **event).pk

        return run_in_threads(send, count)

    def test_two_identical_deliveries_raise_no_conflict(
        self, ambiguous, settlement_account
    ):
        attempt = provider_attempt_for(ambiguous)
        provider = SimulatorTransferProvider()

        results, errors = self._identical(
            attempt, provider, settlement_account, 2
        )

        assert errors == [], errors
        assert len(results) == 2

    def test_two_identical_deliveries_return_the_same_receipt(
        self, ambiguous, settlement_account
    ):
        attempt = provider_attempt_for(ambiguous)
        provider = SimulatorTransferProvider()

        results, errors = self._identical(
            attempt, provider, settlement_account, 2
        )

        assert errors == []
        assert len(set(results)) == 1
        assert ProviderWebhookEvent.objects.count() == 1

    def test_two_identical_deliveries_resolve_money_once(
        self, ambiguous, settlement_account
    ):
        attempt = provider_attempt_for(ambiguous)
        provider = SimulatorTransferProvider()
        before = Journal.objects.count()

        _, errors = self._identical(attempt, provider, settlement_account, 2)

        assert errors == []
        assert Journal.objects.count() == before + 1
        assert ProviderRecoveryEvidence.objects.count() == 1

    def test_five_identical_deliveries_are_all_accepted(
        self, ambiguous, settlement_account
    ):
        attempt = provider_attempt_for(ambiguous)
        provider = SimulatorTransferProvider()
        before = Journal.objects.count()

        results, errors = self._identical(
            attempt, provider, settlement_account, 5
        )

        assert errors == [], errors
        assert len(results) == 5
        assert len(set(results)) == 1
        assert ProviderWebhookEvent.objects.count() == 1
        assert ProviderRecoveryEvidence.objects.count() == 1
        assert Journal.objects.count() == before + 1

    def test_five_identical_deliveries_release_the_hold_once(
        self, ambiguous, settlement_account
    ):
        attempt = provider_attempt_for(ambiguous)
        provider = SimulatorTransferProvider()

        _, errors = self._identical(attempt, provider, settlement_account, 5)

        ambiguous.hold.refresh_from_db()
        assert errors == []
        assert ambiguous.hold.status == HoldStatus.RELEASED
        assert wallet_balance_projection(
            ambiguous.wallet
        ).posted == Money(3_000, 'NGN')

    def test_identical_failure_deliveries_are_also_idempotent(
        self, ambiguous, settlement_account
    ):
        attempt = provider_attempt_for(ambiguous)
        provider = SimulatorTransferProvider()
        before = Journal.objects.count()

        results, errors = self._identical(
            attempt, provider, settlement_account, 5,
            provider_event_id='evt-identical-failure',
            outcome=RecoveryOutcome.FAILED,
            failure_code=ProviderFailureCode.REQUEST_REJECTED,
        )

        ambiguous.hold.refresh_from_db()
        assert errors == [], errors
        assert len(set(results)) == 1
        assert Journal.objects.count() == before
        assert ambiguous.hold.status == HoldStatus.RELEASED
        assert ProviderWebhookEvent.objects.count() == 1
        assert ProviderRecoveryEvidence.objects.count() == 1
        assert wallet_balance_projection(
            ambiguous.wallet
        ).available == Money(10_000, 'NGN')

    def test_two_identical_failure_deliveries_raise_no_conflict(
        self, ambiguous, settlement_account
    ):
        attempt = provider_attempt_for(ambiguous)
        provider = SimulatorTransferProvider()

        _, errors = self._identical(
            attempt, provider, settlement_account, 2,
            provider_event_id='evt-identical-failure-2',
            outcome=RecoveryOutcome.FAILED,
            failure_code=ProviderFailureCode.REQUEST_REJECTED,
        )

        assert errors == [], errors
        assert ProviderWebhookEvent.objects.count() == 1

    def test_the_transaction_state_survives_the_caught_integrity_error(
        self, ambiguous, settlement_account
    ):
        """No TransactionManagementError: the insert is savepoint-isolated."""
        from django.db.transaction import TransactionManagementError

        attempt = provider_attempt_for(ambiguous)
        provider = SimulatorTransferProvider()

        _, errors = self._identical(attempt, provider, settlement_account, 4)

        assert not any(
            isinstance(error, TransactionManagementError) for error in errors
        )
        assert errors == []

    def test_the_attempt_is_never_rewritten(self, ambiguous, settlement_account):
        attempt = provider_attempt_for(ambiguous)
        provider = SimulatorTransferProvider()

        self._identical(attempt, provider, settlement_account, 4)

        attempt.refresh_from_db()
        assert attempt.status == ProviderAttemptStatus.UNKNOWN


@on_postgres
class TestConflictingContentStillConflicts:
    """Concurrency must not weaken the other half of the distinction."""

    def _deliver_first(self, attempt, provider, counterpart, **overrides):
        event = {
            'provider_event_id': 'evt-shared-identity',
            'outcome': RecoveryOutcome.SUCCEEDED,
            'client_reference': attempt.client_reference,
        }
        event.update(overrides)
        return deliver_webhook(provider, counterpart, **event)

    def test_a_different_outcome_conflicts(self, ambiguous, settlement_account):
        attempt = provider_attempt_for(ambiguous)
        provider = SimulatorTransferProvider()
        self._deliver_first(attempt, provider, settlement_account)

        with pytest.raises(ProviderWebhookConflictError):
            deliver_webhook(
                provider, settlement_account,
                provider_event_id='evt-shared-identity',
                outcome=RecoveryOutcome.FAILED,
                client_reference=attempt.client_reference,
                failure_code=ProviderFailureCode.REQUEST_REJECTED,
            )

    def test_a_different_client_reference_conflicts(
        self, ambiguous, settlement_account
    ):
        attempt = provider_attempt_for(ambiguous)
        provider = SimulatorTransferProvider()
        self._deliver_first(attempt, provider, settlement_account)

        with pytest.raises(ProviderWebhookConflictError):
            deliver_webhook(
                provider, settlement_account,
                provider_event_id='evt-shared-identity',
                outcome=RecoveryOutcome.SUCCEEDED,
                client_reference='SW-somewhere-else',
            )

    def test_a_different_provider_reference_conflicts(
        self, ambiguous, settlement_account
    ):
        attempt = provider_attempt_for(ambiguous)
        provider = SimulatorTransferProvider()
        self._deliver_first(attempt, provider, settlement_account)

        with pytest.raises(ProviderWebhookConflictError):
            deliver_webhook(
                provider, settlement_account,
                provider_event_id='evt-shared-identity',
                outcome=RecoveryOutcome.SUCCEEDED,
                client_reference=attempt.client_reference,
                provider_reference='SIM-different',
            )

    def test_a_different_failure_code_conflicts(
        self, ambiguous, settlement_account
    ):
        attempt = provider_attempt_for(ambiguous)
        provider = SimulatorTransferProvider()
        self._deliver_first(
            attempt, provider, settlement_account,
            provider_event_id='evt-failure-identity',
            outcome=RecoveryOutcome.FAILED,
            failure_code=ProviderFailureCode.REQUEST_REJECTED,
        )

        with pytest.raises(ProviderWebhookConflictError):
            deliver_webhook(
                provider, settlement_account,
                provider_event_id='evt-failure-identity',
                outcome=RecoveryOutcome.FAILED,
                client_reference=attempt.client_reference,
                failure_code=ProviderFailureCode.DESTINATION_REJECTED,
            )

    def test_a_conflict_leaves_the_original_receipt_untouched(
        self, ambiguous, settlement_account
    ):
        attempt = provider_attempt_for(ambiguous)
        provider = SimulatorTransferProvider()
        self._deliver_first(attempt, provider, settlement_account)
        before = Journal.objects.count()

        with pytest.raises(ProviderWebhookConflictError):
            deliver_webhook(
                provider, settlement_account,
                provider_event_id='evt-shared-identity',
                outcome=RecoveryOutcome.SUCCEEDED,
                client_reference='SW-somewhere-else',
            )

        receipt = ProviderWebhookEvent.objects.get(
            provider_event_id='evt-shared-identity'
        )
        assert receipt.client_reference == attempt.client_reference
        assert ProviderWebhookEvent.objects.count() == 1
        assert Journal.objects.count() == before

    def test_concurrent_conflicting_content_still_conflicts(
        self, ambiguous, settlement_account
    ):
        """One identity, two different stories, arriving together.

        Both deliveries name the *same* attempt, so neither can be dismissed as
        unmatched — they differ only in an immutable field. Exactly one may
        win; the other must be told, not silently accepted as a duplicate.
        """
        attempt = provider_attempt_for(ambiguous)
        provider = SimulatorTransferProvider()
        before = Journal.objects.count()
        references = ['SIM-one', 'SIM-two']

        def send(index):
            return deliver_webhook(
                provider, settlement_account,
                provider_event_id='evt-racing-identity',
                outcome=RecoveryOutcome.SUCCEEDED,
                client_reference=attempt.client_reference,
                provider_reference=references[index],
            ).pk

        results, errors = run_in_threads(send, 2)

        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], ProviderWebhookConflictError)
        # One receipt, one evidence row, one financial mutation.
        assert ProviderWebhookEvent.objects.count() == 1
        assert ProviderRecoveryEvidence.objects.count() == 1
        assert Journal.objects.count() == before + 1

    def test_the_winner_of_a_conflicting_race_is_recorded_intact(
        self, ambiguous, settlement_account
    ):
        attempt = provider_attempt_for(ambiguous)
        provider = SimulatorTransferProvider()
        references = ['SIM-one', 'SIM-two']

        def send(index):
            return deliver_webhook(
                provider, settlement_account,
                provider_event_id='evt-racing-intact',
                outcome=RecoveryOutcome.SUCCEEDED,
                client_reference=attempt.client_reference,
                provider_reference=references[index],
            ).pk

        run_in_threads(send, 2)

        receipt = ProviderWebhookEvent.objects.get(
            provider_event_id='evt-racing-intact'
        )
        assert receipt.provider_reference in references
        assert receipt.processing_status == WebhookProcessingStatus.PROCESSED
