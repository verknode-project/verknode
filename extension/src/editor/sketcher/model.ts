/**
 * Molecule model of the built-in sketcher, with fixed geometry:
 * a fixed bond length, new bonds at 120° to existing ones (zigzag chains),
 * regular rings fused onto atoms and bonds, and implicit hydrogens on
 * heteroatoms. Coordinates are screen pixels (y down); molfiles use Å
 * (bond length 1.5, y up).
 */
export interface Atom {
  id: number;
  x: number;
  y: number;
  element: string;
  charge: number;
  isotope?: number | null;
}

export interface Bond {
  id: number;
  /** for wedge / hash bonds: the stereocentre (narrow end) */
  a: number;
  b: number;
  order: 1 | 2 | 3;
  stereo: 'wedge' | 'hash' | null;
}

export interface Mol {
  atoms: Atom[];
  bonds: Bond[];
  nextId: number;
}

export interface Point {
  x: number;
  y: number;
}

/** bond length (px) */
export const BOND = 32;
const ANGSTROM_PER_PX = 1.5 / BOND;
/** distance (px) within which a point hits an atom / joins an existing atom */
export const ATOM_HIT = 9;
export const BOND_HIT = 6;

export const emptyMol = (): Mol => ({ atoms: [], bonds: [], nextId: 1 });
export const cloneMol = (m: Mol): Mol => ({
  atoms: m.atoms.map(a => ({ ...a })),
  bonds: m.bonds.map(b => ({ ...b })),
  nextId: m.nextId
});

// ------------------------------------------------------------------ queries
export const atomById = (m: Mol, id: number) => m.atoms.find(a => a.id === id);
export const bondsOf = (m: Mol, id: number) => m.bonds.filter(b => b.a === id || b.b === id);
export const otherEnd = (b: Bond, id: number) => (b.a === id ? b.b : b.a);
export const neighbors = (m: Mol, id: number): Atom[] => bondsOf(m, id).map(b => atomById(m, otherEnd(b, id))!);
export const bondBetween = (m: Mol, a: number, b: number) =>
  m.bonds.find(x => (x.a === a && x.b === b) || (x.a === b && x.b === a));
export const dist = (p: Point, q: Point) => Math.hypot(p.x - q.x, p.y - q.y);

export function atomAt(m: Mol, p: Point, radius = ATOM_HIT, except?: Set<number>): Atom | undefined {
  let best: Atom | undefined;
  let bestD = radius;
  for (const a of m.atoms) {
    const d = dist(a, p);
    if (d <= bestD && !except?.has(a.id)) {
      best = a;
      bestD = d;
    }
  }
  return best;
}

export function bondAt(m: Mol, p: Point, radius = BOND_HIT): Bond | undefined {
  let best: Bond | undefined;
  let bestD = radius;
  for (const b of m.bonds) {
    const s = atomById(m, b.a)!;
    const e = atomById(m, b.b)!;
    const len2 = (e.x - s.x) ** 2 + (e.y - s.y) ** 2 || 1;
    const t = Math.max(0, Math.min(1, ((p.x - s.x) * (e.x - s.x) + (p.y - s.y) * (e.y - s.y)) / len2));
    if (t < 0.15 || t > 0.85) {
      continue; // near the ends the atoms win
    }
    const d = dist(p, { x: s.x + t * (e.x - s.x), y: s.y + t * (e.y - s.y) });
    if (d <= bestD) {
      best = b;
      bestD = d;
    }
  }
  return best;
}

// ------------------------------------------------------------------ valence
const VALENCES: Record<string, number[]> = {
  H: [1],
  B: [3],
  C: [4],
  N: [3, 5],
  O: [2],
  F: [1],
  Si: [4],
  P: [3, 5],
  S: [2, 4, 6],
  Cl: [1],
  Se: [2, 4, 6],
  Br: [1],
  I: [1, 3, 5]
};

/** Hydrogens implied by the usual valences (organic subset only). */
export function implicitH(m: Mol, a: Atom): number {
  const vals = VALENCES[a.element];
  if (!vals) {
    return 0;
  }
  const used = bondsOf(m, a.id).reduce((s, b) => s + b.order, 0);
  // charges: C+/C- and B- change the valence like carbon/boron, the others like N+ (4) or O- (1)
  const shift = a.element === 'C' ? -Math.abs(a.charge) : a.element === 'B' ? -a.charge : a.charge;
  const fit = vals.map(v => v + shift).find(v => v >= used);
  return fit === undefined ? 0 : fit - used;
}

