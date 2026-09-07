"""Six end-to-end scenarios through the locked money core.

Every other test file in this package proves one milestone's rules in
isolation. These six read the other way round: they follow a single customer's
money from a funded wallet to a settled ledger, through whichever of M1-M8 the
story actually touches, and assert what the customer's balance did.

They exist because a system can pass every unit boundary and still be wrong
where the parts meet. The scenarios that matter are the ones where the seams
are:

1. money leaves, once
2. the rail says no, and the reservation comes back
3. **the rail says nothing** — and the answer, arriving later, is yes
4. the rail says nothing, and the answer, arriving later, is no
5. the provider's own file agrees with what we concluded
6. the provider's own file disagrees, and our books do not move

Nothing here fabricates state. Opening funds are posted as real double-entry
through M2; every transition goes through the public M4/M5/M6/M7 service that
owns it; the provider is the deterministic simulator. No test writes a status,
a hold, or a journal directly — that is asserted in
:class:`TestTheScenariosUseTheRealCore` at the bottom of the file, and by the
per-milestone boundary suites these scenarios deliberately do not duplicate.

Counts are asserted as carefully as statuses. "Exactly one journal" and
"exactly one submission" are the two facts that separate a payment system from
a plausible-looking one, and ambiguity is where both are lost.
"""

import ast
from datetime import timedelta
from pathlib import Path

import pytest
from django.utils import timezone

from moneycore.domain.errors import ProviderExecutionAlreadyStartedError
from moneycore.domain.holds import HoldStatus
from moneycore.domain.ledger import EntryDirection
from moneycore.domain.money import Money
from moneycore.domain.providers import ProviderAttemptStatus
from moneycore.domain.reconciliation import (
    ReconciliationOutcome,
    ReconciliationRunStatus,
    ReconciliationStatus,
    ReconciliationWindow,
)
from moneycore.domain.recovery import RecoveryOutcome
from moneycore.domain.transactions import TransactionStatus
from moneycore.domain.transfers import VerifiedBankAccount
from moneycore.models import (
    Journal,
    JournalEntry,
    ProviderExecutionAttempt,
    ProviderRecoveryEvidence,
)
from moneycore.providers.simulator import (
    SimulatorTransferProvider,
    StatusScenario,
    TransferScenario,
)
from moneycore.services.holds import wallet_balance_projection
from moneycore.services.provider_execution import (
    execute_transfer,
    provider_attempt_for,
)
from moneycore.services.provider_recovery import recover_provider_attempt
from moneycore.services.reconciliation import (
    items_for,
    reconcile_transfers,
    run_summary,
)
from moneycore.services.transfers import prepare_transfer

pytestmark = pytest.mark.django_db

#: The destination every scenario pays. M5's verified-destination snapshot,
#: built directly: these scenarios are about what happens to *our* money, and
#: rehearsing account resolution in each one would bury that.
DESTINATION = VerifiedBankAccount(
    account_number='0123456789',
    bank_code='SIM-001',
    bank_name='Simulated First Bank',
    account_name='Ada Okafor',
)

OPENING = 10_000
AMOUNT = 7_000
REMAINING = OPENING - AMOUNT


# ---------------------------------------------------------------------------
# Shared reading, not shared doing
# ---------------------------------------------------------------------------
#
# The helpers below only *observe*. Every state change in these scenarios is
# written out in the test that depends on it, so each one reads as the business
# flow rather than as a call to something that hides the transition.


def balances(wallet):
    """``(posted, held, available)`` as plain minor units, for readability."""
    projection = wallet_balance_projection(wallet)
    return (
        projection.posted.minor_units,
        projection.held.minor_units,
        projection.available.minor_units,
    )


def wallet_effect(wallet_account):
    """The net signed effect of every posted entry on the wallet's account.

    Derived from the ledger rather than from a balance helper, so a scenario
    can assert the *journal* moved the money and not merely that some
    projection agrees.
    """
    total = 0
    for entry in JournalEntry.objects.filter(ledger_account=wallet_account):
        signed = (
            entry.amount_minor
            if entry.direction == EntryDirection.CREDIT
            else -entry.amount_minor
        )
        total += signed
    return total


