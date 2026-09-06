"""The reconciliation vocabulary, records, windows and comparison.

Two properties carry this file: a provider record is validated evidence rather
than a payload, and comparison is deterministic and reports *every* difference
rather than the first one it happens to find.
"""

from datetime import datetime, timedelta, timezone as tz

import pytest

from moneycore.domain.errors import (
    ReconciliationRecordInvalidError,
    ReconciliationWindowInvalidError,
)
from moneycore.domain.reconciliation import (
    DISCREPANCY_FLAGS,
    Comparison,
    InternalTransferView,
    ProviderTransferRecord,
    ProviderTransferRecordPage,
    ReconciliationOutcome,
    ReconciliationRunStatus,
    ReconciliationStatus,
    ReconciliationWindow,
    TransferReconciliationProvider,
    compare,
)

OBSERVED = datetime(2026, 1, 1, 12, 0, tzinfo=tz.utc)


def make_record(**overrides):
    values = {
        'provider_key': 'simulator',
        'provider_record_id': 'rec-1',
        'outcome': ReconciliationOutcome.SUCCEEDED,
        'amount_minor': 7_000,
        'currency': 'NGN',
        'observed_at': OBSERVED,
        'client_reference': 'SW-abc123',
    }
    values.update(overrides)
    return ProviderTransferRecord(**values)


def make_view(**overrides):
    values = {
        'attempt_id': 1,
        'client_reference': 'SW-abc123',
        'provider_reference': '',
        'outcome': ReconciliationOutcome.SUCCEEDED,
        'amount_minor': 7_000,
        'currency': 'NGN',
    }
    values.update(overrides)
    return InternalTransferView(**values)


class TestVocabulary:
    def test_outcomes_are_the_same_three_as_everywhere_else(self):
        assert ReconciliationOutcome.ALL == {
            'succeeded', 'failed', 'unresolved'
        }

    def test_statuses_are_exactly_four(self):
        assert ReconciliationStatus.ALL == {
            'matched', 'discrepancy', 'provider_only', 'internal_only'
        }

    def test_run_statuses_are_exactly_three(self):
        assert ReconciliationRunStatus.ALL == {
            'started', 'completed', 'failed_internal'
        }

    @pytest.mark.parametrize(
        'forbidden',
        ['partial_success', 'retrying', 'queued', 'reconciled', 'pending'],
    )
    def test_no_speculative_run_state_exists(self, forbidden):
        assert forbidden not in ReconciliationRunStatus.ALL

    def test_a_failed_run_is_terminal(self):
        assert ReconciliationRunStatus.FAILED_INTERNAL in (
            ReconciliationRunStatus.TERMINAL
        )
        assert ReconciliationRunStatus.STARTED not in (
            ReconciliationRunStatus.TERMINAL
        )

    def test_there_are_exactly_four_discrepancy_flags(self):
        assert DISCREPANCY_FLAGS == (
            'outcome_mismatch',
            'amount_mismatch',
            'currency_mismatch',
            'reference_mismatch',
        )


