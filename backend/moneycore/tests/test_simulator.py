"""The deterministic simulator.

Determinism is the requirement: an investor demo must produce the same result
every time, and a concurrency test must be able to count calls exactly.
"""

import pytest

from moneycore.domain.providers import (
    ProviderAccountResolution,
    ProviderAccountResolutionFailed,
    ProviderFailureCode,
    ProviderTransferFailed,
    ProviderTransferRequest,
    ProviderTransferSucceeded,
    ProviderTransferUnknown,
    TransferProvider,
)
from moneycore.providers.simulator import (
    AccountResolutionScenario,
    SimulatorTransferProvider,
    TransferScenario,
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


class TestItSatisfiesTheInterface:
    def test_it_is_a_transfer_provider(self):
        assert isinstance(SimulatorTransferProvider(), TransferProvider)

    def test_its_key_says_what_it_is(self):
        assert SimulatorTransferProvider().provider_key == 'simulator'

    def test_the_key_names_no_vendor(self):
        from pathlib import Path

        from moneycore.providers import simulator

        source = Path(simulator.__file__).read_text(encoding='utf-8').lower()
        for brand in (
            'paystack', 'flutterwave', 'monnify', 'interswitch', 'kuda',
        ):
            assert brand not in source


class TestScenarioConfiguration:
    def test_an_unknown_transfer_scenario_is_refused(self):
        with pytest.raises(ValueError):
            SimulatorTransferProvider(transfer_scenario='whatever')

    def test_an_unknown_resolution_scenario_is_refused(self):
        with pytest.raises(ValueError):
            SimulatorTransferProvider(account_resolution_scenario='whatever')

    def test_an_unnormalised_failure_code_is_refused(self):
        with pytest.raises(ValueError):
            SimulatorTransferProvider(failure_code='timeout')

    def test_the_outcome_comes_from_configuration_not_the_destination(self):
        """No magic account numbers: behaviour is stated, not smuggled in."""
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.SUCCESS
        )

        for account_number in ('0000000000', '9999999999', '0123456789'):
            result = provider.submit_transfer(
                make_request(destination_account_number=account_number)
            )
            assert isinstance(result, ProviderTransferSucceeded)

    def test_it_defaults_to_success(self):
        assert SimulatorTransferProvider().transfer_scenario == (
            TransferScenario.SUCCESS
        )


class TestTransferSuccess:
    def test_it_returns_definitive_success(self):
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.SUCCESS
        )

        assert isinstance(
            provider.submit_transfer(make_request()), ProviderTransferSucceeded
        )

    def test_the_reference_is_derived_from_our_own(self):
        provider = SimulatorTransferProvider()

        result = provider.submit_transfer(make_request(client_reference='SW-xyz'))

        assert result.provider_reference == 'SIM-SW-xyz'

    def test_the_same_request_always_gives_the_same_reference(self):
        provider = SimulatorTransferProvider()

        first = provider.submit_transfer(make_request())
        second = provider.submit_transfer(make_request())

        assert first.provider_reference == second.provider_reference

    def test_two_providers_agree(self):
        """Determinism across instances, not just within one."""
        first = SimulatorTransferProvider().submit_transfer(make_request())
        second = SimulatorTransferProvider().submit_transfer(make_request())

        assert first == second


class TestTransferKnownFailure:
    def test_it_returns_definitive_failure(self):
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.KNOWN_FAILURE
        )

        result = provider.submit_transfer(make_request())

        assert isinstance(result, ProviderTransferFailed)

    def test_the_failure_code_is_configurable_and_normalised(self):
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.KNOWN_FAILURE,
            failure_code=ProviderFailureCode.DESTINATION_REJECTED,
        )

        result = provider.submit_transfer(make_request())

        assert result.failure_code == 'destination_rejected'
        assert result.failure_code in ProviderFailureCode.ALL


class TestAmbiguousAfterSubmission:
    """The scenario the whole milestone exists for."""

    def test_it_returns_an_explicit_unknown(self):
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
        )

        result = provider.submit_transfer(make_request())

        assert isinstance(result, ProviderTransferUnknown)

    def test_the_reason_names_the_semantics_not_a_mechanism(self):
        """"Response not received", not "timeout" — the ambiguity is the point."""
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
        )

        result = provider.submit_transfer(make_request())

        assert result.ambiguity_reason == 'response_not_received'

    def test_it_is_not_an_exception(self):
        """Returned, so no caller can accidentally catch it as a failure."""
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
        )

        result = provider.submit_transfer(make_request())

        assert not isinstance(result, Exception)

    def test_it_carries_no_failure_code(self):
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
        )

        assert not hasattr(provider.submit_transfer(make_request()), 'failure_code')

    def test_it_records_that_the_request_went_out(self):
        """It may have been executed, so the request is remembered."""
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
        )

        provider.submit_transfer(make_request())

        assert provider.submit_call_count == 1
        assert len(provider.submitted_requests) == 1


