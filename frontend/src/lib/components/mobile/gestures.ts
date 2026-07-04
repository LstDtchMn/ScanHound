/** Pure touch-drag state machine with axis locking.
 *  DOM-free: feed it coordinates from pointer/touch events; it answers
 *  whether the gesture belongs to us (locked to our axis) or to the
 *  scroller (locked to the other axis), and whether release commits. */

export type Axis = 'x' | 'y';

export interface DragState {
  dx: number;
  dy: number;
  active: boolean;
  locked: Axis | null;
}

export interface DragEnd {
  committed: boolean;
  direction: 'left' | 'right' | 'up' | 'down' | null;
}

const IDLE: DragState = { dx: 0, dy: 0, active: false, locked: null };

export function createDragTracker(opts: { axis: Axis; threshold: number; lockSlop?: number }) {
  const lockSlop = opts.lockSlop ?? 8;
  let startX = 0;
  let startY = 0;
  let cur: DragState = { ...IDLE };

  function start(x: number, y: number): void {
    startX = x;
    startY = y;
    cur = { dx: 0, dy: 0, active: true, locked: null };
  }

  function move(x: number, y: number): DragState {
    if (!cur.active) return cur;
    const rawDx = x - startX;
    const rawDy = y - startY;
    if (cur.locked === null) {
      if (Math.hypot(rawDx, rawDy) < lockSlop) return cur;
      const dominant: Axis = Math.abs(rawDx) >= Math.abs(rawDy) ? 'x' : 'y';
      cur = { ...cur, locked: dominant };
    }
    if (cur.locked !== opts.axis) return cur; // the scroller owns this gesture
    cur = {
      ...cur,
      dx: opts.axis === 'x' ? rawDx : 0,
      dy: opts.axis === 'y' ? rawDy : 0
    };
    return cur;
  }

  function end(): DragEnd {
    const delta = opts.axis === 'x' ? cur.dx : cur.dy;
    const committed = cur.locked === opts.axis && Math.abs(delta) >= opts.threshold;
    let direction: DragEnd['direction'] = null;
    if (cur.locked === opts.axis && delta !== 0) {
      direction = opts.axis === 'x' ? (delta > 0 ? 'right' : 'left') : (delta > 0 ? 'down' : 'up');
    }
    cur = { ...IDLE };
    return { committed, direction };
  }

  function cancel(): void {
    cur = { ...IDLE };
  }

  return {
    start, move, end, cancel,
    get state() { return cur; }
  };
}
