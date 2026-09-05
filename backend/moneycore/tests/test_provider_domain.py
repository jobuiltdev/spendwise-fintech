"""The provider-neutral vocabulary.

The point of these tests is that ambiguity is a first-class value with its own
type, and that impossible outcomes cannot be constructed at all.
"""

import pytest

from moneycore.domain.errors import ProviderResultInvalidError
from moneycore.domain.providers import (
    PROVIDER_REFERENCE_MAX_LENGTH,
    AccountResolutionFailureReason,
    ProviderAccountResolution,
    ProviderAccountResolutionFailed,
    ProviderAttemptStatus,
    ProviderFailureCode,
    ProviderOperation,
    ProviderTransferFailed,
    ProviderTransferRequest,
    ProviderTransferSucceeded,
    ProviderTransferUnknown,
    TransferProvider,
    normalise_provider_reference,
    require_transfer_result,
)


def make_request(**overrides):
    values = {
        'amount_minor': 7_000,
        'currency': 'NGN',
        'destination_account_number': '0123456789',
        'destination_bank_code': 'SIM-001',
        'client_reference': 'SW-abc123',
    }
    values.update(overrides)
    return ProviderTransferRequest(**values)


class TestAttemptVocabulary:
    def test_the_statuses_are_exactly_the_four_m6_defined(self):
        assert ProviderAttemptStatus.ALL == {
            'started', 'succeeded', 'failed', 'unknown'
        }

    def test_finished_excludes_started(self):
        assert ProviderAttemptStatus.FINISHED == {
            'succeeded', 'failed', 'unknown'
        }
        assert ProviderAttemptStatus.STARTED not in ProviderAttemptStatus.FINISHED

    def test_unknown_is_terminal_for_an_attempt(self):
        """An attempt is one interaction, and that interaction is over.

        Deliberately the opposite of the transaction rule: the *operation* is
        still unresolved even though this *observation* is complete.
        """
        assert ProviderAttemptStatus.UNKNOWN in ProviderAttemptStatus.FINISHED

    def test_unknown_is_still_not_terminal_for_a_transaction(self):
        from moneycore.domain.transactions import TransactionStatus

        assert TransactionStatus.UNKNOWN not in TransactionStatus.TERMINAL

    @pytest.mark.parametrize(
        'forbidden',
        ['pending', 'processing', 'submitted', 'settling', 'confirming',
         'reversed', 'reconciled', 'retrying', 'queued'],
    )
    def test_no_second_business_lifecycle_state_exists(self, forbidden):
        assert forbidden not in ProviderAttemptStatus.ALL

    def test_the_only_operation_is_submitting_a_transfer(self):
        assert ProviderOperation.ALL == {'submit_transfer'}


class TestFailureCodes:
    def test_the_vocabulary_is_small_and_closed(self):
        assert ProviderFailureCode.ALL == {
            'destination_rejected',
            'request_rejected',
            'provider_unavailable',
            'unknown_provider_failure',
        }

    @pytest.mark.parametrize(
        'forbidden',
        ['timeout', 'timed_out', 'no_response', 'unknown', 'ambiguous',
         'connection_lost', 'response_lost'],
    )
    def test_no_ambiguous_condition_has_a_failure_code(self, forbidden):
        """There is deliberately no way to record a timeout as a failure."""
        assert forbidden not in ProviderFailureCode.ALL

    def test_resolution_failures_separate_not_found_from_unavailable(self):
        assert AccountResolutionFailureReason.NOT_FOUND != (
            AccountResolutionFailureReason.UNAVAILABLE
        )
        assert AccountResolutionFailureReason.ALL == {
            'not_found', 'unavailable', 'invalid_request'
        }


