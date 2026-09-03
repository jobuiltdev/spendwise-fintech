import { useCallback, useState } from 'react';
import { StyleSheet } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import Animated, {
  Easing,
  runOnJS,
  useAnimatedStyle,
  useSharedValue,
  withDelay,
  withSequence,
  withTiming,
} from 'react-native-reanimated';

import { ThemedText } from '@/components/themed-text';
import { Radius, Spacing } from '@/constants/theme';
import { useToastListener } from '@/hooks/use-toast';

const VISIBLE_DURATION = 2000;

export function ToastHost() {
  const [message, setMessage] = useState<string | null>(null);
  const opacity = useSharedValue(0);

  const handleShow = useCallback(
    (nextMessage: string) => {
      setMessage(nextMessage);
      opacity.set(
        withSequence(
          withTiming(1, { duration: 200, easing: Easing.out(Easing.cubic) }),
          withDelay(
            VISIBLE_DURATION,
            withTiming(0, { duration: 250 }, (finished) => {
              if (finished) runOnJS(setMessage)(null);
            })
          )
        )
      );
    },
    [opacity]
  );

  useToastListener(handleShow);

  const animatedStyle = useAnimatedStyle(() => ({ opacity: opacity.get() }));

  if (!message) return null;

  return (
    <SafeAreaView style={styles.wrapper} pointerEvents="none">
      <Animated.View style={[styles.pill, animatedStyle]}>
        <ThemedText type="smallBold" style={styles.text}>
          {message}
        </ThemedText>
      </Animated.View>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  wrapper: {
    ...StyleSheet.absoluteFill,
    alignItems: 'center',
    justifyContent: 'flex-end',
    paddingBottom: Spacing.six,
  },
  pill: {
    backgroundColor: '#1F2937',
    borderRadius: Radius.pill,
    paddingHorizontal: Spacing.four,
    paddingVertical: Spacing.two,
  },
  text: {
    color: '#ffffff',
  },
});