/** Atoms are labelled unless they are plain carbons with bonds. */
export const showLabel = (m: Mol, a: Atom) =>
  a.element !== 'C' || a.charge !== 0 || !!a.isotope || bondsOf(m, a.id).length === 0;

// ------------------------------------------------------------------ geometry
const norm = (t: number) => Math.atan2(Math.sin(t), Math.cos(t));
const angleTo = (p: Point, q: Point) => Math.atan2(q.y - p.y, q.x - p.x);
export const polar = (p: Point, angle: number, length = BOND): Point => ({
  x: p.x + Math.cos(angle) * length,
  y: p.y + Math.sin(angle) * length
});
const DEG = Math.PI / 180;

/** Snap an angle to multiples of 15°. */
export const snapAngle = (t: number, step = 15 * DEG) => Math.round(t / step) * step;

/**
 * Direction for a new bond from an atom: up-right for a lone atom, 120°
 * from a single neighbour (continuing a zigzag, away from other atoms),
 * else the middle of the largest free angle.
 */
export function freeDirection(m: Mol, id: number): number {
  const a = atomById(m, id)!;
  const ns = neighbors(m, id);
  if (!ns.length) {
    return -30 * DEG;
  }
  if (ns.length === 1) {
    const base = angleTo(a, ns[0]);
    const options = [base + 120 * DEG, base - 120 * DEG];
    // zigzag: trans to the neighbour's own other neighbour; otherwise keep away from atoms
    const score = (t: number) => {
      const p = polar(a, t);
      const others = m.atoms.filter(x => x.id !== id);
      return Math.min(...others.map(x => dist(p, x)), 1e9) + Math.cos(t) * 0.01 - Math.sin(t) * 0.001;
    };
    return norm(score(options[0]) >= score(options[1]) ? options[0] : options[1]);
  }
  const angles = ns.map(n => angleTo(a, n)).sort((x, y) => x - y);
  let best = 0;
  let bestGap = -1;
  for (let i = 0; i < angles.length; i++) {
    const from = angles[i];
    const to = i + 1 < angles.length ? angles[i + 1] : angles[0] + 2 * Math.PI;
    if (to - from > bestGap) {
      bestGap = to - from;
      best = from + (to - from) / 2;
    }
  }
  return norm(best);
}

// ------------------------------------------------------------------ editing (in place on a clone)
export function addAtom(m: Mol, p: Point, element = 'C'): number {
  const id = m.nextId++;
  m.atoms.push({ id, x: p.x, y: p.y, element, charge: 0 });
  return id;
}

/** Atom at `p`, or a new carbon there. */
export function atomOrNew(m: Mol, p: Point, except?: Set<number>): number {
  return atomAt(m, p, ATOM_HIT, except)?.id ?? addAtom(m, p);
}

export function addBond(m: Mol, a: number, b: number, order: 1 | 2 | 3 = 1, stereo: Bond['stereo'] = null): number {
  const existing = bondBetween(m, a, b);
  if (existing) {
    return existing.id;
  }
  const id = m.nextId++;
  m.bonds.push({ id, a, b, order, stereo });
  return id;
}

/** New bond from an atom in its free direction (to an existing atom if one is there). */
export function growBond(m: Mol, from: number, order: 1 | 2 | 3 = 1, stereo: Bond['stereo'] = null): number {
  const a = atomById(m, from)!;
  const to = atomOrNew(m, polar(a, freeDirection(m, from)), new Set([from]));
  addBond(m, from, to, order, stereo);
  return to;
}

export function deleteAtom(m: Mol, id: number) {
  m.atoms = m.atoms.filter(a => a.id !== id);
  m.bonds = m.bonds.filter(b => b.a !== id && b.b !== id);
}

/** Delete a bond; carbons left without bonds go too. */
export function deleteBond(m: Mol, id: number) {
  const b = m.bonds.find(x => x.id === id);
  if (!b) {
    return;
  }
  m.bonds = m.bonds.filter(x => x.id !== id);
  for (const end of [b.a, b.b]) {
    const a = atomById(m, end);
    if (a && a.element === 'C' && !a.charge && !bondsOf(m, end).length) {
      deleteAtom(m, end);
    }
  }
}

/** Click on a bond with a plain bond tool: single → double → triple → single. */
export function cycleBond(m: Mol, id: number) {
  const b = m.bonds.find(x => x.id === id)!;
  b.order = b.order === 1 ? 2 : b.order === 2 ? 3 : 1;
  b.stereo = null;
}

