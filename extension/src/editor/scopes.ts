import { contours } from 'd3-contour';

import { ConnectionInfo, NodeDescription, NodeInfo } from '../types';

/**
 * Scope blocks (loops, try/catch, IF/CASE switches, cross-validation): a node
 * declaring closing nodes (closedBy) and its matching closer, with every
 * node in between. Used to draw faint bubbles around related nodes.
 */
export type ScopeKind = 'loop' | 'try' | 'switch' | 'group';

export interface ScopeBlock {
  /** label anchor (loop/try/switch start node, top-left node of a group) */
  start: number;
  end: number | null; // null: not closed yet
  members: number[];
  kind: ScopeKind;
  /** number of blocks nested inside (outer blocks get more padding) */
  depth: number;
  /** group annotation id (kind 'group') */
  groupId?: number;
  /** margin around the nodes, set by layoutBlocks */
  pad?: number;
}

const BASE_PAD = 14;
/** extra margin of an enclosing bubble over the ones inside */
const NEST_STEP = 14;
/** extra margin of a bubble that partly overlaps an earlier one, so both outlines stay visible */
const OVERLAP_STEP = 14;

/**
 * Does `outer` enclose `inner`? Bubbles with the same nodes are layered in
 * list order: the later one goes around the earlier one.
 */
export function encloses(blocks: ScopeBlock[], outer: ScopeBlock, inner: ScopeBlock): boolean {
  if (outer === inner || !inner.members.every(m => outer.members.includes(m))) {
    return false;
  }
  return outer.members.length > inner.members.length || blocks.indexOf(outer) > blocks.indexOf(inner);
}

/**
 * Nesting depth and margin of every bubble: an enclosing bubble gets more
 * margin than everything inside it; bubbles that share only some nodes are
 * offset from each other so each outline (and label) stays visible.
 */
export function layoutBlocks(blocks: ScopeBlock[]): ScopeBlock[] {
  const depthOf = new Map<ScopeBlock, number>();
  const depth = (b: ScopeBlock): number => {
    if (!depthOf.has(b)) {
      depthOf.set(b, 0); // guards against cycles
      let d = 0;
      for (const o of blocks) {
        if (encloses(blocks, b, o)) {
          d = Math.max(d, 1 + depth(o));
        }
      }
      depthOf.set(b, d);
    }
    return depthOf.get(b)!;
  };
  blocks.forEach(b => (b.depth = depth(b)));
  const byDepth = [...blocks].sort((a, b) => a.depth - b.depth || blocks.indexOf(a) - blocks.indexOf(b));
  for (const b of byDepth) {
    const overlaps = blocks.filter(
      o =>
        blocks.indexOf(o) < blocks.indexOf(b) &&
        o.members.some(m => b.members.includes(m)) &&
        !encloses(blocks, o, b) &&
        !encloses(blocks, b, o)
    ).length;
    const inner = blocks.filter(o => encloses(blocks, b, o)).map(o => (o.pad ?? BASE_PAD) + NEST_STEP);
    b.pad = Math.max(BASE_PAD + overlaps * OVERLAP_STEP, ...inner);
  }
  return blocks;
}