class TestSuccessIsDefinitive:
    def test_it_carries_no_failure_code_field_at_all(self):
        """Impossible states are unrepresentable, not merely validated."""
        result = ProviderTransferSucceeded(provider_reference='SIM-1')

        assert not hasattr(result, 'failure_code')

    def test_it_cannot_be_given_one(self):
        with pytest.raises(TypeError):
            ProviderTransferSucceeded(failure_code='request_rejected')

    def test_a_provider_reference_is_optional(self):
        assert ProviderTransferSucceeded().provider_reference == ''

    def test_it_is_frozen(self):
        from dataclasses import FrozenInstanceError

        result = ProviderTransferSucceeded(provider_reference='SIM-1')

        with pytest.raises(FrozenInstanceError):
            result.provider_reference = 'SIM-2'


class TestFailureIsDefinitive:
    def test_it_requires_a_normalised_code(self):
        result = ProviderTransferFailed(
            failure_code=ProviderFailureCode.DESTINATION_REJECTED
        )

        assert result.failure_code == 'destination_rejected'

    @pytest.mark.parametrize(
        'code', ['timeout', 'weird', '', None, 502, 'TIMEOUT'],
    )
    def test_an_unnormalised_code_is_refused(self, code):
        with pytest.raises(ProviderResultInvalidError):
            ProviderTransferFailed(failure_code=code)

    def test_a_provider_reference_is_optional_on_failure_too(self):
        result = ProviderTransferFailed(
            failure_code=ProviderFailureCode.REQUEST_REJECTED
        )

        assert result.provider_reference == ''


class TestAmbiguityIsExplicit:
    def test_it_is_its_own_type(self):
        result = ProviderTransferUnknown(ambiguity_reason='response_not_received')

        assert not isinstance(result, ProviderTransferFailed)
        assert not isinstance(result, ProviderTransferSucceeded)

    def test_it_carries_no_failure_code_field(self):
        result = ProviderTransferUnknown()

        assert not hasattr(result, 'failure_code')

    def test_it_cannot_borrow_a_failure_code_as_its_reason(self):
        with pytest.raises(ProviderResultInvalidError):
            ProviderTransferUnknown(
                ambiguity_reason=ProviderFailureCode.REQUEST_REJECTED
            )

    def test_it_may_carry_a_provider_reference(self):
        """Some rails answer with a reference and then go quiet."""
        result = ProviderTransferUnknown(
            ambiguity_reason='response_not_received',
            provider_reference='SIM-9',
        )

        assert result.provider_reference == 'SIM-9'

    def test_a_reason_is_optional(self):
        assert ProviderTransferUnknown().ambiguity_reason == ''

    def test_there_is_no_boolean_success_flag_anywhere(self):
        """A bool cannot distinguish "definitely not" from "we do not know"."""
        for result in (
            ProviderTransferSucceeded(),
            ProviderTransferFailed(failure_code='request_rejected'),
            ProviderTransferUnknown(),
        ):
            for forbidden in ('success', 'succeeded', 'ok', 'is_success'):
                assert not hasattr(result, forbidden), forbidden


class TestResultValidation:
    @pytest.mark.parametrize(
        'value', [None, True, False, 'ok', 0, {'status': 'ok'}, []],
    )
    def test_anything_that_is_not_an_outcome_is_refused(self, value):
        with pytest.raises(ProviderResultInvalidError):
            require_transfer_result(value)

    @pytest.mark.parametrize(
        'result',
        [
            ProviderTransferSucceeded(),
            ProviderTransferFailed(failure_code='request_rejected'),
            ProviderTransferUnknown(),
        ],
    )
    def test_the_three_honest_outcomes_pass(self, result):
        assert require_transfer_result(result) is result

    def test_the_error_names_what_came_back(self):
        with pytest.raises(ProviderResultInvalidError) as raised:
            require_transfer_result(None)

        assert raised.value.details['returned'] == 'NoneType'
        assert raised.value.code == 'provider_result_invalid'


