/**
 * Characterization: form validation schemas.
 *
 * These are the client-side gate on what reaches the API. The rules worth
 * protecting are the money-shaped ones — an amount must be positive and no finer
 * than two decimal places — and the date-range consistency checks, since the
 * backend's create path does not always repeat them.
 */

import { budgetFormSchema } from '@/types/budget';
import { dateSchema, expenseFormSchema } from '@/types/expense';
import { groupFormSchema, joinGroupSchema } from '@/types/group';
import { recurringFormSchema } from '@/types/recurring';

const validExpense = {
  amount: '25.50',
  description: 'Lunch',
  date: '2024-01-15',
  category: 1,
  payment_method: null,
  payment_status: 'completed' as const,
};

const validBudget = {
  category: 1,
  amount: '100.00',
  period: 'monthly' as const,
  start_date: '2024-01-01',
  end_date: '',
  alert_threshold: '80',
};

const validRecurring = {
  description: 'Gym',
  amount: '50.00',
  category: 1,
  payment_method: null,
  frequency: 'monthly' as const,
  interval: '1',
  start_date: '2024-01-01',
  end_date: '',
};

describe('dateSchema', () => {
  it('accepts an ISO calendar date', () => {
    expect(dateSchema.safeParse('2024-01-15').success).toBe(true);
  });

  it.each(['15-01-2024', '2024/01/15', '2024-1-5', 'today', ''])(
    'rejects %s',
    (value) => {
      expect(dateSchema.safeParse(value).success).toBe(false);
    }
  );
});

describe('expenseFormSchema', () => {
  it('accepts a well-formed expense', () => {
    expect(expenseFormSchema.safeParse(validExpense).success).toBe(true);
  });

  it.each(['0', '0.00', '-5.00'])('rejects a non-positive amount %s', (amount) => {
    expect(expenseFormSchema.safeParse({ ...validExpense, amount }).success).toBe(false);
  });

  it('rejects an amount with more than two decimal places', () => {
    expect(expenseFormSchema.safeParse({ ...validExpense, amount: '10.005' }).success).toBe(false);
  });

  it('accepts a whole-number amount', () => {
    expect(expenseFormSchema.safeParse({ ...validExpense, amount: '10' }).success).toBe(true);
  });

  it.each(['abc', '10,50', '1e3', ''])('rejects a non-numeric amount %s', (amount) => {
    expect(expenseFormSchema.safeParse({ ...validExpense, amount }).success).toBe(false);
  });

  it('requires a description', () => {
    expect(expenseFormSchema.safeParse({ ...validExpense, description: '' }).success).toBe(false);
  });

  it('caps the description length', () => {
    const result = expenseFormSchema.safeParse({
      ...validExpense,
      description: 'x'.repeat(256),
    });

    expect(result.success).toBe(false);
  });

  it('allows an uncategorised expense', () => {
    expect(expenseFormSchema.safeParse({ ...validExpense, category: null }).success).toBe(true);
  });

  it.each(['pending', 'completed', 'cancelled'] as const)(
    'accepts the %s payment status',
    (payment_status) => {
      expect(expenseFormSchema.safeParse({ ...validExpense, payment_status }).success).toBe(true);
    }
  );

  it('rejects an unknown payment status', () => {
    expect(
      expenseFormSchema.safeParse({ ...validExpense, payment_status: 'settled' }).success
    ).toBe(false);
  });
});

