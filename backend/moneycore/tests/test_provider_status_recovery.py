"""Resolving an unresolved transfer by asking the rail what happened.

The financial invariants are the point: a definitive answer resolves the
transaction through M4/M5, an unresolved one changes nothing at all, and the
original attempt is never rewritten either way.
"""

import pytest

from moneycore.domain.errors import (
    ProviderRecoveryNotAllowedError,
    ProviderRecoveryResultInvalidError,
)
from moneycore.domain.holds import HoldStatus
from moneycore.domain.ledger import LedgerAccountType
from moneycore.domain.money import Money
from moneycore.domain.providers import ProviderAttemptStatus, ProviderFailureCode
from moneycore.domain.recovery import EvidenceSource, RecoveryOutcome
from moneycore.domain.transactions import TransactionStatus
from moneycore.domain.transfers import VerifiedBankAccount
from moneycore.models import (
    Journal,
    ProviderExecutionAttempt,
    ProviderRecoveryEvidence,
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
    attempts_awaiting_recovery,
    recover_provider_attempt,
    recovery_evidence_for,
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
        code='internal:m7-counterpart:NGN',
        name='Internal counterpart',
        account_type=LedgerAccountType.ASSET,
        currency='NGN',
    )


def ambiguous_transfer(wallet, counterpart, key='recover-1'):
    """posted 10 000, transfer 7 000, executed into an ambiguous answer."""
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


def started_transfer(wallet, counterpart, key='started-1'):
    """An attempt claimed but never finished — M6's crash window."""
    from moneycore.services.provider_execution import _claim_execution

    transfer = prepare_transfer(wallet, DESTINATION, 7_000, idempotency_key=key)
    _claim_execution(transfer, 'simulator')
    transfer.refresh_from_db()
    return transfer


class TestUnknownResolvedToSuccess:
    """The headline recovery: ambiguity becomes settled truth, once."""

    @pytest.fixture
    def recovered(self, funded_wallet, settlement_account):
        transfer = ambiguous_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)
        provider = SimulatorTransferProvider(
            status_scenario=StatusScenario.SUCCESS
        )
        evidence = recover_provider_attempt(
            attempt, provider=provider, counterpart_account=settlement_account
        )
        transfer.refresh_from_db()
        return transfer, attempt, evidence, provider

    def test_the_transaction_succeeds(self, recovered):
        transfer, _, _, _ = recovered

        assert transfer.status == TransactionStatus.SUCCEEDED

    def test_the_reservation_is_released(self, recovered):
        transfer, _, _, _ = recovered

        transfer.hold.refresh_from_db()
        assert transfer.hold.status == HoldStatus.RELEASED

    def test_exactly_one_journal_is_posted(self, recovered, funded_wallet):
        transfer, _, _, _ = recovered

        assert transfer.journal is not None
        assert Journal.objects.count() == 2  # fixture funding + this transfer

    def test_the_balance_picture_is_exact(self, recovered):
        transfer, _, _, _ = recovered

        projection = wallet_balance_projection(transfer.wallet)
        assert projection.posted == Money(3_000, 'NGN')
        assert projection.held == Money(0, 'NGN')
        assert projection.available == Money(3_000, 'NGN')

    def test_the_attempt_remains_unknown(self, recovered):
        """History is not rewritten. That interaction really did not know."""
        _, attempt, _, _ = recovered

        attempt.refresh_from_db()
        assert attempt.status == ProviderAttemptStatus.UNKNOWN

    def test_the_evidence_says_succeeded(self, recovered):
        _, _, evidence, _ = recovered

        assert evidence.outcome == RecoveryOutcome.SUCCEEDED
        assert evidence.source == EvidenceSource.STATUS_QUERY

    def test_the_story_is_readable_afterwards(self, recovered):
        """Attempt says unknown, evidence says succeeded. Both are true."""
        _, attempt, _, _ = recovered

        history = list(recovery_evidence_for(attempt))
        assert [e.outcome for e in history] == [RecoveryOutcome.SUCCEEDED]
        assert attempt.status == ProviderAttemptStatus.UNKNOWN

    def test_nothing_was_resubmitted(self, recovered):
        _, _, _, provider = recovered

        assert provider.submit_call_count == 0
        assert provider.status_call_count == 1

    def test_no_second_attempt_was_created(self, recovered):
        assert ProviderExecutionAttempt.objects.count() == 1


