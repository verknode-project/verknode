import React, { useContext, useEffect, useLayoutEffect, useRef, useState } from 'react';

import { InputWidgetInfo, OutputWidgetInfo, WidgetInfo, WidgetLayout } from '../../types';
import { BarChart, formatValue, LineChart, ScatterChart } from './charts';
import { BUILTIN_CONTROLS } from './controls';
import { HtmlFrame } from './HtmlFrame';
import { DisplayLoader } from './frames';
import { ChatRenderer } from './chat';
import { useWidgetSelection, WidgetSelection } from './selection';
import { dockGroup, groupTop, Rect as DockRect, snapGroup, snapResize } from './snap';
import { TailStore } from './tails';
import { INPUT_CONTROLS, OUTPUT_RENDERERS, OutputRendererProps, registerInputControl, registerOutputRenderer } from './registry';

/** Rows clicked in a table display: click selects one, Ctrl toggles, Shift extends a range. */
function useRowSelection(selection: any, onSelect?: (s: any) => void) {
  const anchor = useRef<number | null>(null);
  const selected = new Set<number>(selection?.rows ?? []);
  const click = (i: number, e: React.MouseEvent) => {
    if (!onSelect) {
      return;
    }
    let next: Set<number>;
    if (e.shiftKey && anchor.current !== null) {
      next = e.ctrlKey || e.metaKey ? new Set(selected) : new Set();
      for (let k = Math.min(anchor.current, i); k <= Math.max(anchor.current, i); k++) {
        next.add(k);
      }
    } else if (e.ctrlKey || e.metaKey) {
      next = new Set(selected);
      next.has(i) ? next.delete(i) : next.add(i);
      anchor.current = i;
    } else {
      next = selected.size === 1 && selected.has(i) ? new Set() : new Set([i]);
      anchor.current = i;
    }
    onSelect(next.size ? { rows: [...next].sort((a, b) => a - b) } : null);
  };
  return { selected, click };
}