export function findScopeBlocks(
  nodes: NodeInfo[],
  connections: ConnectionInfo[],
  repository: NodeDescription[]
): ScopeBlock[] {
  const types = new Map(repository.map(d => [d.factory, d]));
  const byId = new Map(nodes.map(n => [n.id, n]));
  const parents = new Map<number, number[]>();
  const children = new Map<number, number[]>();
  for (const c of connections) {
    parents.set(c.destID, [...(parents.get(c.destID) ?? []), c.sourceID]);
    children.set(c.sourceID, [...(children.get(c.sourceID) ?? []), c.destID]);
  }
  const closersOf = (id: number) => types.get(byId.get(id)?.factory ?? '')?.closedBy ?? [];
  const closerFactories = new Set(repository.flatMap(d => d.closedBy ?? []));
  const reach = (start: number, next: Map<number, number[]>) => {
    const seen = new Set<number>();
    const todo = [...(next.get(start) ?? [])];
    while (todo.length) {
      const n = todo.pop()!;
      if (!seen.has(n)) {
        seen.add(n);
        todo.push(...(next.get(n) ?? []));
      }
    }
    return seen;
  };

  // matching start of a closing node: walk upstream, skipping nested scopes
  const matched = new Map<number, number | null>();
  const startOf = (end: number): number | null => {
    if (matched.has(end)) {
      return matched.get(end)!;
    }
    matched.set(end, null); // guards against cycles
    const factory = byId.get(end)?.factory ?? '';
    const seen = new Set<number>();
    const todo = [...(parents.get(end) ?? [])];
    let found: number | null = null;
    while (todo.length && found === null) {
      const u = todo.shift()!;
      if (seen.has(u)) {
        continue;
      }
      seen.add(u);
      if (closersOf(u).includes(factory)) {
        found = u;
      } else if (closerFactories.has(byId.get(u)?.factory ?? '')) {
        const inner = startOf(u);
        todo.push(...(parents.get(inner ?? u) ?? []));
      } else {
        todo.push(...(parents.get(u) ?? []));
      }
    }
    matched.set(end, found);
    return found;
  };

  const ends = new Map<number, number>(); // start -> end
  for (const n of nodes) {
    if (closerFactories.has(n.factory)) {
      const s = startOf(n.id);
      if (s !== null && !ends.has(s)) {
        ends.set(s, n.id);
      }
    }
  }

  const blocks: ScopeBlock[] = [];
  for (const n of nodes) {
    if (!closersOf(n.id).length) {
      continue;
    }
    const end = ends.get(n.id) ?? null;
    const downstream = reach(n.id, children);
    let members: number[];
    if (end !== null) {
      const upstreamOfEnd = reach(end, parents);
      members = [n.id, ...[...downstream].filter(d => upstreamOfEnd.has(d)), end];
    } else {
      members = [n.id, ...downstream]; // open scope: everything after the start
    }
    const kind: ScopeKind = n.loopRole === 'start' ? 'loop' : /^Try\b/.test(n.name) ? 'try' : 'switch';
    blocks.push({ start: n.id, end, members: [...new Set(members)], kind, depth: 0 });
  }
  return layoutBlocks(blocks);
}

// ------------------------------------------------------------------ geometry
export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

interface Point {
  x: number;
  y: number;
}

/** Shape of a block bubble (flow coordinates). */
export interface BubbleShape {
  /** SVG path (may contain holes: use fill-rule evenodd) */
  path: string;
  /** bounding box of the path */
  box: Rect;
  /** label position (just above the start node) */
  label: Point;
}

/** margin around the nodes of a block; outer blocks get more */
export const bubblePadding = (block: ScopeBlock) => block.pad ?? BASE_PAD + block.depth * NEST_STEP;

/** Convex hull (Andrew's monotone chain), without collinear points. */
function convexHull(points: Point[]): Point[] {
  const pts = [...points].sort((a, b) => a.x - b.x || a.y - b.y);
  if (pts.length < 3) {
    return pts;
  }
  const cross = (o: Point, a: Point, b: Point) => (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x);
  const half = (list: Point[]) => {
    const out: Point[] = [];
    for (const p of list) {
      while (out.length >= 2 && cross(out[out.length - 2], out[out.length - 1], p) <= 0) {
        out.pop();
      }
      out.push(p);
    }
    return out.slice(0, -1);
  };
  return [...half(pts), ...half([...pts].reverse())];
}

/** Is the point inside (or on) the convex polygon? */
function inConvex(hull: Point[], x: number, y: number): boolean {
  if (hull.length < 3) {
    return false;
  }
  let sign = 0;
  for (let i = 0; i < hull.length; i++) {
    const a = hull[i];
    const b = hull[(i + 1) % hull.length];
    const c = (b.x - a.x) * (y - a.y) - (b.y - a.y) * (x - a.x);
    if (Math.abs(c) < 1e-9) {
      continue;
    }
    if (sign === 0) {
      sign = Math.sign(c);
    } else if (Math.sign(c) !== sign) {
      return false;
    }
  }
  return true;
}