def window_around_now():
    now = timezone.now()
    return ReconciliationWindow(
        start=now - timedelta(hours=1), end=now + timedelta(hours=1)
    )


def matching_record(attempt, *, amount_minor, record_id='rec-1'):
    """A provider record naming this exact execution, by client reference."""
    return SimulatorTransferProvider().record_matching(
        provider_record_id=record_id,
        client_reference=attempt.client_reference,
        outcome=ReconciliationOutcome.SUCCEEDED,
        amount_minor=amount_minor,
        currency='NGN',
        observed_at=timezone.now(),
    )


@pytest.fixture
def opening_wallet(funded_wallet):
    """A wallet holding 10 000, posted as real double-entry by M2.

    ``funded_wallet`` funds through ``post_journal`` with a genuine debit and
    credit — there is no balance column to set, and no deposit feature was
    invented to get money in.
    """
    assert balances(funded_wallet) == (OPENING, 0, OPENING)
    return funded_wallet


# ---------------------------------------------------------------------------
# 1. The transfer that works
# ---------------------------------------------------------------------------


class TestSuccessfulTransfer:
    def test_successful_transfer_posts_once_and_releases_reserved_funds(
        self, opening_wallet, wallet_account, counterpart_account
    ):
        """Money leaves once: one journal, one submission, one release."""
        funding_journals = Journal.objects.count()

        transfer = prepare_transfer(
            opening_wallet, DESTINATION, AMOUNT, idempotency_key='scenario-1'
        )

        # Reserved, not yet spent. The customer cannot double-spend it, and
        # the ledger has not moved.
        assert balances(opening_wallet) == (OPENING, AMOUNT, REMAINING)
        assert Journal.objects.count() == funding_journals
        assert transfer.hold.status == HoldStatus.ACTIVE

        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.SUCCESS
        )
        execute_transfer(
            transfer, provider=provider, counterpart_account=counterpart_account
        )
        transfer.refresh_from_db()

        assert transfer.status == TransactionStatus.SUCCEEDED

        attempts = ProviderExecutionAttempt.objects.filter(
            financial_transaction=transfer.financial_transaction_id
        )
        assert attempts.count() == 1
        assert attempts.get().status == ProviderAttemptStatus.SUCCEEDED
        assert provider.submit_call_count == 1

        # Exactly one success journal, and it is the one that moved the money.
        assert Journal.objects.count() == funding_journals + 1
        assert transfer.financial_transaction.journal_id is not None
        assert wallet_effect(wallet_account) == OPENING - AMOUNT

        # The reservation is gone because the money actually went, not because
        # anything cancelled it.
        transfer.hold.refresh_from_db()
        assert transfer.hold.status == HoldStatus.RELEASED
        assert balances(opening_wallet) == (REMAINING, 0, REMAINING)

    def test_a_successful_transfer_is_never_submitted_twice(
        self, opening_wallet, counterpart_account
    ):
        """Re-executing a settled transfer is refused before it reaches a rail.

        The refusal is the point: M6 will not claim a second execution, so
        there is no path by which the customer pays twice.
        """
        transfer = prepare_transfer(
            opening_wallet, DESTINATION, AMOUNT, idempotency_key='scenario-1b'
        )
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.SUCCESS
        )
        execute_transfer(
            transfer, provider=provider, counterpart_account=counterpart_account
        )
        journals = Journal.objects.count()

        with pytest.raises(ProviderExecutionAlreadyStartedError):
            execute_transfer(
                transfer, provider=provider,
                counterpart_account=counterpart_account,
            )

        assert provider.submit_call_count == 1
        assert Journal.objects.count() == journals
        assert ProviderExecutionAttempt.objects.count() == 1


# ---------------------------------------------------------------------------
# 2. The transfer the rail refuses
# ---------------------------------------------------------------------------


