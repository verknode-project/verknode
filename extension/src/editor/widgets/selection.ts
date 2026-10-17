import { useSyncExternalStore } from 'react';

type Point = { x: number; y: number };

/**
 * Selected canvas widgets of one editor (by node id), and widgets being moved
 * as a group. A widget is selected when the marquee or a Ctrl/Shift+click on
 * its title bar picked it ("explicit"), or when its node is selected and the
 * widget is not anchored ("derived"). While a group moves, `moving` holds each
 * widget's canvas position at the start and `delta` the distance so far.
 */
export class WidgetSelection {
  has(id: number): boolean {
    return this._explicit.has(id) || this._derived.has(id);
  }

  get ids(): number[] {
    return [...new Set([...this._explicit, ...this._derived])];
  }

  /** Marquee / Ctrl+click picks (add: keep the current ones). */
  setExplicit(ids: Iterable<number>, add = false): void {
    this._explicit = new Set([...(add ? this._explicit : []), ...ids]);
    this._emit();
  }

  toggleExplicit(id: number): void {
    const next = new Set(this._explicit);
    if (this.has(id)) {
      next.delete(id);
      this._derived.delete(id);
    } else {
      next.add(id);
    }
    this._explicit = next;
    this._emit();
  }

  /** The widgets of the selected nodes. */
  setDerived(ids: Iterable<number>): void {
    const next = new Set(ids);
    if (next.size !== this._derived.size || [...next].some(id => !this._derived.has(id))) {
      this._derived = next;
      this._emit();
    }
  }

  clear(): void {
    if (this._explicit.size || this._derived.size) {
      this._explicit = new Set();
      this._derived = new Set();
      this._emit();
    }
  }

  /**
   * What dragging the hovered title bar would do: the widgets that move (`main`: the group's
   * top widget when the whole group moves) and those of its group that would stay.
   */
  setHint(hint: { moving: number[]; main: number | null; staying: number[] } | null): void {
    this._hint = hint;
    this._emit();
  }

  /** The hovered title bar's hint (none while widgets move). */
  get hint(): { moving: number[]; main: number | null; staying: number[] } | null {
    return this._moving ? null : this._hint;
  }

  /** Canvas position of a widget that is being moved with a group (null: it is not). */
  movingPosition(id: number): Point | null {
    const start = this._moving?.get(id);
    return start ? { x: start.x + this._delta.x, y: start.y + this._delta.y } : null;
  }

  startMove(starts: Map<number, Point>): void {
    this._moving = starts;
    this._delta = { x: 0, y: 0 };
    this._emit();
  }

  moveBy(dx: number, dy: number): void {
    if (this._moving) {
      this._delta = { x: dx, y: dy };
      this._emit();
    }
  }

  /** Where the group ended up (canvas positions by node id). */
  get moved(): Map<number, Point> {
    const out = new Map<number, Point>();
    for (const id of this._moving?.keys() ?? []) {
      out.set(id, this.movingPosition(id)!);
    }
    return out;
  }

  endMove(): void {
    if (this._moving) {
      this._moving = null;
      this._emit();
    }
  }

  subscribe = (listener: () => void): (() => void) => {
    this._listeners.add(listener);
    return () => this._listeners.delete(listener);
  };

  snapshot = (): number => this._version;

  private _emit(): void {
    this._version++;
    this._listeners.forEach(l => l());
  }

  private _explicit = new Set<number>();
  private _derived = new Set<number>();
  private _moving: Map<number, Point> | null = null;
  private _hint: { moving: number[]; main: number | null; staying: number[] } | null = null;
  private _delta: Point = { x: 0, y: 0 };
  private _version = 0;
  private _listeners = new Set<() => void>();
}

/** Re-render when the selection (or a group move) changes. */
export function useWidgetSelection(store?: WidgetSelection): void {
  useSyncExternalStore(store?.subscribe ?? NOOP_SUBSCRIBE, store?.snapshot ?? ZERO);
}

const NOOP_SUBSCRIBE = () => () => undefined;
const ZERO = () => 0;
