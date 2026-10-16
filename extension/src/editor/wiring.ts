import { ConnectionInfo, NodeInfo, PortInfo } from '../types';

/**
 * Connecting several nodes at once:
 *
 * - planAutoConnect (Tab with several nodes selected): joins nearby
 *   selected nodes left to right through free, compatible ports;
 * - planInsert (a node or connected group dropped onto a connection):
 *   splits the connection and routes it through the dropped nodes.
 */
export interface Link {
  sourceID: number;
  sourcePort: number;
  destID: number;
  destPort: number;
}

/** connections farther apart than this (px) are not made automatically */
const MAX_DISTANCE = 700;

/** Approximate position of a port (ports spread over the 40px icon, which starts ~16px below the node top). */
function portPoint(n: NodeInfo, p: PortInfo, side: 'in' | 'out'): { x: number; y: number } {
  const ports = (side === 'in' ? n.inPorts : n.outPorts).filter(q => !q.implicit);
  const i = ports.findIndex(q => q.index === p.index);
  const frac = p.implicit ? 0 : (i + 1) / (ports.length + 1);
  return { x: n.x + (side === 'in' ? 28 : 68), y: n.y + 16 + frac * 40 };
}

const portTaken = (connections: { destID: number; destPort: number }[], id: number, port: number) =>
  connections.some(c => c.destID === id && c.destPort === port);

/** Does `to` follow `from` (existing or planned connections)? */
function reaches(links: { sourceID: number; destID: number }[], from: number, to: number): boolean {
  const seen = new Set<number>();
  const todo = [from];
  while (todo.length) {
    const n = todo.pop()!;
    if (n === to) {
      return true;
    }
    if (!seen.has(n)) {
      seen.add(n);
      todo.push(...links.filter(l => l.sourceID === n).map(l => l.destID));
    }
  }
  return false;
}

/**
 * Connections that join the selected nodes: from left to right, each free
 * input port is fed by the closest compatible output of a selected node on
 * its left. Nodes that already follow each other are not joined again, so
 * a row of nodes becomes a chain and a node feeding two nodes on its right
 * becomes a fork.
 */
export function planAutoConnect(selected: NodeInfo[], connections: ConnectionInfo[]): Link[] {
  const candidates: (Link & { cost: number })[] = [];
  for (const a of selected) {
    for (const b of selected) {
      if (a === b || b.x <= a.x + 20) {
        continue;
      }
      for (const o of a.outPorts.filter(p => !p.implicit)) {
        for (const i of b.inPorts.filter(p => !p.implicit && p.type === o.type && !portTaken(connections, b.id, p.index))) {
          const from = portPoint(a, o, 'out');
          const to = portPoint(b, i, 'in');
          const cost = Math.hypot(to.x - from.x, (to.y - from.y) * 1.5);
          if (cost <= MAX_DISTANCE) {
            candidates.push({ sourceID: a.id, sourcePort: o.index, destID: b.id, destPort: i.index, cost });
          }
        }
      }
    }
  }
  candidates.sort((x, y) => x.cost - y.cost);
  const links: Link[] = [];
  const all = () => [...connections, ...links];
  for (const c of candidates) {
    if (portTaken(all(), c.destID, c.destPort) || reaches(all(), c.sourceID, c.destID) || reaches(all(), c.destID, c.sourceID)) {
      continue;
    }
    links.push({ sourceID: c.sourceID, sourcePort: c.sourcePort, destID: c.destID, destPort: c.destPort });
  }
  return links;
}

/** Port of a node for a connection of `type` (implicit flow variable ports only for flow variables). */
function portFor(ports: PortInfo[], type: string, free: (p: PortInfo) => boolean): PortInfo | undefined {
  const fits = ports.filter(p => p.type === type && free(p));
  return fits.find(p => !p.implicit) ?? (type === 'flowvariable' ? fits.find(p => p.implicit) : undefined);
}

/**
 * Connections that put the dragged nodes into `edge`: its source feeds the
 * first dragged node (no input from the other dragged nodes, leftmost) and
 * the last one (no output to the others, rightmost) feeds its target.
 * Null when the nodes have no matching free input / output.
 */
export function planInsert(dragged: NodeInfo[], edge: ConnectionInfo, connections: ConnectionInfo[]): Link[] | null {
  const ids = new Set(dragged.map(n => n.id));
  if (ids.has(edge.sourceID) || ids.has(edge.destID)) {
    return null;
  }
  const internal = connections.filter(c => ids.has(c.sourceID) && ids.has(c.destID));
  const inPort = (n: NodeInfo) => portFor(n.inPorts, edge.type, p => !portTaken(connections, n.id, p.index));
  const outPort = (n: NodeInfo) => portFor(n.outPorts, edge.type, () => true);
  const entry = dragged
    .filter(n => inPort(n) && !internal.some(c => c.destID === n.id))
    .sort((a, b) => a.x - b.x)[0];
  const exit = dragged
    .filter(n => outPort(n) && !internal.some(c => c.sourceID === n.id))
    .sort((a, b) => b.x - a.x)[0];
  if (!entry || !exit || (entry !== exit && !reaches(internal, entry.id, exit.id))) {
    return null;
  }
  return [
    { sourceID: edge.sourceID, sourcePort: edge.sourcePort, destID: entry.id, destPort: inPort(entry)!.index },
    { sourceID: exit.id, sourcePort: outPort(exit)!.index, destID: edge.destID, destPort: edge.destPort }
  ];
}

/**
 * Connections that close the gap left by deleting nodes (Shift+Del): every
 * connection that left the deleted nodes is fed by the connection that came
 * into them on the way to it, with the same port type - preferably into the
 * node it left from, then into the first input port.
 */
export function planBridge(deleted: Set<number>, connections: ConnectionInfo[]): Link[] {
  const inside = connections.filter(c => deleted.has(c.sourceID) && deleted.has(c.destID));
  const incoming = connections.filter(c => !deleted.has(c.sourceID) && deleted.has(c.destID));
  const outgoing = connections.filter(c => deleted.has(c.sourceID) && !deleted.has(c.destID));
  const links: Link[] = [];
  for (const o of outgoing) {
    const feed = incoming
      .filter(i => i.type === o.type && (i.destID === o.sourceID || reaches(inside, i.destID, o.sourceID)))
      .sort((a, b) => Number(a.destID !== o.sourceID) - Number(b.destID !== o.sourceID) || a.destPort - b.destPort)[0];
    if (feed && feed.sourceID !== o.destID) {
      links.push({ sourceID: feed.sourceID, sourcePort: feed.sourcePort, destID: o.destID, destPort: o.destPort });
    }
  }
  return links;
}
