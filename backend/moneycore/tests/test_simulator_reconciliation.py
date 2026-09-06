"""The simulator's reconciliation data, and the demo it makes possible.

The story worth telling: an ambiguous transfer, later resolved, reconciling
clean — and then a deliberately corrupted provider record producing a
discrepancy while the ledger stays exactly where it was.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from moneycore.domain.holds import HoldStatus
from moneycore.domain.ledger import LedgerAccountType
from moneycore.domain.money import Money
from moneycore.domain.reconciliation import (
    ProviderTransferRecord,
    ProviderTransferRecordPage,
    ReconciliationOutcome,
    ReconciliationStatus,
    ReconciliationWindow,
)
from moneycore.domain.transactions import TransactionStatus
from moneycore.domain.transfers import VerifiedBankAccount
from moneycore.models import Journal
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
from moneycore.services.provider_recovery import recover_provider_attempt
from moneycore.services.reconciliation import (
    items_for,
    reconcile_transfers,
    run_summary,
)
from moneycore.services.transfers import prepare_transfer

DESTINATION = VerifiedBankAccount(
    account_number='0123456789',
    bank_code='SIM-001',
    bank_name='Simulated First Bank',
    account_name='Ada Okafor',
)


def a_record(**overrides):
    values = {
        'provider_record_id': 'rec-1',
        'client_reference': 'SW-abc',
        'outcome': ReconciliationOutcome.SUCCEEDED,
        'amount_minor': 7_000,
        'currency': 'NGN',
        'observed_at': timezone.now(),
    }
    values.update(overrides)
    return SimulatorTransferProvider().record_matching(**values)


class TestListingIsDeterministic:
    def test_it_returns_what_was_configured(self):
        record = a_record()
        provider = SimulatorTransferProvider(reconciliation_records=[record])
        window = ReconciliationWindow(
            start=record.observed_at - timedelta(hours=1),
            end=record.observed_at + timedelta(hours=1),
        )

        page = provider.list_transfer_records(window=window)

        assert page.records == (record,)
        assert page.next_cursor == ''

    def test_it_invents_nothing(self):
        provider = SimulatorTransferProvider(reconciliation_records=[])
        now = timezone.now()
        window = ReconciliationWindow(
            start=now - timedelta(hours=1), end=now + timedelta(hours=1)
        )

        assert provider.list_transfer_records(window=window).records == ()

    def test_it_filters_by_the_half_open_window(self):
        now = timezone.now()
        inside = a_record(provider_record_id='in', observed_at=now)
        on_the_end = a_record(
            provider_record_id='edge', observed_at=now + timedelta(hours=1)
        )
        provider = SimulatorTransferProvider(
            reconciliation_records=[inside, on_the_end]
        )
        window = ReconciliationWindow(
            start=now, end=now + timedelta(hours=1)
        )

        records = provider.list_transfer_records(window=window).records

        assert [r.provider_record_id for r in records] == ['in']

    def test_the_same_window_always_gives_the_same_pages(self):
        records = [
            a_record(provider_record_id=f'rec-{index}') for index in range(3)
        ]
        provider = SimulatorTransferProvider(reconciliation_records=records)
        now = timezone.now()
        window = ReconciliationWindow(
            start=now - timedelta(hours=1), end=now + timedelta(hours=1)
        )

        first = provider.list_transfer_records(window=window)
        second = provider.list_transfer_records(window=window)

        assert first == second

    def test_paging_covers_every_record_exactly_once(self):
        records = [
            a_record(provider_record_id=f'rec-{index}') for index in range(5)
        ]
        provider = SimulatorTransferProvider(
            reconciliation_records=records, reconciliation_page_size=2
        )
        now = timezone.now()
        window = ReconciliationWindow(
            start=now - timedelta(hours=1), end=now + timedelta(hours=1)
        )

        seen, cursor = [], ''
        while True:
            page = provider.list_transfer_records(window=window, cursor=cursor)
            seen.extend(r.provider_record_id for r in page.records)
            cursor = page.next_cursor
            if not cursor:
                break

        assert sorted(seen) == [f'rec-{index}' for index in range(5)]
        assert len(seen) == len(set(seen))

    def test_a_configured_failure_raises(self):
        from moneycore.domain.errors import ReconciliationProviderError

        provider = SimulatorTransferProvider(reconciliation_fails=True)
        now = timezone.now()
        window = ReconciliationWindow(
            start=now - timedelta(hours=1), end=now + timedelta(hours=1)
        )

        with pytest.raises(ReconciliationProviderError):
            provider.list_transfer_records(window=window)

    def test_listing_never_submits(self):
        provider = SimulatorTransferProvider(reconciliation_records=[a_record()])
        now = timezone.now()
        window = ReconciliationWindow(
            start=now - timedelta(hours=1), end=now + timedelta(hours=1)
        )

        for _ in range(3):
            provider.list_transfer_records(window=window)

        assert provider.submit_call_count == 0
        assert provider.reconciliation_call_count == 3

    def test_it_performs_no_io_and_no_sleeping(self):
        from pathlib import Path

        from moneycore.providers import simulator

        source = Path(simulator.__file__).read_text(encoding='utf-8')
        for forbidden in (
            'time.sleep', 'import requests', 'import httpx', 'socket',
            'urllib', 'while True', 'import random', 'random.', 'uuid4',
        ):
            assert forbidden not in source, forbidden


@pytest.mark.django_db
class TestTheInvestorStory:
    """Ambiguity, resolution, a clean reconciliation — then a corrupted one."""

    @pytest.fixture
    def settlement_account(self, db):
        return open_ledger_account(
            code='internal:m8-demo:NGN',
            name='Internal counterpart',
            account_type=LedgerAccountType.ASSET,
            currency='NGN',
        )

    @pytest.fixture
    def window(self):
        now = timezone.now()
        return ReconciliationWindow(
            start=now - timedelta(hours=1), end=now + timedelta(hours=1)
        )

    def _resolved_transfer(self, wallet, counterpart, key='demo'):
        transfer = prepare_transfer(
            wallet, DESTINATION, 7_000, idempotency_key=key
        )
        execute_transfer(
            transfer,
            provider=SimulatorTransferProvider(
                transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
            ),
            counterpart_account=counterpart,
        )
        transfer.refresh_from_db()
        assert transfer.status == TransactionStatus.UNKNOWN

        recover_provider_attempt(
            provider_attempt_for(transfer),
            provider=SimulatorTransferProvider(
                status_scenario=StatusScenario.SUCCESS
            ),
            counterpart_account=counterpart,
        )
        transfer.refresh_from_db()
        return transfer

    def test_the_whole_story_reconciles_clean(
        self, funded_wallet, settlement_account, window
    ):
        transfer = self._resolved_transfer(funded_wallet, settlement_account)
        attempt = provider_attempt_for(transfer)

        # The transfer resolved once, without ever being resubmitted.
        assert transfer.status == TransactionStatus.SUCCEEDED
        assert Journal.objects.count() == 2  # funding + this transfer
        assert wallet_balance_projection(
            funded_wallet
        ).posted == Money(3_000, 'NGN')

        provider = SimulatorTransferProvider(
            reconciliation_records=[
                SimulatorTransferProvider().record_matching(
                    provider_record_id='rec-demo',
                    client_reference=attempt.client_reference,
                    outcome=ReconciliationOutcome.SUCCEEDED,
                    amount_minor=7_000,
                    currency='NGN',
                    observed_at=timezone.now(),
                )
            ]
        )
        run = reconcile_transfers(provider=provider, window=window)

        assert items_for(run).get().overall_status == (
            ReconciliationStatus.MATCHED
        )
        assert run_summary(run)['matched'] == 1
        assert run_summary(run)['discrepancies'] == 0

    def test_a_corrupted_amount_shows_a_discrepancy(
        self, funded_wallet, settlement_account, window
    ):
        """7 100 instead of 7 000 — reported, and the ledger left alone."""
        transfer = self._resolved_transfer(
            funded_wallet, settlement_account, key='demo-bad'
        )
        attempt = provider_attempt_for(transfer)
        before = wallet_balance_projection(funded_wallet)
        journals = Journal.objects.count()

        provider = SimulatorTransferProvider(
            reconciliation_records=[
                SimulatorTransferProvider().record_matching(
                    provider_record_id='rec-demo-bad',
                    client_reference=attempt.client_reference,
                    outcome=ReconciliationOutcome.SUCCEEDED,
                    amount_minor=7_100,
                    currency='NGN',
                    observed_at=timezone.now(),
                )
            ]
        )
        run = reconcile_transfers(provider=provider, window=window)

        item = items_for(run).get()
        assert item.overall_status == ReconciliationStatus.DISCREPANCY
        assert item.discrepancy_codes == ('amount_mismatch',)
        assert item.provider_amount_minor == 7_100
        assert item.internal_amount_minor == 7_000

        # The discipline the demo exists to show.
        transfer.refresh_from_db()
        assert wallet_balance_projection(funded_wallet) == before
        assert Journal.objects.count() == journals
        assert transfer.status == TransactionStatus.SUCCEEDED

    def test_the_story_reproduces_identically(
        self, funded_wallet, settlement_account, window
    ):
        outcomes = []
        for index in range(2):
            transfer = prepare_transfer(
                funded_wallet, DESTINATION, 3_000,
                idempotency_key=f'demo-repeat-{index}',
            )
            execute_transfer(
                transfer,
                provider=SimulatorTransferProvider(
                    transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
                ),
                counterpart_account=settlement_account,
            )
            recover_provider_attempt(
                provider_attempt_for(transfer),
                provider=SimulatorTransferProvider(
                    status_scenario=StatusScenario.SUCCESS
                ),
                counterpart_account=settlement_account,
            )
            attempt = provider_attempt_for(transfer)
            run = reconcile_transfers(
                provider=SimulatorTransferProvider(
                    reconciliation_records=[
                        SimulatorTransferProvider().record_matching(
                            provider_record_id=f'rec-repeat-{index}',
                            client_reference=attempt.client_reference,
                            outcome=ReconciliationOutcome.SUCCEEDED,
                            amount_minor=3_000,
                            currency='NGN',
                            observed_at=timezone.now(),
                        )
                    ]
                ),
                window=window,
            )
            outcomes.append(
                items_for(run).filter(
                    provider_record_id=f'rec-repeat-{index}'
                ).get().overall_status
            )

        assert outcomes == [
            ReconciliationStatus.MATCHED, ReconciliationStatus.MATCHED
        ]
