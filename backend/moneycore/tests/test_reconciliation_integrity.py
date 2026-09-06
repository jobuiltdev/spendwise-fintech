"""SpendWise disagreeing with itself is not a discrepancy.

A provider and SpendWise differing is expected and gets classified. SpendWise's
own records contradicting each other is a defect in our system, and filing it
alongside ordinary differences would bury the one that matters.

Every corruption here uses **raw SQL**, going around the ORM and the services,
because there is no legitimate way to produce these states. Doing so turned up
a better result than expected: for the journal invariants the database itself
refuses the corruption, so M8's checks there are defence in depth rather than
the primary guarantee. Where the schema does *not* constrain — hold status
against transaction status — M8's checking is what catches it.
"""

from datetime import timedelta

import pytest
from django.db import IntegrityError, connection, transaction as db_transaction
from django.utils import timezone

from moneycore.domain.errors import ReconciliationInternalIntegrityError
from moneycore.domain.holds import HoldStatus
from moneycore.domain.ledger import LedgerAccountType
from moneycore.domain.reconciliation import (
    ReconciliationRunStatus,
    ReconciliationWindow,
)
from moneycore.domain.transactions import TransactionStatus
from moneycore.domain.transfers import VerifiedBankAccount
from moneycore.models import (
    Journal,
    ProviderReconciliationItem,
    ProviderReconciliationRun,
)
from moneycore.providers.simulator import (
    SimulatorTransferProvider,
    TransferScenario,
)
from moneycore.services.ledger import open_ledger_account
from moneycore.services.provider_execution import (
    execute_transfer,
    provider_attempt_for,
)
from moneycore.services.reconciliation import reconcile_transfers
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
        code='internal:m8-integrity:NGN',
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


def raw_update(sql, params):
    """Corrupt state the way nothing in production ever could."""
    with db_transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(sql, params)


def transfer_with(wallet, counterpart, scenario, key, amount=7_000):
    transfer = prepare_transfer(wallet, DESTINATION, amount, idempotency_key=key)
    execute_transfer(
        transfer,
        provider=SimulatorTransferProvider(transfer_scenario=scenario),
        counterpart_account=counterpart,
    )
    transfer.refresh_from_db()
    return transfer


def reconcile(window):
    return reconcile_transfers(
        provider=SimulatorTransferProvider(reconciliation_records=()),
        window=window,
    )


def free_the_hold(transfer):
    raw_update(
        'UPDATE moneycore_fundshold SET status = %s, released_at = %s '
        'WHERE id = %s',
        [HoldStatus.RELEASED, timezone.now(), transfer.financial_transaction.hold_id],
    )


def reactivate_the_hold(transfer):
    raw_update(
        'UPDATE moneycore_fundshold SET status = %s, released_at = NULL '
        'WHERE id = %s',
        [HoldStatus.ACTIVE, transfer.financial_transaction.hold_id],
    )


class TestTheDatabaseAlreadyForbidsJournalCorruption:
    """A stronger result than M8 detecting it: it cannot be created at all.

    M4's ``moneycore_txn_succeeded_requires_journal`` ties success and a posted
    journal together as one fact, enforced by the database. Separating them —
    even by raw SQL, around every service and model guard — is refused.

    M8's integrity rules for these cases therefore stand as defence in depth,
    unreachable while that constraint does. Which is the right place for the
    guarantee to live.
    """

    def test_a_succeeded_transaction_cannot_lose_its_journal(
        self, funded_wallet, settlement_account
    ):
        transfer = transfer_with(
            funded_wallet, settlement_account,
            TransferScenario.SUCCESS, 'integrity-1',
        )

        with pytest.raises(IntegrityError):
            raw_update(
                'UPDATE moneycore_financialtransaction SET journal_id = NULL '
                'WHERE id = %s',
                [transfer.financial_transaction_id],
            )

    def test_a_failed_transaction_cannot_gain_a_journal(
        self, funded_wallet, settlement_account
    ):
        succeeded = transfer_with(
            funded_wallet, settlement_account,
            TransferScenario.SUCCESS, 'integrity-2a', amount=3_000,
        )
        failed = transfer_with(
            funded_wallet, settlement_account,
            TransferScenario.KNOWN_FAILURE, 'integrity-2b', amount=3_000,
        )

        with pytest.raises(IntegrityError):
            raw_update(
                'UPDATE moneycore_financialtransaction SET journal_id = %s '
                'WHERE id = %s',
                [
                    succeeded.financial_transaction.journal_id,
                    failed.financial_transaction_id,
                ],
            )

    def test_an_unknown_transaction_cannot_gain_a_journal(
        self, funded_wallet, settlement_account
    ):
        succeeded = transfer_with(
            funded_wallet, settlement_account,
            TransferScenario.SUCCESS, 'integrity-3a', amount=3_000,
        )
        unknown = transfer_with(
            funded_wallet, settlement_account,
            TransferScenario.AMBIGUOUS_AFTER_SUBMISSION, 'integrity-3b',
            amount=3_000,
        )

        with pytest.raises(IntegrityError):
            raw_update(
                'UPDATE moneycore_financialtransaction SET journal_id = %s '
                'WHERE id = %s',
                [
                    succeeded.financial_transaction.journal_id,
                    unknown.financial_transaction_id,
                ],
            )

    def test_m8_checks_it_anyway(self):
        """Written down even though the database currently makes it
        unreachable — the constraint could be relaxed, the rule should not."""
        import inspect

        from moneycore.services import reconciliation

        source = inspect.getsource(reconciliation._require_internal_integrity)

        assert 'journal_id is None' in source
        assert 'has no posted journal' in source
        assert 'has a posted journal' in source