class TestUnreachableBeforeSubmission:
    """Sharply distinct from ambiguity: the adapter can prove nothing was sent."""

    def test_it_is_a_definitive_failure(self):
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.UNREACHABLE_BEFORE_SUBMISSION
        )

        result = provider.submit_transfer(make_request())

        assert isinstance(result, ProviderTransferFailed)
        assert result.failure_code == ProviderFailureCode.PROVIDER_UNAVAILABLE

    def test_it_is_not_ambiguous(self):
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.UNREACHABLE_BEFORE_SUBMISSION
        )

        result = provider.submit_transfer(make_request())

        assert not isinstance(result, ProviderTransferUnknown)

    def test_it_carries_no_provider_reference(self):
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.UNREACHABLE_BEFORE_SUBMISSION
        )

        assert provider.submit_transfer(make_request()).provider_reference == ''


class TestAccountResolution:
    def test_success_returns_a_resolution(self):
        provider = SimulatorTransferProvider()

        result = provider.resolve_bank_account(
            bank_code='SIM-001', account_number='0000000001'
        )

        assert isinstance(result, ProviderAccountResolution)
        assert result.account_name == 'Ada Okafor'
        assert result.bank_name == 'Simulated First Bank'

    def test_an_unmapped_account_still_resolves_deterministically(self):
        provider = SimulatorTransferProvider()

        result = provider.resolve_bank_account(
            bank_code='SIM-009', account_number='0123456789'
        )

        assert result.account_name == 'Simulated Account Holder'
        assert result.bank_name == 'Simulated Bank'

    def test_not_found_is_reported_as_such(self):
        provider = SimulatorTransferProvider(
            account_resolution_scenario=AccountResolutionScenario.NOT_FOUND
        )

        result = provider.resolve_bank_account(
            bank_code='SIM-001', account_number='0123456789'
        )

        assert isinstance(result, ProviderAccountResolutionFailed)
        assert result.reason == 'not_found'

    def test_unavailable_is_a_different_answer(self):
        provider = SimulatorTransferProvider(
            account_resolution_scenario=AccountResolutionScenario.UNAVAILABLE
        )

        result = provider.resolve_bank_account(
            bank_code='SIM-001', account_number='0123456789'
        )

        assert isinstance(result, ProviderAccountResolutionFailed)
        assert result.reason == 'unavailable'

    def test_resolution_is_repeatable(self):
        provider = SimulatorTransferProvider()

        first = provider.resolve_bank_account(
            bank_code='SIM-001', account_number='0000000002'
        )
        second = provider.resolve_bank_account(
            bank_code='SIM-001', account_number='0000000002'
        )

        assert first == second

    def test_the_fixture_is_simulator_local_and_tiny(self):
        """Not a bank directory. O-28 stays open."""
        from moneycore.providers.simulator import SIMULATED_BANKS

        assert len(SIMULATED_BANKS) <= 5
        assert all(code.startswith('SIM-') for code in SIMULATED_BANKS)
        assert all('Simulated' in name for name in SIMULATED_BANKS.values())

    def test_no_bank_model_was_created(self):
        from django.apps import apps

        declared = {
            m.__name__.lower() for m in apps.get_app_config('moneycore').get_models()
        }

        assert declared.isdisjoint({'bank', 'bankdirectory', 'bankaccount'})


class TestNoRandomnessOrHiddenState:
    def test_the_module_imports_no_randomness(self):
        from pathlib import Path

        from moneycore.providers import simulator

        source = Path(simulator.__file__).read_text(encoding='utf-8')
        for forbidden in ('import random', 'random.', 'uuid4', 'secrets'):
            assert forbidden not in source

    def test_it_performs_no_io_and_no_sleeping(self):
        from pathlib import Path

        from moneycore.providers import simulator

        source = Path(simulator.__file__).read_text(encoding='utf-8')
        for forbidden in (
            'time.sleep', 'import requests', 'import httpx', 'socket',
            'urllib', 'while True', 'for _ in range',
        ):
            assert forbidden not in source

    def test_call_counts_start_at_zero_per_instance(self):
        provider = SimulatorTransferProvider()

        assert provider.submit_call_count == 0
        assert provider.resolve_call_count == 0

    def test_counts_are_per_instance_not_global(self):
        first = SimulatorTransferProvider()
        second = SimulatorTransferProvider()

        first.submit_transfer(make_request())

        assert first.submit_call_count == 1
        assert second.submit_call_count == 0

    def test_it_never_retries_internally(self):
        provider = SimulatorTransferProvider(
            transfer_scenario=TransferScenario.AMBIGUOUS_AFTER_SUBMISSION
        )

        provider.submit_transfer(make_request())

        assert provider.submit_call_count == 1
