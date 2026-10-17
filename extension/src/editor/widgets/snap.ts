/**
 * Docking between canvas widgets: snapping a dragged or resized widget to the
 * others, and the groups of widgets that touch. Rectangles are in canvas
 * coordinates (a widget's outer box; x / y as in its free layout).
 */

export type Rect = { x: number; y: number; w: number; h: number };

/** Closer than this (in canvas pixels) and two widgets touch. */
const TOUCH = 1.5;

/** How far [a0, a1] and [b0, b1] overlap (negative: the gap between them). */
function overlap(a0: number, a1: number, b0: number, b1: number): number {
  return Math.min(a1, b1) - Math.max(a0, b0);
}

/** The smallest of the candidate shifts within `limit` (null: none). */
function nearest(candidates: number[], limit: number): number | null {
  let best: number | null = null;
  for (const c of candidates) {
    if (Math.abs(c) <= limit && (best === null || Math.abs(c) < Math.abs(best))) {
      best = c;
    }
  }
  return best;
}

/** Change of a widget's size (its right / bottom edge moving) to line up with the others' edges. */
export function snapResize(r: Rect, others: Rect[], limit: number): { dw: number; dh: number } {
  const ws: number[] = [];
  const hs: number[] = [];
  for (const o of others) {
    if (overlap(r.y, r.y + r.h, o.y, o.y + o.h) > -2 * limit) {
      ws.push(o.x - (r.x + r.w), o.x + o.w - (r.x + r.w));
    }
    if (overlap(r.x, r.x + r.w, o.x, o.x + o.w) > -2 * limit) {
      hs.push(o.y - (r.y + r.h), o.y + o.h - (r.y + r.h));
    }
  }
  return { dw: nearest(ws, limit) ?? 0, dh: nearest(hs, limit) ?? 0 };
}

/** Do two widgets touch (side by side or one above the other, overlapping along that edge)? */
export function touching(a: Rect, b: Rect): boolean {
  const side = Math.abs(a.x + a.w - b.x) <= TOUCH || Math.abs(b.x + b.w - a.x) <= TOUCH;
  const stacked = Math.abs(a.y + a.h - b.y) <= TOUCH || Math.abs(b.y + b.h - a.y) <= TOUCH;
  return (
    (side && overlap(a.y, a.y + a.h, b.y, b.y + b.h) > 4) || (stacked && overlap(a.x, a.x + a.w, b.x, b.x + b.w) > 4)
  );
}

/** The docked group of a widget: it and the widgets touching it, directly or through others. */
export function dockGroup(id: number, rects: Map<number, Rect>, members: Set<number>): number[] {
  if (!members.has(id)) {
    return [id];
  }
  const group = new Set([id]);
  const todo = [id];
  while (todo.length) {
    const a = rects.get(todo.pop()!)!;
    for (const m of members) {
      if (!group.has(m) && touching(a, rects.get(m)!)) {
        group.add(m);
        todo.push(m);
      }
    }
  }
  return [...group];
}

/** Shifts within `limit` that would snap a moved widget to the others, on each axis. */
function candidates(r: Rect, others: Rect[], limit: number): { xs: number[]; ys: number[] } {
  const xs: number[] = [];
  const ys: number[] = [];
  for (const o of others) {
    if (overlap(r.y, r.y + r.h, o.y, o.y + o.h) > -2 * limit) {
      xs.push(o.x + o.w - r.x, o.x - (r.x + r.w), o.x - r.x, o.x + o.w - (r.x + r.w));
    }
    if (overlap(r.x, r.x + r.w, o.x, o.x + o.w) > -2 * limit) {
      ys.push(o.y + o.h - r.y, o.y - (r.y + r.h), o.y - r.y, o.y + o.h - (r.y + r.h));
    }
  }
  return { xs: xs.filter(c => Math.abs(c) <= limit), ys: ys.filter(c => Math.abs(c) <= limit) };
}

/**
 * Shift for widgets moved together (or one) so they snap to the widgets that stay. The two axes
 * are chosen together: a position where they dock (touch another widget) wins over one that is
 * merely nearer (e.g. in line with a wider widget next to the one they should dock to), positions
 * where they would overlap another widget lose, and otherwise the smallest shift wins.
 */