class TestKnownProviderFailure:
    def test_known_failure_restores_available_funds_without_posting(
        self, opening_wallet, wallet_account, counterpart_account
    ):
        """A definite no gives the customer their money back to spend.

        Nothing is posted, because nothing moved. The distinction that matters
        is against scenario 3: *this* rail answered.
        """
        funding_journals = Journal.objects.count()
        before = wallet_effect(wallet_account)

        transfer = prepare_transfer(
            opening_wallet, DESTINATION, AMOUNT, idempotency_key='scenario-2'
        )
        assert balances(opening_wallet) == (OPENING, AMOUNT, REMAINING)

        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.KNOWN_FAILURE
        )
        execute_transfer(
            transfer, provider=provider, counterpart_account=counterpart_account
        )
        transfer.refresh_from_db()

        assert transfer.status == TransactionStatus.FAILED

        attempts = ProviderExecutionAttempt.objects.filter(
            financial_transaction=transfer.financial_transaction_id
        )
        assert attempts.count() == 1
        assert attempts.get().status == ProviderAttemptStatus.FAILED

        # KNOWN_FAILURE is a rejection of something that *was* sent, so one
        # submission is the correct count — and there is no second.
        assert provider.submit_call_count == 1

        # No success journal, and the ledger is exactly where it started.
        assert Journal.objects.count() == funding_journals
        assert transfer.financial_transaction.journal_id is None
        assert wallet_effect(wallet_account) == before

        transfer.hold.refresh_from_db()
        assert transfer.hold.status == HoldStatus.RELEASED
        assert balances(opening_wallet) == (OPENING, 0, OPENING)

    def test_a_failed_transfer_is_never_retried(
        self, opening_wallet, counterpart_account
    ):
        """Re-executing a failed transfer sends nothing to the rail."""
        transfer = prepare_transfer(
            opening_wallet, DESTINATION, AMOUNT, idempotency_key='scenario-2b'
        )
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.KNOWN_FAILURE
        )
        execute_transfer(
            transfer, provider=provider, counterpart_account=counterpart_account
        )

        with pytest.raises(ProviderExecutionAlreadyStartedError):
            execute_transfer(
                transfer, provider=provider,
                counterpart_account=counterpart_account,
            )

        assert provider.submit_call_count == 1
        assert ProviderExecutionAttempt.objects.count() == 1


# ---------------------------------------------------------------------------
# 3. The transfer whose answer never came back — and later, was yes
# ---------------------------------------------------------------------------


