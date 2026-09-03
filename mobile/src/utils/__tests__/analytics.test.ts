/**
 * Characterization: trend-window maths.
 *
 * These build the date buckets the spending charts are drawn from. The maths is
 * deterministic and reusable, so it is worth protecting.
 *
 * Deliberately not characterized: GRADE_COLOR_KEY and anything else that exists
 * to render the numeric Financial Health score, which is being removed from the
 * customer experience.
 */

import {
  buildTrendWindows,
  getAverageDailySpending,
  getHighestBucket,
  getLowestBucket,
  getTrendDirection,
  SEVERITY_LABEL,
  type TrendBucket,
} from '@/utils/analytics';

const bucket = (key: string, total: number): TrendBucket => ({
  key,
  label: key,
  total,
  start: new Date(2024, 0, 1),
  end: new Date(2024, 0, 31),
});

describe('buildTrendWindows', () => {
  it.each([
    ['weekly', 8],
    ['monthly', 6],
    ['yearly', 4],
  ] as const)('produces %s buckets for %s granularity', (granularity, count) => {
    expect(buildTrendWindows(granularity)).toHaveLength(count);
  });

  it('returns windows in chronological order, oldest first', () => {
    const windows = buildTrendWindows('monthly');

    for (let i = 1; i < windows.length; i++) {
      expect(windows[i].start.getTime()).toBeGreaterThan(windows[i - 1].start.getTime());
    }
  });

  it('gives every window a distinct key', () => {
    const windows = buildTrendWindows('weekly');

    expect(new Set(windows.map((w) => w.key)).size).toBe(windows.length);
  });

  it('never ends a window before it starts', () => {
    for (const granularity of ['weekly', 'monthly', 'yearly'] as const) {
      for (const window of buildTrendWindows(granularity)) {
        expect(window.end.getTime()).toBeGreaterThanOrEqual(window.start.getTime());
      }
    }
  });

  it('builds weekly windows that are exactly seven days long', () => {
    for (const window of buildTrendWindows('weekly')) {
      const days = Math.round((window.end.getTime() - window.start.getTime()) / 86_400_000);
      expect(days).toBe(6);
    }
  });

  it('starts weekly windows on a Monday', () => {
    for (const window of buildTrendWindows('weekly')) {
      expect(window.start.getDay()).toBe(1);
    }
  });

  it('builds monthly windows that span whole calendar months', () => {
    for (const window of buildTrendWindows('monthly')) {
      expect(window.start.getDate()).toBe(1);
      // The last day of the month: adding a day rolls into the next month.
      const dayAfter = new Date(window.end);
      dayAfter.setDate(dayAfter.getDate() + 1);
      expect(dayAfter.getDate()).toBe(1);
    }
  });

  it('covers six distinct months without skipping a short one', () => {
    const months = buildTrendWindows('monthly').map((w) => w.start.getMonth());

    expect(new Set(months).size).toBe(6);
  });

  it('ends the current period on or after today', () => {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const windows = buildTrendWindows('monthly');

    expect(windows[windows.length - 1].end.getTime()).toBeGreaterThanOrEqual(today.getTime());
  });
});

describe('getHighestBucket', () => {
  it('finds the largest total', () => {
    expect(getHighestBucket([bucket('a', 10), bucket('b', 30), bucket('c', 20)])?.key).toBe('b');
  });

  it('returns null for an empty series', () => {
    expect(getHighestBucket([])).toBeNull();
  });

  it('keeps the first of equal maxima', () => {
    expect(getHighestBucket([bucket('a', 10), bucket('b', 10)])?.key).toBe('a');
  });
});

describe('getLowestBucket', () => {
  it('finds the smallest non-zero total', () => {
    expect(getLowestBucket([bucket('a', 10), bucket('b', 30)])?.key).toBe('a');
  });

  it('ignores zero-spend periods, which are not a meaningful low', () => {
    expect(getLowestBucket([bucket('a', 0), bucket('b', 30)])?.key).toBe('b');
  });

  it('returns null when nothing was spent at all', () => {
    expect(getLowestBucket([bucket('a', 0), bucket('b', 0)])).toBeNull();
  });

  it('returns null for an empty series', () => {
    expect(getLowestBucket([])).toBeNull();
  });
});

describe('getAverageDailySpending', () => {
  it('divides the total across the days', () => {
    expect(getAverageDailySpending(300, 30)).toBe(10);
  });

  it('returns zero rather than dividing by zero', () => {
    expect(getAverageDailySpending(300, 0)).toBe(0);
  });

  it('returns zero for a negative day count', () => {
    expect(getAverageDailySpending(300, -5)).toBe(0);
  });
});

describe('getTrendDirection', () => {
  it('reports a rise above the five percent band', () => {
    expect(getTrendDirection([bucket('a', 100), bucket('b', 120)])).toBe('up');
  });

  it('reports a fall below the five percent band', () => {
    expect(getTrendDirection([bucket('a', 100), bucket('b', 80)])).toBe('down');
  });

  it('treats a small change as stable', () => {
    expect(getTrendDirection([bucket('a', 100), bucket('b', 103)])).toBe('stable');
  });

  it('needs at least two buckets to have a direction', () => {
    expect(getTrendDirection([bucket('a', 100)])).toBe('stable');
    expect(getTrendDirection([])).toBe('stable');
  });

  it('treats spending after a zero period as a rise', () => {
    expect(getTrendDirection([bucket('a', 0), bucket('b', 50)])).toBe('up');
  });

  it('treats two empty periods as stable rather than dividing by zero', () => {
    expect(getTrendDirection([bucket('a', 0), bucket('b', 0)])).toBe('stable');
  });

  it('compares only the two most recent buckets', () => {
    expect(getTrendDirection([bucket('a', 1000), bucket('b', 100), bucket('c', 120)])).toBe('up');
  });
});

describe('SEVERITY_LABEL', () => {
  it('covers every anomaly severity', () => {
    expect(SEVERITY_LABEL).toEqual({
      low: 'Low',
      medium: 'Medium',
      high: 'High',
      critical: 'Critical',
    });
  });
});
