/**
 * Characterization: group member display helpers.
 *
 * `computeNetBalance` deliberately trusts the server's `balance` rather than
 * recomputing from paid/owes — recomputing would silently ignore recorded
 * settlements, which is exactly the kind of client-side financial arithmetic the
 * fintech rules rule out.
 */

import { computeNetBalance, getInitials, memberDisplayName } from '@/utils/group';
import type { GroupMembership } from '@/types/group';

function membership(overrides: Partial<GroupMembership> = {}): GroupMembership {
  return {
    id: 1,
    user: 1,
    user_details: {
      id: 1,
      username: 'alice',
      email: 'alice@example.com',
      first_name: '',
      last_name: '',
      date_joined: '2024-01-01T00:00:00Z',
    },
    username: 'alice',
    role: 'member',
    nickname: '',
    balance: '0.00',
    joined_at: '2024-01-01T00:00:00Z',
    ...overrides,
  };
}

describe('computeNetBalance', () => {
  it('reads the server-authoritative balance', () => {
    expect(computeNetBalance({ balance: '42.50' })).toBe(42.5);
  });

  it('preserves a negative balance', () => {
    expect(computeNetBalance({ balance: '-17.25' })).toBe(-17.25);
  });

  it('reads a settled balance as zero', () => {
    expect(computeNetBalance({ balance: '0.00' })).toBe(0);
  });
});

describe('memberDisplayName', () => {
  it('prefers the group nickname', () => {
    const member = membership({
      nickname: 'Al',
      user_details: { ...membership().user_details, first_name: 'Alice', last_name: 'Adams' },
    });

    expect(memberDisplayName(member)).toBe('Al');
  });

  it('falls back to the full name', () => {
    const member = membership({
      user_details: { ...membership().user_details, first_name: 'Alice', last_name: 'Adams' },
    });

    expect(memberDisplayName(member)).toBe('Alice Adams');
  });

  it('uses just the first name when there is no surname', () => {
    const member = membership({
      user_details: { ...membership().user_details, first_name: 'Alice' },
    });

    expect(memberDisplayName(member)).toBe('Alice');
  });

  it('falls back to the username when no name is set', () => {
    expect(memberDisplayName(membership())).toBe('alice');
  });
});

describe('getInitials', () => {
  it('takes the first and last initials of a full name', () => {
    expect(getInitials('Alice Adams')).toBe('AA');
  });

  it('uses the first two letters of a single name', () => {
    expect(getInitials('Alice')).toBe('AL');
  });

  it('skips middle names', () => {
    expect(getInitials('Alice Beatrice Adams')).toBe('AA');
  });

  it('ignores surrounding and repeated whitespace', () => {
    expect(getInitials('  Alice   Adams  ')).toBe('AA');
  });

  it('falls back to a placeholder for an empty name', () => {
    expect(getInitials('')).toBe('?');
    expect(getInitials('   ')).toBe('?');
  });
});
