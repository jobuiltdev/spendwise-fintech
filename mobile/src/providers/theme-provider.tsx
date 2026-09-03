import { createContext, useContext, useEffect, useMemo, useState, type PropsWithChildren } from 'react';

import { useColorScheme as useSystemColorScheme } from '@/hooks/use-color-scheme';
import { storage } from '@/services/local-storage';

export type ThemePreference = 'light' | 'dark' | 'system';
export type ColorScheme = 'light' | 'dark';

const THEME_PREFERENCE_KEY = 'spendwise_theme_preference';

interface ThemePreferenceContextValue {
  preference: ThemePreference;
  colorScheme: ColorScheme;
  setPreference: (preference: ThemePreference) => void;
}

const ThemePreferenceContext = createContext<ThemePreferenceContextValue | null>(null);

export function ThemePreferenceProvider({ children }: PropsWithChildren) {
  const systemScheme = useSystemColorScheme();
  const [preference, setPreferenceState] = useState<ThemePreference>('system');

  useEffect(() => {
    (async () => {
      const stored = await storage.getItem(THEME_PREFERENCE_KEY);
      if (stored === 'light' || stored === 'dark' || stored === 'system') {
        setPreferenceState(stored);
      }
    })();
  }, []);

  const setPreference = (next: ThemePreference) => {
    setPreferenceState(next);
    void storage.setItem(THEME_PREFERENCE_KEY, next);
  };

  const colorScheme: ColorScheme =
    preference === 'system' ? (systemScheme === 'dark' ? 'dark' : 'light') : preference;

  const value = useMemo(
    () => ({ preference, colorScheme, setPreference }),
    [preference, colorScheme]
  );

  return <ThemePreferenceContext.Provider value={value}>{children}</ThemePreferenceContext.Provider>;
}

export function useThemePreference() {
  const context = useContext(ThemePreferenceContext);
  if (!context) {
    throw new Error('useThemePreference must be used within a ThemePreferenceProvider');
  }
  return context;
}
