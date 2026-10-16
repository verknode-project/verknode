import { NodeDescription } from '../../types';

/**
 * How well a node matches typed text: exact name > prefix > word start >
 * acronym (e.g. "gb" for GroupBy) > substring > category > letters in order.
 * 0 = no match.
 */
export function matchScore(node: NodeDescription, query: string): number {
  const q = query.trim().toLowerCase();
  if (!q) {
    return 1;
  }
  const name = node.name.toLowerCase();
  if (name === q) {
    return 100;
  }
  if (name.startsWith(q)) {
    return 80;
  }
  const words = name.split(/[\s\-_()/&]+/).filter(Boolean);
  if (words.some(w => w.startsWith(q))) {
    return 65;
  }
  const acronyms = [words.map(w => w[0]).join(''), node.name.replace(/[^A-Z]/g, '').toLowerCase()];
  if (acronyms.some(a => a.startsWith(q))) {
    return 55;
  }
  if (name.includes(q)) {
    return 40;
  }
  if (node.category.toLowerCase().includes(q)) {
    return 20;
  }
  let i = 0;
  for (const ch of name) {
    if (ch === q[i]) {
      i++;
    }
  }
  return i === q.length ? 10 : 0;
}

/** Match score below which a match is "loose" (only shown if nothing better matches). */
export const STRONG_MATCH = 20;

/** Nodes matching a search (same rules as the quick-add), for the node repository. */
export function matchingNodes(nodes: NodeDescription[], query: string): NodeDescription[] {
  if (!query.trim()) {
    return nodes;
  }
  const scored = nodes.map(n => ({ n, s: matchScore(n, query) })).filter(x => x.s > 0);
  const strong = scored.some(x => x.s >= STRONG_MATCH);
  return scored.filter(x => !strong || x.s >= STRONG_MATCH).map(x => x.n);
}