/**
 * Closing radii tried in turn: gaps narrower than about twice the radius are
 * filled, larger blank areas stay concave. The first radius that keeps the
 * block in one piece wins; if none does, the convex hull is used.
 */
const CLOSE_RADII = [70, 160];
/** rounding of the inner corners left by carving out other nodes */
const CARVE_ROUNDING = 24;
/**
 * Straightening: a dent in the outline no deeper than this (px) is bridged
 * by a straight line, so runs of nodes get straight edges instead of a dip
 * between every pair; deeper bays are real concavity and stay.
 */
const DENT_DEPTH = 30;
/** longest straight bridge (px) */
const DENT_SPAN = 700;
/** clearance between a bubble and a node that does not belong to it */
const CLEARANCE = 8;
/** gap between a nested bubble and its parent */
const NEST_GAP = 6;
/** grid resolution: at least this many pixels per cell, at most MAX_CELLS cells per block */
const MIN_CELL = 5;
const MAX_CELLS = 16000;
const FAR = 1e20;

/** Exact Euclidean distance transform (Felzenszwalb & Huttenlocher): distance to the nearest set cell. */
function distanceTransform(mask: Uint8Array, nx: number, ny: number): Float32Array {
  const n = Math.max(nx, ny);
  const f = new Float64Array(n);
  const d = new Float64Array(n);
  const v = new Int32Array(n);
  const z = new Float64Array(n + 1);
  const grid = new Float64Array(nx * ny);
  for (let i = 0; i < grid.length; i++) {
    grid[i] = mask[i] ? 0 : FAR;
  }
  const pass = (len: number) => {
    let k = 0;
    v[0] = 0;
    z[0] = -Infinity;
    z[1] = Infinity;
    for (let q = 1; q < len; q++) {
      let s = (f[q] + q * q - (f[v[k]] + v[k] * v[k])) / (2 * q - 2 * v[k]);
      while (s <= z[k]) {
        k--;
        s = (f[q] + q * q - (f[v[k]] + v[k] * v[k])) / (2 * q - 2 * v[k]);
      }
      k++;
      v[k] = q;
      z[k] = s;
      z[k + 1] = Infinity;
    }
    k = 0;
    for (let q = 0; q < len; q++) {
      while (z[k + 1] < q) {
        k++;
      }
      d[q] = (q - v[k]) * (q - v[k]) + f[v[k]];
    }
  };
  for (let x = 0; x < nx; x++) {
    for (let y = 0; y < ny; y++) {
      f[y] = grid[y * nx + x];
    }
    pass(ny);
    for (let y = 0; y < ny; y++) {
      grid[y * nx + x] = d[y];
    }
  }
  const out = new Float32Array(nx * ny);
  for (let y = 0; y < ny; y++) {
    for (let x = 0; x < nx; x++) {
      f[x] = grid[y * nx + x];
    }
    pass(nx);
    for (let x = 0; x < nx; x++) {
      out[y * nx + x] = Math.sqrt(d[x]);
    }
  }
  return out;
}

/** Corner-cutting smoothing of a closed ring. */
function chaikin(ring: Point[], rounds: number): Point[] {
  let pts = ring;
  for (let r = 0; r < rounds; r++) {
    const next: Point[] = [];
    for (let i = 0; i < pts.length; i++) {
      const a = pts[i];
      const b = pts[(i + 1) % pts.length];
      next.push({ x: 0.75 * a.x + 0.25 * b.x, y: 0.75 * a.y + 0.25 * b.y });
      next.push({ x: 0.25 * a.x + 0.75 * b.x, y: 0.25 * a.y + 0.75 * b.y });
    }
    pts = next;
  }
  return pts;
}

interface Grid {
  x0: number;
  y0: number;
  cell: number;
  nx: number;
  ny: number;
}