class TestUnknownResolvedToFailure:
    @pytest.fixture
    def recovered(self, funded_wallet, settlement_account):
        transfer = ambiguous_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)
        provider = SimulatorTransferProvider(
            status_scenario=StatusScenario.FAILURE,
            status_failure_code=ProviderFailureCode.DESTINATION_REJECTED,
        )
        evidence = recover_provider_attempt(
            attempt, provider=provider, counterpart_account=settlement_account
        )
        transfer.refresh_from_db()
        return transfer, attempt, evidence, provider

    def test_the_transaction_fails(self, recovered):
        transfer, _, _, _ = recovered

        assert transfer.status == TransactionStatus.FAILED
        assert transfer.financial_transaction.failure_code == 'destination_rejected'

    def test_the_reservation_is_released(self, recovered):
        transfer, _, _, _ = recovered

        transfer.hold.refresh_from_db()
        assert transfer.hold.status == HoldStatus.RELEASED

    def test_no_journal_is_posted(self, recovered):
        transfer, _, _, _ = recovered

        assert transfer.journal is None
        assert Journal.objects.count() == 1  # only the fixture funding

    def test_the_funds_are_restored(self, recovered):
        transfer, _, _, _ = recovered

        projection = wallet_balance_projection(transfer.wallet)
        assert projection.posted == Money(10_000, 'NGN')
        assert projection.held == Money(0, 'NGN')
        assert projection.available == Money(10_000, 'NGN')

    def test_the_attempt_remains_unknown(self, recovered):
        _, attempt, _, _ = recovered

        attempt.refresh_from_db()
        assert attempt.status == ProviderAttemptStatus.UNKNOWN

    def test_the_evidence_carries_the_normalised_code(self, recovered):
        _, _, evidence, _ = recovered

        assert evidence.outcome == RecoveryOutcome.FAILED
        assert evidence.failure_code == 'destination_rejected'
        assert evidence.failure_code in ProviderFailureCode.ALL

    def test_the_transfer_is_preserved(self, recovered):
        transfer, _, _, _ = recovered

        assert Transfer.objects.filter(pk=transfer.pk).exists()
        assert transfer.destination_account_number == '0123456789'