class TestAmbiguousTransferRecoveringSuccessfully:
    """The scenario the whole architecture is shaped around.

    A timeout is not a failure. The request went out; the answer did not come
    back; the money may already have gone. Releasing the reservation here would
    let the customer spend money that has left, and resubmitting would send it
    twice. So the reservation stands, nothing is posted, and the system waits
    for the truth.
    """

    def test_ambiguous_transfer_recovers_successfully_without_resubmission(
        self, opening_wallet, wallet_account, counterpart_account
    ):
        funding_journals = Journal.objects.count()

        transfer = prepare_transfer(
            opening_wallet, DESTINATION, AMOUNT, idempotency_key='scenario-3'
        )
        submitting = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
        )
        execute_transfer(
            transfer, provider=submitting,
            counterpart_account=counterpart_account,
        )
        transfer.refresh_from_db()

        # --- unresolved, and honest about it ---------------------------
        assert transfer.status == TransactionStatus.UNKNOWN
        attempt = provider_attempt_for(transfer)
        assert attempt.status == ProviderAttemptStatus.UNKNOWN
        assert ProviderExecutionAttempt.objects.count() == 1
        assert submitting.submit_call_count == 1

        # The reservation stands and nothing is posted. Both matter: the first
        # stops a double-spend, the second stops us claiming a payment we
        # cannot prove.
        assert balances(opening_wallet) == (OPENING, AMOUNT, REMAINING)
        assert Journal.objects.count() == funding_journals
        assert transfer.financial_transaction.journal_id is None
        assert transfer.hold.status == HoldStatus.ACTIVE

        # --- the truth arrives -----------------------------------------
        asking = SimulatorTransferProvider(
            status_scenario=StatusScenario.SUCCESS
        )
        evidence = recover_provider_attempt(
            attempt, provider=asking, counterpart_account=counterpart_account
        )
        transfer.refresh_from_db()

        assert transfer.status == TransactionStatus.SUCCEEDED
        assert evidence.outcome == RecoveryOutcome.SUCCEEDED
        assert ProviderRecoveryEvidence.objects.filter(
            provider_attempt=attempt
        ).count() == 1

        # The attempt keeps saying what it saw. History is not rewritten by a
        # later observation — that is what the evidence row is for.
        attempt.refresh_from_db()
        assert attempt.status == ProviderAttemptStatus.UNKNOWN

        # --- settled, exactly once -------------------------------------
        assert Journal.objects.count() == funding_journals + 1
        assert transfer.financial_transaction.journal_id is not None
        assert wallet_effect(wallet_account) == OPENING - AMOUNT

        transfer.hold.refresh_from_db()
        assert transfer.hold.status == HoldStatus.RELEASED
        assert balances(opening_wallet) == (REMAINING, 0, REMAINING)

        # The invariant this scenario exists for.
        assert submitting.submit_call_count == 1
        assert asking.submit_call_count == 0
        assert asking.status_call_count == 1
        assert ProviderExecutionAttempt.objects.count() == 1

    def test_asking_again_after_recovery_changes_nothing(
        self, opening_wallet, counterpart_account
    ):
        """Recovery is observational, so repeating it is safe and idempotent."""
        transfer = prepare_transfer(
            opening_wallet, DESTINATION, AMOUNT, idempotency_key='scenario-3b'
        )
        execute_transfer(
            transfer,
            provider=SimulatorTransferProvider(
                transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
            ),
            counterpart_account=counterpart_account,
        )
        attempt = provider_attempt_for(transfer)
        asking = SimulatorTransferProvider(
            status_scenario=StatusScenario.SUCCESS
        )
        recover_provider_attempt(
            attempt, provider=asking, counterpart_account=counterpart_account
        )
        journals = Journal.objects.count()

        recover_provider_attempt(
            attempt, provider=asking, counterpart_account=counterpart_account
        )
        transfer.refresh_from_db()

        assert Journal.objects.count() == journals
        assert transfer.status == TransactionStatus.SUCCEEDED
        assert asking.submit_call_count == 0
        assert ProviderExecutionAttempt.objects.count() == 1


# ---------------------------------------------------------------------------
# 4. The transfer whose answer never came back — and later, was no
# ---------------------------------------------------------------------------


class TestAmbiguousTransferRecoveringAsFailed:
    def test_ambiguous_transfer_can_resolve_failed_without_resubmission(
        self, opening_wallet, wallet_account, counterpart_account
    ):
        """The same ambiguity, the opposite answer, and still one submission."""
        funding_journals = Journal.objects.count()
        before = wallet_effect(wallet_account)

        transfer = prepare_transfer(
            opening_wallet, DESTINATION, AMOUNT, idempotency_key='scenario-4'
        )
        submitting = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
        )
        execute_transfer(
            transfer, provider=submitting,
            counterpart_account=counterpart_account,
        )
        transfer.refresh_from_db()

        assert transfer.status == TransactionStatus.UNKNOWN
        assert balances(opening_wallet) == (OPENING, AMOUNT, REMAINING)
        assert Journal.objects.count() == funding_journals

        attempt = provider_attempt_for(transfer)
        asking = SimulatorTransferProvider(
            status_scenario=StatusScenario.FAILURE
        )
        evidence = recover_provider_attempt(
            attempt, provider=asking, counterpart_account=counterpart_account
        )
        transfer.refresh_from_db()

        assert transfer.status == TransactionStatus.FAILED
        assert evidence.outcome == RecoveryOutcome.FAILED
        assert ProviderRecoveryEvidence.objects.filter(
            provider_attempt=attempt
        ).count() == 1

        attempt.refresh_from_db()
        assert attempt.status == ProviderAttemptStatus.UNKNOWN

        # Nothing was ever posted, so nothing needs reversing.
        assert Journal.objects.count() == funding_journals
        assert transfer.financial_transaction.journal_id is None
        assert wallet_effect(wallet_account) == before

        transfer.hold.refresh_from_db()
        assert transfer.hold.status == HoldStatus.RELEASED
        assert balances(opening_wallet) == (OPENING, 0, OPENING)

        assert submitting.submit_call_count == 1
        assert asking.submit_call_count == 0
        assert ProviderExecutionAttempt.objects.count() == 1


