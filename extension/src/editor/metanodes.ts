import { ConnectionInfo, MetanodeInfo, NodeInfo, NodeState, PortInfo, WorkflowView } from '../types';

/**
 * Metanodes in the editor. The workflow graph is flat: nodes inside a
 * metanode carry `parent`, and every metanode port is a hidden pass-through
 * node. The editor shows one level at a time - the workflow or the inside
 * of a metanode - as a view of its own: metanodes become nodes with ports,
 * and inside a metanode its ports appear on an input and an output bar.
 * Connection ends are translated back with `flatEnd`.
 */
export const IN_BAR = -1;
export const OUT_BAR = -2;
export const METANODE_FACTORY = 'verknode.metanode';

const port = (raw: Map<number, NodeInfo>, ids: number[], prefix: string): PortInfo[] =>
  ids.map((b, k) => ({
    index: k + 1,
    type: raw.get(b)?.inPorts.find(p => p.index === 1)?.type ?? 'table',
    name: `${prefix} ${k + 1}`,
    description: '',
    optional: false,
    implicit: false
  }));

/** Level (metanode id or null) that the outputs of a node feed into. */
function levelOut(raw: Map<number, NodeInfo>, metas: Map<number, MetanodeInfo>, id: number): number | null {
  const b = raw.get(id)?.boundary;
  if (!b) {
    return raw.get(id)?.parent ?? null;
  }
  return b.side === 'in' ? b.metanode : (metas.get(b.metanode)?.parent ?? null);
}

/** Metanode ids inside `id` (all depths) and `id` itself. */
export function metanodeTree(view: WorkflowView, id: number): Set<number> {
  const out = new Set([id]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const m of view.metanodes ?? []) {
      if (m.parent !== null && out.has(m.parent) && !out.has(m.id)) {
        out.add(m.id);
        grew = true;
      }
    }
  }
  return out;
}

/** Nodes inside a metanode (all depths, without its port nodes). */
export function innerNodes(view: WorkflowView, id: number): NodeInfo[] {
  const tree = metanodeTree(view, id);
  return view.nodes.filter(n => !n.boundary && n.parent != null && tree.has(n.parent));
}

/** Summary state of a metanode: green only when everything inside is executed. */
function metanodeState(nodes: NodeInfo[]): NodeState {
  const states = new Set(nodes.map(n => n.state));
  for (const s of ['EXECUTING', 'QUEUED', 'IDLE'] as NodeState[]) {
    if (states.has(s)) {
      return s;
    }
  }
  return nodes.length && states.size === 1 && states.has('EXECUTED') ? 'EXECUTED' : 'CONFIGURED';
}

function metanodeInfo(view: WorkflowView, raw: Map<number, NodeInfo>, m: MetanodeInfo): NodeInfo {
  const inner = innerNodes(view, m.id);
  const failed = inner.filter(n => n.message?.level === 'ERROR').length;
  return {
    id: m.id,
    factory: METANODE_FACTORY,
    name: m.name,
    nodeType: 'Metanode',
    icon: '⊞',
    category: '',
    x: m.x,
    y: m.y,
    annotation: '',
    state: metanodeState(inner),
    inactive: false,
    inactivePorts: m.outputs.map((b, k) => (raw.get(b)?.inactivePorts.includes(1) ? k + 1 : -1)).filter(i => i > 0),
    message: failed ? { level: 'ERROR', text: `${failed} node${failed > 1 ? 's' : ''} inside failed` } : null,
    inPorts: port(raw, m.inputs, 'In'),
    outPorts: port(raw, m.outputs, 'Out'),
    hasDialog: false,
    hasView: false,
    viewAvailable: false,
    loopRole: null,
    usesVariables: false,
    parent: m.parent,
    kind: 'metanode'
  };
}

