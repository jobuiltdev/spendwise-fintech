"""The destination value object, and the verification boundary.

The point of these tests is as much what M5 *refuses to do* as what it does:
there is no lookup, no bank directory and no way to obtain a verified
destination from inside the money core. Faking verification would be worse than
not offering it.
"""

import pytest

from moneycore.domain.errors import InvalidTransferDestinationError
from moneycore.domain.transfers import (
    NGN_BANK_ACCOUNT_NUMBER_LENGTH,
    VerifiedBankAccount,
    as_snapshot,
    normalise_narration,
)


def make(**overrides):
    values = {
        'account_number': '0123456789',
        'bank_code': 'NG-058',
        'bank_name': 'Example Bank',
        'account_name': 'Ada Okafor',
    }
    values.update(overrides)
    return VerifiedBankAccount(**values)


class TestValidDestination:
    def test_a_well_formed_destination_is_accepted(self):
        destination = make()

        assert destination.account_number == '0123456789'
        assert destination.bank_code == 'NG-058'
        assert destination.bank_name == 'Example Bank'
        assert destination.account_name == 'Ada Okafor'

    def test_it_is_frozen(self):
        from dataclasses import FrozenInstanceError

        destination = make()

        with pytest.raises(FrozenInstanceError):
            destination.account_number = '9999999999'

    def test_two_identical_destinations_compare_equal(self):
        assert make() == make()

    def test_a_different_account_number_is_a_different_destination(self):
        assert make() != make(account_number='9876543210')

    def test_a_different_bank_is_a_different_destination(self):
        assert make() != make(bank_code='NG-011')

    def test_a_different_account_name_is_a_different_destination(self):
        assert make() != make(account_name='Someone Else')

    def test_the_snapshot_maps_onto_the_transfer_columns(self):
        assert as_snapshot(make()) == {
            'recipient_name': 'Ada Okafor',
            'destination_account_number': '0123456789',
            'destination_bank_code': 'NG-058',
            'destination_bank_name': 'Example Bank',
        }


class TestAccountNumber:
    """Ten digits — the CURRENT Nigerian bank-transfer product rule.

    Deliberately not framed as a universal moneycore invariant: a future rail
    with a different account format needs a destination type of its own.
    """

    def test_the_current_rule_is_ten_digits(self):
        assert NGN_BANK_ACCOUNT_NUMBER_LENGTH == 10

    @pytest.mark.parametrize(
        'value',
        ['', '   ', '123', '012345678', '01234567890', 'ABCDEFGHIJ',
         '012345678A', '0123-45678', '+123456789'],
    )
    def test_a_malformed_account_number_is_refused(self, value):
        with pytest.raises(InvalidTransferDestinationError):
            make(account_number=value)

    def test_surrounding_whitespace_is_stripped(self):
        """Copy-and-paste routinely carries some; rejecting it is pedantry."""
        assert make(account_number='  0123456789  ').account_number == '0123456789'

    @pytest.mark.parametrize('value', ['01234 56789', '01\n23456789', '0123\t456789'])
    def test_internal_whitespace_is_refused_not_repaired(self, value):
        """Being lenient about these digits is how money reaches the wrong person."""
        with pytest.raises(InvalidTransferDestinationError):
            make(account_number=value)

    @pytest.mark.parametrize('value', ['\t0123456789', '0123456789\n'])
    def test_surrounding_tabs_and_newlines_are_stripped_like_spaces(self, value):
        assert make(account_number=value).account_number == '0123456789'

    @pytest.mark.parametrize('value', [123456789, None, 1.0, True, b'0123456789'])
    def test_a_non_text_account_number_is_refused(self, value):
        with pytest.raises(InvalidTransferDestinationError):
            make(account_number=value)

    def test_leading_zeros_are_preserved(self):
        assert make(account_number='0000000001').account_number == '0000000001'

    def test_the_error_names_the_field(self):
        with pytest.raises(InvalidTransferDestinationError) as raised:
            make(account_number='123')

        assert raised.value.details['field'] == 'account_number'

    def test_the_error_carries_a_stable_code(self):
        with pytest.raises(InvalidTransferDestinationError) as raised:
            make(account_number='123')

        assert raised.value.code == 'invalid_transfer_destination'


class TestBankCode:
    """An opaque validated string. M5 knows no provider's code vocabulary."""

    @pytest.mark.parametrize('value', ['058', 'NG-058', 'ng_058', 'A1'])
    def test_a_reasonable_code_shape_is_accepted(self, value):
        assert make(bank_code=value).bank_code == value

    @pytest.mark.parametrize(
        'value', ['', '   ', 'NG 058', 'NG/058', 'NG.058', 'x' * 31, '-058'],
    )
    def test_an_unusable_code_is_refused(self, value):
        with pytest.raises(InvalidTransferDestinationError):
            make(bank_code=value)

    def test_surrounding_whitespace_is_stripped(self):
        assert make(bank_code='  058  ').bank_code == '058'

    @pytest.mark.parametrize('value', [58, None, True])
    def test_a_non_text_code_is_refused(self, value):
        with pytest.raises(InvalidTransferDestinationError):
            make(bank_code=value)

    def test_no_provider_code_vocabulary_is_bundled(self):
        """No bank directory, no provider code list, no lookup table."""
        from pathlib import Path

        from moneycore.domain import transfers

        source = Path(transfers.__file__).read_text(encoding='utf-8').lower()

        for forbidden in ('paystack', 'flutterwave', 'monnify', 'anchor'):
            assert forbidden not in source

    def test_the_module_ships_no_bank_list(self):
        from moneycore.domain import transfers

        for forbidden in (
            'BANKS', 'BANK_CODES', 'BANK_DIRECTORY', 'NIGERIAN_BANKS',
            'lookup_bank', 'resolve_bank', 'list_banks',
        ):
            assert not hasattr(transfers, forbidden), f'{forbidden} exists'


