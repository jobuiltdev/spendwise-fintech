/**
 * Characterization: the API client's auth and refresh behaviour.
 *
 * This is the most safety-critical reusable infrastructure in the app: refresh
 * tokens rotate on every use, so a second concurrent refresh would redeem — and
 * thereby invalidate — a token the first one is still relying on. The guarantees
 * pinned here are that concurrent 401s share exactly one refresh, that the
 * original request is replayed once with the new token, and that a failed
 * refresh clears the session rather than leaving it half-alive.
 *
 * The module keeps state at module scope (the in-memory access token and the
 * in-flight refresh promise), so each test re-imports it fresh.
 */

import axios, { AxiosError, type AxiosResponse, type InternalAxiosRequestConfig } from 'axios';

import { api, setAccessToken, setSessionExpiredHandler } from '@/services/api';
import * as tokenStorage from '@/services/token-storage';

jest.mock('@/services/token-storage', () => ({
  getStoredTokens: jest.fn(),
  setStoredTokens: jest.fn(),
  clearStoredTokens: jest.fn(),
}));

// Deterministic base URL: the real module derives one from expo-constants.
jest.mock('@/constants/config', () => ({
  API_BASE_URL: 'http://testserver/api',
  GOOGLE_CLIENT_ID: '',
  GOOGLE_RELAY_URL: '',
}));

const FRESH_TOKEN = 'fresh-access-token';

function unauthorized(config: InternalAxiosRequestConfig): AxiosError {
  const response = {
    data: { detail: 'Token is invalid or expired' },
    status: 401,
    statusText: 'Unauthorized',
    headers: {},
    config,
  } as AxiosResponse;
  return new AxiosError('Unauthorized', 'ERR_BAD_REQUEST', config, {}, response);
}

function ok(config: InternalAxiosRequestConfig, data: unknown = { ok: true }): AxiosResponse {
  return { data, status: 200, statusText: 'OK', headers: {}, config } as AxiosResponse;
}

function authHeaderOf(config: InternalAxiosRequestConfig): string | undefined {
  const value = config.headers?.get?.('Authorization') ?? config.headers?.Authorization;
  return value == null ? undefined : String(value);
}

/**
 * The module's own setters are the supported way to reset its state, so tests
 * drive them rather than reaching for a fresh module registry. The in-flight
 * refresh promise clears itself in a `finally`, so nothing leaks between tests.
 */
const postSpy = jest.spyOn(axios, 'post');
const originalAdapter = api.defaults.adapter;

