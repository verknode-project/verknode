import ELK, { ElkNode } from 'elkjs/lib/elk.bundled.js';

/**
 * Auto layout and alignment of nodes. Auto layout is ELK's layered layout (left to right): it puts the
 * nodes in columns by their connections, orders each column to cross as few connections as it can, and
 * keeps groups (group annotations, loop / try / switch blocks) together as nested boxes.
 */

type Point = { x: number; y: number };
export type Rect = { x: number; y: number; w: number; h: number };

/** A node as laid out: the box it takes (with its docked canvas widget) and its ports, in canvas coordinates. */
export interface LayoutNode {
  id: number;
  box: Rect;
  /** the node's position (what moves) */
  position: Point;
  /** port centres: inputs on the left, outputs on the right */
  ports: { kind: 'in' | 'out'; index: number; at: Point }[];
}

/** A canvas widget placed by itself (a free one): laid out beside its node, inputs before it, outputs after. */
export interface LayoutWidget {
  node: number;
  box: Rect;
  input: boolean;
}

export interface LayoutLink {
  sourceID: number;
  sourcePort: number;
  destID: number;
  destPort: number;
}

const GAP_COLUMNS = 70; // between columns (connections run there)
const GAP_NODES = 28; // between nodes of a column
const GROUP_PAD = { top: 44, left: 26, bottom: 26, right: 26 }; // (top: room for the bubble's label)

let elk: InstanceType<typeof ELK> | null = null;

/**
 * Groups that can be laid out as boxes: each nested in the other or apart. Of two that partly overlap,
 * the smaller one wins (the other is not kept together).
 */
function nestGroups(groups: number[][], ids: Set<number>) {
  const sets = groups
    .map(g => [...new Set(g.filter(id => ids.has(id)))])
    .filter(g => g.length > 0)
    .sort((a, b) => a.length - b.length);
  const kept: number[][] = [];
  for (const g of sets) {
    const s = new Set(g);
    const fits = kept.every(k => {
      const shared = k.filter(id => s.has(id)).length;
      return shared === 0 || shared === k.length; // apart, or k inside g
    });
    if (fits && !kept.some(k => k.length === g.length && k.every(id => s.has(id)))) {
      kept.push(g);
    }
  }
  // each group's parent: the smallest one around it (kept is smallest first)
  const parent = kept.map((g, i) => {
    const j = kept.findIndex((k, kj) => kj > i && g.every(id => k.includes(id)));
    return j < 0 ? null : j;
  });
  // each node's group: the smallest one it is in
  const home = new Map<number, number>();
  kept.forEach((g, i) => g.forEach(id => home.has(id) || home.set(id, i)));
  return { kept, parent, home };
}

/**
 * New positions for the nodes: laid out from the left, groups kept together, connections crossing as
 * little as possible. The current arrangement decides ties, so a tidy workflow stays as it is. The result
 * starts at `origin` (the top left of the laid out nodes' boxes).
 */
