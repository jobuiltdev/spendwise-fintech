/**
 * Characterization: recurring display helpers.
 *
 * Recurring V1 is planning and intelligence only. What is pinned here is the
 * cadence arithmetic and due-date urgency — the "when do I expect this" logic
 * the planning experience is built on.
 *
 * Deliberately not characterized: any copy implying automated debiting. The
 * fintech rules forbid "Pay automatically", "Payment method", "Pause payment"
 * and "Next debit" framing, so none of that is locked in here.
 */

import { daysUntil, getDueUrgency, getIntervalLabel, RECURRING_STATUS_LABEL } from '@/utils/recurring';

function isoDaysFromToday(offset: number): string {
  const date = new Date();
  date.setHours(0, 0, 0, 0);
  date.setDate(date.getDate() + offset);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(
    date.getDate()
  ).padStart(2, '0')}`;
}

describe('getIntervalLabel', () => {
  it.each(['daily', 'weekly', 'biweekly', 'monthly', 'quarterly', 'yearly'] as const)(
    'uses the plain frequency label for %s at interval 1',
    (frequency) => {
      expect(getIntervalLabel(frequency, 1)).not.toContain('Every');
    }
  );

  it('treats interval 0 as a plain frequency rather than "Every 0"', () => {
    expect(getIntervalLabel('weekly', 0)).not.toContain('Every');
  });

  it.each([
    ['daily', 3, 'Every 3 days'],
    ['weekly', 2, 'Every 2 weeks'],
    ['biweekly', 2, 'Every 2 fortnights'],
    ['monthly', 6, 'Every 6 months'],
    ['quarterly', 2, 'Every 2 quarters'],
    ['yearly', 5, 'Every 5 years'],
  ] as const)('describes %s at interval %s', (frequency, interval, expected) => {
    expect(getIntervalLabel(frequency, interval)).toBe(expected);
  });
});

describe('daysUntil', () => {
  it('counts forward to a future date', () => {
    expect(daysUntil(isoDaysFromToday(7))).toBe(7);
  });

  it('returns zero for today', () => {
    expect(daysUntil(isoDaysFromToday(0))).toBe(0);
  });

  it('returns a negative count for a past date', () => {
    expect(daysUntil(isoDaysFromToday(-4))).toBe(-4);
  });

  it('ignores the time of day on the reference date', () => {
    const reference = new Date();
    reference.setHours(23, 59, 0, 0);

    expect(daysUntil(isoDaysFromToday(1), reference)).toBe(1);
  });
});

describe('getDueUrgency', () => {
  it('flags a past date as overdue', () => {
    expect(getDueUrgency(isoDaysFromToday(-1))).toBe('overdue');
  });

  it('flags today as due soon', () => {
    expect(getDueUrgency(isoDaysFromToday(0))).toBe('due-soon');
  });

  it('flags the edge of the three-day window as due soon', () => {
    expect(getDueUrgency(isoDaysFromToday(3))).toBe('due-soon');
  });

  it('flags anything beyond the window as upcoming', () => {
    expect(getDueUrgency(isoDaysFromToday(4))).toBe('upcoming');
  });
});

describe('RECURRING_STATUS_LABEL', () => {
  it('covers every lifecycle status', () => {
    expect(RECURRING_STATUS_LABEL).toEqual({
      active: 'Active',
      paused: 'Paused',
      completed: 'Completed',
    });
  });
});