class TestProviderRecordValidation:
    def test_a_well_formed_record_is_accepted(self):
        record = make_record()

        assert record.identity == ('simulator', 'rec-1')
        assert record.amount_minor == 7_000

    def test_it_is_frozen(self):
        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            make_record().amount_minor = 1

    @pytest.mark.parametrize('value', ['', '   ', None, 42])
    def test_a_missing_record_id_is_refused(self, value):
        with pytest.raises(ReconciliationRecordInvalidError):
            make_record(provider_record_id=value)

    @pytest.mark.parametrize('value', ['', '   ', None])
    def test_a_missing_provider_key_is_refused(self, value):
        with pytest.raises(ReconciliationRecordInvalidError):
            make_record(provider_key=value)

    @pytest.mark.parametrize('outcome', ['settling', 'pending', '', None, 1])
    def test_an_unknown_outcome_is_refused(self, outcome):
        with pytest.raises(ReconciliationRecordInvalidError):
            make_record(outcome=outcome)

    @pytest.mark.parametrize('amount', [0, -1, 70.0, '7000', True, None])
    def test_a_non_integer_or_non_positive_amount_is_refused(self, amount):
        with pytest.raises(ReconciliationRecordInvalidError):
            make_record(amount_minor=amount)

    def test_a_decimal_amount_is_refused(self):
        from decimal import Decimal

        with pytest.raises(ReconciliationRecordInvalidError):
            make_record(amount_minor=Decimal('70.00'))

    def test_money_stays_integer_minor_units(self):
        record = make_record()

        assert isinstance(record.amount_minor, int)
        assert not isinstance(record.amount_minor, bool)

    @pytest.mark.parametrize('currency', ['ngn', 'NG', 'NGNN', '', None])
    def test_an_invalid_currency_is_refused(self, currency):
        with pytest.raises(ReconciliationRecordInvalidError):
            make_record(currency=currency)

    def test_a_non_ngn_currency_is_accepted(self):
        """The money domain is generic; reconciliation does not narrow it."""
        assert make_record(currency='USD').currency == 'USD'

    def test_a_naive_observation_time_is_refused(self):
        with pytest.raises(ReconciliationRecordInvalidError):
            make_record(observed_at=datetime(2026, 1, 1, 12, 0))

    def test_a_non_datetime_observation_time_is_refused(self):
        with pytest.raises(ReconciliationRecordInvalidError):
            make_record(observed_at='2026-01-01')

    def test_a_record_naming_nothing_is_refused(self):
        with pytest.raises(ReconciliationRecordInvalidError):
            make_record(client_reference='', provider_reference='')

    def test_a_provider_reference_alone_is_enough(self):
        record = make_record(client_reference='', provider_reference='SIM-9')

        assert record.provider_reference == 'SIM-9'

    def test_control_characters_are_refused(self):
        with pytest.raises(ReconciliationRecordInvalidError):
            make_record(client_reference='SW-a\nb')

    def test_it_carries_no_raw_payload(self):
        names = set(vars(make_record()))

        assert names.isdisjoint({
            'payload', 'raw', 'body', 'json', 'response', 'headers',
            'provider_status', 'native_status',
        })

    def test_identity_ignores_amount_and_time(self):
        """Identity is the pair, and nothing else."""
        first = make_record()
        second = make_record(amount_minor=9_999, observed_at=OBSERVED + timedelta(hours=1))

        assert first.identity == second.identity

    def test_comparable_covers_everything_two_deliveries_must_agree_on(self):
        record = make_record()

        assert record.comparable == (
            record.outcome,
            record.amount_minor,
            record.currency,
            record.client_reference,
            record.provider_reference,
            record.failure_code,
            record.observed_at,
        )


class TestRecordPage:
    def test_it_holds_records(self):
        page = ProviderTransferRecordPage(records=(make_record(),))

        assert len(page.records) == 1
        assert page.next_cursor == ''

    def test_a_non_record_is_refused(self):
        with pytest.raises(ReconciliationRecordInvalidError):
            ProviderTransferRecordPage(records=({'id': 'rec-1'},))

    def test_a_list_is_normalised_to_a_tuple(self):
        page = ProviderTransferRecordPage(records=[make_record()])

        assert isinstance(page.records, tuple)


class TestWindow:
    def test_a_valid_window_is_accepted(self):
        window = ReconciliationWindow(
            start=OBSERVED, end=OBSERVED + timedelta(days=1)
        )

        assert window.start < window.end

    def test_it_is_half_open(self):
        """Consecutive windows tile without overlap or gap."""
        start = OBSERVED
        end = OBSERVED + timedelta(days=1)
        window = ReconciliationWindow(start=start, end=end)

        assert window.contains(start)
        assert not window.contains(end)
        assert window.contains(end - timedelta(microseconds=1))

    def test_an_end_before_the_start_is_refused(self):
        with pytest.raises(ReconciliationWindowInvalidError):
            ReconciliationWindow(start=OBSERVED, end=OBSERVED - timedelta(days=1))

    def test_an_empty_window_is_refused(self):
        with pytest.raises(ReconciliationWindowInvalidError):
            ReconciliationWindow(start=OBSERVED, end=OBSERVED)

    @pytest.mark.parametrize('field', ['start', 'end'])
    def test_a_naive_bound_is_refused(self, field):
        bounds = {'start': OBSERVED, 'end': OBSERVED + timedelta(days=1)}
        bounds[field] = datetime(2026, 1, 1, 12, 0)

        with pytest.raises(ReconciliationWindowInvalidError):
            ReconciliationWindow(**bounds)

    @pytest.mark.parametrize('field', ['start', 'end'])
    def test_a_non_datetime_bound_is_refused(self, field):
        bounds = {'start': OBSERVED, 'end': OBSERVED + timedelta(days=1)}
        bounds[field] = '2026-01-01'

        with pytest.raises(ReconciliationWindowInvalidError):
            ReconciliationWindow(**bounds)

    def test_no_maximum_window_length_is_invented(self):
        """Production window sizing is not this milestone's to decide."""
        window = ReconciliationWindow(
            start=OBSERVED, end=OBSERVED + timedelta(days=3650)
        )

        assert window.end > window.start


