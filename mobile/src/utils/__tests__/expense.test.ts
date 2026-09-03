/**
 * Characterization: category breakdown mapping.
 *
 * Translates the backend's raw ORM `.values()` shape into the view model the
 * charts consume. Worth protecting because it is the one place that decides how
 * uncategorised spending is labelled and how percentages are derived.
 */

import { mapCategoryBreakdown } from '@/utils/expense';
import type { ExpenseSummaryResponse } from '@/types/expense';

function summary(overrides: Partial<ExpenseSummaryResponse> = {}): ExpenseSummaryResponse {
  return {
    total_amount: '100.00',
    expense_count: 3,
    average_amount: '33.33',
    category_breakdown: [],
    daily_totals: [],
    ...overrides,
  };
}

describe('mapCategoryBreakdown', () => {
  it('converts amounts to numbers and derives a percentage of the total', () => {
    const rows = mapCategoryBreakdown(
      summary({
        total_amount: '200.00',
        category_breakdown: [
          { category__id: 1, category__name: 'Food', category__color: '#111', total: '50.00', count: 2 },
        ],
      })
    );

    expect(rows).toEqual([
      { id: 1, name: 'Food', color: '#111', total: 50, count: 2, percentage: 25 },
    ]);
  });

  it('sorts by spend, largest first', () => {
    const rows = mapCategoryBreakdown(
      summary({
        total_amount: '100.00',
        category_breakdown: [
          { category__id: 1, category__name: 'Food', category__color: '#111', total: '20.00', count: 1 },
          { category__id: 2, category__name: 'Rent', category__color: '#222', total: '70.00', count: 1 },
          { category__id: 3, category__name: 'Fun', category__color: '#333', total: '10.00', count: 1 },
        ],
      })
    );

    expect(rows.map((row) => row.name)).toEqual(['Rent', 'Food', 'Fun']);
  });

  it('labels uncategorised spending rather than dropping it', () => {
    const rows = mapCategoryBreakdown(
      summary({
        category_breakdown: [
          { category__id: null, category__name: null, category__color: null, total: '40.00', count: 1 },
        ],
      })
    );

    expect(rows[0].id).toBeNull();
    expect(rows[0].name).toBe('Uncategorized');
    expect(rows[0].color).toBe('#9AA0A6');
  });

  it('reports zero percent rather than NaN when nothing was spent', () => {
    const rows = mapCategoryBreakdown(
      summary({
        total_amount: '0.00',
        category_breakdown: [
          { category__id: 1, category__name: 'Food', category__color: '#111', total: '0.00', count: 0 },
        ],
      })
    );

    expect(rows[0].percentage).toBe(0);
  });

  it('returns an empty list for an account with no spending', () => {
    expect(mapCategoryBreakdown(summary())).toEqual([]);
  });

  it('produces percentages that add up to the whole', () => {
    const rows = mapCategoryBreakdown(
      summary({
        total_amount: '100.00',
        category_breakdown: [
          { category__id: 1, category__name: 'A', category__color: '#111', total: '60.00', count: 1 },
          { category__id: 2, category__name: 'B', category__color: '#222', total: '40.00', count: 1 },
        ],
      })
    );

    expect(rows.reduce((sum, row) => sum + row.percentage, 0)).toBeCloseTo(100, 10);
  });
});
