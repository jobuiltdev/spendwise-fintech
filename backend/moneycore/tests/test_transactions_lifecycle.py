"""The transaction state machine, and the UNKNOWN / timeout semantics.

The most important assertions in this file are the ones proving that entering
UNKNOWN changes nothing financial — no journal, no release, no balance move —
and that no path turns ambiguity into failure.
"""

import pytest
from django.utils import timezone

from moneycore.domain.errors import (
    InvalidTransactionTransitionError,
    TransactionAlreadyResolvedError,
    TransactionHoldInvalidError,
    TransactionHoldRequiredError,
)
from moneycore.domain.holds import HoldStatus
from moneycore.domain.money import Money
from moneycore.domain.transactions import (
    TRANSACTION_TRANSITIONS,
    TransactionDirection,
    TransactionStatus,
    can_transition,
    execution_may_have_started,
    is_terminal,
    requires_reservation,
)
from moneycore.models import FundsHold, Journal, JournalEntry
from moneycore.services.holds import create_hold, wallet_balance_projection
from moneycore.services.transactions import (
    attach_hold,
    create_transaction,
    fail_transaction,
    mark_unknown,
    start_processing,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def outgoing(funded_wallet):
    """A CREATED outgoing transaction with 7 000 reserved against 10 000."""
    txn = create_transaction(
        funded_wallet, TransactionDirection.OUTGOING, 7_000,
        idempotency_key='lifecycle-1',
    )
    hold = create_hold(funded_wallet, 7_000)
    return attach_hold(txn, hold)


@pytest.fixture
def incoming(funded_wallet):
    return create_transaction(
        funded_wallet, TransactionDirection.INCOMING, 3_000,
        idempotency_key='lifecycle-in',
    )


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class TestStateVocabulary:
    def test_the_states_are_exactly_the_five_m4_defined(self):
        assert TransactionStatus.ALL == {
            'created', 'processing', 'unknown', 'succeeded', 'failed'
        }

    @pytest.mark.parametrize(
        'forbidden', ['cancelled', 'cancelling', 'abandoned', 'draft', 'voided'],
    )
    def test_no_cancellation_or_draft_state_exists(self, forbidden):
        """Locked architecture §8.5: backing out creates no transaction at all.

        A FinancialTransaction is not a persisted draft of a UI flow, so there
        is nothing here to abandon. Persisted drafts, if ever needed, belong to
        the transfer/product layer.
        """
        assert forbidden not in TransactionStatus.ALL

    @pytest.mark.parametrize(
        'forbidden',
        ['pending', 'awaiting_confirmation', 'submitted', 'settling',
         'completed', 'confirming'],
    )
    def test_no_routine_or_customer_facing_state_exists(self, forbidden):
        """"Confirming" is product wording for UNKNOWN, never a backend state."""
        assert forbidden not in TransactionStatus.ALL

    def test_unknown_is_not_terminal(self):
        assert not is_terminal(TransactionStatus.UNKNOWN)
        assert TransactionStatus.UNKNOWN not in TransactionStatus.TERMINAL

    @pytest.mark.parametrize('status', ['succeeded', 'failed'])
    def test_the_two_terminal_states(self, status):
        assert is_terminal(status)
        assert TRANSACTION_TRANSITIONS[status] == frozenset()

    def test_terminal_is_exactly_succeeded_and_failed(self):
        assert TransactionStatus.TERMINAL == {'succeeded', 'failed'}

    def test_every_state_has_a_transition_rule(self):
        assert set(TRANSACTION_TRANSITIONS) == TransactionStatus.ALL

    def test_execution_may_have_started_in_processing_and_unknown(self):
        assert execution_may_have_started(TransactionStatus.PROCESSING)
        assert execution_may_have_started(TransactionStatus.UNKNOWN)
        assert not execution_may_have_started(TransactionStatus.CREATED)

    def test_only_outgoing_requires_a_reservation(self):
        assert requires_reservation(TransactionDirection.OUTGOING)
        assert not requires_reservation(TransactionDirection.INCOMING)


class TestTransitionTable:
    @pytest.mark.parametrize(
        'start,target',
        [
            ('created', 'processing'),
            ('created', 'failed'),
            ('processing', 'succeeded'),
            ('processing', 'failed'),
            ('processing', 'unknown'),
            ('unknown', 'succeeded'),
            ('unknown', 'failed'),
        ],
    )
    def test_allowed(self, start, target):
        assert can_transition(start, target)

    @pytest.mark.parametrize(
        'start,target',
        [
            # Ambiguity never rewinds: an execution may already have happened.
            ('unknown', 'processing'),
            # Terminal is terminal.
            ('failed', 'processing'),
            ('failed', 'succeeded'),
            ('failed', 'unknown'),
            ('succeeded', 'failed'),
            ('succeeded', 'processing'),
            # There is no cancellation, from any state.
            ('created', 'cancelled'),
            ('processing', 'cancelled'),
            ('unknown', 'cancelled'),
            # No skipping the middle.
            ('created', 'succeeded'),
            ('created', 'unknown'),
            # Self transitions are caller mistakes, not silent successes.
            ('processing', 'processing'),
            ('created', 'created'),
        ],
    )
    def test_forbidden(self, start, target):
        assert not can_transition(start, target)


# ---------------------------------------------------------------------------
# Allowed transitions through the service
# ---------------------------------------------------------------------------


class TestStartProcessing:
    def test_an_outgoing_transaction_with_a_reservation_starts(self, outgoing):
        started = start_processing(outgoing)

        assert started.status == TransactionStatus.PROCESSING
        assert started.processing_at is not None

    def test_an_incoming_transaction_needs_no_reservation(self, incoming):
        started = start_processing(incoming)

        assert started.status == TransactionStatus.PROCESSING
        assert started.hold_id is None

    def test_an_outgoing_transaction_without_a_reservation_is_refused(
        self, funded_wallet
    ):
        txn = create_transaction(
            funded_wallet, TransactionDirection.OUTGOING, 1_000,
            idempotency_key='no-hold',
        )

        with pytest.raises(TransactionHoldRequiredError):
            start_processing(txn)

    def test_an_outgoing_transaction_with_a_released_reservation_is_refused(
        self, outgoing
    ):
        from moneycore.services.holds import release_hold

        release_hold(outgoing.hold)

        with pytest.raises(TransactionHoldInvalidError):
            start_processing(outgoing)

    def test_an_outgoing_transaction_with_a_time_expired_reservation_is_refused(
        self, funded_wallet
    ):
        """M4 invents no expiry policy and never renews a reservation."""
        from datetime import timedelta

        txn = create_transaction(
            funded_wallet, TransactionDirection.OUTGOING, 1_000,
            idempotency_key='expired-hold',
        )
        overdue = FundsHold.objects.create(
            wallet=funded_wallet, currency='NGN', amount_minor=1_000,
            status=HoldStatus.ACTIVE,
            expires_at=timezone.now() - timedelta(hours=1),
        )
        attach_hold(txn, overdue)

        with pytest.raises(TransactionHoldInvalidError):
            start_processing(txn)

    def test_starting_posts_no_journal_and_moves_no_balance(self, outgoing):
        before = (Journal.objects.count(), JournalEntry.objects.count())
        projection_before = wallet_balance_projection(outgoing.wallet)

        start_processing(outgoing)

        assert (Journal.objects.count(), JournalEntry.objects.count()) == before
        assert wallet_balance_projection(outgoing.wallet) == projection_before


class TestAttachHold:
    def test_a_hold_can_be_attached_before_execution(self, funded_wallet):
        txn = create_transaction(
            funded_wallet, TransactionDirection.OUTGOING, 1_000,
            idempotency_key='attach-1',
        )
        hold = create_hold(funded_wallet, 1_000)

        attached = attach_hold(txn, hold)

        assert attached.hold_id == hold.pk

    def test_a_hold_cannot_be_attached_once_processing(self, outgoing):
        started = start_processing(outgoing)
        other = create_hold(started.wallet, 1_000)

        with pytest.raises(InvalidTransactionTransitionError):
            attach_hold(started, other)

    def test_a_hold_for_a_different_wallet_is_refused(self, funded_wallet):
        from django.contrib.auth.models import User

        from moneycore.domain.ledger import LedgerAccountType
        from moneycore.services.ledger import open_wallet_ledger_account
        from moneycore.services.provisioning import provision_financial_account

        other_wallet = provision_financial_account(
            User.objects.create_user('hold-other')
        ).wallet
        open_wallet_ledger_account(
            other_wallet, account_type=LedgerAccountType.LIABILITY
        )
        foreign = FundsHold.objects.create(
            wallet=other_wallet, currency='NGN', amount_minor=100,
            status=HoldStatus.ACTIVE,
        )
        txn = create_transaction(
            funded_wallet, TransactionDirection.OUTGOING, 100,
            idempotency_key='foreign-hold',
        )

        with pytest.raises(TransactionHoldInvalidError):
            attach_hold(txn, foreign)

    def test_a_hold_already_funding_another_transaction_is_refused(
        self, funded_wallet
    ):
        hold = create_hold(funded_wallet, 1_000)
        first = create_transaction(
            funded_wallet, TransactionDirection.OUTGOING, 1_000,
            idempotency_key='owner-1',
        )
        second = create_transaction(
            funded_wallet, TransactionDirection.OUTGOING, 1_000,
            idempotency_key='owner-2',
        )
        attach_hold(first, hold)

        with pytest.raises(TransactionHoldInvalidError):
            attach_hold(second, hold)

    def test_the_hold_amount_need_not_equal_the_transaction_amount(
        self, funded_wallet
    ):
        """A reservation may cover more than the principal — fees, later."""
        txn = create_transaction(
            funded_wallet, TransactionDirection.OUTGOING, 1_000,
            idempotency_key='bigger-hold',
        )
        hold = create_hold(funded_wallet, 1_050)

        attached = attach_hold(txn, hold)

        assert attached.hold.amount_minor == 1_050
        assert attached.amount_minor == 1_000


# ---------------------------------------------------------------------------
# UNKNOWN — the central M4 semantics
# ---------------------------------------------------------------------------


class TestUnknownSemantics:
    @pytest.fixture
    def processing(self, outgoing):
        return start_processing(outgoing)

    def test_processing_may_become_unknown(self, processing):
        unresolved = mark_unknown(processing)

        assert unresolved.status == TransactionStatus.UNKNOWN
        assert unresolved.unknown_at is not None

    def test_unknown_is_persisted_and_survives_a_reload(self, processing):
        unresolved = mark_unknown(processing)

        from moneycore.models import FinancialTransaction

        reloaded = FinancialTransaction.objects.get(pk=unresolved.pk)
        assert reloaded.status == TransactionStatus.UNKNOWN

    def test_unknown_is_not_resolved(self, processing):
        unresolved = mark_unknown(processing)

        assert unresolved.resolved_at is None
        assert not unresolved.is_terminal

    def test_entering_unknown_posts_no_journal(self, processing):
        before = (Journal.objects.count(), JournalEntry.objects.count())

        mark_unknown(processing)

        assert (Journal.objects.count(), JournalEntry.objects.count()) == before

    def test_entering_unknown_keeps_the_hold_active(self, processing):
        """This is what keeps customer funds reserved while confirming."""
        mark_unknown(processing)

        processing.hold.refresh_from_db()
        assert processing.hold.status == HoldStatus.ACTIVE

    def test_entering_unknown_changes_no_balance(self, processing):
        before = wallet_balance_projection(processing.wallet)

        mark_unknown(processing)

        after = wallet_balance_projection(processing.wallet)
        assert after == before
        assert after.posted == Money(10_000, 'NGN')
        assert after.held == Money(7_000, 'NGN')
        assert after.available == Money(3_000, 'NGN')

    def test_unknown_cannot_go_back_to_processing(self, processing):
        unresolved = mark_unknown(processing)

        with pytest.raises(InvalidTransactionTransitionError):
            start_processing(unresolved)

    def test_unknown_may_resolve_to_failure(self, processing):
        unresolved = mark_unknown(processing)

        failed = fail_transaction(unresolved, failure_code='not_executed')

        assert failed.status == TransactionStatus.FAILED

    def test_a_created_transaction_cannot_jump_to_unknown(self, outgoing):
        with pytest.raises(InvalidTransactionTransitionError):
            mark_unknown(outgoing)


class TestTimeoutIsNeverFailure:
    def test_no_timeout_helper_exists(self):
        from moneycore.services import transactions

        for forbidden in (
            'mark_timeout_failed', 'timeout_transaction', 'handle_timeout',
            'fail_on_timeout', 'expire_transaction',
        ):
            assert not hasattr(transactions, forbidden), f'{forbidden} exists'

    def test_no_retry_helper_exists(self):
        """A retry is a new operation, never a resurrection of terminal state."""
        from moneycore.services import transactions

        for forbidden in (
            'retry_transaction', 'resubmit_transaction', 'reset_transaction',
            'reopen_transaction',
        ):
            assert not hasattr(transactions, forbidden), f'{forbidden} exists'

    def test_the_service_surface_is_exactly_the_m4_state_machine(self):
        import inspect

        from moneycore.services import transactions

        public = sorted(
            name
            for name, value in vars(transactions).items()
            if inspect.isfunction(value)
            and value.__module__ == transactions.__name__
            and not name.startswith('_')
        )

        assert public == [
            'attach_hold', 'create_transaction', 'fail_transaction',
            'mark_unknown', 'start_processing', 'succeed_transaction',
        ]


# ---------------------------------------------------------------------------
# Known failure
# ---------------------------------------------------------------------------


class TestKnownFailure:
    @pytest.fixture
    def processing(self, outgoing):
        return start_processing(outgoing)

    def test_processing_may_fail(self, processing):
        failed = fail_transaction(processing, failure_code='rejected')

        assert failed.status == TransactionStatus.FAILED
        assert failed.failure_code == 'rejected'
        assert failed.resolved_at is not None

    def test_failure_releases_the_reservation(self, processing):
        failed = fail_transaction(processing)

        failed.hold.refresh_from_db()
        assert failed.hold.status == HoldStatus.RELEASED

    def test_failure_restores_spendability_without_moving_posted_funds(
        self, processing
    ):
        fail_transaction(processing)

        projection = wallet_balance_projection(processing.wallet)
        assert projection.posted == Money(10_000, 'NGN')
        assert projection.held == Money(0, 'NGN')
        assert projection.available == Money(10_000, 'NGN')

    def test_failure_posts_no_journal(self, processing):
        before = (Journal.objects.count(), JournalEntry.objects.count())

        fail_transaction(processing)

        assert (Journal.objects.count(), JournalEntry.objects.count()) == before

    def test_failure_from_unknown_behaves_identically(self, processing):
        unresolved = mark_unknown(processing)

        failed = fail_transaction(unresolved)

        failed.hold.refresh_from_db()
        assert failed.status == TransactionStatus.FAILED
        assert failed.hold.status == HoldStatus.RELEASED
        assert wallet_balance_projection(
            processing.wallet
        ).available == Money(10_000, 'NGN')

    def test_a_created_transaction_may_fail_on_a_pre_execution_check(
        self, funded_wallet
    ):
        txn = create_transaction(
            funded_wallet, TransactionDirection.OUTGOING, 1_000,
            idempotency_key='pre-fail',
        )

        failed = fail_transaction(txn, failure_code='rejected_before_execution')

        assert failed.status == TransactionStatus.FAILED

    def test_no_provider_text_is_stored(self, processing):
        """failure_code is a normalised internal code; M6 defines the vocabulary."""
        failed = fail_transaction(processing, failure_code='insufficient_funds')

        assert failed.failure_code == 'insufficient_funds'
        assert len(failed.failure_code) <= 64

    def test_a_resolved_transaction_cannot_fail_again(self, processing):
        fail_transaction(processing)

        with pytest.raises(TransactionAlreadyResolvedError):
            fail_transaction(processing)


# ---------------------------------------------------------------------------
# No cancellation
# ---------------------------------------------------------------------------


class TestThereIsNoCancellation:
    """Locked architecture §8.5, implemented rather than reinterpreted.

    Backing out before execution is accepted does not create a financial
    transaction, so there is nothing here to cancel. A `FinancialTransaction`
    is not a persisted draft of a UI flow.
    """

    def test_the_service_offers_no_cancellation_entry_point(self):
        from moneycore.services import transactions

        for forbidden in (
            'cancel_transaction', 'cancel', 'abandon_transaction', 'abandon',
            'void_transaction', 'void', 'discard_transaction',
        ):
            assert not hasattr(transactions, forbidden), f'{forbidden} exists'

    def test_the_domain_defines_no_cancellation_state(self):
        from moneycore.domain import transactions as domain

        for forbidden in ('CANCELLED', 'ABANDONED', 'VOIDED', 'DRAFT'):
            assert not hasattr(domain.TransactionStatus, forbidden)

    def test_no_transition_leads_out_of_created_except_execution_or_failure(self):
        assert TRANSACTION_TRANSITIONS[TransactionStatus.CREATED] == frozenset({
            TransactionStatus.PROCESSING,
            TransactionStatus.FAILED,
        })

    def test_no_cancellation_vocabulary_survives_in_the_modules(self):
        from pathlib import Path

        from moneycore.domain import transactions as domain
        from moneycore.services import transactions as service

        for module in (domain, service):
            source = Path(module.__file__).read_text(encoding='utf-8')
            # The modules state that cancellation does not exist; they must not
            # define, name or dispatch on one.
            assert 'CANCELLED' not in source
            assert 'def cancel' not in source

    def test_a_created_transaction_can_only_execute_or_fail(self, outgoing):
        """Nothing else is reachable, so nothing else is a way out."""
        reachable = {
            target
            for target in TransactionStatus.ALL
            if can_transition(TransactionStatus.CREATED, target)
        }

        assert reachable == {TransactionStatus.PROCESSING, TransactionStatus.FAILED}

    def test_a_pre_execution_failure_still_releases_the_reservation(self, outgoing):
        """The one legitimate pre-execution exit, and it is authoritative.

        This is not cancellation by another name: it records that a check
        genuinely failed and the money is known not to have moved.
        """
        failed = fail_transaction(outgoing, failure_code='limit_exceeded')

        failed.hold.refresh_from_db()
        assert failed.status == TransactionStatus.FAILED
        assert failed.hold.status == HoldStatus.RELEASED
        assert wallet_balance_projection(
            outgoing.wallet
        ).available == Money(10_000, 'NGN')

    def test_a_pre_execution_failure_posts_no_journal(self, outgoing):
        before = Journal.objects.count()

        fail_transaction(outgoing, failure_code='limit_exceeded')

        assert Journal.objects.count() == before


class TestTerminalStatesAreFinal:
    @pytest.mark.parametrize(
        'operation',
        ['start_processing', 'mark_unknown', 'fail_transaction'],
    )
    def test_nothing_moves_a_failed_transaction(self, outgoing, operation):
        import moneycore.services.transactions as service

        started = start_processing(outgoing)
        fail_transaction(started)

        with pytest.raises(TransactionAlreadyResolvedError):
            getattr(service, operation)(started)

    @pytest.mark.parametrize(
        'operation',
        ['start_processing', 'mark_unknown', 'fail_transaction'],
    )
    def test_nothing_moves_a_transaction_that_failed_before_execution(
        self, outgoing, operation
    ):
        import moneycore.services.transactions as service

        fail_transaction(outgoing, failure_code='limit_exceeded')

        with pytest.raises(TransactionAlreadyResolvedError):
            getattr(service, operation)(outgoing)
