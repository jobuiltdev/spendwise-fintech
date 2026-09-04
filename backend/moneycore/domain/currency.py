"""Currency-code validation, shared by the Money value type and the Wallet model.

One definition so a currency code means exactly the same thing whether it arrives
as a value object or as a persisted column: ISO-4217 alpha-3, uppercase. Codes
are never normalised — a lowercase code is a caller bug, and quietly
upper-casing it would hide that.

This module deliberately holds no list of *which* currencies exist. Banking V1 is
NGN-oriented, but nothing here is specific to NGN, and no allow-list is invented
before there is a decision to base one on.
"""

from __future__ import annotations

import re
from typing import Final

CURRENCY_CODE_PATTERN: Final = re.compile(r'^[A-Z]{3}$')

CURRENCY_CODE_LENGTH: Final = 3

INVALID_CURRENCY_MESSAGE: Final = (
    'currency must be an ISO-4217 alpha-3 code in uppercase'
)


def is_valid_currency_code(value: object) -> bool:
    """True when ``value`` is a well-formed ISO-4217 alpha-3 code."""
    return isinstance(value, str) and bool(CURRENCY_CODE_PATTERN.match(value))
