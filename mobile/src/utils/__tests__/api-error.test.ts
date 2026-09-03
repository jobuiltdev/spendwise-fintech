/**
 * Characterization: API error message extraction.
 *
 * This decides what the user is told when a request fails. The distinction that
 * matters most is between "the server rejected this" and "the request never
 * arrived" — telling someone their password is wrong when the network dropped
 * is a lie the fintech copy rules explicitly forbid.
 */

import { getApiErrorMessage } from '@/utils/api-error';

const FALLBACK = 'Something went wrong.';
const OFFLINE = "Can't reach the server. Check your connection and try again.";

describe('getApiErrorMessage', () => {
  describe('when no response arrived', () => {
    it.each([
      ['a bare error', new Error('Network Error')],
      ['an undefined error', undefined],
      ['a null error', null],
      ['an error with no response field', { message: 'boom' }],
    ])('reports a connectivity problem for %s', (_label, error) => {
      expect(getApiErrorMessage(error, FALLBACK)).toBe(OFFLINE);
    });

    it('does not blame the user input when the request never got evaluated', () => {
      expect(getApiErrorMessage(new Error('timeout'), 'Current password is incorrect')).toBe(
        OFFLINE
      );
    });
  });

  describe('when the server responded', () => {
    it('prefers the custom-action "error" key', () => {
      const error = { response: { data: { error: 'Group has no members' } } };

      expect(getApiErrorMessage(error, FALLBACK)).toBe('Group has no members');
    });

    it('uses DRF\'s framework-level "detail" key', () => {
      const error = { response: { data: { detail: 'Not found.' } } };

      expect(getApiErrorMessage(error, FALLBACK)).toBe('Not found.');
    });

    it('prefers "error" over "detail" when both are present', () => {
      const error = { response: { data: { error: 'first', detail: 'second' } } };

      expect(getApiErrorMessage(error, FALLBACK)).toBe('first');
    });

    it('reads the first message out of a per-field error list', () => {
      const error = {
        response: { data: { current_password: ['Current password is incorrect'] } },
      };

      expect(getApiErrorMessage(error, FALLBACK)).toBe('Current password is incorrect');
    });

    it('reads a per-field error given as a plain string', () => {
      const error = { response: { data: { avatar: 'No image was provided' } } };

      expect(getApiErrorMessage(error, FALLBACK)).toBe('No image was provided');
    });

    it('falls back when the body is empty', () => {
      expect(getApiErrorMessage({ response: { data: {} } }, FALLBACK)).toBe(FALLBACK);
    });

    it('falls back when the body is not an object', () => {
      expect(getApiErrorMessage({ response: { data: 'plain text' } }, FALLBACK)).toBe(FALLBACK);
    });

    it('falls back when a field error list is empty', () => {
      expect(getApiErrorMessage({ response: { data: { amount: [] } } }, FALLBACK)).toBe(FALLBACK);
    });

    it('falls back when a field value is neither string nor string list', () => {
      expect(getApiErrorMessage({ response: { data: { amount: 42 } } }, FALLBACK)).toBe(FALLBACK);
    });
  });
});
