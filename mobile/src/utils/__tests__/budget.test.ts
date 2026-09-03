/**
 * Characterization: budget display helpers.
 *
 * These decide how a budget's progress reads. The thresholds intentionally
 * mirror the server's own bucketing so card badges agree with the overview
 * counts, and the date parsing deliberately stays in local time so a budget
 * doesn't appear to end a day early in negative-UTC zones.
 */

import {
  BUDGET_STATUS_LABEL,
  getBudgetDaysRemaining,
  getBudgetEndDate,
  getBudgetSpentWindowEnd,
  getBudgetStatusLevel,
} from '@/utils/budget';

describe('getBudgetStatusLevel', () => {
  it.each([
    [0, 'safe'],
    [49.9, 'safe'],
    [50, 'warning'],
    [79.9, 'warning'],
    [80, 'critical'],
    [99.9, 'critical'],
    [100, 'exceeded'],
    [250, 'exceeded'],
  ])('maps %s%% used to %s', (percentage, expected) => {
    expect(getBudgetStatusLevel(percentage)).toBe(expected);
  });

  it('has a label for every level', () => {
    for (const level of ['safe', 'warning', 'critical', 'exceeded'] as const) {
      expect(BUDGET_STATUS_LABEL[level]).toBeTruthy();
    }
  });
});

describe('getBudgetEndDate', () => {
  it('prefers an explicit end date', () => {
    const end = getBudgetEndDate({
      start_date: '2024-01-01',
      end_date: '2024-01-20',
      period: 'monthly',
    });

    expect(end.getFullYear()).toBe(2024);
    expect(end.getMonth()).toBe(0);
    expect(end.getDate()).toBe(20);
  });

  it('ends a daily budget on its start day', () => {
    const end = getBudgetEndDate({ start_date: '2024-03-10', end_date: null, period: 'daily' });

    expect(end.getDate()).toBe(10);
    expect(end.getMonth()).toBe(2);
  });

  it('ends a weekly budget six days after the start', () => {
    const end = getBudgetEndDate({ start_date: '2024-03-10', end_date: null, period: 'weekly' });

    expect(end.getDate()).toBe(16);
  });

  it('ends a monthly budget on the last day of the month', () => {
    const end = getBudgetEndDate({ start_date: '2024-02-01', end_date: null, period: 'monthly' });

    expect(end.getMonth()).toBe(1);
    expect(end.getDate()).toBe(29); // 2024 is a leap year
  });

  it('ends a quarterly budget the day before the same date three months on', () => {
    const end = getBudgetEndDate({ start_date: '2024-01-01', end_date: null, period: 'quarterly' });

    expect(end.getMonth()).toBe(2);
    expect(end.getDate()).toBe(31);
  });

  it('ends a yearly budget the day before the anniversary', () => {
    const end = getBudgetEndDate({ start_date: '2024-01-01', end_date: null, period: 'yearly' });

    expect(end.getFullYear()).toBe(2024);
    expect(end.getMonth()).toBe(11);
    expect(end.getDate()).toBe(31);
  });

  it('parses dates in local time rather than shifting a day', () => {
    const end = getBudgetEndDate({ start_date: '2024-06-15', end_date: '2024-06-15', period: 'daily' });

    expect(end.getDate()).toBe(15);
    expect(end.getHours()).toBe(0);
  });
});

describe('getBudgetSpentWindowEnd', () => {
  it('uses the explicit end date when there is one', () => {
    const end = getBudgetSpentWindowEnd({ end_date: '2024-01-20' });

    expect(end.getDate()).toBe(20);
  });

  it('falls back to today for an open-ended budget', () => {
    // Matches the server, which sums expenses through `end_date or today`.
    const end = getBudgetSpentWindowEnd({ end_date: null });
    const today = new Date();

    expect(end.toDateString()).toBe(today.toDateString());
  });
});

describe('getBudgetDaysRemaining', () => {
  function isoDaysFromToday(offset: number): string {
    const date = new Date();
    date.setHours(0, 0, 0, 0);
    date.setDate(date.getDate() + offset);
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(
      date.getDate()
    ).padStart(2, '0')}`;
  }

  it('counts whole days until the end date', () => {
    const remaining = getBudgetDaysRemaining({
      start_date: isoDaysFromToday(-1),
      end_date: isoDaysFromToday(5),
      period: 'monthly',
    });

    expect(remaining).toBe(5);
  });

  it('reports zero on the final day', () => {
    const remaining = getBudgetDaysRemaining({
      start_date: isoDaysFromToday(-3),
      end_date: isoDaysFromToday(0),
      period: 'monthly',
    });

    expect(remaining).toBe(0);
  });

  it('returns null once the period has ended', () => {
    const remaining = getBudgetDaysRemaining({
      start_date: isoDaysFromToday(-10),
      end_date: isoDaysFromToday(-1),
      period: 'monthly',
    });

    expect(remaining).toBeNull();
  });
});