# ---------------------------------------------------------------------------
# 5. The provider's file agrees
# ---------------------------------------------------------------------------


class TestRecoveredSuccessReconciles:
    def test_recovered_success_reconciles_as_matched(
        self, opening_wallet, wallet_account, counterpart_account
    ):
        """The full chain, then the rail's own file, and they agree.

        This is the scenario that says the three subsystems tell one story:
        an ambiguous submission, an answer recovered later, and a provider
        export that confirms it — with reconciliation touching nothing.
        """
        transfer = prepare_transfer(
            opening_wallet, DESTINATION, AMOUNT, idempotency_key='scenario-5'
        )
        submitting = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
        )
        execute_transfer(
            transfer, provider=submitting,
            counterpart_account=counterpart_account,
        )
        attempt = provider_attempt_for(transfer)
        recover_provider_attempt(
            attempt,
            provider=SimulatorTransferProvider(
                status_scenario=StatusScenario.SUCCESS
            ),
            counterpart_account=counterpart_account,
        )
        transfer.refresh_from_db()
        assert transfer.status == TransactionStatus.SUCCEEDED

        settled = (
            transfer.status,
            transfer.financial_transaction.journal_id,
            transfer.hold.status,
            Journal.objects.count(),
            JournalEntry.objects.count(),
            balances(opening_wallet),
            ProviderExecutionAttempt.objects.count(),
        )

        reporting = SimulatorTransferProvider(
            reconciliation_records=[
                matching_record(
                    attempt, amount_minor=AMOUNT, record_id='rec-scenario-5'
                )
            ]
        )
        run = reconcile_transfers(
            provider=reporting, window=window_around_now()
        )

        assert run.status == ReconciliationRunStatus.COMPLETED

        item = items_for(run).get()
        assert item.overall_status == ReconciliationStatus.MATCHED
        assert item.discrepancy_codes == ()
        assert item.internal_outcome == ReconciliationOutcome.SUCCEEDED
        assert item.provider_outcome == ReconciliationOutcome.SUCCEEDED

        summary = run_summary(run)
        assert summary['matched'] == 1
        assert summary['discrepancies'] == 0

        # Observational: every financial fact is exactly where it was.
        transfer.refresh_from_db()
        transfer.hold.refresh_from_db()
        assert (
            transfer.status,
            transfer.financial_transaction.journal_id,
            transfer.hold.status,
            Journal.objects.count(),
            JournalEntry.objects.count(),
            balances(opening_wallet),
            ProviderExecutionAttempt.objects.count(),
        ) == settled
        assert submitting.submit_call_count == 1
        assert reporting.submit_call_count == 0
        assert wallet_effect(wallet_account) == OPENING - AMOUNT


# ---------------------------------------------------------------------------
# 6. The provider's file disagrees
# ---------------------------------------------------------------------------