/** Wedge / hash a bond; again on the same kind flips its direction. */
export function setStereo(m: Mol, id: number, stereo: 'wedge' | 'hash') {
  const b = m.bonds.find(x => x.id === id)!;
  if (b.stereo === stereo) {
    [b.a, b.b] = [b.b, b.a];
  } else if (!b.stereo && bondsOf(m, b.b).length > bondsOf(m, b.a).length) {
    // narrow end at the likely stereocentre (the more substituted atom)
    [b.a, b.b] = [b.b, b.a];
  }
  b.order = 1;
  b.stereo = stereo;
}

/**
 * Regular ring of `size` atoms: centred on a point, sharing an atom (in its
 * free direction) or fused onto a bond (on the side with fewer atoms).
 * Aromatic rings alternate double bonds, keeping a shared double bond.
 */
export function addRing(
  m: Mol,
  size: number,
  aromatic: boolean,
  target: { point: Point } | { atom: number } | { bond: number }
) {
  const R = BOND / (2 * Math.sin(Math.PI / size));
  const apothem = BOND / (2 * Math.tan(Math.PI / size));
  let centre: Point;
  let start: Point;
  let shared: number[] = [];
  if ('bond' in target) {
    const b = m.bonds.find(x => x.id === target.bond)!;
    const p = atomById(m, b.a)!;
    const q = atomById(m, b.b)!;
    const mid = { x: (p.x + q.x) / 2, y: (p.y + q.y) / 2 };
    const len = dist(p, q) || 1;
    const nx = -(q.y - p.y) / len;
    const ny = (q.x - p.x) / len;
    // side with fewer neighbouring atoms
    const side = [...neighbors(m, p.id), ...neighbors(m, q.id)]
      .filter(a => a.id !== p.id && a.id !== q.id)
      .reduce((s, a) => s + Math.sign((a.x - mid.x) * nx + (a.y - mid.y) * ny), 0);
    const sgn = side > 0 ? -1 : 1;
    centre = { x: mid.x + nx * apothem * sgn, y: mid.y + ny * apothem * sgn };
    start = p;
    shared = [p.id, q.id];
  } else if ('atom' in target) {
    const a = atomById(m, target.atom)!;
    centre = polar(a, freeDirection(m, a.id), R);
    start = a;
    shared = [a.id];
  } else {
    centre = target.point;
    start = { x: centre.x, y: centre.y - R }; // pointy top
  }
  const a0 = angleTo(centre, start);
  // go round so that a shared bond's second atom comes next
  let step = (2 * Math.PI) / size;
  if (shared.length === 2) {
    const second = atomById(m, shared[1])!;
    const next = polar(centre, a0 + step, R);
    if (dist(next, second) > 1) {
      step = -step;
    }
  }
  const ids: number[] = [];
  for (let i = 0; i < size; i++) {
    const p = polar(centre, a0 + i * step, R);
    ids.push(i < shared.length ? shared[i] : atomOrNew(m, p));
  }
  const ringBonds: Bond[] = [];
  for (let i = 0; i < size; i++) {
    const id = addBond(m, ids[i], ids[(i + 1) % size]);
    ringBonds.push(m.bonds.find(b => b.id === id)!);
  }
  if (aromatic && kekulize(m, ids)) {
    return;
  }
  if (aromatic && size % 2 === 0) {
    // fallback: alternate double bonds; choose the parity that keeps existing double
    // bonds and gives no atom two double bonds
    const conflicts = (parity: number) =>
      ringBonds.reduce((n, b, i) => {
        const dbl = i % 2 === parity;
        if (!dbl) {
          return n + (b.order === 2 ? 5 : 0);
        }
        const extra = [b.a, b.b].some(x => bondsOf(m, x).some(o => o !== b && o.order === 2 && !ringBonds.includes(o)));
        return n + (extra ? 1 : 0);
      }, 0);
    const parity = conflicts(0) <= conflicts(1) ? 0 : 1;
    ringBonds.forEach((b, i) => {
      if (i % 2 === parity) {
        b.order = 2;
        b.stereo = null;
      }
    });
  }
}