class TestUnknownStaysUnresolved:
    """A lookup that cannot tell us changes nothing whatsoever."""

    @pytest.fixture
    def recovered(self, funded_wallet, settlement_account):
        transfer = ambiguous_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)
        provider = SimulatorTransferProvider(
            status_scenario=StatusScenario.UNRESOLVED
        )
        evidence = recover_provider_attempt(
            attempt, provider=provider, counterpart_account=settlement_account
        )
        transfer.refresh_from_db()
        return transfer, attempt, evidence

    def test_the_transaction_stays_unknown(self, recovered):
        transfer, _, _ = recovered

        assert transfer.status == TransactionStatus.UNKNOWN

    def test_the_reservation_stays_active(self, recovered):
        transfer, _, _ = recovered

        transfer.hold.refresh_from_db()
        assert transfer.hold.status == HoldStatus.ACTIVE

    def test_nothing_is_posted(self, recovered):
        transfer, _, _ = recovered

        assert transfer.journal is None
        assert Journal.objects.count() == 1

    def test_the_balance_is_untouched(self, recovered):
        transfer, _, _ = recovered

        projection = wallet_balance_projection(transfer.wallet)
        assert projection.posted == Money(10_000, 'NGN')
        assert projection.held == Money(7_000, 'NGN')
        assert projection.available == Money(3_000, 'NGN')

    def test_the_asking_is_still_recorded(self, recovered):
        """That SpendWise looked and could not be told is worth keeping."""
        _, _, evidence = recovered

        assert evidence.outcome == RecoveryOutcome.UNRESOLVED
        assert evidence.failure_code == ''

    def test_a_not_found_answer_is_also_unresolved(
        self, funded_wallet, settlement_account
    ):
        """Never a failure: it establishes nothing about a request that may
        never have arrived."""
        transfer = ambiguous_transfer(
            funded_wallet, settlement_account, key='not-found'
        )
        attempt = provider_attempt_for(transfer)

        evidence = recover_provider_attempt(
            attempt,
            provider=SimulatorTransferProvider(
                status_scenario=StatusScenario.NOT_FOUND
            ),
            counterpart_account=settlement_account,
        )
        transfer.refresh_from_db()
        transfer.hold.refresh_from_db()

        assert evidence.outcome == RecoveryOutcome.UNRESOLVED
        assert transfer.status == TransactionStatus.UNKNOWN
        assert transfer.hold.status == HoldStatus.ACTIVE

    def test_repeated_unresolved_lookups_change_nothing(
        self, funded_wallet, settlement_account
    ):
        """Asking is observational, so it is safe to repeat."""
        transfer = ambiguous_transfer(
            funded_wallet, settlement_account, key='repeat-unresolved'
        )
        attempt = provider_attempt_for(transfer)
        provider = SimulatorTransferProvider(
            status_scenario=StatusScenario.UNRESOLVED
        )

        for _ in range(3):
            recover_provider_attempt(
                attempt, provider=provider,
                counterpart_account=settlement_account,
            )

        transfer.refresh_from_db()
        assert transfer.status == TransactionStatus.UNKNOWN
        assert Journal.objects.count() == 1
        assert recovery_evidence_for(attempt).count() == 3
        assert provider.submit_call_count == 0


class TestStartedRecovery:
    """M6's crash window: claimed, maybe submitted, never finished."""

    def test_the_starting_state_is_what_m6_leaves(
        self, funded_wallet, settlement_account
    ):
        transfer = started_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)

        assert attempt.status == ProviderAttemptStatus.STARTED
        assert transfer.status == TransactionStatus.PROCESSING
        assert transfer.hold.status == HoldStatus.ACTIVE

    def test_a_definitive_success_resolves_it(
        self, funded_wallet, settlement_account
    ):
        transfer = started_transfer(
            funded_wallet, settlement_account, key='started-success'
        )
        attempt = provider_attempt_for(transfer)

        recover_provider_attempt(
            attempt,
            provider=SimulatorTransferProvider(
                status_scenario=StatusScenario.SUCCESS
            ),
            counterpart_account=settlement_account,
        )
        transfer.refresh_from_db()
        transfer.hold.refresh_from_db()

        assert transfer.status == TransactionStatus.SUCCEEDED
        assert transfer.hold.status == HoldStatus.RELEASED
        assert wallet_balance_projection(
            transfer.wallet
        ).posted == Money(3_000, 'NGN')

    def test_a_definitive_failure_resolves_it(
        self, funded_wallet, settlement_account
    ):
        transfer = started_transfer(
            funded_wallet, settlement_account, key='started-failure'
        )
        attempt = provider_attempt_for(transfer)

        recover_provider_attempt(
            attempt,
            provider=SimulatorTransferProvider(
                status_scenario=StatusScenario.FAILURE
            ),
            counterpart_account=settlement_account,
        )
        transfer.refresh_from_db()
        transfer.hold.refresh_from_db()

        assert transfer.status == TransactionStatus.FAILED
        assert transfer.hold.status == HoldStatus.RELEASED
        assert transfer.journal is None
        assert wallet_balance_projection(
            transfer.wallet
        ).available == Money(10_000, 'NGN')

    def test_an_unresolved_lookup_leaves_it_processing(
        self, funded_wallet, settlement_account
    ):
        transfer = started_transfer(
            funded_wallet, settlement_account, key='started-unresolved'
        )
        attempt = provider_attempt_for(transfer)

        recover_provider_attempt(
            attempt,
            provider=SimulatorTransferProvider(
                status_scenario=StatusScenario.UNRESOLVED
            ),
            counterpart_account=settlement_account,
        )
        transfer.refresh_from_db()
        attempt.refresh_from_db()

        projection = wallet_balance_projection(transfer.wallet)
        assert transfer.status == TransactionStatus.PROCESSING
        assert attempt.status == ProviderAttemptStatus.STARTED
        assert projection.posted == Money(10_000, 'NGN')
        assert projection.held == Money(7_000, 'NGN')
        assert projection.available == Money(3_000, 'NGN')
        assert Journal.objects.count() == 1

    def test_an_unresolved_lookup_does_not_turn_started_into_unknown(
        self, funded_wallet, settlement_account
    ):
        """They record different historical facts and are not interchangeable."""
        transfer = started_transfer(
            funded_wallet, settlement_account, key='started-not-unknown'
        )
        attempt = provider_attempt_for(transfer)

        recover_provider_attempt(
            attempt,
            provider=SimulatorTransferProvider(
                status_scenario=StatusScenario.UNRESOLVED
            ),
            counterpart_account=settlement_account,
        )

        attempt.refresh_from_db()
        assert attempt.status == ProviderAttemptStatus.STARTED
        assert attempt.status != ProviderAttemptStatus.UNKNOWN

    def test_the_attempt_stays_started_even_after_definitive_success(
        self, funded_wallet, settlement_account
    ):
        """Chosen model: the attempt records the interaction, evidence the rest."""
        transfer = started_transfer(
            funded_wallet, settlement_account, key='started-history'
        )
        attempt = provider_attempt_for(transfer)

        recover_provider_attempt(
            attempt,
            provider=SimulatorTransferProvider(
                status_scenario=StatusScenario.SUCCESS
            ),
            counterpart_account=settlement_account,
        )

        attempt.refresh_from_db()
        assert attempt.status == ProviderAttemptStatus.STARTED
        assert attempt.finished_at is None
        assert recovery_evidence_for(attempt).get().outcome == (
            RecoveryOutcome.SUCCEEDED
        )

    def test_started_is_never_read_as_a_failure(
        self, funded_wallet, settlement_account
    ):
        transfer = started_transfer(
            funded_wallet, settlement_account, key='started-not-failed'
        )
        attempt = provider_attempt_for(transfer)

        assert attempt.may_have_been_submitted
        assert transfer.status != TransactionStatus.FAILED


