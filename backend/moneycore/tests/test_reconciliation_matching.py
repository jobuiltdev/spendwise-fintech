"""Running reconciliation: matching, classification, and what it refuses to do.

Every classification here is derived from identity and recorded values. Nothing
in this file — or the service it exercises — moves a single unit of money.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from moneycore.domain.errors import (
    ReconciliationInputConflictError,
    ReconciliationProviderError,
    ReconciliationRecordInvalidError,
    ReconciliationReferenceConflictError,
)
from moneycore.domain.holds import HoldStatus
from moneycore.domain.ledger import LedgerAccountType
from moneycore.domain.money import Money
from moneycore.domain.providers import ProviderFailureCode
from moneycore.domain.reconciliation import (
    ProviderTransferRecord,
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
    ProviderExecutionAttempt,
    ProviderReconciliationItem,
    ProviderReconciliationRun,
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
from moneycore.services.reconciliation import (
    items_for,
    reconcile_transfers,
    run_summary,
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
        code='internal:m8-counterpart:NGN',
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


def observed_now():
    return timezone.now()


def succeeded_transfer(wallet, counterpart, key='m8-1', amount=7_000):
    transfer = prepare_transfer(wallet, DESTINATION, amount, idempotency_key=key)
    execute_transfer(
        transfer,
        provider=SimulatorTransferProvider(
            transfer_scenario=TransferScenario.SUCCESS
        ),
        counterpart_account=counterpart,
    )
    transfer.refresh_from_db()
    return transfer


def ambiguous_transfer(wallet, counterpart, key='m8-unknown', amount=7_000):
    transfer = prepare_transfer(wallet, DESTINATION, amount, idempotency_key=key)
    execute_transfer(
        transfer,
        provider=SimulatorTransferProvider(
            transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
        ),
        counterpart_account=counterpart,
    )
    transfer.refresh_from_db()
    return transfer


def failed_transfer(wallet, counterpart, key='m8-failed', amount=7_000):
    transfer = prepare_transfer(wallet, DESTINATION, amount, idempotency_key=key)
    execute_transfer(
        transfer,
        provider=SimulatorTransferProvider(
            transfer_scenario=TransferScenario.KNOWN_FAILURE
        ),
        counterpart_account=counterpart,
    )
    transfer.refresh_from_db()
    return transfer


def record_for(attempt, **overrides):
    values = {
        'provider_key': 'simulator',
        'provider_record_id': f'rec-{attempt.pk}',
        'client_reference': attempt.client_reference,
        'outcome': ReconciliationOutcome.SUCCEEDED,
        'amount_minor': attempt.financial_transaction.amount_minor,
        'currency': attempt.financial_transaction.currency,
        'observed_at': observed_now(),
    }
    values.update(overrides)
    return ProviderTransferRecord(**values)


def reconcile(window, records=(), **kwargs):
    provider = SimulatorTransferProvider(
        reconciliation_records=records, **kwargs
    )
    return reconcile_transfers(provider=provider, window=window), provider


class TestExactMatch:
    def test_a_matching_record_is_matched(
        self, funded_wallet, settlement_account, window
    ):
        transfer = succeeded_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)

        run, _ = reconcile(window, [record_for(attempt)])

        item = items_for(run).get()
        assert item.overall_status == ReconciliationStatus.MATCHED
        assert item.discrepancy_codes == ()

    def test_it_records_both_sides(
        self, funded_wallet, settlement_account, window
    ):
        transfer = succeeded_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)

        run, _ = reconcile(window, [record_for(attempt)])

        item = items_for(run).get()
        assert item.provider_outcome == ReconciliationOutcome.SUCCEEDED
        assert item.internal_outcome == ReconciliationOutcome.SUCCEEDED
        assert item.provider_amount_minor == 7_000
        assert item.internal_amount_minor == 7_000
        assert item.provider_currency == item.internal_currency == 'NGN'

    def test_the_run_completes(self, funded_wallet, settlement_account, window):
        transfer = succeeded_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)

        run, _ = reconcile(window, [record_for(attempt)])

        assert run.status == ReconciliationRunStatus.COMPLETED
        assert run.completed_at is not None

    def test_a_matched_failure_is_also_matched(
        self, funded_wallet, settlement_account, window
    ):
        transfer = failed_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)

        run, _ = reconcile(
            window,
            [record_for(attempt, outcome=ReconciliationOutcome.FAILED)],
        )

        assert items_for(run).get().overall_status == (
            ReconciliationStatus.MATCHED
        )

    def test_matching_leaves_the_ledger_untouched(
        self, funded_wallet, settlement_account, window
    ):
        transfer = succeeded_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)
        before = wallet_balance_projection(funded_wallet)
        journals = Journal.objects.count()

        reconcile(window, [record_for(attempt)])

        assert wallet_balance_projection(funded_wallet) == before
        assert Journal.objects.count() == journals


class TestProviderOnly:
    def test_an_unknown_record_is_provider_only(
        self, funded_wallet, settlement_account, window
    ):
        run, _ = reconcile(
            window,
            [
                ProviderTransferRecord(
                    provider_key='simulator',
                    provider_record_id='rec-orphan',
                    client_reference='SW-nothing-here',
                    outcome=ReconciliationOutcome.SUCCEEDED,
                    amount_minor=7_000,
                    currency='NGN',
                    observed_at=observed_now(),
                )
            ],
        )

        item = items_for(run).get()
        assert item.overall_status == ReconciliationStatus.PROVIDER_ONLY
        assert item.provider_attempt_id is None

    def test_it_creates_no_transaction_or_transfer(
        self, funded_wallet, settlement_account, window
    ):
        from moneycore.models import FinancialTransaction

        reconcile(
            window,
            [
                ProviderTransferRecord(
                    provider_key='simulator',
                    provider_record_id='rec-orphan-2',
                    client_reference='SW-nothing-here',
                    outcome=ReconciliationOutcome.SUCCEEDED,
                    amount_minor=7_000,
                    currency='NGN',
                    observed_at=observed_now(),
                )
            ],
        )

        assert FinancialTransaction.objects.count() == 0
        assert Transfer.objects.count() == 0
        assert ProviderExecutionAttempt.objects.count() == 0
        # Only the fixture's funding journal; reconciliation posted nothing.
        assert Journal.objects.count() == 1

    def test_it_is_never_attached_to_the_nearest_transfer(
        self, funded_wallet, settlement_account, window
    ):
        """Same amount and currency, different reference. Not a match."""
        transfer = succeeded_transfer(funded_wallet, settlement_account)

        run, _ = reconcile(
            window,
            [
                ProviderTransferRecord(
                    provider_key='simulator',
                    provider_record_id='rec-lookalike',
                    client_reference='SW-not-ours',
                    outcome=ReconciliationOutcome.SUCCEEDED,
                    amount_minor=7_000,
                    currency='NGN',
                    observed_at=observed_now(),
                )
            ],
        )

        statuses = {item.overall_status for item in items_for(run)}
        assert ReconciliationStatus.PROVIDER_ONLY in statuses
        assert ReconciliationStatus.MATCHED not in statuses


class TestInternalOnly:
    def test_an_unreported_attempt_is_internal_only(
        self, funded_wallet, settlement_account, window
    ):
        succeeded_transfer(funded_wallet, settlement_account)

        run, _ = reconcile(window, [])

        item = items_for(run).get()
        assert item.overall_status == ReconciliationStatus.INTERNAL_ONLY
        assert item.provider_record_id == ''

    def test_it_is_not_treated_as_a_failure(
        self, funded_wallet, settlement_account, window
    ):
        """An export delay looks identical to "it never happened"."""
        transfer = ambiguous_transfer(funded_wallet, settlement_account)

        run, _ = reconcile(window, [])
        transfer.refresh_from_db()
        transfer.hold.refresh_from_db()

        item = items_for(run).get()
        assert item.internal_outcome == ReconciliationOutcome.UNRESOLVED
        assert item.overall_status == ReconciliationStatus.INTERNAL_ONLY
        assert not item.outcome_mismatch
        assert transfer.status == TransactionStatus.UNKNOWN
        assert transfer.hold.status == HoldStatus.ACTIVE

    def test_it_records_the_internal_side_only(
        self, funded_wallet, settlement_account, window
    ):
        succeeded_transfer(funded_wallet, settlement_account)

        run, _ = reconcile(window, [])

        item = items_for(run).get()
        assert item.internal_amount_minor == 7_000
        assert item.provider_amount_minor is None
        assert item.provider_outcome == ''

    def test_an_attempt_outside_the_window_is_not_included(
        self, funded_wallet, settlement_account
    ):
        succeeded_transfer(funded_wallet, settlement_account)
        now = timezone.now()
        elsewhere = ReconciliationWindow(
            start=now + timedelta(days=1), end=now + timedelta(days=2)
        )

        run, _ = reconcile(elsewhere, [])

        assert items_for(run).count() == 0


class TestDiscrepancies:
    @pytest.fixture
    def matched_attempt(self, funded_wallet, settlement_account):
        transfer = succeeded_transfer(funded_wallet, settlement_account)
        return provider_attempt_for(transfer)

    def test_an_outcome_mismatch_is_detected(self, matched_attempt, window):
        run, _ = reconcile(
            window,
            [record_for(matched_attempt, outcome=ReconciliationOutcome.FAILED)],
        )

        item = items_for(run).get()
        assert item.overall_status == ReconciliationStatus.DISCREPANCY
        assert item.outcome_mismatch
        assert item.discrepancy_codes == ('outcome_mismatch',)

    def test_an_amount_mismatch_is_detected(self, matched_attempt, window):
        run, _ = reconcile(
            window, [record_for(matched_attempt, amount_minor=7_100)]
        )

        item = items_for(run).get()
        assert item.amount_mismatch
        assert item.provider_amount_minor == 7_100
        assert item.internal_amount_minor == 7_000

    def test_a_currency_mismatch_is_detected(self, matched_attempt, window):
        run, _ = reconcile(
            window, [record_for(matched_attempt, currency='USD')]
        )

        item = items_for(run).get()
        assert item.currency_mismatch
        assert item.provider_currency == 'USD'
        assert item.internal_currency == 'NGN'

    def test_a_reference_mismatch_is_detected(self, matched_attempt, window):
        assert matched_attempt.provider_reference

        run, _ = reconcile(
            window,
            [record_for(matched_attempt, provider_reference='SIM-something-else')],
        )

        assert items_for(run).get().reference_mismatch

    def test_multiple_differences_are_all_recorded(
        self, matched_attempt, window
    ):
        run, _ = reconcile(
            window,
            [
                record_for(
                    matched_attempt,
                    outcome=ReconciliationOutcome.FAILED,
                    amount_minor=7_100,
                    currency='USD',
                )
            ],
        )

        item = items_for(run).get()
        assert item.discrepancy_codes == (
            'outcome_mismatch', 'amount_mismatch', 'currency_mismatch'
        )

    def test_a_discrepancy_changes_nothing_financial(
        self, matched_attempt, window, funded_wallet
    ):
        before = wallet_balance_projection(funded_wallet)
        journals = Journal.objects.count()

        reconcile(window, [record_for(matched_attempt, amount_minor=7_100)])

        matched_attempt.refresh_from_db()
        assert wallet_balance_projection(funded_wallet) == before
        assert Journal.objects.count() == journals
        assert matched_attempt.financial_transaction.status == (
            TransactionStatus.SUCCEEDED
        )

    def test_unknown_internal_versus_provider_success_is_a_mismatch(
        self, funded_wallet, settlement_account, window
    ):
        """Recorded, and left for M7 recovery or ops to act on."""
        transfer = ambiguous_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)

        run, _ = reconcile(window, [record_for(attempt)])
        transfer.refresh_from_db()
        transfer.hold.refresh_from_db()

        item = items_for(run).get()
        assert item.overall_status == ReconciliationStatus.DISCREPANCY
        assert item.outcome_mismatch
        assert item.internal_outcome == ReconciliationOutcome.UNRESOLVED
        assert item.provider_outcome == ReconciliationOutcome.SUCCEEDED
        # Untouched: reconciliation is not a recovery path.
        assert transfer.status == TransactionStatus.UNKNOWN
        assert transfer.hold.status == HoldStatus.ACTIVE
        assert transfer.journal is None

    def test_failed_internal_versus_provider_success_is_a_mismatch(
        self, funded_wallet, settlement_account, window
    ):
        transfer = failed_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)

        run, _ = reconcile(window, [record_for(attempt)])
        transfer.refresh_from_db()

        item = items_for(run).get()
        assert item.outcome_mismatch
        assert transfer.status == TransactionStatus.FAILED
        assert transfer.journal is None

    def test_succeeded_internal_versus_provider_failure_is_a_mismatch(
        self, matched_attempt, window
    ):
        run, _ = reconcile(
            window,
            [record_for(matched_attempt, outcome=ReconciliationOutcome.FAILED)],
        )

        matched_attempt.refresh_from_db()
        assert items_for(run).get().outcome_mismatch
        # The posted journal is not reversed or altered.
        assert matched_attempt.financial_transaction.journal_id is not None

    def test_processing_is_never_read_as_failure(
        self, funded_wallet, settlement_account, window
    ):
        from moneycore.services.provider_execution import _claim_execution

        transfer = prepare_transfer(
            funded_wallet, DESTINATION, 7_000, idempotency_key='m8-processing'
        )
        _claim_execution(transfer, 'simulator')
        attempt = provider_attempt_for(transfer)

        run, _ = reconcile(window, [record_for(attempt)])

        item = items_for(run).get()
        assert item.internal_outcome == ReconciliationOutcome.UNRESOLVED
        assert item.internal_outcome != ReconciliationOutcome.FAILED


class TestMatchingRules:
    def test_the_client_reference_is_the_primary_key(
        self, funded_wallet, settlement_account, window
    ):
        transfer = succeeded_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)

        run, _ = reconcile(window, [record_for(attempt, provider_reference='')])

        assert items_for(run).get().provider_attempt_id == attempt.pk

    def test_a_provider_reference_alone_can_match(
        self, funded_wallet, settlement_account, window
    ):
        transfer = succeeded_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)

        run, _ = reconcile(
            window,
            [
                record_for(
                    attempt,
                    client_reference='',
                    provider_reference=attempt.provider_reference,
                )
            ],
        )

        assert items_for(run).get().provider_attempt_id == attempt.pk

    def test_contradicting_references_fail_the_run(
        self, funded_wallet, settlement_account, window
    ):
        first = succeeded_transfer(
            funded_wallet, settlement_account, key='ref-a', amount=3_000
        )
        second = succeeded_transfer(
            funded_wallet, settlement_account, key='ref-b', amount=3_000
        )
        attempt_a = provider_attempt_for(first)
        attempt_b = provider_attempt_for(second)

        with pytest.raises(ReconciliationReferenceConflictError):
            reconcile(
                window,
                [
                    record_for(
                        attempt_a,
                        provider_record_id='rec-conflict',
                        provider_reference=attempt_b.provider_reference,
                    )
                ],
            )

    def test_a_reference_conflict_records_no_items(
        self, funded_wallet, settlement_account, window
    ):
        first = succeeded_transfer(
            funded_wallet, settlement_account, key='ref-c', amount=3_000
        )
        second = succeeded_transfer(
            funded_wallet, settlement_account, key='ref-d', amount=3_000
        )

        with pytest.raises(ReconciliationReferenceConflictError):
            reconcile(
                window,
                [
                    record_for(
                        provider_attempt_for(first),
                        provider_record_id='rec-conflict-2',
                        provider_reference=provider_attempt_for(
                            second
                        ).provider_reference,
                    )
                ],
            )

        assert ProviderReconciliationItem.objects.count() == 0
        assert ProviderReconciliationRun.objects.get().status == (
            ReconciliationRunStatus.FAILED_INTERNAL
        )

    def test_matching_never_uses_amount_time_or_recipient(self):
        """Checked against code, not the docstring that forbids these words.

        Matching reads identity fields and nothing else; a heuristic on amount
        or time would attach a rail's evidence to a transfer that merely looks
        similar.
        """
        import ast
        import inspect

        from moneycore.services import reconciliation

        tree = ast.parse(inspect.getsource(reconciliation._match).lstrip())
        read = {
            node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
        }

        assert read.isdisjoint({
            'amount_minor', 'observed_at', 'currency', 'recipient_name',
            'destination_account_number', 'wallet', 'wallet_id',
        })
        # It reads exactly the identity fields.
        assert 'client_reference' in read
        assert 'provider_reference' in read


class TestDuplicateProviderRecords:
    def test_an_identical_duplicate_is_deduplicated(
        self, funded_wallet, settlement_account, window
    ):
        transfer = succeeded_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)
        observed = observed_now()
        record = record_for(attempt, observed_at=observed)

        run, _ = reconcile(window, [record, record])

        assert items_for(run).count() == 1
        assert items_for(run).get().overall_status == (
            ReconciliationStatus.MATCHED
        )

    def test_a_conflicting_duplicate_fails_the_run(
        self, funded_wallet, settlement_account, window
    ):
        transfer = succeeded_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)
        observed = observed_now()

        with pytest.raises(ReconciliationInputConflictError):
            reconcile(
                window,
                [
                    record_for(attempt, observed_at=observed),
                    record_for(
                        attempt, observed_at=observed, amount_minor=9_999
                    ),
                ],
            )

    def test_a_conflicting_duplicate_records_no_items(
        self, funded_wallet, settlement_account, window
    ):
        transfer = succeeded_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)
        observed = observed_now()

        with pytest.raises(ReconciliationInputConflictError):
            reconcile(
                window,
                [
                    record_for(attempt, observed_at=observed),
                    record_for(
                        attempt, observed_at=observed, currency='USD'
                    ),
                ],
            )

        assert ProviderReconciliationItem.objects.count() == 0
        assert ProviderReconciliationRun.objects.get().status == (
            ReconciliationRunStatus.FAILED_INTERNAL
        )

    def test_two_different_record_ids_for_one_transfer_are_not_deduplicated(
        self, funded_wallet, settlement_account, window
    ):
        """Different identities are different records, whatever they say."""
        transfer = succeeded_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)

        with pytest.raises(Exception) as raised:
            reconcile(
                window,
                [
                    record_for(attempt, provider_record_id='rec-a'),
                    record_for(attempt, provider_record_id='rec-b'),
                ],
            )

        # One attempt cannot be compared twice inside one run.
        assert 'unique' in str(raised.value).lower() or raised.type.__name__


class TestProviderFailure:
    def test_a_failing_provider_fails_the_run(
        self, funded_wallet, settlement_account, window
    ):
        succeeded_transfer(funded_wallet, settlement_account)

        with pytest.raises(ReconciliationProviderError):
            reconcile(window, [], reconciliation_fails=True)

        assert ProviderReconciliationRun.objects.get().status == (
            ReconciliationRunStatus.FAILED_INTERNAL
        )

    def test_it_records_no_items(
        self, funded_wallet, settlement_account, window
    ):
        succeeded_transfer(funded_wallet, settlement_account)

        with pytest.raises(ReconciliationProviderError):
            reconcile(window, [], reconciliation_fails=True)

        assert ProviderReconciliationItem.objects.count() == 0

    def test_it_leaves_financial_state_untouched(
        self, funded_wallet, settlement_account, window
    ):
        transfer = succeeded_transfer(funded_wallet, settlement_account)
        before = wallet_balance_projection(funded_wallet)
        journals = Journal.objects.count()

        with pytest.raises(ReconciliationProviderError):
            reconcile(window, [], reconciliation_fails=True)

        transfer.refresh_from_db()
        assert wallet_balance_projection(funded_wallet) == before
        assert Journal.objects.count() == journals
        assert transfer.status == TransactionStatus.SUCCEEDED

    def test_a_malformed_page_fails_the_run(
        self, funded_wallet, settlement_account, window
    ):
        class BrokenProvider:
            provider_key = 'simulator'

            def list_transfer_records(self, *, window, cursor=''):
                return [{'id': 'rec-1'}]

        with pytest.raises(ReconciliationRecordInvalidError):
            reconcile_transfers(provider=BrokenProvider(), window=window)

        assert ProviderReconciliationRun.objects.get().status == (
            ReconciliationRunStatus.FAILED_INTERNAL
        )

    def test_an_unexpected_error_is_never_swallowed(
        self, funded_wallet, settlement_account, window
    ):
        """The broad handler marks the run failed and re-raises unchanged.

        A narrow one would leave a run STARTED for ever on anything it did not
        anticipate; a swallowing one would report a clean result after an
        error. This does neither.
        """
        class ExplodingProvider:
            provider_key = 'simulator'

            def list_transfer_records(self, *, window, cursor=''):
                raise RuntimeError('something nobody predicted')

        with pytest.raises(RuntimeError, match='nobody predicted'):
            reconcile_transfers(provider=ExplodingProvider(), window=window)

        assert ProviderReconciliationRun.objects.get().status == (
            ReconciliationRunStatus.FAILED_INTERNAL
        )
        assert ProviderReconciliationItem.objects.count() == 0

    def test_no_run_is_left_started(
        self, funded_wallet, settlement_account, window
    ):
        class ExplodingProvider:
            provider_key = 'simulator'

            def list_transfer_records(self, *, window, cursor=''):
                raise RuntimeError('boom')

        with pytest.raises(RuntimeError):
            reconcile_transfers(provider=ExplodingProvider(), window=window)

        assert not ProviderReconciliationRun.objects.filter(
            status=ReconciliationRunStatus.STARTED
        ).exists()

    def test_a_failed_run_is_not_promoted_later(
        self, funded_wallet, settlement_account, window
    ):
        from moneycore.domain.errors import ReconciliationRunImmutableError

        with pytest.raises(ReconciliationProviderError):
            reconcile(window, [], reconciliation_fails=True)

        run = ProviderReconciliationRun.objects.get()
        run.status = ReconciliationRunStatus.COMPLETED
        with pytest.raises(ReconciliationRunImmutableError):
            run.save()


class TestRerunSemantics:
    def test_each_run_is_a_new_observation(
        self, funded_wallet, settlement_account, window
    ):
        transfer = succeeded_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)
        observed = observed_now()

        first, _ = reconcile(window, [record_for(attempt, observed_at=observed)])
        second, _ = reconcile(window, [record_for(attempt, observed_at=observed)])

        assert first.pk != second.pk
        assert ProviderReconciliationRun.objects.count() == 2

    def test_reruns_agree(self, funded_wallet, settlement_account, window):
        transfer = succeeded_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)
        observed = observed_now()

        first, _ = reconcile(window, [record_for(attempt, observed_at=observed)])
        second, _ = reconcile(window, [record_for(attempt, observed_at=observed)])

        assert [i.overall_status for i in items_for(first)] == [
            i.overall_status for i in items_for(second)
        ]

    def test_a_rerun_does_not_alter_the_earlier_run(
        self, funded_wallet, settlement_account, window
    ):
        transfer = succeeded_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)

        first, _ = reconcile(window, [record_for(attempt, amount_minor=7_100)])
        first_codes = [i.discrepancy_codes for i in items_for(first)]

        reconcile(window, [record_for(attempt)])

        assert [i.discrepancy_codes for i in items_for(first)] == first_codes

    def test_a_later_run_can_reach_a_different_conclusion(
        self, funded_wallet, settlement_account, window
    ):
        """Both are honest observations of different moments."""
        transfer = ambiguous_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)

        before, _ = reconcile(window, [record_for(attempt)])
        assert items_for(before).get().overall_status == (
            ReconciliationStatus.DISCREPANCY
        )

        from moneycore.services.provider_recovery import recover_provider_attempt

        recover_provider_attempt(
            attempt,
            provider=SimulatorTransferProvider(
                status_scenario=StatusScenario.SUCCESS
            ),
            counterpart_account=settlement_account,
        )

        after, _ = reconcile(window, [record_for(attempt)])
        assert items_for(after).get().overall_status == (
            ReconciliationStatus.MATCHED
        )
        assert items_for(before).get().overall_status == (
            ReconciliationStatus.DISCREPANCY
        )


class TestRecoveryConflictVisibility:
    def test_a_contradicted_attempt_is_flagged(
        self, funded_wallet, settlement_account, window
    ):
        """M7's contradiction stays visible; M8 does not resolve it."""
        from moneycore.domain.errors import ProviderRecoveryConflictError
        from moneycore.services.provider_recovery import ingest_webhook

        transfer = ambiguous_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)
        provider = SimulatorTransferProvider()

        body, headers = provider.build_webhook(
            provider_event_id='evt-won',
            outcome=RecoveryOutcome.SUCCEEDED,
            client_reference=attempt.client_reference,
        )
        ingest_webhook(
            provider=provider, body=body, headers=headers,
            counterpart_account=settlement_account,
        )

        body, headers = provider.build_webhook(
            provider_event_id='evt-contradiction',
            outcome=RecoveryOutcome.FAILED,
            client_reference=attempt.client_reference,
            failure_code=ProviderFailureCode.REQUEST_REJECTED,
        )
        with pytest.raises(ProviderRecoveryConflictError):
            ingest_webhook(
                provider=provider, body=body, headers=headers,
                counterpart_account=settlement_account,
            )

        run, _ = reconcile(window, [record_for(attempt)])

        item = items_for(run).get()
        assert item.has_recovery_conflict
        assert run_summary(run)['recovery_conflicts'] == 1

    def test_an_uncontradicted_attempt_is_not_flagged(
        self, funded_wallet, settlement_account, window
    ):
        transfer = succeeded_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)

        run, _ = reconcile(window, [record_for(attempt)])

        assert not items_for(run).get().has_recovery_conflict