class TestComparison:
    def test_full_agreement_is_matched(self):
        result = compare(make_record(), make_view())

        assert result.overall_status == ReconciliationStatus.MATCHED
        assert not result.has_discrepancy
        assert result.flags == (False, False, False, False)

    def test_an_outcome_difference_is_a_discrepancy(self):
        result = compare(
            make_record(outcome=ReconciliationOutcome.FAILED), make_view()
        )

        assert result.overall_status == ReconciliationStatus.DISCREPANCY
        assert result.outcome_mismatch
        assert not result.amount_mismatch

    def test_an_amount_difference_is_a_discrepancy(self):
        result = compare(make_record(amount_minor=7_100), make_view())

        assert result.overall_status == ReconciliationStatus.DISCREPANCY
        assert result.amount_mismatch

    def test_a_currency_difference_is_a_discrepancy(self):
        result = compare(make_record(currency='USD'), make_view())

        assert result.currency_mismatch

    def test_a_contradicting_provider_reference_is_a_discrepancy(self):
        result = compare(
            make_record(provider_reference='SIM-other'),
            make_view(provider_reference='SIM-ours'),
        )

        assert result.reference_mismatch

    def test_an_absent_reference_on_either_side_is_not_a_mismatch(self):
        assert not compare(
            make_record(provider_reference=''),
            make_view(provider_reference='SIM-ours'),
        ).reference_mismatch
        assert not compare(
            make_record(provider_reference='SIM-theirs'),
            make_view(provider_reference=''),
        ).reference_mismatch

    def test_every_difference_is_reported_not_just_the_first(self):
        """A single enum would have had to discard one of these."""
        result = compare(
            make_record(
                outcome=ReconciliationOutcome.FAILED,
                amount_minor=7_100,
                currency='USD',
                provider_reference='SIM-other',
            ),
            make_view(provider_reference='SIM-ours'),
        )

        assert result.overall_status == ReconciliationStatus.DISCREPANCY
        assert result.flags == (True, True, True, True)

    def test_it_is_deterministic(self):
        record, view = make_record(amount_minor=7_100), make_view()

        assert compare(record, view) == compare(record, view)

    def test_it_does_not_depend_on_field_order_or_identity(self):
        first = compare(make_record(currency='USD'), make_view())
        second = compare(make_record(currency='USD'), make_view(attempt_id=99))

        assert first.overall_status == second.overall_status
        assert first.flags == second.flags

    def test_a_matched_comparison_never_carries_a_flag(self):
        result = compare(make_record(), make_view())

        assert not any(result.flags)

    def test_a_discrepancy_always_carries_at_least_one_flag(self):
        result = compare(make_record(amount_minor=1), make_view())

        assert result.overall_status == ReconciliationStatus.DISCREPANCY
        assert any(result.flags)


class TestInternalView:
    def test_it_carries_no_model_instance(self):
        """Nothing downstream can reach a financial row through it."""
        from django.db.models import Model

        view = make_view()

        for value in vars(view).values():
            assert not isinstance(value, Model)

    def test_it_names_no_wallet_transfer_or_journal(self):
        names = set(vars(make_view()))

        assert names.isdisjoint({
            'wallet', 'transfer', 'journal', 'hold', 'transaction',
            'financial_transaction',
        })

    def test_it_is_frozen(self):
        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            make_view().outcome = ReconciliationOutcome.FAILED


class TestTheInterfaceIsReadOnly:
    def test_it_declares_exactly_one_operation(self):
        methods = {
            name for name in dir(TransferReconciliationProvider)
            if not name.startswith('_')
        }

        assert methods == {'list_transfer_records'}

    @pytest.mark.parametrize(
        'forbidden',
        ['submit_transfer', 'correct', 'adjust', 'reverse', 'settle',
         'update_transfer', 'mark_matched'],
    )
    def test_no_mutating_operation_exists(self, forbidden):
        assert not hasattr(TransferReconciliationProvider, forbidden)

    def test_no_vendor_is_named(self):
        from pathlib import Path

        from moneycore.domain import reconciliation

        source = Path(reconciliation.__file__).read_text(encoding='utf-8').lower()
        for brand in (
            'paystack', 'flutterwave', 'monnify', 'interswitch', 'kuda',
            'nomba', 'stripe',
        ):
            assert brand not in source