/** Bonds whose removal disconnects the molecule (not in any ring), Tarjan's algorithm. */
function bridges(m: Mol): Set<number> {
  const order = new Map<number, number>();
  const low = new Map<number, number>();
  const out = new Set<number>();
  let t = 0;
  const visit = (id: number, via: number | null) => {
    order.set(id, t);
    low.set(id, t++);
    for (const b of bondsOf(m, id)) {
      if (b.id === via) {
        continue;
      }
      const n = otherEnd(b, id);
      if (!order.has(n)) {
        visit(n, b.id);
        low.set(id, Math.min(low.get(id)!, low.get(n)!));
        if (low.get(n)! > order.get(id)!) {
          out.add(b.id);
        }
      } else {
        low.set(id, Math.min(low.get(id)!, order.get(n)!));
      }
    }
  };
  m.atoms.forEach(a => !order.has(a.id) && visit(a.id, null));
  return out;
}

/**
 * Kekulé structure for an aromatic ring and the conjugated ring system it is
 * fused to: double bonds are placed (a perfect matching of the ring carbons)
 * so that every carbon gets exactly one, keeping exocyclic C=O and the like.
 * False when that is impossible (the caller falls back to alternation).
 */
function kekulize(m: Mol, ring: number[]): boolean {
  const bridge = bridges(m);
  const exocyclicDouble = (id: number) => bondsOf(m, id).some(b => b.order === 2 && bridge.has(b.id));
  const eligible = (id: number) => {
    const a = atomById(m, id)!;
    return a.element === 'C' && !a.charge && !exocyclicDouble(id) && bondsOf(m, id).length <= 3;
  };
  const conjugated = (id: number) => ring.includes(id) || bondsOf(m, id).some(b => b.order === 2);
  // the ring system: eligible conjugated atoms joined to the ring by ring bonds
  const system = new Set(ring.filter(eligible));
  const todo = [...system];
  while (todo.length) {
    const id = todo.pop()!;
    for (const b of bondsOf(m, id)) {
      const n = otherEnd(b, id);
      if (!bridge.has(b.id) && !system.has(n) && eligible(n) && conjugated(n)) {
        system.add(n);
        todo.push(n);
      }
    }
  }
  if (system.size % 2) {
    return false;
  }
  const inner = m.bonds.filter(b => !bridge.has(b.id) && system.has(b.a) && system.has(b.b));
  const atoms = [...system].sort((x, y) => x - y);
  const mate = new Map<number, number>();
  let steps = 0;
  const search = (): boolean => {
    if (++steps > 20000) {
      return false;
    }
    const free = atoms.find(a => !mate.has(a));
    if (free === undefined) {
      return true;
    }
    for (const b of inner) {
      if (b.a !== free && b.b !== free) {
        continue;
      }
      const n = otherEnd(b, free);
      if (!mate.has(n)) {
        mate.set(free, n);
        mate.set(n, free);
        if (search()) {
          return true;
        }
        mate.delete(free);
        mate.delete(n);
      }
    }
    return false;
  };
  if (!search()) {
    return false;
  }
  for (const b of inner) {
    b.order = mate.get(b.a) === b.b ? 2 : 1;
    b.stereo = null;
  }
  return true;
}

/**
 * Zigzag chain of `count` bonds from an atom (or a new atom at `start`)
 * along `angle`: bonds alternate ±30° around it, continuing an existing zigzag.
 */
export function addChain(m: Mol, from: number | Point, angle: number, count: number): number[] {
  let id = typeof from === 'number' ? from : atomOrNew(m, from);
  const first = atomById(m, id)!;
  // start on the side that continues the zigzag of an existing bond
  let sign = 1;
  const prev = neighbors(m, id)[0];
  if (prev) {
    const back = angleTo(first, prev);
    sign = Math.cos(back - (angle + 30 * DEG)) > Math.cos(back - (angle - 30 * DEG)) ? -1 : 1;
  }
  const out = [id];
  for (let i = 0; i < count; i++) {
    const a = atomById(m, id)!;
    const next = atomOrNew(m, polar(a, angle + sign * 30 * DEG), new Set([id]));
    addBond(m, id, next);
    out.push(next);
    id = next;
    sign = -sign;
  }
  return out;
}

/** Atoms joined to a set of atoms (connected component). */
export function fragmentOf(m: Mol, id: number): Set<number> {
  const seen = new Set<number>([id]);
  const todo = [id];
  while (todo.length) {
    const x = todo.pop()!;
    for (const n of neighbors(m, x)) {
      if (!seen.has(n.id)) {
        seen.add(n.id);
        todo.push(n.id);
      }
    }
  }
  return seen;
}

