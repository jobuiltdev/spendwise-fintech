"""Transfer outcomes: UNKNOWN, known failure, and the absence of cancellation.

M5 adds no state machine. Every outcome here is M4's, reached through a thin
wrapper that validates the relationship and delegates.
"""

import pytest

from moneycore.domain.errors import (
    TransactionAlreadyResolvedError,
    TransferTransactionMismatchError,
)
from moneycore.domain.holds import HoldStatus
from moneycore.domain.money import Money
from moneycore.domain.transactions import TransactionDirection, TransactionStatus
from moneycore.domain.transfers import VerifiedBankAccount
from moneycore.models import Journal, Transfer
from moneycore.services.holds import wallet_balance_projection
from moneycore.services.transactions import start_processing
from moneycore.services.transfers import (
    fail_transfer,
    mark_transfer_unknown,
    prepare_transfer,
)

pytestmark = pytest.mark.django_db


DESTINATION = VerifiedBankAccount(
    account_number='0123456789',
    bank_code='NG-058',
    bank_name='Example Bank',
    account_name='Ada Okafor',
)


@pytest.fixture
def prepared(funded_wallet):
    return prepare_transfer(
        funded_wallet, DESTINATION, 7_000, idempotency_key='outcome-1'
    )


@pytest.fixture
def processing(prepared):
    start_processing(prepared.financial_transaction)
    prepared.refresh_from_db()
    return prepared


class TestUnknownTransfer:
    """The state that must never be mistaken for failure."""

    def test_the_transaction_becomes_unknown(self, processing):
        transfer = mark_transfer_unknown(processing)

        assert transfer.status == TransactionStatus.UNKNOWN

    def test_the_transfer_still_exists_unchanged(self, processing):
        transfer = mark_transfer_unknown(processing)

        assert Transfer.objects.filter(pk=transfer.pk).exists()
        assert transfer.destination_account_number == '0123456789'
        assert transfer.recipient_name == 'Ada Okafor'

    def test_the_reservation_stays_active(self, processing):
        """The funds stay unavailable while SpendWise works out what happened."""
        transfer = mark_transfer_unknown(processing)

        transfer.hold.refresh_from_db()
        assert transfer.hold.status == HoldStatus.ACTIVE
        assert transfer.hold.released_at is None

    def test_nothing_is_posted(self, processing):
        before = Journal.objects.count()

        mark_transfer_unknown(processing)

        assert Journal.objects.count() == before

    def test_the_balance_picture_is_unchanged(self, processing):
        before = wallet_balance_projection(processing.wallet)

        mark_transfer_unknown(processing)

        after = wallet_balance_projection(processing.wallet)
        assert after == before
        assert after.posted == Money(10_000, 'NGN')
        assert after.held == Money(7_000, 'NGN')
        assert after.available == Money(3_000, 'NGN')

    def test_unknown_is_not_terminal(self, processing):
        transfer = mark_transfer_unknown(processing)

        assert not transfer.financial_transaction.is_terminal
        assert transfer.financial_transaction.resolved_at is None

    def test_there_is_no_transfer_level_pending_state(self):
        """No PENDING, CONFIRMING or AWAITING_CONFIRMATION constant in M5.

        Matched as whole uppercase tokens, which is the shape a real status
        constant would take — prose like "depending on" is not a state.
        """
        import re
        from pathlib import Path

        from moneycore.domain import transfers as domain
        from moneycore.services import transfers as service

        pattern = re.compile(
            r'\b(PENDING|CONFIRMING|AWAITING_CONFIRMATION|IN_PROGRESS|SETTLING)\b'
        )
        for module in (domain, service):
            source = Path(module.__file__).read_text(encoding='utf-8')
            assert not pattern.search(source), module.__name__

    def test_that_pending_guard_actually_matches_something(self):
        """The guard above is only worth having if it can fail."""
        import re

        pattern = re.compile(
            r'\b(PENDING|CONFIRMING|AWAITING_CONFIRMATION|IN_PROGRESS|SETTLING)\b'
        )

        assert pattern.search('status = PENDING')
        assert pattern.search('CONFIRMING = "confirming"')
        assert not pattern.search('depending on the design')

    def test_no_provider_brand_appears(self):
        """No payment provider is named anywhere in the transfer domain.

        NUBAN deliberately *is* mentioned: it is Nigeria's national account
        numbering standard and the correct name for the ten-digit rule, not a
        provider's vocabulary.
        """
        from pathlib import Path

        from moneycore.domain import transfers as domain
        from moneycore.services import transfers as service

        for module in (domain, service):
            source = Path(module.__file__).read_text(encoding='utf-8').lower()
            for forbidden in (
                'paystack', 'flutterwave', 'monnify', 'interswitch', 'anchor',
            ):
                assert forbidden not in source, f'{module.__name__}: {forbidden}'

    def test_it_can_still_resolve_afterwards(self, processing):
        mark_transfer_unknown(processing)

        transfer = fail_transfer(processing, failure_code='not_executed')

        assert transfer.status == TransactionStatus.FAILED