class TestHoldCorruptionIsDetectedByM8:
    """The schema does not tie hold status to transaction status.

    Nothing constrains a released hold on an unresolved transaction, or an
    active one on a settled transaction — so this is exactly where M8's own
    integrity checking earns its place.
    """

    def test_a_succeeded_transaction_still_holding_funds_is_detected(
        self, funded_wallet, settlement_account, window
    ):
        transfer = transfer_with(
            funded_wallet, settlement_account,
            TransferScenario.SUCCESS, 'integrity-4',
        )
        reactivate_the_hold(transfer)

        with pytest.raises(ReconciliationInternalIntegrityError):
            reconcile(window)

    def test_it_is_not_reported_as_an_ordinary_discrepancy(
        self, funded_wallet, settlement_account, window
    ):
        transfer = transfer_with(
            funded_wallet, settlement_account,
            TransferScenario.SUCCESS, 'integrity-5',
        )
        reactivate_the_hold(transfer)

        with pytest.raises(ReconciliationInternalIntegrityError):
            reconcile(window)

        assert ProviderReconciliationItem.objects.count() == 0

    def test_the_run_is_recorded_as_failed(
        self, funded_wallet, settlement_account, window
    ):
        transfer = transfer_with(
            funded_wallet, settlement_account,
            TransferScenario.SUCCESS, 'integrity-6',
        )
        reactivate_the_hold(transfer)

        with pytest.raises(ReconciliationInternalIntegrityError):
            reconcile(window)

        assert ProviderReconciliationRun.objects.get().status == (
            ReconciliationRunStatus.FAILED_INTERNAL
        )

    def test_a_failed_transaction_still_holding_funds_is_detected(
        self, funded_wallet, settlement_account, window
    ):
        transfer = transfer_with(
            funded_wallet, settlement_account,
            TransferScenario.KNOWN_FAILURE, 'integrity-7',
        )
        reactivate_the_hold(transfer)

        with pytest.raises(ReconciliationInternalIntegrityError):
            reconcile(window)

    def test_an_unresolved_transaction_that_released_its_hold_is_detected(
        self, funded_wallet, settlement_account, window
    ):
        transfer = transfer_with(
            funded_wallet, settlement_account,
            TransferScenario.AMBIGUOUS_AFTER_SUBMISSION, 'integrity-8',
        )
        free_the_hold(transfer)

        with pytest.raises(ReconciliationInternalIntegrityError):
            reconcile(window)

    def test_a_correct_failure_reconciles_normally(
        self, funded_wallet, settlement_account, window
    ):
        transfer_with(
            funded_wallet, settlement_account,
            TransferScenario.KNOWN_FAILURE, 'integrity-9',
        )

        run = reconcile(window)

        assert run.status == ReconciliationRunStatus.COMPLETED

    def test_a_correct_unknown_reconciles_normally(
        self, funded_wallet, settlement_account, window
    ):
        transfer_with(
            funded_wallet, settlement_account,
            TransferScenario.AMBIGUOUS_AFTER_SUBMISSION, 'integrity-10',
        )

        run = reconcile(window)

        assert run.status == ReconciliationRunStatus.COMPLETED


class TestAttemptWithoutStartedExecution:
    def test_an_attempt_on_a_created_transaction_is_detected(
        self, funded_wallet, settlement_account, window
    ):
        """M6 starts the transaction before writing the attempt row."""
        transfer = transfer_with(
            funded_wallet, settlement_account,
            TransferScenario.AMBIGUOUS_AFTER_SUBMISSION, 'integrity-11',
        )
        raw_update(
            'UPDATE moneycore_financialtransaction SET status = %s, '
            'processing_at = NULL WHERE id = %s',
            [TransactionStatus.CREATED, transfer.financial_transaction_id],
        )

        with pytest.raises(ReconciliationInternalIntegrityError):
            reconcile(window)

    def test_it_records_no_items(
        self, funded_wallet, settlement_account, window
    ):
        transfer = transfer_with(
            funded_wallet, settlement_account,
            TransferScenario.AMBIGUOUS_AFTER_SUBMISSION, 'integrity-12',
        )
        raw_update(
            'UPDATE moneycore_financialtransaction SET status = %s, '
            'processing_at = NULL WHERE id = %s',
            [TransactionStatus.CREATED, transfer.financial_transaction_id],
        )

        with pytest.raises(ReconciliationInternalIntegrityError):
            reconcile(window)

        assert ProviderReconciliationItem.objects.count() == 0