describe('api client', () => {
  let loaded: {
    api: typeof api;
    setAccessToken: typeof setAccessToken;
    setSessionExpiredHandler: typeof setSessionExpiredHandler;
    tokenStorage: typeof tokenStorage;
    postSpy: typeof postSpy;
  };

  beforeEach(() => {
    // reset, not clear: implementations must not leak between tests (one test
    // installs a deliberately un-settling refresh promise).
    jest.resetAllMocks();
    setAccessToken(null);
    setSessionExpiredHandler(null);
    api.defaults.adapter = originalAdapter;

    loaded = { api, setAccessToken, setSessionExpiredHandler, tokenStorage, postSpy };

    (tokenStorage.getStoredTokens as jest.Mock).mockResolvedValue({
      access: 'stale-access-token',
      refresh: 'stored-refresh-token',
    });
    (tokenStorage.setStoredTokens as jest.Mock).mockResolvedValue(undefined);
    (tokenStorage.clearStoredTokens as jest.Mock).mockResolvedValue(undefined);
  });

  afterEach(() => {
    setAccessToken(null);
    setSessionExpiredHandler(null);
    api.defaults.adapter = originalAdapter;
  });

  describe('request authorization', () => {
    it('sends no Authorization header before a token is set', async () => {
      const adapter = jest.fn(async (config: InternalAxiosRequestConfig) => ok(config));
      loaded.api.defaults.adapter = adapter;

      await loaded.api.get('/expenses/');

      expect(authHeaderOf(adapter.mock.calls[0][0])).toBeUndefined();
    });

    it('attaches the in-memory access token to every request', async () => {
      const adapter = jest.fn(async (config: InternalAxiosRequestConfig) => ok(config));
      loaded.api.defaults.adapter = adapter;
      loaded.setAccessToken('in-memory-token');

      await loaded.api.get('/expenses/');

      expect(authHeaderOf(adapter.mock.calls[0][0])).toBe('Bearer in-memory-token');
    });

    it('stops sending the header once the token is cleared', async () => {
      const adapter = jest.fn(async (config: InternalAxiosRequestConfig) => ok(config));
      loaded.api.defaults.adapter = adapter;
      loaded.setAccessToken('in-memory-token');
      loaded.setAccessToken(null);

      await loaded.api.get('/expenses/');

      expect(authHeaderOf(adapter.mock.calls[0][0])).toBeUndefined();
    });
  });

  describe('401 handling', () => {
    /** 401s until the refreshed token shows up, then succeeds. */
    function adapterThatAcceptsOnly(token: string) {
      return jest.fn(async (config: InternalAxiosRequestConfig) => {
        if (authHeaderOf(config) === `Bearer ${token}`) return ok(config);
        throw unauthorized(config);
      });
    }

    it('refreshes and replays the original request', async () => {
      loaded.api.defaults.adapter = adapterThatAcceptsOnly(FRESH_TOKEN);
      loaded.postSpy.mockResolvedValue({
        data: { access: FRESH_TOKEN, refresh: 'rotated-refresh-token' },
      } as never);
      loaded.setAccessToken('stale-access-token');

      const response = await loaded.api.get('/expenses/');

      expect(response.status).toBe(200);
      expect(loaded.postSpy).toHaveBeenCalledTimes(1);
      expect(loaded.postSpy).toHaveBeenCalledWith('http://testserver/api/token/refresh/', {
        refresh: 'stored-refresh-token',
      });
    });

    it('persists the rotated refresh token, not just the new access token', async () => {
      loaded.api.defaults.adapter = adapterThatAcceptsOnly(FRESH_TOKEN);
      loaded.postSpy.mockResolvedValue({
        data: { access: FRESH_TOKEN, refresh: 'rotated-refresh-token' },
      } as never);
      loaded.setAccessToken('stale-access-token');

      await loaded.api.get('/expenses/');

      expect(loaded.tokenStorage.setStoredTokens).toHaveBeenCalledWith({
        access: FRESH_TOKEN,
        refresh: 'rotated-refresh-token',
      });
    });

    it('keeps the previous refresh token when the server returns none', async () => {
      loaded.api.defaults.adapter = adapterThatAcceptsOnly(FRESH_TOKEN);
      loaded.postSpy.mockResolvedValue({ data: { access: FRESH_TOKEN } } as never);
      loaded.setAccessToken('stale-access-token');

      await loaded.api.get('/expenses/');

      expect(loaded.tokenStorage.setStoredTokens).toHaveBeenCalledWith({
        access: FRESH_TOKEN,
        refresh: 'stored-refresh-token',
      });
    });

    it('shares one refresh across concurrent 401s', async () => {
      loaded.api.defaults.adapter = adapterThatAcceptsOnly(FRESH_TOKEN);
      // A refresh that takes a moment, so all three 401s are in the
      // interceptor while it is still in flight.
      loaded.postSpy.mockImplementation(async () => {
        await new Promise((resolve) => setTimeout(resolve, 20));
        return { data: { access: FRESH_TOKEN, refresh: 'rotated-refresh-token' } } as never;
      });
      loaded.setAccessToken('stale-access-token');

      const responses = await Promise.all([
        loaded.api.get('/expenses/'),
        loaded.api.get('/budgets/'),
        loaded.api.get('/categories/'),
      ]);

      expect(responses.every((r) => r.status === 200)).toBe(true);
      // Rotation makes a second redemption fatal — exactly one refresh call.
      expect(loaded.postSpy).toHaveBeenCalledTimes(1);
    });

    it('retries a request only once', async () => {
      // Always 401, even after refreshing: the retry must not loop.
      const adapter = jest.fn(async (config: InternalAxiosRequestConfig) => {
        throw unauthorized(config);
      });
      loaded.api.defaults.adapter = adapter;
      loaded.postSpy.mockResolvedValue({
        data: { access: FRESH_TOKEN, refresh: 'rotated-refresh-token' },
      } as never);
      loaded.setAccessToken('stale-access-token');

      await expect(loaded.api.get('/expenses/')).rejects.toBeInstanceOf(AxiosError);

      expect(adapter).toHaveBeenCalledTimes(2);
      expect(loaded.postSpy).toHaveBeenCalledTimes(1);
    });

    it('does not try to refresh when there is no stored refresh token', async () => {
      (loaded.tokenStorage.getStoredTokens as jest.Mock).mockResolvedValue(null);
      loaded.api.defaults.adapter = jest.fn(async (config: InternalAxiosRequestConfig) => {
        throw unauthorized(config);
      });
      loaded.setAccessToken('stale-access-token');

      await expect(loaded.api.get('/expenses/')).rejects.toBeTruthy();

      expect(loaded.postSpy).not.toHaveBeenCalled();
    });
  });

  describe('session expiry', () => {
    function failingRefresh() {
      loaded.api.defaults.adapter = jest.fn(async (config: InternalAxiosRequestConfig) => {
        throw unauthorized(config);
      });
      loaded.postSpy.mockRejectedValue(new Error('refresh rejected'));
      loaded.setAccessToken('stale-access-token');
    }

    it('clears stored tokens when the refresh fails', async () => {
      failingRefresh();

      await expect(loaded.api.get('/expenses/')).rejects.toBeTruthy();

      expect(loaded.tokenStorage.clearStoredTokens).toHaveBeenCalledTimes(1);
    });

    it('notifies the registered session-expired handler', async () => {
      const onExpired = jest.fn();
      loaded.setSessionExpiredHandler(onExpired);
      failingRefresh();

      await expect(loaded.api.get('/expenses/')).rejects.toBeTruthy();

      expect(onExpired).toHaveBeenCalledTimes(1);
    });

    it('forgets the in-memory token so later requests go out unauthenticated', async () => {
      failingRefresh();
      await expect(loaded.api.get('/expenses/')).rejects.toBeTruthy();

      const adapter = jest.fn(async (config: InternalAxiosRequestConfig) => ok(config));
      loaded.api.defaults.adapter = adapter;
      await loaded.api.get('/expenses/');

      expect(authHeaderOf(adapter.mock.calls[0][0])).toBeUndefined();
    });

    it('surfaces the refresh failure to the caller', async () => {
      failingRefresh();

      await expect(loaded.api.get('/expenses/')).rejects.toThrow('refresh rejected');
    });

    it('does not fire the handler once it has been unregistered', async () => {
      const onExpired = jest.fn();
      loaded.setSessionExpiredHandler(onExpired);
      loaded.setSessionExpiredHandler(null);
      failingRefresh();

      await expect(loaded.api.get('/expenses/')).rejects.toBeTruthy();

      expect(onExpired).not.toHaveBeenCalled();
    });
  });

  describe('errors that are not 401', () => {
    it.each([400, 403, 404, 500])('passes a %s straight through', async (status) => {
      const adapter = jest.fn(async (config: InternalAxiosRequestConfig) => {
        const response = { data: {}, status, statusText: '', headers: {}, config } as AxiosResponse;
        throw new AxiosError('failed', 'ERR_BAD_RESPONSE', config, {}, response);
      });
      loaded.api.defaults.adapter = adapter;
      loaded.setAccessToken('token');

      await expect(loaded.api.get('/expenses/')).rejects.toBeInstanceOf(AxiosError);

      expect(loaded.postSpy).not.toHaveBeenCalled();
      expect(adapter).toHaveBeenCalledTimes(1);
    });

    it('passes a network error through without attempting a refresh', async () => {
      loaded.api.defaults.adapter = jest.fn(async () => {
        throw new AxiosError('Network Error', 'ERR_NETWORK');
      });
      loaded.setAccessToken('token');

      await expect(loaded.api.get('/expenses/')).rejects.toThrow('Network Error');

      expect(loaded.postSpy).not.toHaveBeenCalled();
    });
  });

  describe('client configuration', () => {
    it('has a request timeout so a dead backend fails in reasonable time', () => {
      expect(loaded.api.defaults.timeout).toBe(15_000);
    });

    it('is bound to the configured base URL', () => {
      expect(loaded.api.defaults.baseURL).toBe('http://testserver/api');
    });
  });
});