/** Bars with the ports of the metanode whose inside is shown. */
function bars(raw: Map<number, NodeInfo>, m: MetanodeInfo, shown: NodeInfo[]): NodeInfo[] {
  const xs = shown.map(n => n.x);
  const ys = shown.map(n => n.y);
  const left = xs.length ? Math.min(...xs) - 170 : 0;
  const right = xs.length ? Math.max(...xs) + 190 : 400;
  const top = ys.length ? Math.min(...ys) : 0;
  const base = {
    factory: '',
    nodeType: 'MetanodeBar',
    icon: '',
    category: '',
    y: top,
    annotation: '',
    state: 'CONFIGURED' as NodeState,
    inactive: false,
    inactivePorts: [],
    message: null,
    hasDialog: false,
    hasView: false,
    viewAvailable: false,
    loopRole: null,
    usesVariables: false,
    parent: m.id
  };
  return [
    { ...base, id: IN_BAR, name: 'In', x: left, inPorts: [], outPorts: port(raw, m.inputs, 'In'), kind: 'inbar' },
    { ...base, id: OUT_BAR, name: 'Out', x: right, inPorts: port(raw, m.outputs, 'Out'), outPorts: [], kind: 'outbar' }
  ];
}

/** The part of the workflow at one level, as a view the editor can show unchanged. */
export function projectLevel(view: WorkflowView, level: number | null): WorkflowView {
  const raw = new Map(view.nodes.map(n => [n.id, n]));
  const metas = new Map((view.metanodes ?? []).map(m => [m.id, m]));
  const here = (parent: number | null | undefined) => (parent ?? null) === level;
  const nodes = view.nodes.filter(n => !n.boundary && here(n.parent));
  nodes.push(...(view.metanodes ?? []).filter(m => here(m.parent)).map(m => metanodeInfo(view, raw, m)));
  const current = level !== null ? metas.get(level) : undefined;
  if (current) {
    nodes.push(...bars(raw, current, nodes));
  }
  const connections: ConnectionInfo[] = [];
  for (const c of view.connections) {
    if (levelOut(raw, metas, c.sourceID) !== level) {
      continue;
    }
    const s = raw.get(c.sourceID)?.boundary;
    const d = raw.get(c.destID)?.boundary;
    connections.push({
      ...c,
      sourceID: s ? (s.side === 'out' ? s.metanode : IN_BAR) : c.sourceID,
      sourcePort: s ? s.index + 1 : c.sourcePort,
      destID: d ? (d.side === 'in' ? d.metanode : OUT_BAR) : c.destID,
      destPort: d ? d.index + 1 : c.destPort,
      flat: c
    });
  }
  return { ...view, nodes, connections };
}

/** Workflow node and port behind a node/port of a level view (metanode ports, bars). */
export function flatEnd(
  view: WorkflowView,
  level: number | null,
  id: number,
  portIndex: number,
  dir: 'in' | 'out'
): { id: number; port: number } {
  const metas = new Map((view.metanodes ?? []).map(m => [m.id, m]));
  const m = id === IN_BAR || id === OUT_BAR ? (level !== null ? metas.get(level) : undefined) : metas.get(id);
  if (!m) {
    return { id, port: portIndex };
  }
  // inside, the input bar is a source of the metanode's inputs; outside, the metanode's
  // input ports are targets - both are the same pass-through nodes
  const ports = id === IN_BAR || (id !== OUT_BAR && dir === 'in') ? m.inputs : m.outputs;
  const b = ports[portIndex - 1];
  return b === undefined ? { id, port: portIndex } : { id: b, port: 1 };
}

/** Path from the workflow to a level: [null, outer metanode, ..., level]. */
export function levelPath(view: WorkflowView, level: number | null): (MetanodeInfo | null)[] {
  const metas = new Map((view.metanodes ?? []).map(m => [m.id, m]));
  const path: (MetanodeInfo | null)[] = [];
  let m = level !== null ? metas.get(level) : undefined;
  while (m) {
    path.unshift(m);
    m = m.parent !== null ? metas.get(m.parent) : undefined;
  }
  return [null, ...path];
}
