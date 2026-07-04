import { describe, it, expect } from 'vitest';
import { createDragTracker } from './gestures';

describe('createDragTracker (axis=x)', () => {
  it('locks to x when first movement is horizontal, reports dx', () => {
    const t = createDragTracker({ axis: 'x', threshold: 80 });
    t.start(100, 100);
    const s = t.move(120, 103); // 20px right, 3px down → locks x
    expect(s.locked).toBe('x');
    expect(s.dx).toBe(20);
  });

  it('yields to vertical scroll: locks y, reports zero deltas', () => {
    const t = createDragTracker({ axis: 'x', threshold: 80 });
    t.start(100, 100);
    const s = t.move(103, 130); // mostly vertical → locks y
    expect(s.locked).toBe('y');
    expect(s.dx).toBe(0);
    // even a later horizontal move stays dead — scroll owns the gesture
    expect(t.move(200, 130).dx).toBe(0);
  });

  it('stays unlocked inside the slop', () => {
    const t = createDragTracker({ axis: 'x', threshold: 80, lockSlop: 8 });
    t.start(100, 100);
    expect(t.move(104, 102).locked).toBe(null);
  });

  it('commits right past threshold, with direction', () => {
    const t = createDragTracker({ axis: 'x', threshold: 80 });
    t.start(0, 0);
    t.move(90, 0);
    expect(t.end()).toEqual({ committed: true, direction: 'right' });
  });

  it('does not commit below threshold', () => {
    const t = createDragTracker({ axis: 'x', threshold: 80 });
    t.start(0, 0);
    t.move(-50, 0);
    expect(t.end()).toEqual({ committed: false, direction: 'left' });
  });

  it('cancel resets everything', () => {
    const t = createDragTracker({ axis: 'x', threshold: 80 });
    t.start(0, 0);
    t.move(90, 0);
    t.cancel();
    expect(t.end()).toEqual({ committed: false, direction: null });
    expect(t.state.dx).toBe(0);
  });
});

describe('createDragTracker (axis=y) — pull-to-refresh shape', () => {
  it('locks to y on a downward pull and commits down', () => {
    const t = createDragTracker({ axis: 'y', threshold: 70 });
    t.start(50, 0);
    const s = t.move(52, 40);
    expect(s.locked).toBe('y');
    expect(s.dy).toBe(40);
    t.move(52, 80);
    expect(t.end()).toEqual({ committed: true, direction: 'down' });
  });
});