// ------------------------------------------------------------------ import / export
export interface StructureJSON {
  atoms: { element: string; x: number; y: number; charge: number; isotope?: number | null }[];
  bonds: { a: number; b: number; order: number; stereo: string | null }[];
}

/** Molecule from the server's structure() JSON (Å, y up), centred on `centre`. */
export function fromStructure(s: StructureJSON, centre: Point): Mol {
  const m = emptyMol();
  if (!s.atoms.length) {
    return m;
  }
  const xs = s.atoms.map(a => a.x);
  const ys = s.atoms.map(a => a.y);
  const cx = (Math.min(...xs) + Math.max(...xs)) / 2;
  const cy = (Math.min(...ys) + Math.max(...ys)) / 2;
  const ids = s.atoms.map(a =>
    addAtom(m, { x: centre.x + (a.x - cx) / ANGSTROM_PER_PX, y: centre.y - (a.y - cy) / ANGSTROM_PER_PX }, a.element)
  );
  s.atoms.forEach((a, i) => {
    const atom = atomById(m, ids[i])!;
    atom.charge = a.charge;
    atom.isotope = a.isotope ?? null;
  });
  for (const b of s.bonds) {
    const order = (b.order >= 3 ? 3 : b.order >= 2 ? 2 : 1) as 1 | 2 | 3;
    const stereo = b.stereo === 'wedge' || b.stereo === 'hash' ? b.stereo : null;
    addBond(m, ids[b.a], ids[b.b], order, stereo);
  }
  return m;
}

const fmt = (v: number, width: number, digits = 4) => v.toFixed(digits).padStart(width);
const int = (v: number, width = 3) => String(v).padStart(width);

/** MDL molfile (V2000) of the molecule. */
export function toMolfile(m: Mol, name = ''): string {
  const index = new Map(m.atoms.map((a, i) => [a.id, i + 1]));
  const lines = [name, '  verknode sketcher 2D', '', `${int(m.atoms.length)}${int(m.bonds.length)}  0  0  0  0  0  0  0  0999 V2000`];
  for (const a of m.atoms) {
    const sym = (a.element + '   ').slice(0, 3);
    lines.push(`${fmt(a.x * ANGSTROM_PER_PX, 10)}${fmt(-a.y * ANGSTROM_PER_PX, 10)}${fmt(0, 10)} ${sym} 0  0  0  0  0  0  0  0  0  0  0  0`);
  }
  for (const b of m.bonds) {
    const stereo = b.stereo === 'wedge' ? 1 : b.stereo === 'hash' ? 6 : 0;
    lines.push(`${int(index.get(b.a)!)}${int(index.get(b.b)!)}${int(b.order)}${int(stereo)}`);
  }
  const charged = m.atoms.filter(a => a.charge);
  for (let i = 0; i < charged.length; i += 8) {
    const part = charged.slice(i, i + 8);
    lines.push(`M  CHG${int(part.length)}${part.map(a => `${int(index.get(a.id)!, 4)}${int(a.charge, 4)}`).join('')}`);
  }
  const labelled = m.atoms.filter(a => a.isotope);
  for (let i = 0; i < labelled.length; i += 8) {
    const part = labelled.slice(i, i + 8);
    lines.push(`M  ISO${int(part.length)}${part.map(a => `${int(index.get(a.id)!, 4)}${int(a.isotope!, 4)}`).join('')}`);
  }
  lines.push('M  END');
  return lines.join('\n') + '\n';
}

/** Common element symbols, for typed labels. */
export const ELEMENTS = new Set(
  'H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pt Au Hg Tl Pb Bi'.split(
    ' '
  )
);

/**
 * Parse a typed atom label: an element optionally followed by H count and a
 * charge ("N", "NH2", "O-", "N+", "Cl", "13C"); null if not understood.
 */
export function parseLabel(text: string): { element: string; charge: number; isotope: number | null } | null {
  const m = /^\s*(\d+)?([A-Z][a-z]?)(?:H\d*)?\s*(\d*)([+-]*)\s*$/.exec(text);
  if (!m || !ELEMENTS.has(m[2])) {
    return null;
  }
  const signs = m[4];
  const magnitude = m[3] ? parseInt(m[3], 10) : signs.length;
  const charge = signs ? (signs[0] === '+' ? 1 : -1) * magnitude : 0;
  return { element: m[2], charge, isotope: m[1] ? parseInt(m[1], 10) : null };
}
