/**
 * Characterization: group split maths.
 *
 * The defining property is that a split never loses or invents a cent:
 * `distributeCents` uses largest-remainder rounding so the parts always sum to
 * exactly the total, however awkward the division. That guarantee is worth
 * protecting regardless of what happens to the rest of the group UX.
 *
 * Split inputs are the raw strings the form collects, matching SplitInputs.
 */

import {
  buildGroupExpensePayload,
  computeCustomSplits,
  computeEqualSplits,
  computePercentageSplits,
  computeSharesSplits,
  computeSplits,
  validateSplits,
} from '@/utils/group-splits';

const sumOf = (splits: { amount: string }[]) =>
  splits.reduce((total, split) => total + Number(split.amount), 0);

const base = {
  group: 1,
  paid_by: 1,
  amount: '100.00',
  description: 'Dinner',
  date: '2024-01-15',
  notes: '',
};

describe('computeEqualSplits', () => {
  it('divides a clean amount evenly', () => {
    expect(computeEqualSplits(100, [1, 2])).toEqual([
      { user: 1, amount: '50.00' },
      { user: 2, amount: '50.00' },
    ]);
  });

  it('never loses a cent on an indivisible amount', () => {
    const splits = computeEqualSplits(10, [1, 2, 3]);

    expect(sumOf(splits)).toBeCloseTo(10, 10);
    expect(splits.map((s) => s.amount).sort()).toEqual(['3.33', '3.33', '3.34']);
  });

  it('gives the spare cents to the earliest participants', () => {
    const splits = computeEqualSplits(0.1, [1, 2, 3]);

    expect(splits.map((s) => s.amount)).toEqual(['0.04', '0.03', '0.03']);
    expect(sumOf(splits)).toBeCloseTo(0.1, 10);
  });

  it('handles a single participant', () => {
    expect(computeEqualSplits(42.42, [7])).toEqual([{ user: 7, amount: '42.42' }]);
  });

  it('returns nothing for no participants', () => {
    expect(computeEqualSplits(100, [])).toEqual([]);
  });

  it('rounds sub-cent amounts rather than dropping them', () => {
    expect(sumOf(computeEqualSplits(0.01, [1, 2]))).toBeCloseTo(0.01, 10);
  });
});

describe('computePercentageSplits', () => {
  it('applies each percentage to the total', () => {
    const splits = computePercentageSplits(200, [1, 2], { 1: '25', 2: '75' });

    expect(splits).toEqual([
      { user: 1, amount: '50.00', percentage: 25 },
      { user: 2, amount: '150.00', percentage: 75 },
    ]);
  });

  it('still sums to the total when the percentages divide awkwardly', () => {
    const splits = computePercentageSplits(100, [1, 2, 3], {
      1: '33.33',
      2: '33.33',
      3: '33.34',
    });

    expect(sumOf(splits)).toBeCloseTo(100, 10);
  });

  it('treats a missing percentage as zero', () => {
    const splits = computePercentageSplits(100, [1, 2], { 1: '100' });

    expect(splits.map((s) => s.amount)).toEqual(['100.00', '0.00']);
  });

  it('returns zeroes rather than NaN when every weight is zero', () => {
    const splits = computePercentageSplits(100, [1, 2], { 1: '0', 2: '0' });

    expect(splits.map((s) => s.amount)).toEqual(['0.00', '0.00']);
  });
});

describe('computeSharesSplits', () => {
  it('divides in proportion to share counts', () => {
    const splits = computeSharesSplits(90, [1, 2], { 1: '1', 2: '2' });

    expect(splits).toEqual([
      { user: 1, amount: '30.00' },
      { user: 2, amount: '60.00' },
    ]);
  });

  it('still sums to the total on an awkward division', () => {
    const splits = computeSharesSplits(10, [1, 2, 3], { 1: '1', 2: '1', 3: '1' });

    expect(sumOf(splits)).toBeCloseTo(10, 10);
  });
});

describe('computeCustomSplits', () => {
  it('takes the entered amounts verbatim', () => {
    expect(computeCustomSplits([1, 2], { 1: '70', 2: '30' })).toEqual([
      { user: 1, amount: '70.00' },
      { user: 2, amount: '30.00' },
    ]);
  });

  it('treats a missing entry as zero', () => {
    expect(computeCustomSplits([1, 2], { 1: '70' })).toEqual([
      { user: 1, amount: '70.00' },
      { user: 2, amount: '0.00' },
    ]);
  });
});