class TestRecoveryPreconditions:
    def test_a_settled_attempt_cannot_be_recovered(
        self, funded_wallet, settlement_account
    ):
        transfer = prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key='settled'
        )
        execute_transfer(
            transfer,
            provider=SimulatorTransferProvider(
                transfer_scenario=TransferScenario.SUCCESS
            ),
            counterpart_account=settlement_account,
        )
        attempt = provider_attempt_for(transfer)

        with pytest.raises(ProviderRecoveryNotAllowedError):
            recover_provider_attempt(
                attempt,
                provider=SimulatorTransferProvider(
                    status_scenario=StatusScenario.SUCCESS
                ),
                counterpart_account=settlement_account,
            )

    def test_a_failed_attempt_cannot_be_recovered(
        self, funded_wallet, settlement_account
    ):
        transfer = prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key='settled-failed'
        )
        execute_transfer(
            transfer,
            provider=SimulatorTransferProvider(
                transfer_scenario=TransferScenario.KNOWN_FAILURE
            ),
            counterpart_account=settlement_account,
        )
        attempt = provider_attempt_for(transfer)

        with pytest.raises(ProviderRecoveryNotAllowedError):
            recover_provider_attempt(
                attempt,
                provider=SimulatorTransferProvider(
                    status_scenario=StatusScenario.SUCCESS
                ),
                counterpart_account=settlement_account,
            )

    def test_a_nonsense_status_result_is_refused(
        self, funded_wallet, settlement_account
    ):
        transfer = ambiguous_transfer(
            funded_wallet, settlement_account, key='broken-status'
        )
        attempt = provider_attempt_for(transfer)

        class BrokenProvider:
            provider_key = 'broken'

            def get_transfer_status(self, **kwargs):
                return None

        with pytest.raises(ProviderRecoveryResultInvalidError):
            recover_provider_attempt(
                attempt, provider=BrokenProvider(),
                counterpart_account=settlement_account,
            )

    def test_a_broken_lookup_changes_nothing(
        self, funded_wallet, settlement_account
    ):
        transfer = ambiguous_transfer(
            funded_wallet, settlement_account, key='broken-noop'
        )
        attempt = provider_attempt_for(transfer)

        class BrokenProvider:
            provider_key = 'broken'

            def get_transfer_status(self, **kwargs):
                return True

        with pytest.raises(ProviderRecoveryResultInvalidError):
            recover_provider_attempt(
                attempt, provider=BrokenProvider(),
                counterpart_account=settlement_account,
            )

        transfer.refresh_from_db()
        transfer.hold.refresh_from_db()
        assert transfer.status == TransactionStatus.UNKNOWN
        assert transfer.hold.status == HoldStatus.ACTIVE
        assert ProviderRecoveryEvidence.objects.count() == 0

    def test_the_sweep_finds_unresolved_attempts(
        self, funded_wallet, settlement_account
    ):
        ambiguous_transfer(funded_wallet, settlement_account, key='sweep-1')

        awaiting = list(attempts_awaiting_recovery())

        assert len(awaiting) == 1
        assert awaiting[0].status == ProviderAttemptStatus.UNKNOWN

    def test_the_sweep_excludes_settled_attempts(
        self, funded_wallet, settlement_account
    ):
        transfer = prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key='sweep-settled'
        )
        execute_transfer(
            transfer,
            provider=SimulatorTransferProvider(),
            counterpart_account=settlement_account,
        )

        assert attempts_awaiting_recovery().count() == 0