interface BlockResult {
  grid: Grid;
  /** signed-ish distance field: the bubble is where field <= 0 (grid cells) */
  field: Float32Array;
  shape: BubbleShape | null;
}

const cache = new Map<string, BlockResult>();
const CACHE_SIZE = 256;

/** Field value of a block at a point (bilinear; far outside the grid counts as outside). */
function sampleField(r: BlockResult, x: number, y: number): number {
  const { x0, y0, cell, nx, ny } = r.grid;
  const gx = (x - x0) / cell;
  const gy = (y - y0) / cell;
  if (gx < 0 || gy < 0 || gx > nx - 1 || gy > ny - 1) {
    return FAR;
  }
  const i = Math.min(nx - 2, Math.floor(gx));
  const j = Math.min(ny - 2, Math.floor(gy));
  const fx = gx - i;
  const fy = gy - j;
  const f = r.field;
  const k = j * nx + i;
  const top = f[k] * (1 - fx) + f[k + 1] * fx;
  const bottom = f[k + nx] * (1 - fx) + f[k + nx + 1] * fx;
  // the parent field is in its own cell units
  return (top * (1 - fy) + bottom * fy) * cell;
}

/** Distance from a point to a rect (0 inside). */
function rectDistance(r: Rect, p: Point): number {
  return Math.hypot(Math.max(0, r.x - p.x, p.x - (r.x + r.w)), Math.max(0, r.y - p.y, p.y - (r.y + r.h)));
}

/**
 * Bridge shallow dents of a closed outline with straight lines. A bridge
 * from point i to j is taken when every point in between lies inside the
 * bubble side of the line (so the bridge only adds area) and no deeper than
 * DENT_DEPTH, and when `allowed` accepts points along it. The farthest
 * valid j is taken, which makes the bridge tangent to the bulges at both
 * ends.
 */
function straighten(dense: Point[], allowed: (p: Point) => boolean): Point[] {
  // thin the outline (bounds the search below)
  const spacing = 8;
  const ring: Point[] = [];
  for (const p of dense) {
    const last = ring[ring.length - 1];
    if (!last || Math.hypot(p.x - last.x, p.y - last.y) >= spacing) {
      ring.push(p);
    }
  }
  const n = ring.length;
  if (n < 8) {
    return dense;
  }
  let area = 0;
  for (let i = 0; i < n; i++) {
    const a = ring[i];
    const b = ring[(i + 1) % n];
    area += a.x * b.y - b.x * a.y;
  }
  const inside = Math.sign(area); // interior lies on this side of the travel direction
  // start at a point that is not inside a dent: the one farthest from the centre
  const cx = ring.reduce((t, p) => t + p.x, 0) / n;
  const cy = ring.reduce((t, p) => t + p.y, 0) / n;
  let first = 0;
  ring.forEach((p, i) => {
    if (Math.hypot(p.x - cx, p.y - cy) > Math.hypot(ring[first].x - cx, ring[first].y - cy)) {
      first = i;
    }
  });
  const at = (k: number) => ring[(first + k) % n];
  const bridgeOk = (i: number, j: number) => {
    const a = at(i);
    const b = at(j);
    const len = Math.hypot(b.x - a.x, b.y - a.y);
    if (len < 1e-6 || len > DENT_SPAN) {
      return false;
    }
    let deepest = 0;
    for (let k = i + 1; k < j; k++) {
      const p = at(k);
      const cross = ((b.x - a.x) * (p.y - a.y) - (b.y - a.y) * (p.x - a.x)) / len;
      if (cross * inside < -0.5) {
        return false; // the line would cut into the bubble
      }
      deepest = Math.max(deepest, Math.abs(cross));
      if (deepest > DENT_DEPTH) {
        return false;
      }
    }
    if (deepest < 1) {
      return true; // already straight
    }
    const steps = Math.ceil(len / 6);
    for (let t = 1; t < steps; t++) {
      if (!allowed({ x: a.x + ((b.x - a.x) * t) / steps, y: a.y + ((b.y - a.y) * t) / steps })) {
        return false;
      }
    }
    return true;
  };
  const out: Point[] = [];
  const maxSteps = Math.min(n - 1, Math.ceil(DENT_SPAN / spacing));
  let i = 0;
  while (i < n) {
    out.push(at(i));
    let best = i + 1;
    for (let j = Math.min(n, i + maxSteps); j > i + 1; j--) {
      if (bridgeOk(i, j)) {
        best = j;
        break;
      }
    }
    i = best;
  }
  return out;
}