function TableRenderer({ data, width, height, selection, onSelect }: OutputRendererProps) {
  const columns: { name: string; kind: string }[] = data.columns ?? [];
  const rows: any[][] = data.rows ?? [];
  const { selected, click } = useRowSelection(selection, onSelect);
  return (
    <div className="verknode-widget-scroll verknode-widget-table nowheel" style={{ width, height, maxHeight: 'none' }}>
      <table>
        <thead>
          <tr>
            {columns.map(c => (
              <th key={c.name}>{c.name}</th>
            ))}
          </tr>
        </thead>
        <tbody className={onSelect ? 'verknode-selectable' : ''}>
          {rows.map((r, i) => (
            <tr key={i} className={selected.has(i) ? 'verknode-row-selected' : ''} onClick={e => click(i, e)}>
              {r.map((cell, j) => (
                <td key={j} className={['integer', 'long', 'double'].includes(columns[j]?.kind) ? 'verknode-num' : ''}>
                  {cell && typeof cell === 'object' && cell.__molecule__ ? (
                    cell.svg ? (
                      <span className="verknode-widget-mol" title={cell.smiles} dangerouslySetInnerHTML={{ __html: cell.svg }} />
                    ) : (
                      cell.smiles
                    )
                  ) : (
                    formatValue(cell)
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {data.total > rows.length && (
        <div className="verknode-widget-more">
          {rows.length} of {data.total} rows
        </div>
      )}
    </div>
  );
}

function ValueRenderer({ data, height }: OutputRendererProps) {
  const text = formatValue(data.value);
  // numbers and short texts large; longer texts smaller and wrapped
  const size = data.numeric || text.length <= 12 ? 'large' : text.length <= 40 ? 'medium' : 'small';
  return (
    <div className="verknode-widget-value" style={{ height }}>
      <div className={`verknode-widget-value-number verknode-value-${size}`} title={text}>
        {text}
      </div>
      <div className="verknode-widget-value-label">
        {data.how === 'First row' ? data.column : `${data.how} of ${data.column}`}
      </div>
    </div>
  );
}

function ImageRenderer({ data, width, height }: OutputRendererProps) {
  const src = `data:${data.mime};base64,${data.data}`;
  return data.fit ? (
    <img className="verknode-widget-image" src={src} draggable={false} style={{ width, height }} alt="" />
  ) : (
    <div className="verknode-widget-scroll nowheel" style={{ width, height, maxHeight: 'none' }}>
      <img src={src} draggable={false} alt="" />
    </div>
  );
}

function HtmlRenderer({ data, width, height }: OutputRendererProps) {
  return <HtmlFrame html={data.html ?? ''} width={width} height={height} />;
}

for (const [name, control] of Object.entries(BUILTIN_CONTROLS)) {
  registerInputControl(name, control);
}
registerOutputRenderer('table', TableRenderer);
registerOutputRenderer('bar', BarChart);
registerOutputRenderer('line', LineChart);
registerOutputRenderer('scatter', ScatterChart);
registerOutputRenderer('value', ValueRenderer);
registerOutputRenderer('image', ImageRenderer);
registerOutputRenderer('html', HtmlRenderer);
registerOutputRenderer('chat', ChatRenderer);

/** Canvas zoom of the node (its screen size over its own size). */
function zoomOf(el: HTMLElement): number {
  return el.getBoundingClientRect().width / (el.offsetWidth || 1) || 1;
}

/** Follow a pointer drag; deltas are in canvas pixels. onEnd gets whether it moved at all. */
function track(
  e: React.PointerEvent,
  zoom: number,
  onMove: (dx: number, dy: number, ev: PointerEvent) => void,
  onEnd: (moved: boolean, ev: PointerEvent) => void
) {
  e.preventDefault();
  e.stopPropagation();
  const x0 = e.clientX;
  const y0 = e.clientY;
  let moved = false;
  const move = (ev: PointerEvent) => {
    if (!moved && Math.hypot(ev.clientX - x0, ev.clientY - y0) < 3) {
      return;
    }
    moved = true;
    onMove((ev.clientX - x0) / zoom, (ev.clientY - y0) / zoom, ev);
  };
  const up = (ev: PointerEvent) => {
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', up);
    onEnd(moved, ev);
  };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', up);
}

type Point = { x: number; y: number };

/** Widgets snap to each other within this many screen pixels. */
const SNAP = 10;
type Rect = { x: number; y: number; w: number; h: number };

/**
 * Speech-bubble tail from a widget to its node's icon: a wedge whose point
 * touches the icon's edge and whose base lies hidden under the widget (null
 * when the widget covers the icon). Points of an SVG polygon, in the
 * coordinates of the boxes.
 */
export function tail(widget: Rect, icon: Rect): string | null {
  const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), Math.max(lo, hi));
  const aim = { x: icon.x + icon.w / 2, y: icon.y + icon.h / 2 };
  // the wide end is centred on the widget (hidden under it)
  const c = { x: widget.x + widget.w / 2, y: widget.y + widget.h / 2 };
  const dx = aim.x - c.x;
  const dy = aim.y - c.y;
  const len = Math.hypot(dx, dy);
  if (len < 1) {
    return null;
  }
  // fraction of the way from c to the icon's centre where the line crosses a box's edge
  const cross = (r: Rect, from: Point, sx: number, sy: number) => {
    const tx = (sx > 0 ? r.x + r.w : r.x) - from.x;
    const ty = (sy > 0 ? r.y + r.h : r.y) - from.y;
    return Math.min(sx ? Math.abs(tx / dx) : Infinity, sy ? Math.abs(ty / dy) : Infinity);
  };
  const out = cross(widget, c, Math.sign(dx), Math.sign(dy)); // leaves the widget
  const into = 1 - cross(icon, aim, -Math.sign(dx), -Math.sign(dy)); // reaches the icon
  const visible = (into - out) * len;
  if (visible < 2) {
    return null;
  }
  const ux = dx / len;
  const uy = dy / len;
  const tip = { x: c.x + dx * into, y: c.y + dy * into };
  // half its width where it leaves the widget; wider at the hidden base so the sides run straight
  // (but the base stays inside the widget)
  const edgeHalf = Math.min(9, Math.max(4, visible * 0.3));
  const half = Math.min((edgeHalf * into) / (into - out), Math.min(widget.w, widget.h) / 2 - 2);
  const corner = (k: number) => ({
    x: clamp(c.x - uy * half * k, widget.x + 2, widget.x + widget.w - 2),
    y: clamp(c.y + ux * half * k, widget.y + 2, widget.y + widget.h - 2)
  });
  const f = (p: Point) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`;
  return `${f(corner(1))} ${f(tip)} ${f(corner(-1))}`;
}

/**
 * What a widget node shows next to it: the input control or the output
 * display, in a panel docked at a side of the node (inputs left, outputs
 * right by default) or dragged anywhere by its title bar - it then stays at
 * that place on the canvas when the node moves, until it is dropped on its
 * node again (back to the default place). It points to its node like a
 * speech bubble. The corner grip resizes it, the title bar
 * button minimizes it; the result is stored with set_widget_layouts.
 */
export function WidgetPanel(props: {
  widget: WidgetInfo;
  layout: WidgetLayout;
  /** the node's id and position on the canvas (moved widgets keep theirs) */
  id: number;
  at: Point;
  /** where the tail goes (drawn by the editor under all nodes) */
  tails?: TailStore;
  /** node name, shown on the title bar of a minimized widget without a title */
  name: string;
  executing: boolean;
  busy: boolean;
  onChange: (value: any) => void;
  onSelect: (selection: any) => void;
  /** output widgets that wait for input while they run (a chat) */
  onSend?: (value: any) => void;
  onLayout: (layout: WidgetLayout) => Promise<unknown>;
  onSketch: () => void;
  /** the editor's widget selection; canvas positions of the selected widgets that move with a group */
  selection?: WidgetSelection;
  groupStarts?: () => Map<number, Point>;
  /** a group of selected widgets was dragged (their new canvas positions) */
  onGroupMoved?: (moved: Map<number, Point>) => void;
  /** the anchor button: this widget, or all selected ones when it is selected */
  onAnchor?: (anchored: boolean) => void;
  /** dropped somewhere new (the editor stores it, with the widgets it docked to) */
  onDropped?: (layout: WidgetLayout) => Promise<unknown>;
  /** every widget's box on the canvas, and which can be docked to each other (not anchored) */
  dockEnv?: () => { rects: Map<number, DockRect>; members: Set<number> };
}) {
  useWidgetSelection(props.selection);
  const { widget, layout } = props;
  const side = layout.side ?? (widget.type === 'input' ? 'left' : 'right');
  const dock = useRef<HTMLDivElement>(null);
  const body = useRef<HTMLDivElement>(null);
  // while dragging or resizing (until the stored layout changes)
  const [drag, setDrag] = useState<Point | null>(null);
  const [size, setSize] = useState<{ w: number; h: number } | null>(null);
  const layoutKey = JSON.stringify(layout);
  useEffect(() => {
    setDrag(null);
    setSize(null);
  }, [layoutKey]);
  const commit = (next: WidgetLayout) =>
    props.onLayout(next).catch(() => {
      setDrag(null);
      setSize(null);
    });

  // over its node while dragged: dropping it there docks it at the default place
  const [overNode, setOverNode] = useState(false);
  // (positions inside the node's box)
  const grouped = props.selection?.movingPosition(props.id);
  const free = grouped
    ? { x: grouped.x - props.at.x, y: grouped.y - props.at.y }
    : (drag ?? (side === 'free' ? { x: (layout.x ?? 0) - props.at.x, y: (layout.y ?? 0) - props.at.y } : null));
  const selected = !!props.selection?.has(props.id);
  const anchored = !!layout.anchored;
  const w = size?.w ?? layout.w ?? (widget.type === 'output' ? widget.width : undefined);
  const h = size?.h ?? layout.h ?? (widget.type === 'output' ? widget.height : undefined);
  const minimized = !!layout.minimized;

  // the speech-bubble tail (also on every render: the widget or the node may have moved)
  const updateTail = useRef(() => {});
  updateTail.current = () => {
    const el = dock.current;
    const root = el?.closest('.verknode-node') as HTMLElement | null;
    const icon = root?.querySelector('.verknode-node-icon') as HTMLElement | null;
    if (!el || !root || !icon || !root.offsetWidth) {
      return; // (a hidden editor tab cannot measure)
    }
    const zoom = zoomOf(root);
    const rr = root.getBoundingClientRect();
    // canvas coordinates (the node's box starts at its position)
    const at = (r: DOMRect): Rect => ({
      x: props.at.x + (r.left - rr.left) / zoom,
      y: props.at.y + (r.top - rr.top) / zoom,
      w: r.width / zoom,
      h: r.height / zoom
    });
    props.tails?.set(props.id, tail(at(el.getBoundingClientRect()), at(icon.getBoundingClientRect())));
  };
  useLayoutEffect(() => updateTail.current());
  const tailsRef = useRef(props.tails);
  tailsRef.current = props.tails;
  const id = props.id;
  // (and when its content resizes the widget)
  useEffect(() => {
    const observer = new ResizeObserver(() => updateTail.current());
    observer.observe(dock.current!);
    return () => {
      observer.disconnect();
      tailsRef.current?.set(id, null);
    };
  }, []);

  /**
   * What dragging this title bar moves: the widget's docked group when this is the group's top
   * widget (its handle; any other member leaves the group), and the other selected widgets.
   */
  const movePlan = (selectedNow: boolean) => {
    const env = props.dockEnv?.();
    const moving = new Set([props.id]);
    let group = [props.id];
    let top: number | null = null;
    if (env) {
      group = dockGroup(props.id, env.rects, env.members);
      top = group.length > 1 ? groupTop(group, env.rects) : null;
      if (top === props.id) {
        group.forEach(g => moving.add(g));
      }
    }
    if (selectedNow) {
      props.groupStarts?.().forEach((_, id) => moving.add(id));
    }
    return { env, moving, group, top };
  };
  // hovering the title bar outlines what a drag would move (and, in a group, what would stay)
  const [hint, setHintText] = useState('');
  const showHint = () => {
    if (anchored) {
      setHintText('Anchored: stays where it is (unanchor it to move it)');
      return;
    }
    const { moving, group, top } = movePlan(selected);
    const staying = group.filter(id => !moving.has(id));
    props.selection?.setHint({ moving: [...moving], main: top === props.id ? props.id : null, staying });
    const others = moving.size - 1;
    setHintText(
      top === props.id
        ? `Drag to move the group (${group.length} widgets${moving.size > group.length ? ` and ${moving.size - group.length} more selected` : ''})`
        : top !== null
          ? `Drag to take this widget out of its group${others ? ` (with ${others} selected)` : ''}; the top widget's title bar moves the whole group`
          : `Drag to move${others ? ` (with ${others} selected widget${others > 1 ? 's' : ''})` : ''} · near another widget it snaps to it`
    );
  };
  const hideHint = () => props.selection?.setHint(null);

  const startMove = (e: React.PointerEvent) => {
    if (e.button !== 0 || (e.target as HTMLElement).closest('button')) {
      return;
    }
    const sel = props.selection;
    // Ctrl / Shift + click adds or removes the widget from the selection
    if (e.ctrlKey || e.metaKey || e.shiftKey) {
      e.stopPropagation();
      sel?.toggleExplicit(props.id);
      return;
    }
    if (!selected) {
      sel?.setExplicit([props.id]);
    }
    if (anchored) {
      return; // kept where it is
    }
    sel?.setHint(null);
    const el = dock.current!;
    const root = el.closest('.verknode-node') as HTMLElement;
    const zoom = zoomOf(root);
    const { env, moving } = movePlan(selected);
    // snapping to the widgets that stay: next to one, or in line with its edges - any of the moving
    // widgets can dock (e.g. the bottom one of a group next to another widget)
    const movingRects = env ? [...moving].map(id => env.rects.get(id)).filter((r): r is DockRect => !!r) : [];
    const others = env ? [...env.rects].filter(([id]) => !moving.has(id)).map(([, r]) => r) : [];
    const snapped = (dx: number, dy: number) => {
      const s = snapGroup(movingRects.map(r => ({ ...r, x: r.x + dx, y: r.y + dy })), others, SNAP / zoom);
      return { dx: Math.round(dx + s.dx), dy: Math.round(dy + s.dy) };
    };
    if (sel && env && moving.size > 1) {
      sel.startMove(new Map([...moving].filter(id => env.rects.has(id)).map(id => [id, { x: env.rects.get(id)!.x, y: env.rects.get(id)!.y }])));
      track(
        e,
        zoom,
        (dx, dy) => {
          const s = snapped(dx, dy);
          sel.moveBy(s.dx, s.dy);
        },
        moved => (moved ? props.onGroupMoved?.(sel.moved) : sel.endMove())
      );
      return;
    }
    const r = el.getBoundingClientRect();
    const rr = root.getBoundingClientRect();
    const start = { x: (r.left - rr.left) / zoom - root.clientLeft, y: (r.top - rr.top) / zoom - root.clientTop };
    let last = start;
    const onNode = (ev: PointerEvent) => {
      const b = root.getBoundingClientRect();
      return ev.clientX >= b.left && ev.clientX <= b.right && ev.clientY >= b.top && ev.clientY <= b.bottom;
    };
    track(
      e,
      zoom,
      (dx, dy, ev) => {
        const s = snapped(dx, dy);
        setDrag((last = { x: Math.round(start.x + s.dx), y: Math.round(start.y + s.dy) }));
        setOverNode(onNode(ev));
      },
      (moved, ev) => {
        setOverNode(false);
        if (moved && onNode(ev)) {
          void commit({ ...layout, side: undefined, x: undefined, y: undefined });
        } else if (moved) {
          const next: WidgetLayout = { ...layout, side: 'free', x: props.at.x + last.x, y: props.at.y + last.y };
          // (the editor also pins the widgets it docked to that still sit beside their nodes)
          void (props.onDropped ? props.onDropped(next).catch(() => setDrag(null)) : commit(next));
        }
      }
    );
  };

  // the grip sits at the corner away from the node; centred widgets (above / below) grow both ways
  const grip = `${side === 'above' ? 'top' : 'bottom'}-${side === 'left' ? 'left' : 'right'}`;
  const startResize = (e: React.PointerEvent) => {
    const el = body.current!;
    const zoom = zoomOf(el);
    const start = { w: el.offsetWidth, h: el.offsetHeight };
    const sx = side === 'left' ? -1 : side === 'above' || side === 'below' ? 2 : 1;
    const sy = side === 'above' ? -1 : 1;
    // growing to the right / down (free or right of its node): the edges snap to the other widgets' edges
    const env = props.dockEnv?.();
    const own = side === 'free' || side === 'right' ? env?.rects.get(props.id) : undefined;
    const others = env ? [...env.rects].filter(([id]) => id !== props.id).map(([, r]) => r) : [];
    const extraW = dock.current!.offsetWidth - el.offsetWidth;
    const extraH = dock.current!.offsetHeight - el.offsetHeight;
    let last = start;
    track(
      e,
      zoom,
      (dx, dy) => {
        let w = Math.max(80, start.w + sx * dx);
        let h = Math.max(30, start.h + sy * dy);
        if (own) {
          const s = snapResize({ x: own.x, y: own.y, w: w + extraW, h: h + extraH }, others, SNAP / zoom);
          w += s.dw;
          h += s.dh;
        }
        setSize((last = { w: Math.round(w), h: Math.round(h) }));
      },
      moved => moved && void commit({ ...layout, w: last.w, h: last.h })
    );
  };

  const title = widget.type === 'input' ? widget.label : widget.title;
  const hasSelection = widget.type === 'output' && widget.interactive && widget.selection !== null && widget.selection !== undefined;
  return (
    <>
      {overNode && <div className="verknode-dock-target" title="Drop to dock the widget at its default place" />}
      <div
        ref={dock}
        // (nodrag / nopan: dragging in the widget neither moves the node nor pans the canvas; the title bar moves the widget)
        className={`verknode-widget-dock nodrag nopan verknode-dock-${free ? 'free' : side}${drag || grouped ? ' verknode-dragging' : ''}${selected ? ' verknode-widget-selected' : ''}${anchored ? ' verknode-widget-anchored' : ''}`}
        data-widget={props.id}
        style={free ? { left: free.x, top: free.y } : undefined}
        // double-clicks stay inside (on the node they open its dialog)
        onDoubleClick={e => e.stopPropagation()}
      >
        <div className={`verknode-widget verknode-widget-${widget.type === 'input' ? 'in' : 'out'}${widget.type === 'input' ? ` verknode-control-${widget.control}` : ''}${minimized ? ' verknode-widget-minimized' : ''}`}>
          <div
            className="verknode-widget-head nodrag nopan"
            title={`${hint || 'Drag to move'} · Ctrl+click to select · double-click to minimize or restore`}
            onPointerEnter={showHint}
            onPointerLeave={hideHint}
            onPointerDown={startMove}
            onDoubleClick={e => !(e.target as HTMLElement).closest('button') && void commit({ ...layout, minimized: !minimized })}
          >
            <span className="verknode-widget-label">{title || (minimized ? props.name : '')}</span>
            {hasSelection && (
              <button className="verknode-widget-mini" title="Clear the selection" disabled={props.busy} onClick={() => props.onSelect(null)}>
                Clear
              </button>
            )}
            {widget.type === 'input' && widget.edited && (
              <button className="verknode-widget-mini" title="Back to the default value" disabled={props.busy} onClick={() => props.onChange(null)}>
                ↺
              </button>
            )}
            <button
              className={`verknode-widget-mini verknode-widget-anchor${anchored ? ' verknode-active' : ''}`}
              title={
                (anchored ? 'Anchored: stays where it is. Click to free it' : 'Free: click to anchor it where it is') +
                (selected && (props.selection?.ids.length ?? 0) > 1 ? ' (all selected widgets)' : '')
              }
              onClick={() => props.onAnchor?.(!anchored)}
            >
              <svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" aria-hidden="true">
                <circle cx="8" cy="3.2" r="1.7" />
                <path d="M8 4.9V14.2M5 7.6h6M2.6 9.6c0 2.6 2.4 4.6 5.4 4.6s5.4-2 5.4-4.6" />
              </svg>
            </button>
            <button
              className="verknode-widget-mini verknode-widget-min"
              title={minimized ? 'Restore' : 'Minimize'}
              onClick={() => void commit({ ...layout, minimized: !minimized })}
            >
              {minimized ? '▢' : '–'}
            </button>
          </div>
          {!minimized && (
            <div ref={body} className={`verknode-widget-body${h !== undefined ? ' verknode-widget-sized' : ''}`} style={{ width: w, height: h }}>
              {widget.type === 'input' ? (
                <InputBody w={widget} busy={props.busy} width={w} height={h} onChange={props.onChange} onSketch={props.onSketch} />
              ) : (
                <OutputBody
                  id={props.id}
                  w={widget}
                  stale={props.executing}
                  running={props.busy}
                  width={w!}
                  height={h!}
                  onSelect={props.onSelect}
                  onSend={props.onSend}
                />
              )}
            </div>
          )}
          {!minimized && <div className={`verknode-widget-grip verknode-grip-${grip} nodrag nopan`} title="Drag to resize" onPointerDown={startResize} />}
        </div>
      </div>
    </>
  );
}

function InputBody(props: {
  w: InputWidgetInfo;
  busy: boolean;
  width?: number;
  height?: number;
  onChange: (v: any) => void;
  onSketch: () => void;
}) {
  const { w } = props;
  const Control = INPUT_CONTROLS.get(w.control);
  return Control ? (
    <Control
      value={w.value}
      props={w.props ?? {}}
      name={w.name}
      html={w.html}
      disabled={props.busy}
      width={props.width}
      height={props.height}
      onChange={props.onChange}
      onSketch={props.onSketch}
    />
  ) : (
    <span className="verknode-widget-error">Unknown control "{w.control}"</span>
  );
}

/** Big displays, fetched once per key (the view only has {deferred: key} for them). */
const DEFERRED = new Map<string, any>();

function useDisplay(id: number, data: any): any {
  const load = useContext(DisplayLoader);
  const key: string | null = data && typeof data === 'object' && typeof data.deferred === 'string' ? data.deferred : null;
  const [loaded, setLoaded] = useState<{ key: string; data: any } | null>(null);
  useEffect(() => {
    if (key === null || DEFERRED.has(key) || !load) {
      return;
    }
    let live = true;
    void load(id).then(r => {
      if (r.key) {
        DEFERRED.set(r.key, r.data);
        while (DEFERRED.size > 24) {
          DEFERRED.delete(DEFERRED.keys().next().value!);
        }
      }
      if (live) {
        setLoaded({ key: r.key ?? key, data: r.data });
      }
    });
    return () => {
      live = false;
    };
  }, [id, key, load]);
  if (key === null) {
    return data;
  }
  return DEFERRED.get(key) ?? (loaded?.key === key ? loaded.data : undefined);
}

/** Displays that are drawn also without data, and keep their last data while their node runs again (a chat in a loop). */
const LIVE_RENDERERS = new Set(['chat']);

function OutputBody(props: {
  id: number;
  w: OutputWidgetInfo;
  stale: boolean;
  running: boolean;
  width: number;
  height: number;
  onSelect: (s: any) => void;
  onSend?: (value: any) => void;
}) {
  const { w } = props;
  const Renderer = OUTPUT_RENDERERS.get(w.renderer);
  const live = LIVE_RENDERERS.has(w.renderer);
  const fetched = useDisplay(props.id, w.data);
  const last = useRef<any>(null);
  if (fetched !== undefined && fetched !== null) {
    last.current = fetched;
  }
  const data = live && (fetched === undefined || fetched === null) ? last.current ?? {} : fetched;
  if (!live && w.data !== null && w.data !== undefined && data === undefined) {
    return <div className="verknode-widget-placeholder">Loading…</div>;
  }
  if (!live && (w.data === null || w.data === undefined)) {
    return <div className="verknode-widget-placeholder">{props.stale ? 'Executing…' : 'Execute to show'}</div>;
  }
  return Renderer ? (
    <Renderer
      data={data}
      width={props.width}
      height={props.height}
      selection={w.interactive ? w.selection : undefined}
      onSelect={w.interactive ? props.onSelect : undefined}
      onSend={props.onSend}
      running={props.running}
      nodeId={props.id}
      nodeRunning={props.stale}
    />
  ) : (
    <span className="verknode-widget-error">Unknown display "{w.renderer}"</span>
  );
}