class TestDuplicateClientReference:
    def test_the_database_refuses_two_attempts_sharing_one_reference(
        self, funded_wallet, settlement_account
    ):
        """M7 made it unique, so M8's guard is defence in depth here too."""
        first = transfer_with(
            funded_wallet, settlement_account,
            TransferScenario.SUCCESS, 'dup-a', amount=3_000,
        )
        second = transfer_with(
            funded_wallet, settlement_account,
            TransferScenario.KNOWN_FAILURE, 'dup-b', amount=3_000,
        )

        with pytest.raises(IntegrityError):
            raw_update(
                'UPDATE moneycore_providerexecutionattempt '
                'SET client_reference = %s WHERE id = %s',
                [
                    provider_attempt_for(first).client_reference,
                    provider_attempt_for(second).pk,
                ],
            )

    def test_m8_checks_it_anyway(self):
        import inspect

        from moneycore.services import reconciliation

        source = inspect.getsource(reconciliation._classify_and_record)

        assert 'share one client reference' in source


class TestIntegrityFailuresChangeNothing:
    def test_the_ledger_is_untouched(
        self, funded_wallet, settlement_account, window
    ):
        transfer = transfer_with(
            funded_wallet, settlement_account,
            TransferScenario.SUCCESS, 'integrity-13',
        )
        reactivate_the_hold(transfer)
        journals = Journal.objects.count()

        with pytest.raises(ReconciliationInternalIntegrityError):
            reconcile(window)

        assert Journal.objects.count() == journals

    def test_the_transaction_is_untouched(
        self, funded_wallet, settlement_account, window
    ):
        transfer = transfer_with(
            funded_wallet, settlement_account,
            TransferScenario.AMBIGUOUS_AFTER_SUBMISSION, 'integrity-14',
        )
        free_the_hold(transfer)

        with pytest.raises(ReconciliationInternalIntegrityError):
            reconcile(window)

        transfer.refresh_from_db()
        assert transfer.status == TransactionStatus.UNKNOWN

    def test_the_corruption_is_not_repaired(
        self, funded_wallet, settlement_account, window
    ):
        """M8 reports; it does not tidy up after whatever caused this."""
        transfer = transfer_with(
            funded_wallet, settlement_account,
            TransferScenario.SUCCESS, 'integrity-15',
        )
        reactivate_the_hold(transfer)

        with pytest.raises(ReconciliationInternalIntegrityError):
            reconcile(window)

        transfer.financial_transaction.hold.refresh_from_db()
        assert transfer.financial_transaction.hold.status == HoldStatus.ACTIVE

    def test_no_corrective_item_is_invented(
        self, funded_wallet, settlement_account, window
    ):
        transfer = transfer_with(
            funded_wallet, settlement_account,
            TransferScenario.SUCCESS, 'integrity-16',
        )
        reactivate_the_hold(transfer)

        with pytest.raises(ReconciliationInternalIntegrityError):
            reconcile(window)

        assert ProviderReconciliationItem.objects.count() == 0


class TestHealthyStateReconcilesCleanly:
    def test_a_correct_success_needs_no_corruption_to_pass(
        self, funded_wallet, settlement_account, window
    ):
        """The integrity rules do not fire on states the services produce."""
        transfer_with(
            funded_wallet, settlement_account,
            TransferScenario.SUCCESS, 'healthy-1',
        )

        run = reconcile(window)

        assert run.status == ReconciliationRunStatus.COMPLETED
        assert ProviderReconciliationItem.objects.count() == 1

    def test_every_supported_outcome_passes_the_integrity_rules(
        self, funded_wallet, settlement_account, window
    ):
        for index, scenario in enumerate((
            TransferScenario.SUCCESS,
            TransferScenario.KNOWN_FAILURE,
            TransferScenario.AMBIGUOUS_AFTER_SUBMISSION,
        )):
            transfer_with(
                funded_wallet, settlement_account, scenario,
                f'healthy-{index}', amount=1_000,
            )

        run = reconcile(window)

        assert run.status == ReconciliationRunStatus.COMPLETED
        assert ProviderReconciliationItem.objects.count() == 3
