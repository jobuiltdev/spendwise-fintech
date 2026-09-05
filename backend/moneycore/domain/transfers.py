"""The transfer: which customer-originated bank transfer SpendWise intends.

Pure domain. Nothing here touches the database, and nothing here knows that
providers exist.

Four concepts, kept apart
-------------------------
* **Journal** — what money has posted (M2).
* **Hold** — what posted money is reserved (M3).
* **FinancialTransaction** — what operation is happening, and where it has got
  to (M4).
* **Transfer** — *which* bank transfer is being attempted (M5).

A transfer owns business intent: who the money is going to, at which bank, and
what the customer wrote on it. It owns no lifecycle of its own — the linked
`FinancialTransaction` is the authoritative status — and it owns no provider
concept at all. Provider execution, provider references, webhooks,
reconciliation and settlement are M6 and later.

The verification boundary
-------------------------
The locked UX resolves a recipient's real name before the amount is entered.
Doing that requires a provider, and M5 has none — so M5 models the *fact* of a
verified destination without implementing how it is obtained.
:class:`VerifiedBankAccount` is that fact. Constructing one is a caller's
assertion that the destination was verified by something trustworthy; M5
provides no way to obtain one, performs no lookup, and offers no endpoint that
claims to verify an account. M6's provider abstraction is where a real (or
simulated) verification will produce these values.

Scope
-----
**Outgoing bank transfers only.** Receiving money and account funding need
provider and webhook knowledge that does not exist yet, so M5 builds no
incoming transfer object. M4 already supports incoming *transactions*
generically; fabricating an incoming transfer flow purely to exercise that
would be inventing a product surface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from moneycore.domain.errors import InvalidTransferDestinationError

# --------------------------------------------------------------------------
# Destination field rules
# --------------------------------------------------------------------------
# IMPORTANT: the account-number rule below is the **current Nigerian bank
# transfer product rule**, not a universal moneycore invariant. NUBAN account
# numbers are ten digits, and SpendWise's first rail is Nigerian bank transfer,
# so M5 validates ten digits. A future rail with a different account format
# needs a destination type of its own — it does not need this constant widened
# until there is a real second rail to widen it for.

NGN_BANK_ACCOUNT_NUMBER_LENGTH: Final = 10
NGN_BANK_ACCOUNT_NUMBER_PATTERN: Final = re.compile(r'^[0-9]{10}$')

# Bank codes are treated as **opaque validated strings**. M5 takes no position
# on any payment provider's code vocabulary: no provider's bank list appears
# here, and no bank directory is bundled. The bank-directory layer that
# eventually supplies these codes belongs to M6, as does the mapping between a
# provider's vocabulary and ours (O-28). Until then the only safe rule is a
# conservative shape check: short, printable, no whitespace.
BANK_CODE_PATTERN: Final = re.compile(r'^[A-Za-z0-9][A-Za-z0-9\-_]{0,29}$')
BANK_CODE_MAX_LENGTH: Final = 30

BANK_NAME_MAX_LENGTH: Final = 128
ACCOUNT_NAME_MAX_LENGTH: Final = 128

#: Narration is a short customer-supplied note. Bounded so it cannot become a
#: place to stash payloads, and deliberately free of provider vocabulary.
NARRATION_MAX_LENGTH: Final = 100

#: Anything in this class is refused in free-text fields: newlines and control
#: characters have no place in a bank narration or a recipient name, and they
#: are how log-injection and display-spoofing get in.
_CONTROL_CHARACTERS: Final = re.compile(r'[\x00-\x1f\x7f]')


def _require_text(
    value: object, *, field: str, max_length: int, allow_blank: bool = False
) -> str:
    """Normalise and validate one free-text destination field.

    Surrounding whitespace is stripped — a copied-and-pasted value routinely
    carries some, and rejecting it would be pedantry rather than safety.
    Internal whitespace is left exactly as given, because a recipient's name is
    not ours to reformat.
    """
    if not isinstance(value, str):
        raise InvalidTransferDestinationError(
            f'{field} must be text.',
            details={'field': field, 'type': type(value).__name__},
        )

    cleaned = value.strip()
    if not cleaned and not allow_blank:
        raise InvalidTransferDestinationError(
            f'{field} is required.',
            details={'field': field},
        )
    if len(cleaned) > max_length:
        raise InvalidTransferDestinationError(
            f'{field} is longer than {max_length} characters.',
            details={'field': field, 'length': len(cleaned), 'max': max_length},
        )
    if _CONTROL_CHARACTERS.search(cleaned):
        raise InvalidTransferDestinationError(
            f'{field} must not contain control characters or line breaks.',
            details={'field': field},
        )
    return cleaned


def normalise_narration(value: object) -> str:
    """Validate an optional narration, returning the stored form."""
    if value is None:
        return ''
    return _require_text(
        value, field='narration', max_length=NARRATION_MAX_LENGTH, allow_blank=True
    )


@dataclass(frozen=True)
class VerifiedBankAccount:
    """A bank destination whose account name has already been verified.

    Constructing one is an assertion by the caller that the account name came
    from a trustworthy resolution, not from customer input. **M5 provides no
    way to obtain that resolution** — there is no lookup function, no bank
    directory and no endpoint here that claims to verify an account, because
    faking verification is worse than not offering it. M6 supplies the real
    thing.

    Frozen, and validated on construction, so a destination cannot be built
    invalid and cannot drift afterwards. The values validated here are exactly
    the ones snapshotted onto a `Transfer`.
    """

    account_number: str
    bank_code: str
    bank_name: str
    account_name: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, 'account_number', self._clean_account_number(self.account_number)
        )
        object.__setattr__(self, 'bank_code', self._clean_bank_code(self.bank_code))
        object.__setattr__(
            self,
            'bank_name',
            _require_text(
                self.bank_name, field='bank_name', max_length=BANK_NAME_MAX_LENGTH
            ),
        )
        object.__setattr__(
            self,
            'account_name',
            _require_text(
                self.account_name,
                field='account_name',
                max_length=ACCOUNT_NAME_MAX_LENGTH,
            ),
        )

    @staticmethod
    def _clean_account_number(value: object) -> str:
        """Ten digits, per the current NGN bank transfer rule.

        Surrounding whitespace is stripped; **internal whitespace is refused**
        rather than removed. Being lenient about the digits of an account
        number is how money reaches the wrong person, so a value that is not
        already exactly right is rejected rather than repaired.
        """
        if not isinstance(value, str):
            raise InvalidTransferDestinationError(
                'account_number must be text.',
                details={
                    'field': 'account_number', 'type': type(value).__name__
                },
            )

        cleaned = value.strip()
        if not NGN_BANK_ACCOUNT_NUMBER_PATTERN.fullmatch(cleaned):
            raise InvalidTransferDestinationError(
                'account_number must be exactly '
                f'{NGN_BANK_ACCOUNT_NUMBER_LENGTH} digits.',
                details={'field': 'account_number', 'length': len(cleaned)},
            )
        return cleaned

    @staticmethod
    def _clean_bank_code(value: object) -> str:
        """An opaque code. Shape only — M5 knows no bank-code vocabulary."""
        if not isinstance(value, str):
            raise InvalidTransferDestinationError(
                'bank_code must be text.',
                details={'field': 'bank_code', 'type': type(value).__name__},
            )

        cleaned = value.strip()
        if not cleaned:
            raise InvalidTransferDestinationError(
                'bank_code is required.', details={'field': 'bank_code'}
            )
        if not BANK_CODE_PATTERN.fullmatch(cleaned):
            raise InvalidTransferDestinationError(
                'bank_code must be a short code with no whitespace.',
                details={'field': 'bank_code', 'value_length': len(cleaned)},
            )
        return cleaned

    def matches_snapshot(self, transfer) -> bool:
        """Whether a stored transfer records exactly this destination."""
        return (
            transfer.destination_account_number == self.account_number
            and transfer.destination_bank_code == self.bank_code
            and transfer.destination_bank_name == self.bank_name
            and transfer.recipient_name == self.account_name
        )


def as_snapshot(destination: VerifiedBankAccount) -> dict[str, str]:
    """The stored form of a destination, ready for a `Transfer`."""
    return {
        'recipient_name': destination.account_name,
        'destination_account_number': destination.account_number,
        'destination_bank_code': destination.bank_code,
        'destination_bank_name': destination.bank_name,
    }