function computeBlock(
  block: ScopeBlock,
  rects: Rect[],
  others: Rect[],
  pad: number,
  parent: BlockResult | null
): BlockResult | null {
  const margin = Math.max(...CLOSE_RADII) + 2 * pad + 16;
  const gx0 = Math.min(...rects.map(r => r.x)) - margin;
  const gy0 = Math.min(...rects.map(r => r.y)) - margin;
  const gx1 = Math.max(...rects.map(r => r.x + r.w)) + margin;
  const gy1 = Math.max(...rects.map(r => r.y + r.h)) + margin;
  const cell = Math.max(MIN_CELL, Math.sqrt(((gx1 - gx0) * (gy1 - gy0)) / MAX_CELLS));
  const nx = Math.ceil((gx1 - gx0) / cell) + 1;
  const ny = Math.ceil((gy1 - gy0) / cell) + 1;
  const size = nx * ny;
  const x0 = gx0;
  const y0 = gy0;

  const rectMask = (list: Rect[]) => {
    const mask = new Uint8Array(size);
    for (const r of list) {
      const i0 = Math.max(0, Math.ceil((r.x - x0) / cell));
      const i1 = Math.min(nx - 1, Math.floor((r.x + r.w - x0) / cell));
      const j0 = Math.max(0, Math.ceil((r.y - y0) / cell));
      const j1 = Math.min(ny - 1, Math.floor((r.y + r.h - y0) / cell));
      for (let j = j0; j <= j1; j++) {
        if (i0 <= i1) {
          mask.fill(1, j * nx + i0, j * nx + i1 + 1);
        }
      }
      // tiny rects still get their nearest point
      const ci = Math.round((r.x + r.w / 2 - x0) / cell);
      const cj = Math.round((r.y + r.h / 2 - y0) / cell);
      if (ci >= 0 && ci < nx && cj >= 0 && cj < ny) {
        mask[cj * nx + ci] = 1;
      }
    }
    return mask;
  };
  const threshold = (d: Float32Array, test: (v: number) => boolean) => {
    const out = new Uint8Array(size);
    for (let i = 0; i < size; i++) {
      out[i] = test(d[i]) ? 1 : 0;
    }
    return out;
  };
  const closing = (mask: Uint8Array, radius: number) => {
    const r = radius / cell;
    const grown = threshold(distanceTransform(mask, nx, ny), v => v <= r);
    const outside = grown.map(v => 1 - v);
    // beyond the grid counts as outside
    outside.fill(1, 0, nx);
    outside.fill(1, size - nx);
    for (let j = 0; j < ny; j++) {
      outside[j * nx] = outside[j * nx + nx - 1] = 1;
    }
    return threshold(distanceTransform(outside, nx, ny), v => v > r).map((v, i) => v | mask[i]);
  };
  /** do all member points lie in one connected part of {field <= 0}? */
  const connected = (field: Float32Array, members: Uint8Array) => {
    const start = members.findIndex((m, i) => m === 1 && field[i] <= 0);
    if (start < 0) {
      return true;
    }
    const seen = new Uint8Array(size);
    const todo = [start];
    seen[start] = 1;
    while (todo.length) {
      const i = todo.pop()!;
      const x = i % nx;
      for (const k of [x > 0 ? i - 1 : -1, x < nx - 1 ? i + 1 : -1, i - nx, i + nx]) {
        if (k >= 0 && k < size && !seen[k] && field[k] <= 0) {
          seen[k] = 1;
          todo.push(k);
        }
      }
    }
    return members.every((m, i) => !m || seen[i] === 1);
  };

  // nodes inset by the padding, grown by twice the padding at the end
  const inset = rects.map(r => {
    const c = Math.min(pad, r.w / 2, r.h / 2);
    return { x: r.x + c, y: r.y + c, w: r.w - 2 * c, h: r.h - 2 * c };
  });
  const members = rectMask(inset);
  const grow = (2 * pad) / cell;
  const carve = others.length
    ? threshold(distanceTransform(rectMask(others), nx, ny), v => v <= (2 * pad + CLEARANCE + CARVE_ROUNDING) / cell)
    : null;
  const hull = convexHull(
    inset.flatMap(r => [
      { x: r.x, y: r.y },
      { x: r.x + r.w, y: r.y },
      { x: r.x + r.w, y: r.y + r.h },
      { x: r.x, y: r.y + r.h }
    ])
  );
  const hullMask = () => {
    const mask = new Uint8Array(size);
    for (let j = 0; j < ny; j++) {
      for (let i = 0; i < nx; i++) {
        mask[j * nx + i] = inConvex(hull, x0 + i * cell, y0 + j * cell) ? 1 : 0;
      }
    }
    return mask.map((v, i) => v | members[i]);
  };
  const candidates: (() => Uint8Array)[] = [...CLOSE_RADII.map(r => () => closing(members, r)), hullMask];
  const bases = new Map<number, Uint8Array>();
  const base = (k: number) => bases.get(k) ?? bases.set(k, candidates[k]()).get(k)!;
  let field: Float32Array | null = null;
  let carved = false;
  for (const withCarving of carve ? [true, false] : [false]) {
    for (let k = 0; k < candidates.length && !field; k++) {
      let core = base(k);
      const carves = withCarving && core.some((v, i) => v === 1 && carve![i] === 1 && members[i] === 0);
      if (carves) {
        core = closing(
          core.map((v, i) => (v & (1 - carve![i])) | members[i]),
          CARVE_ROUNDING
        );
      }
      const candidate = distanceTransform(core, nx, ny).map(v => v - grow);
      if (connected(candidate, members)) {
        field = candidate;
        carved = carves;
      }
    }
    if (field) {
      break;
    }
  }
  if (!field) {
    return null;
  }

  // nested: stay inside the parent with a gap
  if (parent) {
    const gap = NEST_GAP / cell;
    for (let j = 0; j < ny; j++) {
      for (let i = 0; i < nx; i++) {
        const k = j * nx + i;
        field[k] = Math.max(field[k], sampleField(parent, x0 + i * cell, y0 + j * cell) / cell + gap);
      }
    }
  }
  const grid = { x0, y0, cell, nx, ny };

  // straight bridges must keep clear of other nodes and stay inside the parent
  const allowed = (p: Point) =>
    (!carved || others.every(r => rectDistance(r, p) >= CLEARANCE)) &&
    (!parent || sampleField(parent, p.x, p.y) <= -NEST_GAP);
  const [multi] = contours()
    .size([nx, ny])
    .thresholds([0])(Array.from(field, v => -v));
  const rings = multi.coordinates
    .flatMap(polygon =>
      polygon.map((ring, h) => {
        const pts = ring.slice(0, -1).map(([gx, gy]) => ({ x: x0 + (gx - 0.5) * cell, y: y0 + (gy - 0.5) * cell }));
        // outer rings only: bridging a hole's dents would grow the hole's side
        return chaikin(h === 0 ? straighten(pts, allowed) : pts, 2);
      })
    )
    .filter(ring => ring.length >= 3);
  if (!rings.length) {
    return { grid, field, shape: null };
  }
  const f = (v: number) => v.toFixed(1);
  const path = rings.map(ring => ring.map((p, i) => `${i ? 'L' : 'M'}${f(p.x)},${f(p.y)}`).join('') + 'Z').join('');
  const pts = rings.flat();
  const bx = Math.min(...pts.map(p => p.x));
  const by = Math.min(...pts.map(p => p.y));
  const box = { x: bx, y: by, w: Math.max(...pts.map(p => p.x)) - bx, h: Math.max(...pts.map(p => p.y)) - by };
  const start = rects[block.members.indexOf(block.start)] ?? rects[0];
  return { grid, field, shape: { path, box, label: { x: start.x + 2, y: start.y - pad * 0.85 } } };
}