class TestKnownFailure:
    """Only when it is actually known that the money did not move."""

    def test_the_transaction_fails(self, processing):
        transfer = fail_transfer(processing, failure_code='declined')

        assert transfer.status == TransactionStatus.FAILED
        assert transfer.financial_transaction.failure_code == 'declined'

    def test_the_reservation_is_released(self, processing):
        transfer = fail_transfer(processing, failure_code='declined')

        transfer.hold.refresh_from_db()
        assert transfer.hold.status == HoldStatus.RELEASED

    def test_the_transfer_remains_as_a_historical_attempt(self, processing):
        transfer = fail_transfer(processing, failure_code='declined')

        assert Transfer.objects.filter(pk=transfer.pk).exists()
        assert transfer.destination_account_number == '0123456789'

    def test_nothing_is_posted(self, processing):
        before = Journal.objects.count()

        fail_transfer(processing, failure_code='declined')

        assert Journal.objects.count() == before

    def test_the_funds_are_restored(self, processing):
        fail_transfer(processing, failure_code='declined')

        projection = wallet_balance_projection(processing.wallet)
        assert projection.posted == Money(10_000, 'NGN')
        assert projection.held == Money(0, 'NGN')
        assert projection.available == Money(10_000, 'NGN')

    def test_a_pre_execution_failure_works_the_same_way(self, prepared):
        """The only exit before execution, and it is authoritative."""
        transfer = fail_transfer(prepared, failure_code='limit_exceeded')

        transfer.hold.refresh_from_db()
        assert transfer.status == TransactionStatus.FAILED
        assert transfer.hold.status == HoldStatus.RELEASED
        assert wallet_balance_projection(
            prepared.wallet
        ).available == Money(10_000, 'NGN')

    def test_failure_is_terminal(self, processing):
        fail_transfer(processing, failure_code='declined')

        with pytest.raises(TransactionAlreadyResolvedError):
            fail_transfer(processing, failure_code='again')

    def test_no_provider_error_mapping_exists(self):
        from moneycore.services import transfers

        for forbidden in (
            'map_provider_error', 'provider_failure_code', 'translate_error',
            'FAILURE_CODES', 'PROVIDER_ERRORS',
        ):
            assert not hasattr(transfers, forbidden), f'{forbidden} exists'

    def test_no_timeout_is_treated_as_failure(self):
        """A timeout is mark_transfer_unknown, and M5 offers no other reading."""
        from pathlib import Path

        from moneycore.services import transfers

        source = Path(transfers.__file__).read_text(encoding='utf-8').lower()
        assert 'timeout' not in source

    def test_the_failure_code_is_caller_supplied_and_not_invented(self, processing):
        transfer = fail_transfer(processing, failure_code='whatever_the_caller_said')

        assert transfer.financial_transaction.failure_code == (
            'whatever_the_caller_said'
        )

    def test_a_failure_code_is_optional(self, processing):
        transfer = fail_transfer(processing)

        assert transfer.financial_transaction.failure_code == ''


