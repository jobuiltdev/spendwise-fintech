"""The recovery vocabulary, and the boundary that cannot submit.

Two properties matter most here: an unresolved lookup is its own type rather
than a failure, and the recovery Protocol has no submission method at all.
"""

import pytest

from moneycore.domain.errors import (
    ProviderRecoveryResultInvalidError,
    ProviderWebhookInvalidError,
)
from moneycore.domain.providers import ProviderFailureCode
from moneycore.domain.recovery import (
    NOT_FOUND_IS_DEFINITIVE,
    EvidenceSource,
    NormalisedWebhookEvent,
    ProviderTransferStatusFailed,
    ProviderTransferStatusSucceeded,
    ProviderTransferStatusUnresolved,
    RecoveryOutcome,
    TransferRecoveryProvider,
    WebhookProcessingStatus,
    not_found_is_unresolved,
    require_status_result,
)


def make_event(**overrides):
    values = {
        'provider_key': 'simulator',
        'provider_event_id': 'evt-1',
        'outcome': RecoveryOutcome.SUCCEEDED,
        'client_reference': 'SW-abc123',
    }
    values.update(overrides)
    return NormalisedWebhookEvent(**values)


class TestOutcomeVocabulary:
    def test_the_outcomes_are_exactly_three(self):
        assert RecoveryOutcome.ALL == {'succeeded', 'failed', 'unresolved'}

    def test_only_two_are_definitive(self):
        assert RecoveryOutcome.DEFINITIVE == {'succeeded', 'failed'}
        assert RecoveryOutcome.UNRESOLVED not in RecoveryOutcome.DEFINITIVE

    @pytest.mark.parametrize(
        'forbidden',
        ['pending', 'settling', 'confirming', 'reconciled', 'retrying',
         'queued', 'processing'],
    )
    def test_no_second_lifecycle_state_exists(self, forbidden):
        assert forbidden not in RecoveryOutcome.ALL

    def test_evidence_has_exactly_two_sources(self):
        assert EvidenceSource.ALL == {'status_query', 'webhook'}

    def test_webhook_processing_has_no_retry_or_queue_state(self):
        assert WebhookProcessingStatus.ALL == {
            'received', 'processed', 'conflicted', 'unmatched'
        }
        assert WebhookProcessingStatus.ALL.isdisjoint({
            'pending', 'retrying', 'queued', 'dead_letter', 'invalid'
        })


class TestStatusResultsAreDistinctTypes:
    def test_success_carries_no_failure_code_field(self):
        result = ProviderTransferStatusSucceeded(provider_reference='SIM-1')

        assert not hasattr(result, 'failure_code')
        assert result.outcome == RecoveryOutcome.SUCCEEDED

    def test_success_cannot_be_given_one(self):
        with pytest.raises(TypeError):
            ProviderTransferStatusSucceeded(failure_code='request_rejected')

    def test_failure_requires_a_normalised_code(self):
        result = ProviderTransferStatusFailed(
            failure_code=ProviderFailureCode.DESTINATION_REJECTED
        )

        assert result.outcome == RecoveryOutcome.FAILED
        assert result.failure_code == 'destination_rejected'

    @pytest.mark.parametrize(
        'code', ['timeout', 'not_found', '', None, 404, 'UNRESOLVED'],
    )
    def test_an_unnormalised_failure_code_is_refused(self, code):
        with pytest.raises(ProviderRecoveryResultInvalidError):
            ProviderTransferStatusFailed(failure_code=code)

    def test_unresolved_is_its_own_type(self):
        result = ProviderTransferStatusUnresolved(reason='still_processing')

        assert result.outcome == RecoveryOutcome.UNRESOLVED
        assert not isinstance(result, ProviderTransferStatusFailed)
        assert not isinstance(result, ProviderTransferStatusSucceeded)
        assert not hasattr(result, 'failure_code')

    def test_unresolved_cannot_borrow_a_failure_code_as_its_reason(self):
        with pytest.raises(ProviderRecoveryResultInvalidError):
            ProviderTransferStatusUnresolved(
                reason=ProviderFailureCode.REQUEST_REJECTED
            )

    def test_there_is_no_boolean_success_flag(self):
        for result in (
            ProviderTransferStatusSucceeded(),
            ProviderTransferStatusFailed(failure_code='request_rejected'),
            ProviderTransferStatusUnresolved(),
        ):
            for forbidden in ('success', 'succeeded', 'ok', 'is_success'):
                assert not hasattr(result, forbidden), forbidden

    @pytest.mark.parametrize(
        'value', [None, True, False, 'succeeded', 0, {'status': 'ok'}, []],
    )
    def test_anything_else_is_refused(self, value):
        with pytest.raises(ProviderRecoveryResultInvalidError):
            require_status_result(value)

    def test_they_are_frozen(self):
        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            ProviderTransferStatusSucceeded().provider_reference = 'x'