describe('computeSplits', () => {
  it('dispatches the equal strategy', () => {
    expect(sumOf(computeSplits('equal', 100, [1, 2], {}))).toBeCloseTo(100, 10);
  });

  it('dispatches the percentage strategy', () => {
    const splits = computeSplits('percentage', 100, [1, 2], { 1: '50', 2: '50' });

    expect(sumOf(splits)).toBeCloseTo(100, 10);
  });

  it('dispatches the shares strategy', () => {
    const splits = computeSplits('shares', 100, [1, 2], { 1: '1', 2: '1' });

    expect(sumOf(splits)).toBeCloseTo(100, 10);
  });

  it('dispatches the custom strategy', () => {
    const splits = computeSplits('custom', 100, [1, 2], { 1: '60', 2: '40' });

    expect(sumOf(splits)).toBeCloseTo(100, 10);
  });
});

describe('validateSplits', () => {
  it('accepts an equal split without inspecting the inputs', () => {
    expect(validateSplits('equal', 100, [1, 2], {})).toBeNull();
  });

  it('accepts percentages that total 100', () => {
    expect(validateSplits('percentage', 100, [1, 2], { 1: '40', 2: '60' })).toBeNull();
  });

  it('rejects percentages that do not total 100', () => {
    expect(validateSplits('percentage', 100, [1, 2], { 1: '40', 2: '40' })).toContain('80.0%');
  });

  it('tolerates a small percentage rounding drift', () => {
    expect(validateSplits('percentage', 100, [1, 2], { 1: '50.2', 2: '49.8' })).toBeNull();
  });

  it('accepts custom amounts that total the expense', () => {
    expect(validateSplits('custom', 100, [1, 2], { 1: '60', 2: '40' })).toBeNull();
  });

  it('rejects custom amounts that do not total the expense', () => {
    const error = validateSplits('custom', 100, [1, 2], { 1: '60', 2: '30' });

    expect(error).toContain('90.00');
    expect(error).toContain('100.00');
  });

  it('requires every participant to hold at least one share', () => {
    expect(validateSplits('shares', 100, [1, 2], { 1: '1', 2: '0' })).toBe(
      'Every participant needs at least 1 share.'
    );
  });

  it('accepts shares when everyone holds at least one', () => {
    expect(validateSplits('shares', 100, [1, 2], { 1: '1', 2: '3' })).toBeNull();
  });
});

describe('buildGroupExpensePayload', () => {
  it('uses the backend native equal split when everyone is included', () => {
    const payload = buildGroupExpensePayload(base, 'equal', 100, [1, 2], [1, 2], {});

    expect(payload).toEqual({ ...base, split_type: 'equal' });
    expect(payload).not.toHaveProperty('splits');
  });

  it('falls back to custom for an equal split over a subset of members', () => {
    const payload = buildGroupExpensePayload(base, 'equal', 100, [1], [1, 2], {});

    expect(payload.split_type).toBe('custom');
    expect(payload.splits).toEqual([{ user: 1, amount: '100.00' }]);
  });

  it('keeps the percentage split type and carries percentages through', () => {
    const payload = buildGroupExpensePayload(base, 'percentage', 100, [1, 2], [1, 2], {
      1: '30',
      2: '70',
    });

    expect(payload.split_type).toBe('percentage');
    expect(payload.splits).toEqual([
      { user: 1, amount: '30.00', percentage: 30 },
      { user: 2, amount: '70.00', percentage: 70 },
    ]);
  });

  it('omits percentage for non-percentage split types', () => {
    const payload = buildGroupExpensePayload(base, 'shares', 90, [1, 2], [1, 2], {
      1: '1',
      2: '2',
    });

    expect(payload.splits?.every((split) => !('percentage' in split))).toBe(true);
  });

  it('produces splits that sum to the expense total', () => {
    const payload = buildGroupExpensePayload(base, 'equal', 10, [1, 2, 3], [1, 2, 3, 4], {});

    expect(sumOf(payload.splits ?? [])).toBeCloseTo(10, 10);
  });
});