class TestBankAndAccountNames:
    @pytest.mark.parametrize('field', ['bank_name', 'account_name'])
    @pytest.mark.parametrize('value', ['', '   ', '\t\n'])
    def test_a_blank_name_is_refused(self, field, value):
        with pytest.raises(InvalidTransferDestinationError):
            make(**{field: value})

    @pytest.mark.parametrize('field', ['bank_name', 'account_name'])
    def test_an_overlong_name_is_refused(self, field):
        with pytest.raises(InvalidTransferDestinationError):
            make(**{field: 'x' * 129})

    @pytest.mark.parametrize('field', ['bank_name', 'account_name'])
    def test_control_characters_are_refused(self, field):
        """Newlines in a name are how display spoofing and log injection start."""
        with pytest.raises(InvalidTransferDestinationError):
            make(**{field: 'Ada\nOkafor'})

    @pytest.mark.parametrize('field', ['bank_name', 'account_name'])
    def test_surrounding_whitespace_is_stripped(self, field):
        assert getattr(make(**{field: '  Ada Okafor  '}), field) == 'Ada Okafor'

    def test_internal_spacing_in_a_name_is_left_alone(self):
        """A recipient's name is not ours to reformat."""
        assert make(account_name='Ada  N. Okafor').account_name == 'Ada  N. Okafor'

    def test_a_name_with_accents_is_accepted(self):
        assert make(account_name='Chinwé Obí').account_name == 'Chinwé Obí'

    @pytest.mark.parametrize('field', ['bank_name', 'account_name'])
    @pytest.mark.parametrize('value', [None, 42, True])
    def test_a_non_text_name_is_refused(self, field, value):
        with pytest.raises(InvalidTransferDestinationError):
            make(**{field: value})


class TestNarration:
    def test_it_is_optional(self):
        assert normalise_narration('') == ''
        assert normalise_narration(None) == ''

    def test_it_is_stripped(self):
        assert normalise_narration('  rent  ') == 'rent'

    def test_it_is_bounded(self):
        with pytest.raises(InvalidTransferDestinationError):
            normalise_narration('x' * 101)

    def test_the_boundary_length_is_accepted(self):
        assert normalise_narration('x' * 100) == 'x' * 100

    def test_control_characters_are_refused(self):
        with pytest.raises(InvalidTransferDestinationError):
            normalise_narration('rent\nfor June')

    @pytest.mark.parametrize('value', [42, True, ['rent']])
    def test_non_text_is_refused(self, value):
        with pytest.raises(InvalidTransferDestinationError):
            normalise_narration(value)


class TestVerificationBoundary:
    """M5 models the verified fact. It does not, and must not, produce one."""

    def test_the_domain_offers_no_verification_function(self):
        from moneycore.domain import transfers

        for forbidden in (
            'verify_account', 'verify_bank_account', 'resolve_account',
            'resolve_account_name', 'name_enquiry', 'lookup_account',
            'fetch_account_name',
        ):
            assert not hasattr(transfers, forbidden), f'{forbidden} exists'

    def test_the_service_offers_no_verification_function(self):
        from moneycore.services import transfers

        for forbidden in (
            'verify_account', 'verify_bank_account', 'resolve_account',
            'resolve_account_name', 'name_enquiry', 'lookup_account',
        ):
            assert not hasattr(transfers, forbidden), f'{forbidden} exists'

    def test_constructing_one_is_the_callers_assertion(self):
        """Nothing is checked against any registry — because none exists."""
        destination = make(account_name='Whoever The Caller Verified')

        assert destination.account_name == 'Whoever The Caller Verified'

    def test_no_http_client_is_imported_by_the_transfer_modules(self):
        from pathlib import Path

        from moneycore.domain import transfers as domain
        from moneycore.services import transfers as service

        for module in (domain, service):
            source = Path(module.__file__).read_text(encoding='utf-8')
            for marker in (
                'import requests', 'import httpx', 'from requests', 'from httpx',
                'urllib', 'http.client', 'aiohttp', 'socket',
            ):
                assert marker not in source, f'{module.__name__}: {marker}'

    def test_the_snapshot_helper_needs_a_verified_destination(self):
        with pytest.raises(AttributeError):
            as_snapshot({'account_number': '0123456789'})