class TestNotFoundIsNotFailure:
    """The distinction the brief singles out, and the reason it matters."""

    def test_not_found_is_not_treated_as_definitive(self):
        assert NOT_FOUND_IS_DEFINITIVE is False
        assert not_found_is_unresolved() is True

    def test_there_is_no_not_found_failure_code(self):
        assert 'not_found' not in ProviderFailureCode.ALL
        assert 'no_record' not in ProviderFailureCode.ALL

    def test_the_reasoning_is_recorded_where_adapters_will_read_it(self):
        from pathlib import Path

        from moneycore.domain import recovery

        source = Path(recovery.__file__).read_text(encoding='utf-8')

        assert 'NOT_FOUND_IS_DEFINITIVE' in source
        assert 'indexing delay' in source


class TestNormalisedWebhookEvent:
    def test_a_well_formed_event_is_accepted(self):
        event = make_event()

        assert event.provider_event_id == 'evt-1'
        assert event.is_definitive

    def test_it_is_frozen(self):
        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            make_event().outcome = RecoveryOutcome.FAILED

    def test_an_event_id_is_required(self):
        """Without one, redelivery cannot be told from a second real event."""
        with pytest.raises(ProviderWebhookInvalidError):
            make_event(provider_event_id='')

    def test_an_unknown_outcome_is_refused(self):
        with pytest.raises(ProviderWebhookInvalidError):
            make_event(outcome='settling')

    def test_a_failed_event_needs_a_normalised_code(self):
        with pytest.raises(ProviderWebhookInvalidError):
            make_event(outcome=RecoveryOutcome.FAILED, failure_code='')

    def test_a_failed_event_accepts_a_normalised_code(self):
        event = make_event(
            outcome=RecoveryOutcome.FAILED,
            failure_code=ProviderFailureCode.REQUEST_REJECTED,
        )

        assert event.failure_code == 'request_rejected'

    @pytest.mark.parametrize(
        'outcome', [RecoveryOutcome.SUCCEEDED, RecoveryOutcome.UNRESOLVED],
    )
    def test_only_a_failed_event_may_carry_a_failure_code(self, outcome):
        with pytest.raises(ProviderWebhookInvalidError):
            make_event(outcome=outcome, failure_code='request_rejected')

    def test_an_event_naming_nothing_is_refused(self):
        with pytest.raises(ProviderWebhookInvalidError):
            make_event(client_reference='', provider_reference='')

    def test_a_provider_reference_alone_is_enough(self):
        event = make_event(client_reference='', provider_reference='SIM-9')

        assert event.provider_reference == 'SIM-9'

    def test_control_characters_are_refused(self):
        with pytest.raises(ProviderRecoveryResultInvalidError):
            make_event(client_reference='SW-a\nb')

    def test_unresolved_events_are_not_definitive(self):
        assert not make_event(outcome=RecoveryOutcome.UNRESOLVED).is_definitive

    def test_it_carries_no_payload_or_transport_data(self):
        names = set(vars(make_event()))

        assert names.isdisjoint({
            'body', 'payload', 'raw', 'headers', 'signature', 'secret',
            'http_status', 'received_body',
        })


class TestTheRecoveryInterfaceCannotSubmit:
    """The non-negotiable invariant, enforced by the type itself."""

    def test_it_declares_only_two_operations(self):
        methods = {
            name for name in dir(TransferRecoveryProvider)
            if not name.startswith('_')
        }

        assert methods == {'get_transfer_status', 'verify_and_parse_webhook'}

    @pytest.mark.parametrize(
        'forbidden',
        ['submit_transfer', 'submit', 'send', 'retry', 'resubmit', 'execute',
         'reconcile', 'refund', 'reverse'],
    )
    def test_no_submission_or_mutation_operation_exists(self, forbidden):
        assert not hasattr(TransferRecoveryProvider, forbidden)

    def test_the_recovery_domain_never_mentions_submission(self):
        from pathlib import Path

        from moneycore.domain import recovery

        source = Path(recovery.__file__).read_text(encoding='utf-8')
        code = chr(10).join(
            line for line in source.splitlines()
            if line.strip() and not line.strip().startswith('#')
        )
        # The module docstring explains why submission is forbidden; no code
        # line may name it.
        body = code.split('"""')[-1]

        assert 'submit_transfer' not in body

    def test_no_vendor_is_named(self):
        from pathlib import Path

        from moneycore.domain import recovery

        source = Path(recovery.__file__).read_text(encoding='utf-8').lower()
        for brand in (
            'paystack', 'flutterwave', 'monnify', 'interswitch', 'kuda',
        ):
            assert brand not in source
