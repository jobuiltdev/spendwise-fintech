"""Account resolution: the trusted producer M5 deliberately left absent.

M5 modelled the *fact* of a verified destination and refused to fake one. M6 is
where that fact legitimately comes from — and it still says nothing about which
real provider will eventually supply it.
"""

import pytest

from moneycore.domain.errors import (
    AccountResolutionInvalidError,
    AccountResolutionNotFoundError,
    AccountResolutionUnavailableError,
    ProviderResultInvalidError,
)
from moneycore.domain.providers import (
    AccountResolutionFailureReason,
    ProviderAccountResolutionFailed,
)
from moneycore.domain.transfers import VerifiedBankAccount
from moneycore.providers.simulator import (
    AccountResolutionScenario,
    SimulatorTransferProvider,
)
from moneycore.services.provider_execution import resolve_transfer_destination


class TestSuccessfulResolution:
    def test_it_produces_an_m5_verified_bank_account(self):
        provider = SimulatorTransferProvider()

        destination = resolve_transfer_destination(
            provider, bank_code='SIM-001', account_number='0000000001'
        )

        assert isinstance(destination, VerifiedBankAccount)

    def test_it_carries_the_resolved_name(self):
        provider = SimulatorTransferProvider()

        destination = resolve_transfer_destination(
            provider, bank_code='SIM-001', account_number='0000000001'
        )

        assert destination.account_name == 'Ada Okafor'
        assert destination.bank_name == 'Simulated First Bank'
        assert destination.account_number == '0000000001'
        assert destination.bank_code == 'SIM-001'

    def test_the_result_is_usable_for_a_transfer(self, db, funded_wallet):
        """The whole point: resolution feeds M5 preparation."""
        from moneycore.services.transfers import prepare_transfer

        destination = resolve_transfer_destination(
            SimulatorTransferProvider(),
            bank_code='SIM-001', account_number='0000000002',
        )

        transfer = prepare_transfer(
            funded_wallet, destination, 7_000, idempotency_key='resolved-1'
        )

        assert transfer.recipient_name == 'Bola Adeyemi'
        assert transfer.destination_bank_name == 'Simulated First Bank'

    def test_the_validation_of_m5_still_applies(self):
        """A rail returning a malformed account number is still refused."""
        from moneycore.domain.errors import InvalidTransferDestinationError
        from moneycore.domain.providers import ProviderAccountResolution

        class BadProvider:
            provider_key = 'bad'

            def resolve_bank_account(self, *, bank_code, account_number):
                return ProviderAccountResolution(
                    account_number='12',       # not ten digits
                    bank_code=bank_code,
                    account_name='Ada Okafor',
                    bank_name='Simulated Bank',
                )

        with pytest.raises(InvalidTransferDestinationError):
            resolve_transfer_destination(
                BadProvider(), bank_code='SIM-001', account_number='12'
            )

    def test_it_is_repeatable(self):
        provider = SimulatorTransferProvider()

        first = resolve_transfer_destination(
            provider, bank_code='SIM-001', account_number='0000000003'
        )
        second = resolve_transfer_destination(
            provider, bank_code='SIM-001', account_number='0000000003'
        )

        assert first == second


