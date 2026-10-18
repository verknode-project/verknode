import { useSyncExternalStore } from 'react';

/**
 * Speech-bubble tails of the canvas widgets of one editor: SVG polygon
 * points in canvas coordinates by node id. WidgetPanel measures them; the
 * editor draws them all in one layer under the nodes and widgets, so a tail
 * never covers another node or widget (inside its node it would be drawn
 * above every node stacked below that one).
 */
export class TailStore {
  /** id: a widget's node, or "inspect:<key>" for an inspection widget (Inspector.tsx) */
  set(id: number | string, points: string | null): void {
    if ((this._tails.get(id) ?? null) === points) {
      return;
    }
    const next = new Map(this._tails);
    points === null ? next.delete(id) : next.set(id, points);
    this._tails = next;
    this._listeners.forEach(l => l());
  }

  subscribe = (listener: () => void): (() => void) => {
    this._listeners.add(listener);
    return () => this._listeners.delete(listener);
  };

  snapshot = (): ReadonlyMap<number | string, string> => this._tails;

  private _tails = new Map<number | string, string>();
  private _listeners = new Set<() => void>();
}

export function useTails(store: TailStore): ReadonlyMap<number | string, string> {
  return useSyncExternalStore(store.subscribe, store.snapshot);
}