class TestReconciliationMismatch:
    def test_reconciliation_amount_mismatch_does_not_mutate_financial_truth(
        self, opening_wallet, wallet_account, counterpart_account
    ):
        """The rail reports 7 100 against our 7 000. We report it and stop.

        A system that quietly adjusted its books to agree with someone else's
        file would have no books worth keeping — and the file is at least as
        likely to be the thing that is wrong. So the disagreement is recorded
        as a finding, in full detail, and every financial figure is compared
        before and after to prove nothing moved.
        """
        transfer = prepare_transfer(
            opening_wallet, DESTINATION, AMOUNT, idempotency_key='scenario-6'
        )
        execute_transfer(
            transfer,
            provider=SimulatorTransferProvider(
                transfer_scenario=TransferScenario.SUCCESS
            ),
            counterpart_account=counterpart_account,
        )
        transfer.refresh_from_db()
        attempt = provider_attempt_for(transfer)
        assert transfer.status == TransactionStatus.SUCCEEDED

        # Everything that could conceivably be "repaired", captured first.
        transfer.hold.refresh_from_db()
        before = {
            'status': transfer.status,
            'journal_id': transfer.financial_transaction.journal_id,
            'hold_status': transfer.hold.status,
            'journals': Journal.objects.count(),
            'entries': list(
                JournalEntry.objects.order_by('pk').values_list(
                    'pk', 'ledger_account_id', 'direction', 'amount_minor'
                )
            ),
            'balances': balances(opening_wallet),
            'wallet_effect': wallet_effect(wallet_account),
            'attempts': ProviderExecutionAttempt.objects.count(),
            'attempt_status': attempt.status,
        }

        reporting = SimulatorTransferProvider(
            reconciliation_records=[
                matching_record(
                    attempt, amount_minor=7_100, record_id='rec-scenario-6'
                )
            ]
        )
        run = reconcile_transfers(
            provider=reporting, window=window_around_now()
        )

        assert run.status == ReconciliationRunStatus.COMPLETED

        item = items_for(run).get()
        assert item.overall_status == ReconciliationStatus.DISCREPANCY
        assert item.amount_mismatch is True
        assert item.discrepancy_codes == ('amount_mismatch',)
        assert item.provider_amount_minor == 7_100
        assert item.internal_amount_minor == AMOUNT

        summary = run_summary(run)
        assert summary['matched'] == 0
        assert summary['discrepancies'] == 1
        assert summary['amount_mismatches'] == 1

        # --- and the books did not move --------------------------------
        transfer.refresh_from_db()
        transfer.hold.refresh_from_db()
        attempt.refresh_from_db()
        after = {
            'status': transfer.status,
            'journal_id': transfer.financial_transaction.journal_id,
            'hold_status': transfer.hold.status,
            'journals': Journal.objects.count(),
            'entries': list(
                JournalEntry.objects.order_by('pk').values_list(
                    'pk', 'ledger_account_id', 'direction', 'amount_minor'
                )
            ),
            'balances': balances(opening_wallet),
            'wallet_effect': wallet_effect(wallet_account),
            'attempts': ProviderExecutionAttempt.objects.count(),
            'attempt_status': attempt.status,
        }
        assert after == before

        # No reversal was posted, and no compensating transaction created.
        assert Journal.objects.filter(reverses__isnull=False).count() == 0
        assert balances(opening_wallet) == (REMAINING, 0, REMAINING)

    def test_a_mismatch_can_be_observed_twice_without_converging(
        self, opening_wallet, counterpart_account
    ):
        """A second run reports the same disagreement, and still repairs none."""
        transfer = prepare_transfer(
            opening_wallet, DESTINATION, AMOUNT, idempotency_key='scenario-6b'
        )
        execute_transfer(
            transfer,
            provider=SimulatorTransferProvider(
                transfer_scenario=TransferScenario.SUCCESS
            ),
            counterpart_account=counterpart_account,
        )
        attempt = provider_attempt_for(transfer)
        journals = Journal.objects.count()

        outcomes = []
        for index in range(2):
            reporting = SimulatorTransferProvider(
                reconciliation_records=[
                    matching_record(
                        attempt, amount_minor=7_100,
                        record_id=f'rec-scenario-6b-{index}',
                    )
                ]
            )
            run = reconcile_transfers(
                provider=reporting, window=window_around_now()
            )
            outcomes.append(items_for(run).get().overall_status)

        assert outcomes == [
            ReconciliationStatus.DISCREPANCY, ReconciliationStatus.DISCREPANCY
        ]
        assert Journal.objects.count() == journals
        assert balances(opening_wallet) == (REMAINING, 0, REMAINING)