class TestProviderReference:
    def test_it_is_opaque_and_not_assumed_to_be_any_format(self):
        for value in ('12345', 'a-b-c', 'SIM-xyz', 'ref_1/2', 'ÆØÅ'):
            assert normalise_provider_reference(value) == value

    def test_outer_whitespace_is_trimmed(self):
        assert normalise_provider_reference('  SIM-1  ') == 'SIM-1'

    def test_none_becomes_empty(self):
        assert normalise_provider_reference(None) == ''

    def test_control_characters_are_refused(self):
        with pytest.raises(ProviderResultInvalidError):
            normalise_provider_reference('SIM\n1')

    def test_an_overlong_reference_is_refused(self):
        with pytest.raises(ProviderResultInvalidError):
            normalise_provider_reference('x' * (PROVIDER_REFERENCE_MAX_LENGTH + 1))

    def test_non_text_is_refused(self):
        with pytest.raises(ProviderResultInvalidError):
            normalise_provider_reference(12345)


class TestProviderRequest:
    def test_a_well_formed_request_is_accepted(self):
        request = make_request()

        assert request.amount_minor == 7_000
        assert request.client_reference == 'SW-abc123'

    def test_it_is_frozen(self):
        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            make_request().amount_minor = 1

    @pytest.mark.parametrize('amount', [0, -1, 70.0, '7000', True, None])
    def test_a_bad_amount_is_refused(self, amount):
        with pytest.raises(ProviderResultInvalidError):
            make_request(amount_minor=amount)

    @pytest.mark.parametrize('currency', ['ngn', 'NG', '', None, 'NGNN'])
    def test_a_bad_currency_is_refused(self, currency):
        with pytest.raises(ProviderResultInvalidError):
            make_request(currency=currency)

    @pytest.mark.parametrize(
        'field',
        ['destination_account_number', 'destination_bank_code', 'client_reference'],
    )
    def test_a_missing_required_value_is_refused(self, field):
        with pytest.raises(ProviderResultInvalidError):
            make_request(**{field: '  '})

    def test_the_request_carries_only_business_values(self):
        """No Django model instance may cross the provider boundary."""
        from django.db.models import Model

        request = make_request()

        for value in vars(request).values():
            assert not isinstance(value, Model)
            assert isinstance(value, (str, int))

    def test_it_names_no_wallet_transfer_or_transaction(self):
        names = set(vars(make_request()))

        assert names.isdisjoint({
            'wallet', 'transfer', 'transaction', 'financial_transaction',
            'hold', 'journal', 'user', 'customer',
        })


class TestAccountResolution:
    def test_it_carries_the_same_four_facts_m5_needs(self):
        resolution = ProviderAccountResolution(
            account_number='0123456789',
            bank_code='SIM-001',
            account_name='Ada Okafor',
            bank_name='Simulated First Bank',
        )

        assert set(vars(resolution)) == {
            'account_number', 'bank_code', 'account_name', 'bank_name'
        }

    def test_a_failure_needs_a_known_reason(self):
        with pytest.raises(ProviderResultInvalidError):
            ProviderAccountResolutionFailed(reason='whatever')

    def test_a_failure_is_returned_not_raised(self):
        failure = ProviderAccountResolutionFailed(
            reason=AccountResolutionFailureReason.NOT_FOUND
        )

        assert not isinstance(failure, Exception)


class TestTheInterfaceIsNarrow:
    def test_it_declares_only_two_operations(self):
        methods = {
            name for name in dir(TransferProvider)
            if not name.startswith('_')
        }

        assert methods == {'resolve_bank_account', 'submit_transfer'}

    @pytest.mark.parametrize(
        'forbidden',
        ['check_status', 'poll', 'get_transfer', 'retry', 'handle_webhook',
         'reconcile', 'do_everything', 'execute'],
    )
    def test_no_recovery_or_catch_all_operation_exists(self, forbidden):
        assert not hasattr(TransferProvider, forbidden)

    def test_no_vendor_name_appears_in_the_domain(self):
        from pathlib import Path

        from moneycore.domain import providers

        source = Path(providers.__file__).read_text(encoding='utf-8').lower()
        for brand in (
            'paystack', 'flutterwave', 'monnify', 'interswitch', 'kuda',
        ):
            assert brand not in source
