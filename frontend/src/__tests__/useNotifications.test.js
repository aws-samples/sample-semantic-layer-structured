/**
 * Tests for the useNotifications hook.
 *
 * Covers:
 *  - Adding a notification
 *  - Removing a notification
 *  - Upsert: same ID replaces existing banner (no stacking)
 *  - onDismiss is injected automatically into every item
 *  - success/info auto-dismiss after AUTO_DISMISS_MS
 *  - error/warning do NOT auto-dismiss
 *  - Auto-dismiss timer is cancelled when the notification is manually removed
 *  - Auto-dismiss timer restarts when a notification is replaced
 */

import { act, renderHook } from '@testing-library/react';
import { useNotifications } from '../hooks/useNotifications';

// Use Jest's fake timers for auto-dismiss behaviour
beforeEach(() => {
  jest.useFakeTimers();
});

afterEach(() => {
  jest.runOnlyPendingTimers();
  jest.useRealTimers();
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Shorthand to get the current notification list from a rendered hook. */
const getItems = (result) => result.current.notifications;

// ---------------------------------------------------------------------------
// Basic add / remove
// ---------------------------------------------------------------------------

describe('addNotification', () => {
  it('adds a notification to the list', () => {
    const { result } = renderHook(() => useNotifications());

    act(() => {
      result.current.addNotification({ id: 'n1', type: 'info', content: 'Hello' });
    });

    expect(getItems(result)).toHaveLength(1);
    expect(getItems(result)[0].id).toBe('n1');
    expect(getItems(result)[0].content).toBe('Hello');
  });

  it('adds multiple distinct notifications', () => {
    const { result } = renderHook(() => useNotifications());

    act(() => {
      result.current.addNotification({ id: 'a', type: 'info', content: 'A' });
      result.current.addNotification({ id: 'b', type: 'error', content: 'B' });
    });

    expect(getItems(result)).toHaveLength(2);
  });

  it('generates an id when none is provided', () => {
    const { result } = renderHook(() => useNotifications());

    act(() => {
      result.current.addNotification({ type: 'success', content: 'Auto-id' });
    });

    expect(getItems(result)).toHaveLength(1);
    expect(getItems(result)[0].id).toBeTruthy();
  });
});

describe('removeNotification', () => {
  it('removes a notification by id', () => {
    const { result } = renderHook(() => useNotifications());

    act(() => {
      result.current.addNotification({ id: 'r1', type: 'info', content: 'Remove me' });
    });
    expect(getItems(result)).toHaveLength(1);

    act(() => {
      result.current.removeNotification('r1');
    });
    expect(getItems(result)).toHaveLength(0);
  });

  it('only removes the targeted notification', () => {
    const { result } = renderHook(() => useNotifications());

    act(() => {
      result.current.addNotification({ id: 'keep', type: 'error', content: 'Stay' });
      result.current.addNotification({ id: 'gone', type: 'info', content: 'Go' });
    });

    act(() => {
      result.current.removeNotification('gone');
    });

    expect(getItems(result)).toHaveLength(1);
    expect(getItems(result)[0].id).toBe('keep');
  });
});

// ---------------------------------------------------------------------------
// onDismiss injection
// ---------------------------------------------------------------------------

describe('onDismiss injection', () => {
  it('injects onDismiss into every added notification', () => {
    const { result } = renderHook(() => useNotifications());

    act(() => {
      result.current.addNotification({ id: 'd1', type: 'success', content: 'Dismiss me' });
    });

    expect(typeof getItems(result)[0].onDismiss).toBe('function');
  });

  it('calling onDismiss removes the notification', () => {
    const { result } = renderHook(() => useNotifications());

    act(() => {
      result.current.addNotification({ id: 'd2', type: 'error', content: 'Self-dismiss' });
    });
    expect(getItems(result)).toHaveLength(1);

    act(() => {
      getItems(result)[0].onDismiss();
    });
    expect(getItems(result)).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Upsert (replace by ID)
// ---------------------------------------------------------------------------

describe('upsert — same ID replaces existing banner', () => {
  it('replaces an existing notification when the same id is used', () => {
    const { result } = renderHook(() => useNotifications());

    act(() => {
      result.current.addNotification({ id: 'q1', type: 'info', content: 'Processing…' });
    });
    expect(getItems(result)[0].type).toBe('info');

    act(() => {
      result.current.addNotification({ id: 'q1', type: 'success', content: 'Completed!' });
    });

    // Still only one banner — no stacking
    expect(getItems(result)).toHaveLength(1);
    expect(getItems(result)[0].type).toBe('success');
    expect(getItems(result)[0].content).toBe('Completed!');
  });

  it('replaces info with error on the same id', () => {
    const { result } = renderHook(() => useNotifications());

    act(() => {
      result.current.addNotification({ id: 'q2', type: 'info', content: 'Processing…' });
      result.current.addNotification({ id: 'q2', type: 'error', content: 'Failed!' });
    });

    expect(getItems(result)).toHaveLength(1);
    expect(getItems(result)[0].type).toBe('error');
  });

  it('preserves list order when upserting (replaced item stays in position)', () => {
    const { result } = renderHook(() => useNotifications());

    act(() => {
      result.current.addNotification({ id: 'first', type: 'info', content: 'First' });
      result.current.addNotification({ id: 'second', type: 'error', content: 'Second' });
    });

    act(() => {
      result.current.addNotification({ id: 'first', type: 'success', content: 'First updated' });
    });

    expect(getItems(result)).toHaveLength(2);
    expect(getItems(result)[0].id).toBe('first');
    expect(getItems(result)[0].type).toBe('success');
    expect(getItems(result)[1].id).toBe('second');
  });
});

// ---------------------------------------------------------------------------
// Auto-dismiss
// ---------------------------------------------------------------------------

describe('auto-dismiss', () => {
  it('auto-dismisses success notifications after 5 s', () => {
    const { result } = renderHook(() => useNotifications());

    act(() => {
      result.current.addNotification({ id: 'ad1', type: 'success', content: 'Done' });
    });
    expect(getItems(result)).toHaveLength(1);

    act(() => {
      jest.advanceTimersByTime(5000);
    });
    expect(getItems(result)).toHaveLength(0);
  });

  it('auto-dismisses info notifications after 5 s', () => {
    const { result } = renderHook(() => useNotifications());

    act(() => {
      result.current.addNotification({ id: 'ad2', type: 'info', content: 'Processing…' });
    });

    act(() => {
      jest.advanceTimersByTime(5000);
    });
    expect(getItems(result)).toHaveLength(0);
  });

  it('does NOT auto-dismiss error notifications', () => {
    const { result } = renderHook(() => useNotifications());

    act(() => {
      result.current.addNotification({ id: 'ad3', type: 'error', content: 'Oops' });
    });

    act(() => {
      jest.advanceTimersByTime(10000);
    });
    expect(getItems(result)).toHaveLength(1);
  });

  it('does NOT auto-dismiss warning notifications', () => {
    const { result } = renderHook(() => useNotifications());

    act(() => {
      result.current.addNotification({ id: 'ad4', type: 'warning', content: 'Heads up' });
    });

    act(() => {
      jest.advanceTimersByTime(10000);
    });
    expect(getItems(result)).toHaveLength(1);
  });

  it('does not auto-dismiss before 5 s', () => {
    const { result } = renderHook(() => useNotifications());

    act(() => {
      result.current.addNotification({ id: 'ad5', type: 'success', content: 'Done' });
    });

    act(() => {
      jest.advanceTimersByTime(4999);
    });
    expect(getItems(result)).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// Timer interactions
// ---------------------------------------------------------------------------

describe('auto-dismiss timer interactions', () => {
  it('cancels auto-dismiss timer when notification is manually removed', () => {
    const { result } = renderHook(() => useNotifications());

    act(() => {
      result.current.addNotification({ id: 'tm1', type: 'success', content: 'Done' });
    });

    act(() => {
      result.current.removeNotification('tm1');
    });
    expect(getItems(result)).toHaveLength(0);

    // Advance past auto-dismiss — no errors should be thrown by orphaned timers
    act(() => {
      jest.advanceTimersByTime(5000);
    });
    expect(getItems(result)).toHaveLength(0);
  });

  it('resets auto-dismiss timer when a notification is replaced via upsert', () => {
    const { result } = renderHook(() => useNotifications());

    act(() => {
      result.current.addNotification({ id: 'tm2', type: 'info', content: 'Processing…' });
    });

    // Advance 4 s — not yet dismissed
    act(() => {
      jest.advanceTimersByTime(4000);
    });
    expect(getItems(result)).toHaveLength(1);

    // Replace with success — timer should restart from 0
    act(() => {
      result.current.addNotification({ id: 'tm2', type: 'success', content: 'Done' });
    });

    // Only 1 s has passed since the upsert — should still be visible
    act(() => {
      jest.advanceTimersByTime(1000);
    });
    expect(getItems(result)).toHaveLength(1);
    expect(getItems(result)[0].type).toBe('success');

    // Now advance the remaining 4 s — should auto-dismiss
    act(() => {
      jest.advanceTimersByTime(4000);
    });
    expect(getItems(result)).toHaveLength(0);
  });
});
