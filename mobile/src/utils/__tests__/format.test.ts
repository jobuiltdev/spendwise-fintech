/**
 * Characterization: formatting helpers.
 *
 * `toISODateString` and `formatGreetingTime` are stable and worth pinning.
 *
 * `formatCurrency` is a KNOWN FINTECH CONFLICT and is characterized here only
 * to record what it does today — not to endorse it. It coerces money to a
 * JavaScript number and renders a bare `toFixed(2)`, which conflicts with the
 * fintech money rules (no float arithmetic on authoritative money; tabular,
 * grouped presentation). It is adequate for user-entered tracking figures and
 * must not be used for ledger amounts. Deliberately not "fixed" in this pass.
 */

import { formatCurrency, formatGreetingTime, toISODateString } from '@/utils/format';

describe('toISODateString', () => {
  it('formats a date as YYYY-MM-DD', () => {
    expect(toISODateString(new Date(2024, 0, 15))).toBe('2024-01-15');
  });

  it('zero-pads single-digit months and days', () => {
    expect(toISODateString(new Date(2024, 8, 5))).toBe('2024-09-05');
  });

  it('handles the last day of a leap February', () => {
    expect(toISODateString(new Date(2024, 1, 29))).toBe('2024-02-29');
  });

  it('reads local date parts, so a late-evening local time keeps its own day', () => {
    // Built from local components, so this is the same calendar day the user
    // sees regardless of the runner's timezone offset.
    expect(toISODateString(new Date(2024, 5, 30, 23, 59))).toBe('2024-06-30');
  });
});

describe('formatGreetingTime', () => {
  it.each([
    [0, 'Good morning'],
    [11, 'Good morning'],
    [12, 'Good afternoon'],
    [17, 'Good afternoon'],
    [18, 'Good evening'],
    [23, 'Good evening'],
  ])('returns %s-hour greeting', (hour, expected) => {
    expect(formatGreetingTime(new Date(2024, 0, 1, hour))).toBe(expected);
  });
});

describe('formatCurrency (current tracker behaviour)', () => {
  it('prefixes the currency code and shows two decimals', () => {
    expect(formatCurrency(1234.5, 'NGN')).toBe('NGN 1234.50');
  });

  it('accepts the string amounts the API returns', () => {
    expect(formatCurrency('99.9', 'NGN')).toBe('NGN 99.90');
  });

  it('omits the prefix entirely when no currency is given', () => {
    expect(formatCurrency(10, '')).toBe('10.00');
  });

  it('renders negative amounts with a leading minus', () => {
    expect(formatCurrency(-25.5, 'NGN')).toBe('NGN -25.50');
  });

  describe('known limitations — recorded, not endorsed', () => {
    it('does not group thousands, so large amounts are hard to scan', () => {
      // The fintech money rules call for tabular, grouped presentation.
      expect(formatCurrency(1000000, 'NGN')).toBe('NGN 1000000.00');
    });

    it('coerces through a JavaScript number, so precision is float-bounded', () => {
      // Conflicts with "no float arithmetic for authoritative money". Safe for
      // user-entered tracking values; unsafe for ledger amounts.
      expect(formatCurrency('9007199254740993', 'NGN')).toBe('NGN 9007199254740992.00');
    });

    it('renders a non-numeric amount as NaN rather than failing loudly', () => {
      expect(formatCurrency('not-a-number', 'NGN')).toBe('NGN NaN');
    });
  });
});
