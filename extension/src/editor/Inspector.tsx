import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useReactFlow, useViewport } from '@xyflow/react';

import { WorkflowController } from '../controller';
import { PortData } from '../types';
import { HtmlFrame } from './widgets/HtmlFrame';
import { Cell } from './PortView';
import { tail } from './widgets/WidgetPanel';
import { TailStore } from './widgets/tails';

/**
 * Inspection widgets: a look at the data at a port without opening its view. Hovering a port of an
 * executed node shows one for a moment (an input port: the table that comes in, from the output it is
 * connected to); Ctrl+click on the port keeps it, and "Inspect Outputs" (node menu) keeps one for each
 * output of the selected nodes. A kept one can be moved (its title bar), resized and closed. Its tail
 * points at the output port the data comes from.
 *
 * They are drawn in a layer above the canvas that follows its pan and zoom (canvas coordinates); their
 * tails are those of the canvas widgets (TailStore: drawn under the nodes). place() finds them a spot
 * near their port that covers no node, canvas widget or other inspection widget if it can.
 */
export interface Inspection {
  key: string;
  /** the output the data comes from */
  node: number;
  port: number;
  /** top left, in canvas coordinates */
  at: { x: number; y: number };
  pinned: boolean;
  w?: number;
  h?: number;
}

export const INSPECT_W = 340;
export const INSPECT_H = 210;
const ROWS = 25;

/** Where a node's port handle is, in canvas coordinates (its centre). */
export function portPoint(flow: ReturnType<typeof useReactFlow>, node: number, kind: 'in' | 'out', port: number) {
  const n = flow.getInternalNode(String(node));
  if (!n) {
    return null;
  }
  const bounds = n.internals.handleBounds?.[kind === 'out' ? 'source' : 'target'] ?? [];
  const h = bounds.find(b => b.id === `${kind}-${port}`);
  if (!h) {
    return null;
  }
  const p = n.internals.positionAbsolute;
  return { x: p.x + h.x + h.width / 2, y: p.y + h.y + h.height / 2 };
}

type Box = { x: number; y: number; w: number; h: number };

const overlap = (a: Box, b: Box) =>
  Math.max(0, Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x)) * Math.max(0, Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y));

/** What a new inspection widget should not cover: the nodes, canvas widgets and inspection widgets (canvas coordinates). */
export function obstacles(flow: ReturnType<typeof useReactFlow>, root: HTMLElement | null, inspections: Inspection[]): Box[] {
  const out: Box[] = [];
  for (const n of flow.getNodes()) {
    const i = flow.getInternalNode(n.id);
    if (i && n.measured?.width) {
      out.push({ x: i.internals.positionAbsolute.x, y: i.internals.positionAbsolute.y, w: n.measured.width, h: n.measured.height ?? 0 });
    }
  }
  // (canvas widgets and the inspection widgets as they are drawn)
  root?.querySelectorAll('.verknode-widget-dock, .verknode-widget, .verknode-inspect').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width && r.height) {
      const a = flow.screenToFlowPosition({ x: r.left, y: r.top });
      const b = flow.screenToFlowPosition({ x: r.right, y: r.bottom });
      out.push({ x: a.x, y: a.y, w: b.x - a.x, h: b.y - a.y });
    }
  });
  for (const i of inspections) {  // (not drawn yet)
    out.push({ x: i.at.x, y: i.at.y, w: i.w ?? INSPECT_W, h: i.h ?? INSPECT_H });
  }
  return out;
}

/**
 * Where to put a widget of this size by a port (anchor, in canvas coordinates): the free spot nearest
 * to it - on its side (right of an output, left of an input) when it can - else the one that covers
 * the least.
 */
