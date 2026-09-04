"""Transaction-intent idempotency.

Scope note: this is idempotency of *creating a transaction intent*, and nothing
more. It is not a general idempotency framework, and it says nothing about
provider idempotency (M6) or webhook replay (M7).
"""

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction as db_transaction

from moneycore.domain.errors import (
    TransactionIdempotencyConflictError,
    TransactionIntentImmutableError,
)
from moneycore.domain.transactions import TransactionDirection, TransactionStatus
from moneycore.models import FinancialTransaction
from moneycore.services.holds import create_hold
from moneycore.services.ledger import credit, debit
from moneycore.services.provisioning import provision_financial_account
from moneycore.services.transactions import (
    attach_hold,
    create_transaction,
    fail_transaction,
    start_processing,
    succeed_transaction,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def other_wallet(db):
    return provision_financial_account(
        User.objects.create_user('other-idempotency-user')
    ).wallet


def outgoing(wallet, key, amount=1_000):
    return create_transaction(
        wallet, TransactionDirection.OUTGOING, amount, idempotency_key=key
    )


class TestSameKeySameIntent:
    def test_the_same_transaction_comes_back(self, wallet):
        first = outgoing(wallet, 'repeat-1')
        second = outgoing(wallet, 'repeat-1')

        assert second.pk == first.pk

    def test_no_second_row_is_created(self, wallet):
        outgoing(wallet, 'repeat-2')
        outgoing(wallet, 'repeat-2')
        outgoing(wallet, 'repeat-2')

        assert FinancialTransaction.objects.filter(
            wallet=wallet, idempotency_key='repeat-2'
        ).count() == 1

    def test_an_explicit_matching_currency_is_still_the_same_intent(self, wallet):
        first = create_transaction(
            wallet, TransactionDirection.OUTGOING, 1_000,
            idempotency_key='repeat-3', currency=wallet.currency,
        )
        second = create_transaction(
            wallet, TransactionDirection.OUTGOING, 1_000,
            idempotency_key='repeat-3',
        )

        assert second.pk == first.pk

    def test_incoming_repeats_are_idempotent_too(self, wallet):
        first = create_transaction(
            wallet, TransactionDirection.INCOMING, 500, idempotency_key='in-1'
        )
        second = create_transaction(
            wallet, TransactionDirection.INCOMING, 500, idempotency_key='in-1'
        )

        assert second.pk == first.pk


class TestSameKeyDifferentIntent:
    """A reused key with different terms is a caller bug, never a silent no-op."""

    def test_a_different_amount_is_refused(self, wallet):
        outgoing(wallet, 'conflict-1', amount=1_000)

        with pytest.raises(TransactionIdempotencyConflictError):
            outgoing(wallet, 'conflict-1', amount=2_000)

    def test_a_different_direction_is_refused(self, wallet):
        outgoing(wallet, 'conflict-2', amount=1_000)

        with pytest.raises(TransactionIdempotencyConflictError):
            create_transaction(
                wallet, TransactionDirection.INCOMING, 1_000,
                idempotency_key='conflict-2',
            )

    def test_the_original_is_left_untouched(self, wallet):
        original = outgoing(wallet, 'conflict-3', amount=1_000)

        with pytest.raises(TransactionIdempotencyConflictError):
            outgoing(wallet, 'conflict-3', amount=9_999)

        original.refresh_from_db()
        assert original.amount_minor == 1_000
        assert FinancialTransaction.objects.filter(wallet=wallet).count() == 1

    def test_the_error_reports_both_intents(self, wallet):
        outgoing(wallet, 'conflict-4', amount=1_000)

        with pytest.raises(TransactionIdempotencyConflictError) as raised:
            outgoing(wallet, 'conflict-4', amount=2_000)

        details = raised.value.details
        assert details['existing']['amount_minor'] == 1_000
        assert details['requested']['amount_minor'] == 2_000

    def test_the_conflict_carries_a_stable_code(self, wallet):
        outgoing(wallet, 'conflict-5', amount=1_000)

        with pytest.raises(TransactionIdempotencyConflictError) as raised:
            outgoing(wallet, 'conflict-5', amount=2_000)

        assert raised.value.code == 'transaction_idempotency_conflict'


class TestKeyScope:
    """Keys are per wallet, so two customers cannot collide."""

    def test_two_wallets_may_use_the_same_key(self, wallet, other_wallet):
        first = outgoing(wallet, 'shared-key')
        second = outgoing(other_wallet, 'shared-key')

        assert first.pk != second.pk
        assert FinancialTransaction.objects.filter(
            idempotency_key='shared-key'
        ).count() == 2

    def test_different_keys_on_one_wallet_are_different_transactions(self, wallet):
        first = outgoing(wallet, 'key-a')
        second = outgoing(wallet, 'key-b')

        assert first.pk != second.pk

    def test_the_database_enforces_uniqueness_directly(self, wallet):
        """Not merely an application check — the constraint is the real guard."""
        outgoing(wallet, 'db-guard')

        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                FinancialTransaction.objects.create(
                    wallet=wallet,
                    direction=TransactionDirection.OUTGOING,
                    amount_minor=1_000,
                    currency=wallet.currency,
                    status=TransactionStatus.CREATED,
                    idempotency_key='db-guard',
                )


class TestIdempotencyAcrossTheLifecycle:
    """A replayed key returns the operation as it actually stands."""

    def test_a_processing_transaction_is_returned_not_restarted(self, funded_wallet):
        txn = outgoing(funded_wallet, 'live-1', amount=1_000)
        started = start_processing(attach_hold(txn, create_hold(funded_wallet, 1_000)))

        replay = outgoing(funded_wallet, 'live-1', amount=1_000)

        assert replay.pk == started.pk
        assert replay.status == TransactionStatus.PROCESSING

    def test_a_succeeded_transaction_is_returned_unchanged(
        self, funded_wallet, wallet_account, counterpart_account
    ):
        txn = outgoing(funded_wallet, 'live-2', amount=1_000)
        started = start_processing(attach_hold(txn, create_hold(funded_wallet, 1_000)))
        succeeded = succeed_transaction(
            started,
            entries=[debit(wallet_account, 1_000), credit(counterpart_account, 1_000)],
        )

        replay = outgoing(funded_wallet, 'live-2', amount=1_000)

        assert replay.pk == succeeded.pk
        assert replay.status == TransactionStatus.SUCCEEDED
        assert replay.journal_id == succeeded.journal_id

    def test_a_replay_after_success_posts_nothing_further(
        self, funded_wallet, wallet_account, counterpart_account
    ):
        from moneycore.models import Journal

        txn = outgoing(funded_wallet, 'live-3', amount=1_000)
        started = start_processing(attach_hold(txn, create_hold(funded_wallet, 1_000)))
        succeed_transaction(
            started,
            entries=[debit(wallet_account, 1_000), credit(counterpart_account, 1_000)],
        )
        journals = Journal.objects.count()

        outgoing(funded_wallet, 'live-3', amount=1_000)

        assert Journal.objects.count() == journals

    def test_a_failed_transaction_is_returned_not_retried(self, funded_wallet):
        txn = outgoing(funded_wallet, 'live-4', amount=1_000)
        started = start_processing(attach_hold(txn, create_hold(funded_wallet, 1_000)))
        failed = fail_transaction(started, failure_code='declined')

        replay = outgoing(funded_wallet, 'live-4', amount=1_000)

        assert replay.pk == failed.pk
        assert replay.status == TransactionStatus.FAILED

    def test_a_failed_key_is_not_reusable_for_a_fresh_attempt(self, funded_wallet):
        """FAILED is terminal and remains historical.

        Reusing the key returns the failed operation rather than starting a
        second one, which is what stops a retry loop from spending twice. What
        a legitimate new attempt requires is a fresh-intent policy M5 defines
        (O-27); M4 invents no retry semantics.
        """
        txn = outgoing(funded_wallet, 'live-5', amount=1_000)
        failed = fail_transaction(txn, failure_code='limit_exceeded')

        replay = outgoing(funded_wallet, 'live-5', amount=1_000)

        assert replay.pk == failed.pk
        assert replay.status == TransactionStatus.FAILED

    def test_a_new_key_does_start_a_second_operation(self, funded_wallet):
        first = outgoing(funded_wallet, 'live-6', amount=1_000)
        fail_transaction(first, failure_code='limit_exceeded')

        second = outgoing(funded_wallet, 'live-6-retry', amount=1_000)

        assert second.pk != first.pk
        assert second.status == TransactionStatus.CREATED


class TestTheKeyIsPartOfTheIntent:
    def test_it_cannot_be_rewritten_afterwards(self, wallet):
        txn = outgoing(wallet, 'immutable-key')

        txn.idempotency_key = 'something-else'
        with pytest.raises(TransactionIntentImmutableError):
            txn.save()

    def test_the_stored_key_is_unchanged_after_a_refused_edit(self, wallet):
        txn = outgoing(wallet, 'immutable-key-2')

        txn.idempotency_key = 'something-else'
        with pytest.raises(TransactionIntentImmutableError):
            txn.save()

        txn.refresh_from_db()
        assert txn.idempotency_key == 'immutable-key-2'