export function snapGroup(moving: Rect[], others: Rect[], limit: number): { dx: number; dy: number } {
  const xs = new Set<number>();
  const ys = new Set<number>();
  for (const r of moving) {
    const c = candidates(r, others, limit);
    c.xs.forEach(x => xs.add(x));
    c.ys.forEach(y => ys.add(y));
  }
  let best = { dx: 0, dy: 0, score: -Infinity };
  for (const cx of [null, ...xs]) {
    for (const cy of [null, ...ys]) {
      const dx = cx ?? 0;
      const dy = cy ?? 0;
      const shifted = moving.map(r => ({ ...r, x: r.x + dx, y: r.y + dy }));
      let docks = false;
      let overlaps = false;
      for (const o of others) {
        if (shifted.some(r => touching(r, o))) {
          docks = true;
        }
        if (shifted.some(r => overlap(r.x, r.x + r.w, o.x, o.x + o.w) > TOUCH && overlap(r.y, r.y + r.h, o.y, o.y + o.h) > TOUCH)) {
          overlaps = true;
        }
      }
      // (docking at all counts, not how many widgets it touches: snapping in line wins then)
      const score = (docks ? 1000 : 0) - (overlaps ? 5000 : 0) + (cx !== null ? 50 : 0) + (cy !== null ? 50 : 0) - Math.abs(dx) - Math.abs(dy);
      if (score > best.score) {
        best = { dx, dy, score };
      }
    }
  }
  return { dx: best.dx, dy: best.dy };
}

/** The group's handle: its top widget (the leftmost of the top ones). */
export function groupTop(group: number[], rects: Map<number, Rect>): number {
  return [...group].sort((a, b) => rects.get(a)!.y - rects.get(b)!.y || rects.get(a)!.x - rects.get(b)!.x)[0];
}

/**
 * SVG path of the outline around a set of rectangles (each grown by `pad`):
 * their union's outer edges only, so touching widgets get one outline.
 */
export function outlinePath(rects: Rect[], pad: number): string {
  if (!rects.length) {
    return '';
  }
  const grown = rects.map(r => ({ x0: r.x - pad, y0: r.y - pad, x1: r.x + r.w + pad, y1: r.y + r.h + pad }));
  const xs = [...new Set(grown.flatMap(r => [r.x0, r.x1]))].sort((a, b) => a - b);
  const ys = [...new Set(grown.flatMap(r => [r.y0, r.y1]))].sort((a, b) => a - b);
  // cell (i, j) spans xs[i]..xs[i + 1] and ys[j]..ys[j + 1]
  const covered = (i: number, j: number) => {
    if (i < 0 || j < 0 || i >= xs.length - 1 || j >= ys.length - 1) {
      return false;
    }
    const cx = (xs[i] + xs[i + 1]) / 2;
    const cy = (ys[j] + ys[j + 1]) / 2;
    return grown.some(r => cx > r.x0 && cx < r.x1 && cy > r.y0 && cy < r.y1);
  };
  const f = (v: number) => Math.round(v * 10) / 10;
  const parts: string[] = [];
  // horizontal edges: between a covered and an empty cell, joined along the line
  for (let j = 0; j < ys.length; j++) {
    let start: number | null = null;
    for (let i = 0; i <= xs.length - 1; i++) {
      const edge = i < xs.length - 1 && covered(i, j - 1) !== covered(i, j);
      if (edge && start === null) {
        start = xs[i];
      } else if (!edge && start !== null) {
        parts.push(`M${f(start)} ${f(ys[j])}H${f(xs[i])}`);
        start = null;
      }
    }
  }
  for (let i = 0; i < xs.length; i++) {
    let start: number | null = null;
    for (let j = 0; j <= ys.length - 1; j++) {
      const edge = j < ys.length - 1 && covered(i - 1, j) !== covered(i, j);
      if (edge && start === null) {
        start = ys[j];
      } else if (!edge && start !== null) {
        parts.push(`M${f(xs[i])} ${f(start)}V${f(ys[j])}`);
        start = null;
      }
    }
  }
  return parts.join('');
}
