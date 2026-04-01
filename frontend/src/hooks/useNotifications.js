import { useState, useCallback, useRef, useEffect } from 'react';

/** Types that auto-dismiss after AUTO_DISMISS_MS */
const AUTO_DISMISS_TYPES = ['success', 'info'];
const AUTO_DISMISS_MS = 5000;

/**
 * Manages Cloudscape Flashbar notifications.
 *
 * Features:
 *  - Each item gets `onDismiss` injected automatically (Cloudscape v3 requirement)
 *  - addNotification upserts by ID — same ID replaces the existing banner
 *    (lets "Processing…" → "Success" swap in place without stacking)
 *  - success / info notifications auto-dismiss after AUTO_DISMISS_MS
 *  - error / warning notifications stay until the user dismisses them
 *  - All pending timers are cleared on unmount
 */
export function useNotifications() {
  const [notifications, setNotifications] = useState([]);
  const timers = useRef({});

  // Clear all timers on unmount
  useEffect(() => {
    const currentTimers = timers.current;
    return () => {
      Object.values(currentTimers).forEach(clearTimeout);
    };
  }, []);

  const removeNotification = useCallback((id) => {
    if (timers.current[id]) {
      clearTimeout(timers.current[id]);
      delete timers.current[id];
    }
    setNotifications((prev) => prev.filter((n) => n.id !== id));
  }, []);

  const addNotification = useCallback(
    (notification) => {
      const id = notification.id || String(Date.now());

      // Clear any existing auto-dismiss timer for this ID
      if (timers.current[id]) {
        clearTimeout(timers.current[id]);
        delete timers.current[id];
      }

      const item = {
        ...notification,
        id,
        // Cloudscape v3: onDismiss must be on the item, not on the Flashbar
        onDismiss: () => removeNotification(id),
      };

      setNotifications((prev) => {
        const exists = prev.some((n) => n.id === id);
        // Upsert: replace existing banner with the same ID
        return exists
          ? prev.map((n) => (n.id === id ? item : n))
          : [...prev, item];
      });

      // Auto-dismiss success and info after AUTO_DISMISS_MS
      if (AUTO_DISMISS_TYPES.includes(notification.type)) {
        timers.current[id] = setTimeout(() => removeNotification(id), AUTO_DISMISS_MS);
      }
    },
    [removeNotification],
  );

  return { notifications, addNotification, removeNotification };
}