describe('budgetFormSchema', () => {
  it('accepts a well-formed budget', () => {
    expect(budgetFormSchema.safeParse(validBudget).success).toBe(true);
  });

  it('requires a category', () => {
    expect(budgetFormSchema.safeParse({ ...validBudget, category: 0 }).success).toBe(false);
  });

  it('rejects a non-positive amount', () => {
    expect(budgetFormSchema.safeParse({ ...validBudget, amount: '0' }).success).toBe(false);
  });

  it('accepts an open-ended budget', () => {
    expect(budgetFormSchema.safeParse({ ...validBudget, end_date: '' }).success).toBe(true);
  });

  it('accepts an end date after the start', () => {
    const result = budgetFormSchema.safeParse({
      ...validBudget,
      start_date: '2024-01-01',
      end_date: '2024-02-01',
    });

    expect(result.success).toBe(true);
  });

  it('rejects an end date before the start', () => {
    const result = budgetFormSchema.safeParse({
      ...validBudget,
      start_date: '2024-02-01',
      end_date: '2024-01-01',
    });

    expect(result.success).toBe(false);
  });

  it('rejects an end date equal to the start', () => {
    const result = budgetFormSchema.safeParse({
      ...validBudget,
      start_date: '2024-01-01',
      end_date: '2024-01-01',
    });

    expect(result.success).toBe(false);
  });

  it.each(['0', '101', '-1'])('rejects an out-of-range alert threshold %s', (alert_threshold) => {
    expect(budgetFormSchema.safeParse({ ...validBudget, alert_threshold }).success).toBe(false);
  });

  it.each(['1', '80', '100'])('accepts an in-range alert threshold %s', (alert_threshold) => {
    expect(budgetFormSchema.safeParse({ ...validBudget, alert_threshold }).success).toBe(true);
  });

  it('rejects a fractional alert threshold', () => {
    expect(budgetFormSchema.safeParse({ ...validBudget, alert_threshold: '80.5' }).success).toBe(
      false
    );
  });
});

describe('recurringFormSchema', () => {
  it('accepts a well-formed schedule', () => {
    expect(recurringFormSchema.safeParse(validRecurring).success).toBe(true);
  });

  it('requires an interval of at least one', () => {
    expect(recurringFormSchema.safeParse({ ...validRecurring, interval: '0' }).success).toBe(false);
  });

  it('rejects a fractional interval', () => {
    expect(recurringFormSchema.safeParse({ ...validRecurring, interval: '1.5' }).success).toBe(
      false
    );
  });

  it('requires a category', () => {
    expect(recurringFormSchema.safeParse({ ...validRecurring, category: 0 }).success).toBe(false);
  });

  it('allows no payment method', () => {
    expect(recurringFormSchema.safeParse({ ...validRecurring, payment_method: null }).success).toBe(
      true
    );
  });

  it('rejects an end date before the start', () => {
    const result = recurringFormSchema.safeParse({
      ...validRecurring,
      start_date: '2024-02-01',
      end_date: '2024-01-01',
    });

    expect(result.success).toBe(false);
  });

  it.each(['daily', 'weekly', 'biweekly', 'monthly', 'quarterly', 'yearly', 'custom'] as const)(
    'accepts the %s frequency',
    (frequency) => {
      expect(recurringFormSchema.safeParse({ ...validRecurring, frequency }).success).toBe(true);
    }
  );
});

describe('groupFormSchema', () => {
  it('accepts a named group', () => {
    expect(groupFormSchema.safeParse({ name: 'Flatmates' }).success).toBe(true);
  });

  it('requires a name', () => {
    expect(groupFormSchema.safeParse({ name: '' }).success).toBe(false);
  });

  it('caps the name length', () => {
    expect(groupFormSchema.safeParse({ name: 'x'.repeat(101) }).success).toBe(false);
  });

  it('caps the description length', () => {
    const result = groupFormSchema.safeParse({ name: 'Trip', description: 'x'.repeat(501) });

    expect(result.success).toBe(false);
  });
});

describe('joinGroupSchema', () => {
  it('normalises an invite code to trimmed uppercase', () => {
    const result = joinGroupSchema.parse({ invite_code: '  ab12cd34  ' });

    expect(result.invite_code).toBe('AB12CD34');
  });

  it('requires a code', () => {
    expect(joinGroupSchema.safeParse({ invite_code: '' }).success).toBe(false);
  });
});