export function place(anchor: { x: number; y: number }, side: 'left' | 'right', size: { w: number; h: number }, taken: Box[]) {
  const { w, h } = size;
  const gap = 26;
  const xs = side === 'right' ? [anchor.x + gap, anchor.x - w - gap] : [anchor.x - w - gap, anchor.x + gap];
  const candidates: { x: number; y: number; cost: number }[] = [];
  xs.forEach((x0, k) => {
    for (let step = 0; step <= 12; step++) {
      for (const sign of step ? [-1, 1] : [1]) {
        for (const dx of [0, 1, 2]) {
          const x = x0 + (x0 >= anchor.x ? 1 : -1) * dx * (w / 2 + gap); // (further out on its side)
          const y = anchor.y - h - gap + sign * step * 48;
          const box = { x, y, w, h };
          const covered = taken.reduce((a, b) => a + overlap(box, b), 0);
          const dist = Math.hypot(x + w / 2 - anchor.x, y + h / 2 - anchor.y);
          candidates.push({ x, y, cost: covered * 50 + dist + k * 120 });
        }
      }
    }
  });
  candidates.sort((a, b) => a.cost - b.cost);
  return { x: candidates[0].x, y: candidates[0].y };
}

export function InspectLayer(props: {
  controller: WorkflowController;
  tails: TailStore;
  inspections: Inspection[];
  onMenu: (key: string, e: React.MouseEvent) => void;
  onChange: (key: string, patch: Partial<Inspection>) => void;
  onClose: (key: string) => void;
  onHover: (key: string, inside: boolean) => void;
  onOpen: (node: number, port: number) => void;
}) {
  const { x, y, zoom } = useViewport();
  const flow = useReactFlow();
  if (!props.inspections.length) {
    return null;
  }
  return (
    <div className="verknode-inspect-layer" style={{ transform: `translate(${x}px, ${y}px) scale(${zoom})` }}>
      {props.inspections.map(i => (
        <InspectCard key={i.key} {...props} inspection={i} zoom={zoom} target={portPoint(flow, i.node, 'out', i.port)} />
      ))}
    </div>
  );
}