export async function autoLayout(
  nodes: LayoutNode[],
  links: LayoutLink[],
  groups: number[][],
  origin: Point,
  widgets: LayoutWidget[] = []
): Promise<{ nodes: Map<number, Point>; widgets: Map<number, Point> }> {
  elk ??= new ELK();
  const ids = new Set(nodes.map(n => n.id));
  const { kept, parent, home } = nestGroups(groups, ids);
  // (the current order, top to bottom then left to right, is the model order ELK keeps where it can)
  const ordered = [...nodes].sort((a, b) => a.box.x - b.box.x || a.box.y - b.box.y);
  const elkNode = (n: LayoutNode): ElkNode => ({
    id: `n${n.id}`,
    width: n.box.w,
    height: n.box.h,
    layoutOptions: { 'elk.portConstraints': 'FIXED_POS' },
    ports: n.ports.map(p => ({
      id: `n${n.id}:${p.kind}${p.index}`,
      x: Math.max(0, Math.min(n.box.w, p.at.x - n.box.x)),
      y: Math.max(0, Math.min(n.box.h, p.at.y - n.box.y)),
      width: 1,
      height: 1,
      layoutOptions: { 'elk.port.side': p.kind === 'in' ? 'WEST' : 'EAST' }
    }))
  });
  const groupNodes: ElkNode[] = kept.map((_, i) => ({
    id: `g${i}`,
    children: [],
    layoutOptions: {
      'elk.padding': `[top=${GROUP_PAD.top},left=${GROUP_PAD.left},bottom=${GROUP_PAD.bottom},right=${GROUP_PAD.right}]`
    }
  }));
  const root: ElkNode = {
    id: 'root',
    layoutOptions: {
      'elk.algorithm': 'layered',
      'elk.direction': 'RIGHT',
      'elk.hierarchyHandling': 'INCLUDE_CHILDREN',
      'elk.layered.spacing.nodeNodeBetweenLayers': String(GAP_COLUMNS),
      'elk.spacing.nodeNode': String(GAP_NODES),
      'elk.spacing.componentComponent': String(GAP_COLUMNS),
      'elk.layered.crossingMinimization.strategy': 'LAYER_SWEEP',
      'elk.layered.thoroughness': '30',
      'elk.layered.considerModelOrder.strategy': 'NODES_AND_EDGES',
      'elk.layered.nodePlacement.strategy': 'BRANDES_KOEPF',
      'elk.layered.nodePlacement.bk.fixedAlignment': 'BALANCED',
      'elk.layered.cycleBreaking.strategy': 'DEPTH_FIRST'
    },
    children: [],
    edges: []
  };
  for (const n of ordered) {
    const g = home.get(n.id);
    (g === undefined ? root : groupNodes[g]).children!.push(elkNode(n));
  }
  // (a widget is in its node's group, joined to it like a connection)
  const placed = widgets.filter(w => ids.has(w.node));
  for (const w of placed) {
    const g = home.get(w.node);
    (g === undefined ? root : groupNodes[g]).children!.push({ id: `w${w.node}`, width: w.box.w, height: w.box.h });
  }
  groupNodes.forEach((g, i) => {
    const p = parent[i];
    (p === null ? root : groupNodes[p]).children!.push(g);
  });
  // an edge goes in the innermost box holding both its ends
  const chain = (id: number) => {
    const out: (number | null)[] = [];
    for (let g = home.get(id) ?? null; g !== null; g = parent[g]) {
      out.push(g);
    }
    return [...out, null];
  };
  const container = (a: number, b: number) => {
    const cb = chain(b);
    const g = chain(a).find(x => cb.includes(x)) ?? null;
    return g === null ? root : groupNodes[g];
  };
  const hasPort = (id: number, kind: 'in' | 'out', index: number) =>
    nodes.find(n => n.id === id)?.ports.some(p => p.kind === kind && p.index === index);
  for (const [i, c] of links.entries()) {
    if (!ids.has(c.sourceID) || !ids.has(c.destID) || c.sourceID === c.destID) {
      continue;
    }
    const into = container(c.sourceID, c.destID);
    (into.edges ??= []).push({
      id: `e${i}`,
      sources: [hasPort(c.sourceID, 'out', c.sourcePort) ? `n${c.sourceID}:out${c.sourcePort}` : `n${c.sourceID}`],
      targets: [hasPort(c.destID, 'in', c.destPort) ? `n${c.destID}:in${c.destPort}` : `n${c.destID}`]
    });
  }
  for (const w of placed) {
    const g = home.get(w.node);
    ((g === undefined ? root : groupNodes[g]).edges ??= []).push({
      id: `ew${w.node}`,
      sources: [w.input ? `w${w.node}` : `n${w.node}`],
      targets: [w.input ? `n${w.node}` : `w${w.node}`]
    });
  }
  const done = await elk.layout(root);

  // ELK's coordinates are relative to the parent box: add them up, then move it all to the origin
  const boxes = new Map<number, Point>();
  const widgetBoxes = new Map<number, Point>();
  const walk = (n: ElkNode, dx: number, dy: number) => {
    for (const c of n.children ?? []) {
      const x = dx + (c.x ?? 0);
      const y = dy + (c.y ?? 0);
      if (c.id.startsWith('n')) {
        boxes.set(Number(c.id.slice(1)), { x, y });
      } else if (c.id.startsWith('w')) {
        widgetBoxes.set(Number(c.id.slice(1)), { x, y });
      } else {
        walk(c, x, y);
      }
    }
  };
  walk(done, 0, 0);
  const all = [...boxes.values(), ...widgetBoxes.values()];
  const minX = Math.min(...all.map(p => p.x));
  const minY = Math.min(...all.map(p => p.y));
  const result = new Map<number, Point>();
  const widgetResult = new Map<number, Point>();
  for (const w of placed) {
    const b = widgetBoxes.get(w.node);
    if (b) {
      widgetResult.set(w.node, { x: Math.round(origin.x + b.x - minX), y: Math.round(origin.y + b.y - minY) });
    }
  }
  for (const n of nodes) {
    const b = boxes.get(n.id);
    if (b) {
      result.set(n.id, {
        x: Math.round(origin.x + b.x - minX + (n.position.x - n.box.x)),
        y: Math.round(origin.y + b.y - minY + (n.position.y - n.box.y))
      });
    }
  }
  return { nodes: result, widgets: widgetResult };
}

