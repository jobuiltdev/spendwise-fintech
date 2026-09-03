import { DarkTheme, DefaultTheme, Stack, ThemeProvider as NavigationThemeProvider } from 'expo-router';
import * as SplashScreen from 'expo-splash-screen';

import { AnimatedSplashOverlay } from '@/components/animated-icon';
import { ToastHost } from '@/components/toast-host';
import { AppProviders } from '@/providers';
import { useThemePreference } from '@/providers/theme-provider';

SplashScreen.preventAutoHideAsync();

export default function RootLayout() {
  return (
    <AppProviders>
      <AppShell />
    </AppProviders>
  );
}

// Reads the (possibly user-overridden) color scheme from ThemePreferenceProvider,
// which is mounted by AppProviders above this component - split out so this hook
// call happens inside that provider's subtree rather than above it.
function AppShell() {
  const { colorScheme } = useThemePreference();
  return (
    <NavigationThemeProvider value={colorScheme === 'dark' ? DarkTheme : DefaultTheme}>
      <AnimatedSplashOverlay />
      <Stack screenOptions={{ headerShown: false }} />
      <ToastHost />
    </NavigationThemeProvider>
  );
}