/**
 * Smooth bubbles around blocks, computed on a distance-field grid:
 *
 * 1. the member nodes (inset by the padding) are morphologically closed:
 *    gaps between nearby nodes are filled, so compact groups look like a
 *    squircle, while large blank areas remain smooth concave bays;
 * 2. nodes that do not belong to the block are carved out with a clearance,
 *    and the carved corners rounded;
 * 3. the bubble must stay in one piece: otherwise a larger closing radius
 *    is tried, then the convex hull, and only then are the other nodes
 *    left inside;
 * 4. the result is grown by twice the padding (round, squircle-like
 *    corners); a nested block is clipped to its parent's field minus a gap,
 *    so it can never poke out of the parent;
 * 5. the outline is traced with marching squares, shallow dents are bridged
 *    with straight lines, and the result is smoothed.
 *
 * Each block has its own grid; results are cached by their inputs, so while
 * dragging only the bubbles that are affected get recomputed.
 */
export function bubbleShapes(blocks: ScopeBlock[], nodeRects: Map<number, Rect>): (BubbleShape | null)[] {
  const results = new Map<ScopeBlock, { key: string; result: BlockResult | null }>();
  const order = blocks.map((b, i) => i).sort((a, b) => blocks[b].depth - blocks[a].depth || b - a); // outer first
  const shapes: (BubbleShape | null)[] = blocks.map(() => null);
  const round = (r: Rect) => `${Math.round(r.x)},${Math.round(r.y)},${Math.round(r.w)},${Math.round(r.h)}`;
  for (const bi of order) {
    const block = blocks[bi];
    if (block.members.some(m => !nodeRects.has(m))) {
      continue;
    }
    const rects = block.members.map(m => nodeRects.get(m)!);
    const pad = bubblePadding(block);
    // only nodes close enough to carve into the block's area matter
    const reach = 2 * pad + CLEARANCE + CARVE_ROUNDING + MIN_CELL;
    const bx0 = Math.min(...rects.map(r => r.x)) - reach;
    const by0 = Math.min(...rects.map(r => r.y)) - reach;
    const bx1 = Math.max(...rects.map(r => r.x + r.w)) + reach;
    const by1 = Math.max(...rects.map(r => r.y + r.h)) + reach;
    const memberSet = new Set(block.members);
    // other nodes nearby; one lying on top of a member cannot be kept out
    const overlaps = (a: Rect, b: Rect, d: number) =>
      a.x < b.x + b.w + d && a.x + a.w + d > b.x && a.y < b.y + b.h + d && a.y + a.h + d > b.y;
    const others = [...nodeRects]
      .filter(([id, r]) => !memberSet.has(id) && r.x < bx1 && r.x + r.w > bx0 && r.y < by1 && r.y + r.h > by0)
      .map(([, r]) => r)
      .filter(r => !rects.some(m => overlaps(r, m, CLEARANCE)));
    // nested: the smallest enclosing block (computed before)
    const parent = blocks
      .filter(o => results.has(o) && encloses(blocks, o, block))
      .sort((a, b) => a.depth - b.depth)[0];
    const parentEntry = parent ? results.get(parent)! : null;
    const key = [pad, rects.map(round).join(';'), others.map(round).sort().join(';'), parentEntry?.key ?? ''].join('|');
    let result = cache.get(key);
    if (result === undefined) {
      result = computeBlock(block, rects, others, pad, parentEntry?.result ?? null) ?? undefined;
      if (result) {
        if (cache.size >= CACHE_SIZE) {
          cache.delete(cache.keys().next().value!);
        }
        cache.set(key, result);
      }
    }
    results.set(block, { key, result: result ?? null });
    shapes[bi] = result?.shape ?? null;
  }
  return shapes;
}
