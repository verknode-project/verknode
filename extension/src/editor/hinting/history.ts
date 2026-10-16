/** Which node types the user added after which (per browser, localStorage). */
const HISTORY_KEY = 'verknode-node-transitions';

export type TransitionHistory = Record<string, Record<string, number>>;

export function loadHistory(): TransitionHistory {
  try {
    return JSON.parse(window.localStorage.getItem(HISTORY_KEY) || '{}');
  } catch {
    return {};
  }
}

/** Remember that a `to` node was added after a `from` node. */
export function recordTransition(from: string, to: string): void {
  try {
    const h = loadHistory();
    h[from] = h[from] ?? {};
    h[from][to] = (h[from][to] ?? 0) + 1;
    window.localStorage.setItem(HISTORY_KEY, JSON.stringify(h));
  } catch {
    // storage unavailable: suggestions just don't learn
  }
}