function InspectCard(props: {
  controller: WorkflowController;
  tails: TailStore;
  onMenu: (key: string, e: React.MouseEvent) => void;
  inspection: Inspection;
  zoom: number;
  target: { x: number; y: number } | null;
  onChange: (key: string, patch: Partial<Inspection>) => void;
  onClose: (key: string) => void;
  onHover: (key: string, inside: boolean) => void;
  onOpen: (node: number, port: number) => void;
}) {
  const { controller, inspection: ins, target } = props;
  const info = controller.view?.nodes.find(n => n.id === ins.node);
  const executed = info?.state === 'EXECUTED';
  // (fetched again when the node runs again)
  const stamp = `${info?.state}|${JSON.stringify(info?.outRows ?? null)}|${info?.message?.text ?? ''}`;
  const [data, setData] = useState<PortData | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!executed) {
      return;
    }
    let live = true;
    controller
      .port(ins.node, ins.port, 0, ROWS)
      .then(d => live && (setData(d), setError(null)))
      .catch(e => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [ins.node, ins.port, stamp]);
  // its size (resizable when kept), for the tail
  const card = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: ins.w ?? INSPECT_W, h: ins.h ?? INSPECT_H });
  useLayoutEffect(() => {
    const el = card.current;
    if (!el) {
      return;
    }
    const observer = new ResizeObserver(() => setSize({ w: el.offsetWidth, h: el.offsetHeight }));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  // moving a kept one by its title bar
  const startMove = (e: React.PointerEvent) => {
    if (!ins.pinned || (e.target as HTMLElement).closest('button')) {
      return;
    }
    e.preventDefault();
    e.stopPropagation();
    const start = { x: e.clientX, y: e.clientY, at: ins.at };
    const move = (ev: PointerEvent) =>
      props.onChange(ins.key, {
        at: { x: start.at.x + (ev.clientX - start.x) / props.zoom, y: start.at.y + (ev.clientY - start.y) / props.zoom }
      });
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  };
  // the tail: the canvas widgets' (drawn with theirs, under the nodes)
  const points = target ? tail({ x: ins.at.x, y: ins.at.y, w: size.w, h: size.h }, { x: target.x - 3, y: target.y - 3, w: 6, h: 6 }) : null;
  useLayoutEffect(() => props.tails.set(`inspect:${ins.key}`, points), [points]);
  useEffect(() => () => props.tails.set(`inspect:${ins.key}`, null), []);
  const label = info ? `${info.annotation || info.name}` : `#${ins.node}`;
  const portName = data?.portName ?? (ins.port === 0 ? 'Flow variables' : `port ${ins.port}`);
  return (
    <>
      <div
        ref={card}
        className={`verknode-inspect nodrag nopan nowheel ${ins.pinned ? 'verknode-inspect-pinned' : 'verknode-inspect-peek'}`}
        // (as big as what it shows, up to a size - a kept one can be resized)
        style={{ left: ins.at.x, top: ins.at.y, width: ins.w, height: ins.h, maxWidth: ins.w ? undefined : 1.4 * INSPECT_W, maxHeight: ins.h ? undefined : INSPECT_H }}
        onMouseEnter={() => props.onHover(ins.key, true)}
        onMouseLeave={() => props.onHover(ins.key, false)}
        onPointerDown={e => e.stopPropagation()}
        onDoubleClick={e => e.stopPropagation()}
        onContextMenu={e => {
          e.preventDefault();
          e.stopPropagation();
          props.onMenu(ins.key, e);
        }}
      >
        <div className="verknode-inspect-head" onPointerDown={startMove} title={ins.pinned ? 'Drag to move' : 'Ctrl+click the port to keep it'}>
          <span className="verknode-inspect-title">
            {label} <span className="verknode-inspect-port">· {portName}</span>
          </span>
          {data?.rowCount !== undefined && <span className="verknode-inspect-rows">{data.rowCount.toLocaleString()} rows</span>}
          <button className="verknode-inspect-btn" title="Open the full view" onClick={() => props.onOpen(ins.node, ins.port)}>
            ⤢
          </button>
          {ins.pinned ? (
            <button className="verknode-inspect-btn" title="Close" onClick={() => props.onClose(ins.key)}>
              ×
            </button>
          ) : (
            <button className="verknode-inspect-btn" title="Keep it (or Ctrl+click the port)" onClick={() => props.onChange(ins.key, { pinned: true })}>
              📌
            </button>
          )}
        </div>
        <div className="verknode-inspect-body">
          {!executed ? (
            <div className="verknode-inspect-note">{info ? 'Not executed - run the node to see its output.' : 'The node is gone.'}</div>
          ) : error ? (
            <div className="verknode-inspect-note verknode-dialog-error">{error}</div>
          ) : !data ? (
            <div className="verknode-inspect-note">Loading…</div>
          ) : (
            <Content data={data} />
          )}
        </div>
      </div>
    </>
  );
}

function Content({ data }: { data: PortData }) {
  if (data.inactive) {
    return <div className="verknode-inspect-note">Inactive (a switch did not take this branch).</div>;
  }
  if (data.type === 'table' && data.spec && data.rows) {
    const cols = data.spec.columns;
    return (
      <table className="verknode-inspect-table">
        <thead>
          <tr>
            {cols.map(c => (
              <th key={c.name} title={`${c.name} (${c.type})`}>
                {c.name}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.rows.map((r, i) => (
            <tr key={i}>
              {r.map((v, j) => (
                <Cell key={j} value={v} kind={cols[j]?.kind ?? ''} />
              ))}
            </tr>
          ))}
          {(data.rowCount ?? 0) > data.rows.length && (
            <tr>
              <td className="verknode-inspect-more" colSpan={cols.length}>
                … {((data.rowCount ?? 0) - data.rows.length).toLocaleString()} more rows (⤢ opens them all)
              </td>
            </tr>
          )}
        </tbody>
      </table>
    );
  }
  if (data.html) {
    return <HtmlFrame html={data.html} width={360} height={260} />;
  }
  if (data.image) {
    return <img className="verknode-inspect-image" src={`data:${data.image.mime};base64,${data.image.data}`} />;
  }
  if (data.flowVariables) {
    return (
      <table className="verknode-inspect-table">
        <tbody>
          {data.flowVariables.map(v => (
            <tr key={v.name}>
              <th>{v.name}</th>
              <td>{String(v.value)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  }
  if (data.object) {
    return (
      <table className="verknode-inspect-table">
        <tbody>
          {Object.entries(data.object).map(([k, v]) => (
            <tr key={k}>
              <th>{k}</th>
              <td>{typeof v === 'object' ? JSON.stringify(v) : String(v)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  }
  return <div className="verknode-inspect-note">Nothing to show.</div>;
}