class TestOutcomesValidateTheRelationship:
    """What the thin wrappers add over calling M4 directly."""

    def _incoming_transfer(self, funded_wallet):
        from moneycore.services.transactions import create_transaction

        txn = create_transaction(
            funded_wallet, TransactionDirection.INCOMING, 7_000,
            idempotency_key='incoming-outcome',
        )
        return Transfer.objects.create(
            financial_transaction=txn,
            recipient_name='Ada Okafor',
            destination_account_number='0123456789',
            destination_bank_code='NG-058',
            destination_bank_name='Example Bank',
        )

    def test_failure_refuses_a_non_outgoing_transaction(self, funded_wallet):
        transfer = self._incoming_transfer(funded_wallet)

        with pytest.raises(TransferTransactionMismatchError):
            fail_transfer(transfer, failure_code='declined')

    def test_unknown_refuses_a_non_outgoing_transaction(self, funded_wallet):
        transfer = self._incoming_transfer(funded_wallet)

        with pytest.raises(TransferTransactionMismatchError):
            mark_transfer_unknown(transfer)

    def test_the_wrappers_do_not_duplicate_the_state_machine(self):
        """They validate and delegate. They do not decide transitions."""
        import inspect

        from moneycore.services import transfers

        for name, delegate in (
            ('fail_transfer', 'fail_transaction'),
            ('mark_transfer_unknown', 'mark_unknown'),
        ):
            source = inspect.getsource(getattr(transfers, name))
            assert delegate in source
            assert 'TRANSACTION_TRANSITIONS' not in source
            assert 'can_transition' not in source


class TestNoCancellation:
    """Locked §8.5 and M4-12, carried into the transfer domain."""

    def test_the_service_offers_no_cancellation(self):
        from moneycore.services import transfers

        for forbidden in (
            'cancel_transfer', 'cancel', 'abandon_transfer', 'abandon',
            'void_transfer', 'void', 'discard_transfer', 'delete_transfer',
        ):
            assert not hasattr(transfers, forbidden), f'{forbidden} exists'

    def test_the_domain_defines_no_cancellation_or_draft_state(self):
        from moneycore.domain import transfers

        for forbidden in (
            'TransferStatus', 'TRANSFER_STATUSES', 'CANCELLED', 'DRAFT',
            'ABANDONED', 'VOIDED', 'TransferDraft',
        ):
            assert not hasattr(transfers, forbidden), f'{forbidden} exists'

    def test_no_draft_model_exists(self):
        from django.apps import apps

        declared = {
            m.__name__.lower() for m in apps.get_app_config('moneycore').get_models()
        }

        assert declared.isdisjoint({
            'transferdraft', 'draft', 'pendingtransfer', 'transferintent',
        })

    def test_no_cancellation_vocabulary_survives_in_the_transfer_modules(self):
        from pathlib import Path

        from moneycore.domain import transfers as domain
        from moneycore.services import transfers as service

        for module in (domain, service):
            source = Path(module.__file__).read_text(encoding='utf-8')
            assert 'CANCELLED' not in source
            assert 'def cancel' not in source

    def test_the_transaction_cancelled_state_was_not_resurrected(self):
        from moneycore.domain.transactions import TransactionStatus

        assert TransactionStatus.ALL == {
            'created', 'processing', 'unknown', 'succeeded', 'failed'
        }
        assert TransactionStatus.TERMINAL == {'succeeded', 'failed'}

    def test_no_transfer_status_column_was_added(self):
        names = {f.name for f in Transfer._meta.get_fields()}

        assert names.isdisjoint({
            'status', 'transfer_status', 'bank_status', 'payment_status',
        })


class TestNoExecutionInM5:
    """M6 is the first milestone allowed to start provider execution."""

    def test_the_service_offers_no_start_transfer(self):
        from moneycore.services import transfers

        for forbidden in (
            'start_transfer', 'execute_transfer', 'submit_transfer',
            'send_transfer', 'dispatch_transfer', 'initiate_transfer',
            'retry_transfer', 'resubmit_transfer',
        ):
            assert not hasattr(transfers, forbidden), f'{forbidden} exists'

    def test_the_service_surface_is_exactly_the_m5_boundary(self):
        import inspect

        from moneycore.services import transfers

        public = sorted(
            name
            for name, value in vars(transfers).items()
            if inspect.isfunction(value)
            and value.__module__ == transfers.__name__
            and not name.startswith('_')
        )

        assert public == [
            'fail_transfer', 'mark_transfer_unknown', 'prepare_transfer',
            'succeed_transfer', 'transfer_principal_entries',
        ]

    def test_preparation_leaves_the_transaction_not_started(self, prepared):
        assert prepared.status == TransactionStatus.CREATED
        assert prepared.financial_transaction.processing_at is None

    def test_reaching_processing_needs_m4_directly(self, prepared):
        """Honest: M5 genuinely cannot start execution, so it does not pretend."""
        start_processing(prepared.financial_transaction)
        prepared.refresh_from_db()

        assert prepared.status == TransactionStatus.PROCESSING
