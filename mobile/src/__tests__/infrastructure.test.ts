/**
 * Smoke tests proving the test infrastructure itself works.
 *
 * These assert nothing meaningful about product behaviour — they confirm that
 * TypeScript sources are transformed, the "@/" path alias resolves, and test
 * code executes. Real behaviour tests belong next to the code they cover
 * (see docs/TESTING.md).
 */

import { formatCurrency, toISODateString } from '@/utils/format';

describe('test infrastructure', () => {
  it('transforms TypeScript and resolves the @/ path alias', () => {
    expect(typeof toISODateString).toBe('function');
    expect(typeof formatCurrency).toBe('function');
  });

  it('executes imported module code', () => {
    // A fixed, timezone-independent date: the helper reads local date parts.
    expect(toISODateString(new Date(2024, 0, 15))).toBe('2024-01-15');
  });
});
