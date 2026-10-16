/**
 * Hinting: node suggestions for the Tab quick-add.
 *
 * Candidates are ranked by match quality of the typed text (match.ts) and
 * then by likelihood points from the suggestion rules (rules.ts): open
 * scope closure, the user's history, the current workflow and common
 * nodes. Extend it with registerSuggestionRule().
 */
import { NodeDescription, PortType } from '../../types';
import { matchScore, STRONG_MATCH } from './match';
import { SuggestionContext, suggestionRules } from './rules';

export { matchScore, matchingNodes } from './match';
export { recordTransition } from './history';
export {
  openScopes,
  registerSuggestionRule,
  suggestionRules,
  unregisterSuggestionRule,
  type SuggestionContext,
  type SuggestionRule
} from './rules';

/** Input port of `node` that a connection of `type` should use, if any. */
export function compatibleInput(node: NodeDescription, type: PortType): number | null {
  const explicit = node.inPorts.find(p => p.type === type);
  if (explicit) {
    return explicit.index;
  }
  return type === 'flowvariable' ? 0 : null; // every node has the implicit variable port
}

/** Candidate nodes for the context, best first. */
export function suggestNodes(ctx: SuggestionContext): NodeDescription[] {
  const scorers = suggestionRules().map(rule => rule.prepare(ctx));
  const scored: { node: NodeDescription; match: number; score: number }[] = [];
  for (const node of ctx.repository) {
    if (ctx.portType && compatibleInput(node, ctx.portType) === null) {
      continue;
    }
    const match = matchScore(node, ctx.query);
    if (match <= 0) {
      continue;
    }
    const likely = scorers.reduce((sum, score) => sum + score(node), 0);
    scored.push({ node, match, score: match * 10 + likely });
  }
  // loose (letters in order) matches only when nothing matches better
  const strong = scored.some(s => s.match >= STRONG_MATCH);
  return scored
    .filter(s => !strong || s.match >= STRONG_MATCH)
    .sort((a, b) => b.score - a.score || a.node.name.localeCompare(b.node.name))
    .map(s => s.node);
}