export type Alignment = 'left' | 'center' | 'right' | 'top' | 'middle' | 'bottom' | 'distribute-h' | 'distribute-v';

/**
 * New positions that line up the nodes' icons: their edges or centres on one line (the outermost, or
 * the middle of them all), or spread evenly between the first and the last.
 */
export function align(items: { id: number; icon: Rect; position: Point }[], how: Alignment): Map<number, Point> {
  const result = new Map<number, Point>();
  if (items.length < 2) {
    return result;
  }
  const left = Math.min(...items.map(i => i.icon.x));
  const right = Math.max(...items.map(i => i.icon.x + i.icon.w));
  const top = Math.min(...items.map(i => i.icon.y));
  const bottom = Math.max(...items.map(i => i.icon.y + i.icon.h));
  const move = (i: (typeof items)[number], dx: number, dy: number) =>
    result.set(i.id, { x: Math.round(i.position.x + dx), y: Math.round(i.position.y + dy) });
  if (how === 'distribute-h' || how === 'distribute-v') {
    const h = how === 'distribute-h';
    const centre = (i: (typeof items)[number]) => (h ? i.icon.x + i.icon.w / 2 : i.icon.y + i.icon.h / 2);
    const sorted = [...items].sort((a, b) => centre(a) - centre(b));
    const first = centre(sorted[0]);
    const step = (centre(sorted[sorted.length - 1]) - first) / (sorted.length - 1);
    sorted.forEach((i, k) => {
      const d = first + k * step - centre(i);
      move(i, h ? d : 0, h ? 0 : d);
    });
    return result;
  }
  for (const i of items) {
    switch (how) {
      case 'left':
        move(i, left - i.icon.x, 0);
        break;
      case 'center':
        move(i, (left + right) / 2 - (i.icon.x + i.icon.w / 2), 0);
        break;
      case 'right':
        move(i, right - (i.icon.x + i.icon.w), 0);
        break;
      case 'top':
        move(i, 0, top - i.icon.y);
        break;
      case 'middle':
        move(i, 0, (top + bottom) / 2 - (i.icon.y + i.icon.h / 2));
        break;
      case 'bottom':
        move(i, 0, bottom - (i.icon.y + i.icon.h));
        break;
    }
  }
  return result;
}