class TestResolutionFailuresAreDistinguished:
    def test_a_missing_account_is_reported_as_not_found(self):
        provider = SimulatorTransferProvider(
            account_resolution_scenario=AccountResolutionScenario.NOT_FOUND
        )

        with pytest.raises(AccountResolutionNotFoundError):
            resolve_transfer_destination(
                provider, bank_code='SIM-001', account_number='0123456789'
            )

    def test_an_unreachable_rail_is_a_different_error(self):
        """Never claim an account does not exist because we could not check."""
        provider = SimulatorTransferProvider(
            account_resolution_scenario=AccountResolutionScenario.UNAVAILABLE
        )

        with pytest.raises(AccountResolutionUnavailableError):
            resolve_transfer_destination(
                provider, bank_code='SIM-001', account_number='0123456789'
            )

    def test_the_two_errors_are_not_related_by_inheritance(self):
        assert not issubclass(
            AccountResolutionUnavailableError, AccountResolutionNotFoundError
        )
        assert not issubclass(
            AccountResolutionNotFoundError, AccountResolutionUnavailableError
        )

    def test_they_carry_distinct_stable_codes(self):
        assert AccountResolutionNotFoundError.code == 'account_resolution_not_found'
        assert AccountResolutionUnavailableError.code == (
            'account_resolution_unavailable'
        )

    def test_an_invalid_request_is_its_own_error(self):
        class InvalidProvider:
            provider_key = 'invalid'

            def resolve_bank_account(self, *, bank_code, account_number):
                return ProviderAccountResolutionFailed(
                    reason=AccountResolutionFailureReason.INVALID_REQUEST
                )

        with pytest.raises(AccountResolutionInvalidError):
            resolve_transfer_destination(
                InvalidProvider(), bank_code='X', account_number='1'
            )

    def test_an_unavailable_rail_says_nothing_was_sent(self):
        provider = SimulatorTransferProvider(
            account_resolution_scenario=AccountResolutionScenario.UNAVAILABLE
        )

        with pytest.raises(AccountResolutionUnavailableError) as raised:
            resolve_transfer_destination(
                provider, bank_code='SIM-001', account_number='0123456789'
            )

        assert 'Nothing was sent' in str(raised.value)

    def test_a_nonsense_adapter_result_is_refused(self):
        class BrokenProvider:
            provider_key = 'broken'

            def resolve_bank_account(self, *, bank_code, account_number):
                return {'account_name': 'Ada Okafor'}

        with pytest.raises(ProviderResultInvalidError):
            resolve_transfer_destination(
                BrokenProvider(), bank_code='SIM-001', account_number='0123456789'
            )


class TestResolutionPersistsNothing:
    def test_it_creates_no_transfer_or_transaction(self, db):
        from moneycore.models import (
            FinancialTransaction,
            ProviderExecutionAttempt,
            Transfer,
        )

        resolve_transfer_destination(
            SimulatorTransferProvider(),
            bank_code='SIM-001', account_number='0000000001',
        )

        assert Transfer.objects.count() == 0
        assert FinancialTransaction.objects.count() == 0
        assert ProviderExecutionAttempt.objects.count() == 0

    def test_it_creates_no_attempt_row(self, db):
        """Resolution moves no money, so it claims nothing."""
        from moneycore.models import ProviderExecutionAttempt

        provider = SimulatorTransferProvider()
        for _ in range(3):
            resolve_transfer_destination(
                provider, bank_code='SIM-001', account_number='0000000001'
            )

        assert ProviderExecutionAttempt.objects.count() == 0
        assert provider.resolve_call_count == 3

    def test_no_saved_recipient_is_created(self, db):
        from django.apps import apps

        resolve_transfer_destination(
            SimulatorTransferProvider(),
            bank_code='SIM-001', account_number='0000000001',
        )

        declared = {
            m.__name__.lower() for m in apps.get_app_config('moneycore').get_models()
        }
        assert declared.isdisjoint({'beneficiary', 'recipient', 'bank'})


class TestNoRealDirectory:
    def test_the_service_bundles_no_bank_list(self):
        from moneycore.services import provider_execution

        for forbidden in (
            'BANKS', 'BANK_CODES', 'BANK_DIRECTORY', 'NIGERIAN_BANKS',
        ):
            assert not hasattr(provider_execution, forbidden)

    def test_no_vendor_is_named_in_the_service(self):
        from pathlib import Path

        from moneycore.services import provider_execution

        source = Path(provider_execution.__file__).read_text(encoding='utf-8').lower()
        for brand in (
            'paystack', 'flutterwave', 'monnify', 'interswitch', 'kuda',
        ):
            assert brand not in source

    def test_resolution_takes_the_provider_as_an_argument(self):
        """No global provider, no registry lookup, no configured default."""
        import inspect

        signature = inspect.signature(resolve_transfer_destination)

        assert 'provider' in signature.parameters