# ---------------------------------------------------------------------------
# The scenarios must go through the core, not around it
# ---------------------------------------------------------------------------


class TestTheScenariosUseTheRealCore:
    """A scenario that fabricated its own end state would prove nothing.

    The per-milestone boundary suites already prove the *services* cannot be
    bypassed. What is unproven, and specific to this file, is that these six
    narratives did not reach their assertions by writing the answer down.
    """

    def _tree(self):
        source = Path(__file__).read_text(encoding='utf-8')
        return ast.parse(source)

    def test_no_financial_state_is_assigned_directly(self):
        assigned = set()
        for node in ast.walk(self._tree()):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AugAssign):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute):
                    assigned.add(target.attr)

        assert assigned.isdisjoint({
            'status', 'balance', 'posted', 'held', 'available',
            'journal', 'journal_id', 'hold', 'hold_id', 'amount_minor',
        }), assigned

    def test_no_financial_row_is_created_or_updated_directly(self):
        forbidden = {
            'Journal', 'JournalEntry', 'FundsHold', 'FinancialTransaction',
            'Transfer', 'Wallet', 'ProviderExecutionAttempt',
            'ProviderRecoveryEvidence', 'LedgerAccount',
        }
        written = set()
        for node in ast.walk(self._tree()):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {
                    'create', 'bulk_create', 'update', 'save', 'delete',
                    'get_or_create', 'update_or_create',
                }
                and isinstance(node.func.value, ast.Attribute)
            ):
                continue
            owner = getattr(node.func.value.value, 'id', '')
            if owner in forbidden:
                written.add(f'{owner}.{node.func.attr}')

        assert not written, written

    def test_no_outcome_service_is_called_behind_the_provider(self):
        """M4's outcome mutators belong to M6/M7, never to a scenario."""
        called = set()
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                called.add(node.func.id)

        assert called.isdisjoint({
            'succeed_transaction', 'fail_transaction', 'mark_unknown',
            'succeed_transfer', 'fail_transfer', 'mark_transfer_unknown',
            'start_processing', 'attach_hold', 'release_hold', 'expire_hold',
            'post_journal', 'reverse_journal', 'submit_transfer',
        }), called

    def test_every_scenario_moves_money_through_a_public_service(self):
        """Anti-vacuity: the flow really is prepare -> execute -> recover."""
        called = set()
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                called.add(node.func.id)

        assert {
            'prepare_transfer', 'execute_transfer',
            'recover_provider_attempt', 'reconcile_transfers',
        } <= called

    def test_the_money_core_carries_no_promotional_language(self):
        """Engineering verification, not a sales artefact.

        The words are assembled from fragments so that this assertion does not
        plant the very strings it forbids — a check that fails on its own
        source is worthless.
        """
        # Whole promotional terms only. Bare 'pitch' and 'presentation'
        # match 'representation' and would fail on honest technical prose.
        banned = [
            'invest' + 'or', 'pitch ' + 'deck', 'demo ' + 'account',
            'fund' + 'raising',
        ]
        package = Path(__file__).resolve().parent.parent

        offenders = []
        for path in sorted(package.rglob('*.py')):
            text = path.read_text(encoding='utf-8').lower()
            offenders += [
                f'{path.name}: {word}' for word in banned if word in text
            ]

        assert not offenders, offenders

    def test_the_opening_balance_comes_from_a_real_posting(
        self, opening_wallet, wallet_account
    ):
        """10 000 exists because a balanced journal put it there."""
        assert wallet_effect(wallet_account) == OPENING
        assert balances(opening_wallet) == (OPENING, 0, OPENING)
        assert wallet_balance_projection(opening_wallet).posted == Money(
            OPENING, 'NGN'
        )