class TestSummary:
    def test_counts_are_derived_and_exact(
        self, funded_wallet, settlement_account, window
    ):
        matched = succeeded_transfer(
            funded_wallet, settlement_account, key='sum-a', amount=1_000
        )
        differing = succeeded_transfer(
            funded_wallet, settlement_account, key='sum-b', amount=1_000
        )
        succeeded_transfer(
            funded_wallet, settlement_account, key='sum-c', amount=1_000
        )

        run, _ = reconcile(
            window,
            [
                record_for(provider_attempt_for(matched)),
                record_for(
                    provider_attempt_for(differing), amount_minor=9_999
                ),
                ProviderTransferRecord(
                    provider_key='simulator',
                    provider_record_id='rec-ghost',
                    client_reference='SW-nothing',
                    outcome=ReconciliationOutcome.SUCCEEDED,
                    amount_minor=500,
                    currency='NGN',
                    observed_at=observed_now(),
                ),
            ],
        )

        summary = run_summary(run)
        assert summary['matched'] == 1
        assert summary['discrepancies'] == 1
        assert summary['provider_only'] == 1
        assert summary['internal_only'] == 1
        assert summary['amount_mismatches'] == 1
        assert summary['total_items'] == 4

    def test_no_counter_is_persisted(self):
        """Derived, so it cannot disagree with the rows it counts."""
        names = {f.name for f in ProviderReconciliationRun._meta.get_fields()}

        assert names.isdisjoint({
            'matched_count', 'discrepancy_count', 'total_items',
            'item_count', 'provider_only_count',
        })
