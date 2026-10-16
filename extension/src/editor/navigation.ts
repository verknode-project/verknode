import { ConnectionInfo } from '../types';

/**
 * Keyboard navigation between nodes.
 *
 * "spatial" (the default) ignores connections and takes the nearest node in
 * the direction of the key. Lineage (Alt held, or locked by a double tap):
 * ←/→ go to an upstream / downstream node, ↑/↓ to a sibling (a node that
 * shares a parent or a child); "approximate" picks among several candidates
 * by position (closest; above/below for siblings), "strict" follows port
 * order.
 */
export type Direction = 'left' | 'right' | 'up' | 'down';
export type NavigationMode = 'approximate' | 'strict' | 'spatial';

export const DIRECTIONS: Direction[] = ['left', 'right', 'up', 'down'];
export const ARROWS: Record<Direction, string> = { left: '←', right: '→', up: '↑', down: '↓' };
export const KEY_DIRECTIONS: Record<string, Direction> = {
  ArrowLeft: 'left',
  ArrowRight: 'right',
  ArrowUp: 'up',
  ArrowDown: 'down'
};

export interface NavigationGraph {
  connections: ConnectionInfo[];
  /** node centers */
  positions: Map<number, { x: number; y: number }>;
}

// the implicit flow variable port (0) comes after the data ports
const portRank = (p: number) => (p === 0 ? 1e6 : p);

function unique(ids: number[]): number[] {
  return ids.filter((id, i) => ids.indexOf(id) === i);
}

export function parentsOf(g: NavigationGraph, id: number): number[] {
  return unique(
    g.connections
      .filter(c => c.destID === id)
      .sort((a, b) => portRank(a.destPort) - portRank(b.destPort) || a.sourceID - b.sourceID)
      .map(c => c.sourceID)
  );
}

export function childrenOf(g: NavigationGraph, id: number): number[] {
  return unique(
    g.connections
      .filter(c => c.sourceID === id)
      .sort(
        (a, b) =>
          portRank(a.sourcePort) - portRank(b.sourcePort) || a.destPort - b.destPort || a.destID - b.destID
      )
      .map(c => c.destID)
  );
}

/** Candidate closest to `from`, preferring the same row (vertical distance first). */
function closest(g: NavigationGraph, from: number, candidates: number[]): number | null {
  const p = g.positions.get(from);
  let best: number | null = null;
  let bestScore = Infinity;
  for (const id of candidates) {
    const q = g.positions.get(id);
    if (!p || !q) {
      continue;
    }
    const score = Math.abs(q.y - p.y) * 2 + Math.abs(q.x - p.x);
    if (score < bestScore) {
      bestScore = score;
      best = id;
    }
  }
  return best;
}

/** Nearest candidate strictly above (up) or below (down). */
function vertical(g: NavigationGraph, from: number, candidates: number[], dir: 'up' | 'down'): number | null {
  const p = g.positions.get(from);
  let best: number | null = null;
  let bestScore = Infinity;
  for (const id of candidates) {
    const q = g.positions.get(id);
    if (!p || !q) {
      continue;
    }
    const dy = dir === 'up' ? p.y - q.y : q.y - p.y;
    if (dy <= 1) {
      continue;
    }
    const score = dy + Math.abs(q.x - p.x) * 0.5;
    if (score < bestScore) {
      bestScore = score;
      best = id;
    }
  }
  return best;
}

function siblings(g: NavigationGraph, id: number): number[] {
  const out: number[] = [];
  for (const p of parentsOf(g, id)) {
    out.push(...childrenOf(g, p));
  }
  for (const c of childrenOf(g, id)) {
    out.push(...parentsOf(g, c));
  }
  return unique(out).filter(x => x !== id);
}

function strictSiblings(g: NavigationGraph, id: number): number[] {
  const parent = parentsOf(g, id)[0];
  if (parent !== undefined) {
    return childrenOf(g, parent);
  }
  const child = childrenOf(g, id)[0];
  return child !== undefined ? parentsOf(g, child) : [id];
}

/** Nearest node in a direction, regardless of connections. */
function spatial(g: NavigationGraph, from: number, dir: Direction): number | null {
  const p = g.positions.get(from);
  if (!p) {
    return null;
  }
  let best: number | null = null;
  let bestScore = Infinity;
  for (const [id, q] of g.positions) {
    if (id === from) {
      continue;
    }
    const dx = q.x - p.x;
    const dy = q.y - p.y;
    const [main, side] =
      dir === 'right' ? [dx, dy] : dir === 'left' ? [-dx, dy] : dir === 'down' ? [dy, dx] : [-dy, dx];
    if (main <= 5) {
      continue;
    }
    const score = main + Math.abs(side) * 2;
    if (score < bestScore) {
      bestScore = score;
      best = id;
    }
  }
  return best;
}

export function navigationTarget(
  g: NavigationGraph,
  from: number,
  dir: Direction,
  mode: NavigationMode
): number | null {
  if (mode === 'spatial') {
    return spatial(g, from, dir);
  }
  if (dir === 'left' || dir === 'right') {
    const candidates = dir === 'left' ? parentsOf(g, from) : childrenOf(g, from);
    if (!candidates.length) {
      return null;
    }
    return mode === 'strict' ? candidates[0] : closest(g, from, candidates);
  }
  if (mode === 'strict') {
    const list = strictSiblings(g, from);
    const i = list.indexOf(from);
    const next = i < 0 ? undefined : list[dir === 'up' ? i - 1 : i + 1];
    return next === undefined ? null : next;
  }
  return vertical(g, from, siblings(g, from), dir);
}

/** For every reachable target: the arrows that lead there (hints shown on the nodes). */
export function navigationHints(
  g: NavigationGraph,
  from: number,
  mode: Exclude<NavigationMode, 'spatial'>
): Map<number, { keys: string[]; spatial: string[] }> {
  const hints = new Map<number, { keys: string[]; spatial: string[] }>();
  const add = (id: number | null, key: string, kind: 'keys' | 'spatial') => {
    if (id === null) {
      return;
    }
    const h = hints.get(id) ?? { keys: [], spatial: [] };
    h[kind].push(key);
    hints.set(id, h);
  };
  for (const dir of DIRECTIONS) {
    add(navigationTarget(g, from, dir, mode), ARROWS[dir], 'keys');
    add(navigationTarget(g, from, dir, 'spatial'), ARROWS[dir], 'spatial');
  }
  return hints;
}