class TestIdempotentRecovery:
    """Same-outcome evidence after resolution is a safe no-op."""

    def test_a_repeated_success_lookup_does_not_post_twice(
        self, funded_wallet, settlement_account
    ):
        transfer = ambiguous_transfer(
            funded_wallet, settlement_account, key='idem-success'
        )
        attempt = provider_attempt_for(transfer)
        provider = SimulatorTransferProvider(
            status_scenario=StatusScenario.SUCCESS
        )

        recover_provider_attempt(
            attempt, provider=provider, counterpart_account=settlement_account
        )
        attempt.refresh_from_db()
        recover_provider_attempt(
            attempt, provider=provider, counterpart_account=settlement_account
        )

        transfer.refresh_from_db()
        assert transfer.status == TransactionStatus.SUCCEEDED
        assert Journal.objects.count() == 2
        assert wallet_balance_projection(
            transfer.wallet
        ).posted == Money(3_000, 'NGN')

    def test_both_observations_are_kept(self, funded_wallet, settlement_account):
        """Append-only: the second look is a second row, not an overwrite."""
        transfer = ambiguous_transfer(
            funded_wallet, settlement_account, key='idem-history'
        )
        attempt = provider_attempt_for(transfer)
        provider = SimulatorTransferProvider(
            status_scenario=StatusScenario.SUCCESS
        )

        recover_provider_attempt(
            attempt, provider=provider, counterpart_account=settlement_account
        )
        attempt.refresh_from_db()
        recover_provider_attempt(
            attempt, provider=provider, counterpart_account=settlement_account
        )

        assert recovery_evidence_for(attempt).count() == 2

    def test_a_repeated_failure_lookup_releases_once(
        self, funded_wallet, settlement_account
    ):
        transfer = ambiguous_transfer(
            funded_wallet, settlement_account, key='idem-failure'
        )
        attempt = provider_attempt_for(transfer)
        provider = SimulatorTransferProvider(
            status_scenario=StatusScenario.FAILURE
        )

        recover_provider_attempt(
            attempt, provider=provider, counterpart_account=settlement_account
        )
        attempt.refresh_from_db()
        recover_provider_attempt(
            attempt, provider=provider, counterpart_account=settlement_account
        )

        transfer.refresh_from_db()
        transfer.hold.refresh_from_db()
        assert transfer.status == TransactionStatus.FAILED
        assert transfer.hold.status == HoldStatus.RELEASED
        assert Journal.objects.count() == 1
