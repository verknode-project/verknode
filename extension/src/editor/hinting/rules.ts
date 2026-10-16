import { ConnectionInfo, NodeDescription, NodeInfo, PortType } from '../../types';
import { loadHistory } from './history';

/**
 * Suggestion rules for the Tab quick-add ("hinting"). Each rule adds
 * likelihood points to candidate nodes; the quick-add ranks candidates by
 * match quality and then by the sum of the rule points.
 *
 * Point scale: ordinary preferences ~0-10; a rule that wants its candidates
 * pinned to the top of the initial list uses >= 50 (e.g. closing an open
 * loop). Add rules with registerSuggestionRule().
 */
export interface SuggestionContext {
  /** node the new node will be connected to (null: nothing selected) */
  source: NodeInfo | null;
  /** type of the source output port */
  portType: PortType | null;
  query: string;
  nodes: NodeInfo[];
  connections: ConnectionInfo[];
  /** all node types (descriptions, incl. closedBy) */
  repository: NodeDescription[];
}

export interface SuggestionRule {
  id: string;
  /** what the rule does (for documentation / settings) */
  description: string;
  /** called once per suggestion request; returns the points for a candidate */
  prepare(ctx: SuggestionContext): (candidate: NodeDescription) => number;
}

// ---------------------------------------------------------------- graph helpers
function graph(ctx: SuggestionContext) {
  const parents = new Map<number, number[]>();
  const children = new Map<number, number[]>();
  for (const c of ctx.connections) {
    parents.set(c.destID, [...(parents.get(c.destID) ?? []), c.sourceID]);
    children.set(c.sourceID, [...(children.get(c.sourceID) ?? []), c.destID]);
  }
  const walk = (start: number, next: Map<number, number[]>): number[] => {
    const seen = new Set<number>([start]);
    const order = [start];
    for (let i = 0; i < order.length; i++) {
      for (const n of next.get(order[i]) ?? []) {
        if (!seen.has(n)) {
          seen.add(n);
          order.push(n);
        }
      }
    }
    return order; // breadth first: nearest first
  };
  return {
    upstream: (id: number) => walk(id, parents),
    downstream: (id: number) => walk(id, children)
  };
}

/**
 * Scopes (loops, try/catch, IF/CASE switches, cross-validation ...) that are
 * open at `source`: an upstream node (or the source itself) declares closing
 * nodes (closedBy) and none of them is downstream of it yet. Innermost first.
 */
export function openScopes(ctx: SuggestionContext): { start: NodeInfo; closers: string[] }[] {
  if (!ctx.source) {
    return [];
  }
  const types = new Map(ctx.repository.map(d => [d.factory, d]));
  const byId = new Map(ctx.nodes.map(n => [n.id, n]));
  const g = graph(ctx);
  const open: { start: NodeInfo; closers: string[] }[] = [];
  for (const id of g.upstream(ctx.source.id)) {
    const node = byId.get(id);
    const closers = node ? (types.get(node.factory)?.closedBy ?? []) : [];
    if (!node || !closers.length) {
      continue;
    }
    const closed = g.downstream(id).some(d => closers.includes(byId.get(d)?.factory ?? ''));
    if (!closed) {
      open.push({ start: node, closers });
    }
  }
  return open;
}

// ---------------------------------------------------------------- built-in rules
/** Close open scopes first: the innermost scope's closing nodes go to the top. */
export const scopeClosureRule: SuggestionRule = {
  id: 'scope-closure',
  description: 'Inside a loop, try/catch, IF/CASE switch or other scope that is not closed yet, suggest its closing node first.',
  prepare(ctx) {
    const points = new Map<string, number>();
    openScopes(ctx).forEach((scope, depth) =>
      scope.closers.forEach((factory, i) => {
        const p = 100 - depth * 20 - i;
        points.set(factory, Math.max(points.get(factory) ?? 0, p));
      })
    );
    return c => points.get(c.factory) ?? 0;
  }
};

/** Nodes this user added after the source node type before. */
export const historyRule: SuggestionRule = {
  id: 'history',
  description: 'Prefer nodes you previously added after this node type (remembered in this browser).',
  prepare(ctx) {
    const counts = ctx.source ? (loadHistory()[ctx.source.factory] ?? {}) : {};
    return c => 3 * Math.log1p(counts[c.factory] ?? 0);
  }
};

/** Nodes that follow the source node type elsewhere in this workflow. */
export const workflowRule: SuggestionRule = {
  id: 'workflow',
  description: 'Prefer nodes that follow the same node type elsewhere in this workflow.',
  prepare(ctx) {
    const counts: Record<string, number> = {};
    if (ctx.source) {
      const byId = new Map(ctx.nodes.map(n => [n.id, n]));
      for (const c of ctx.connections) {
        const from = byId.get(c.sourceID);
        const to = byId.get(c.destID);
        if (from && to && from.factory === ctx.source.factory) {
          counts[to.factory] = (counts[to.factory] ?? 0) + 1;
        }
      }
    }
    return c => 2 * Math.log1p(counts[c.factory] ?? 0);
  }
};

// commonly used nodes, most common first
const COMMON = [
  'Row Filter',
  'Column Filter',
  'GroupBy',
  'Joiner',
  'Sorter',
  'Math Formula',
  'String Manipulation',
  'Rule Engine',
  'Missing Value',
  'Column Expression',
  'Column Renamer',
  'Concatenate',
  'Duplicate Row Filter',
  'Row Splitter',
  'Pivot',
  'Value Counter',
  'Statistics',
  'Table View',
  'Bar Chart',
  'Scatter Plot',
  'CSV Writer',
  'Python Script',
  'Partitioning',
  'Row Sampling'
];
const COMMON_SOURCES = ['CSV Reader', 'Table Creator', 'Excel Reader', 'Parquet Reader', 'Table Reader', 'Python Script'];

/** Generally common nodes (source nodes when nothing is selected). */
export const commonRule: SuggestionRule = {
  id: 'common',
  description: 'Prefer commonly used nodes (source nodes when nothing is selected).',
  prepare(ctx) {
    const list = ctx.source ? COMMON : COMMON_SOURCES;
    return c => {
      const i = list.indexOf(c.name);
      const prior = i < 0 ? 0 : (list.length - i) / list.length;
      return prior + (!ctx.source && c.nodeType === 'Source' ? 0.5 : 0);
    };
  }
};

// ---------------------------------------------------------------- registry
const RULES: SuggestionRule[] = [scopeClosureRule, historyRule, workflowRule, commonRule];

export function suggestionRules(): readonly SuggestionRule[] {
  return RULES;
}

/** Add (or replace, by id) a suggestion rule. */
export function registerSuggestionRule(rule: SuggestionRule): void {
  const i = RULES.findIndex(r => r.id === rule.id);
  if (i >= 0) {
    RULES[i] = rule;
  } else {
    RULES.push(rule);
  }
}

export function unregisterSuggestionRule(id: string): void {
  const i = RULES.findIndex(r => r.id === id);
  if (i >= 0) {
    RULES.splice(i, 1);
  }
}
