import { Handle, Node, NodeProps, Position } from '@xyflow/react';
import React, { memo } from 'react';

import { NodeInfo, NodeState, NodeTiming, PortInfo, WidgetLayout } from '../types';
import { iconBackground } from './themes';
import { WidgetSelection } from './widgets/selection';
import { TailStore } from './widgets/tails';
import { WidgetPanel } from './widgets/WidgetPanel';

/** Node icon; its colour comes from the canvas theme (themes.ts) by node type and category. */
export function NodeIcon({ nodeType, icon, size = 40, category }: { nodeType: string; icon: string; size?: number; category?: string }) {
  const glyphSize = icon.length > 3 ? 0.26 : icon.length > 2 ? 0.3 : icon.length > 1 ? 0.36 : 0.5;
  return (
    <div
      className={`verknode-node-icon verknode-type-${nodeType}`}
      style={{ background: iconBackground(nodeType, category), width: size, height: size, fontSize: size * glyphSize }}
    >
      <span>{icon}</span>
    </div>
  );
}

export interface WorkflowNodeData extends Record<string, unknown> {
  node: NodeInfo;
  connectedIn: number[];
  connectedOut: number[];
  /** arrow keys leading to this node from the current node (structural / Ctrl spatial) */
  navHints?: { keys: string[]; spatial: string[] };
  /** open an output port's view; tab = new browser tab instead of window */
  onOpenPort?: (node: number, port: number, tab: boolean) => void;
  /** a port hovered (inspection widget for a moment), left, or Ctrl+clicked (kept) */
  onInspect?: (node: number, kind: 'in' | 'out', port: number, action: 'enter' | 'leave' | 'pin') => void;
  /** height of a metanode port bar */
  barHeight?: number;
  /** Ctrl+click on the traffic light: execute / reset / cancel the node */
  onLight?: (node: number, action: 'execute' | 'reset' | 'cancel') => void;
  /** widget nodes: new value of an input widget (null = its default), open the sketcher */
  onWidget?: (node: number, value: any) => void;
  onSketch?: (node: number) => void;
  /** ... new selection on an interactive output widget (null = none), new placement / size */
  onSelect?: (node: number, selection: any) => void;
  /** ... something for an output widget that waits for it while its node runs (a chat message) */
  onSend?: (node: number, value: any) => void;
  onWidgetLayout?: (node: number, layout: WidgetLayout) => Promise<unknown>;
  /** the workflow is executing (input widgets cannot change then) */
  busy?: boolean;
  /** speech-bubble tails of the widgets (drawn by the editor) */
  tails?: TailStore;
  /** selected widgets, moving them as a group, anchoring */
  widgetSelection?: WidgetSelection;
  widgetStarts?: () => Map<number, { x: number; y: number }>;
  onWidgetsMoved?: (moved: Map<number, { x: number; y: number }>) => void;
  onAnchor?: (node: number, anchored: boolean) => void;
  dockEnv?: () => { rects: Map<number, { x: number; y: number; w: number; h: number }>; members: Set<number> };
  onWidgetDropped?: (node: number, layout: WidgetLayout) => Promise<unknown>;
}

export type WorkflowNodeType = Node<WorkflowNodeData, 'node' | 'metabar'>;

function portTop(i: number, n: number): string {
  return `${((i + 1) / (n + 1)) * 100}%`;
}

function PortHandle(props: {
  kind: 'source' | 'target';
  port: PortInfo;
  top: string;
  connected: boolean;
  inactive?: boolean;
  onOpen?: (tab: boolean) => void;
  onInspect?: (action: 'enter' | 'leave' | 'pin') => void;
}) {
  const { kind, port, top, connected, inactive, onOpen, onInspect } = props;
  const isVar = port.type === 'flowvariable';
  const isSquare = !isVar && port.type !== 'table';
  const cls = [
    'verknode-port',
    isVar ? 'verknode-port-var' : isSquare ? `verknode-port-square verknode-port-${port.type}` : 'verknode-port-table',
    port.implicit ? 'verknode-port-implicit' : '',
    connected ? 'verknode-connected' : '',
    port.optional && !port.implicit ? 'verknode-port-optional' : '',
    inactive ? 'verknode-port-inactive' : ''
  ].join(' ');
  return (
    <Handle
      id={`${kind === 'target' ? 'in' : 'out'}-${port.index}`}
      type={kind}
      position={kind === 'target' ? Position.Left : Position.Right}
      className={cls + (port.color ? ' verknode-port-custom' : '')}
      style={port.color ? ({ top, '--verknode-port-color': port.color } as React.CSSProperties) : { top }}
      title={
        `${port.index}: ${port.name}${port.optional && !port.implicit ? ' (optional)' : ''}` +
        (onOpen ? '\nDouble-click to open (Ctrl: in a new tab)' : '') +
        (onInspect ? '\nHover to look at the data, Ctrl+click to keep it' : '')
      }
      onMouseEnter={onInspect && (() => onInspect('enter'))}
      onMouseLeave={onInspect && (() => onInspect('leave'))}
      onClick={
        onInspect &&
        ((e: React.MouseEvent) => {
          if (e.ctrlKey || e.metaKey) {
            e.stopPropagation();
            onInspect('pin');
          }
        })
      }
      onDoubleClick={
        onOpen &&
        ((e: React.MouseEvent) => {
          // double-click an output port to open its view
          e.stopPropagation();
          onOpen(e.ctrlKey || e.metaKey);
        })
      }
    >
      <svg viewBox="0 0 10 10" width="10" height="10">
        {isVar ? (
          <circle cx="5" cy="5" r="4" />
        ) : isSquare ? (
          <rect x="1" y="1" width="8" height="8" />
        ) : (
          <polygon points="1,0.5 9.5,5 1,9.5" />
        )}
        {inactive && <path d="M1 1 L9 9 M9 1 L1 9" className="verknode-port-x" />}
      </svg>
    </Handle>
  );
}

/** Short row count: 950, 12.3k, 4.5M. */
export function formatRows(n: number): string {
  if (n < 1000) {
    return String(n);
  }
  if (n < 1e6) {
    return `${(n / 1e3).toFixed(n < 1e4 ? 1 : 0)}k`;
  }
  return `${(n / 1e6).toFixed(n < 1e7 ? 1 : 0)}M`;
}

const LIGHT_ACTIONS = {
  execute: { icon: '▶', title: 'Double-click or Ctrl+click: execute' },
  reset: { icon: '↺', title: 'Ctrl+click: reset' },
  cancel: { icon: '■', title: 'Ctrl+click: cancel' }
} as const;

/**
 * Traffic light; with Ctrl held, hovering shows what a click does (execute
 * a configured node, reset an executed one, cancel a running one).
 */
function NodeLight(props: { node: NodeInfo; onAction?: (action: 'execute' | 'reset' | 'cancel') => void }) {
  const { state } = props.node;
  const action =
    state === 'EXECUTING' || state === 'QUEUED' ? 'cancel' : state === 'EXECUTED' ? 'reset' : state === 'CONFIGURED' ? 'execute' : null;
  if (!action || !props.onAction) {
    return <StatusLight state={state} />;
  }
  const act = (e: React.PointerEvent | React.MouseEvent) => {
    if (e.ctrlKey || e.metaKey) {
      e.stopPropagation();
      e.preventDefault();
      return true;
    }
    return false;
  };
  return (
    <div
      className={`verknode-light-action verknode-light-${action}`}
      title={LIGHT_ACTIONS[action].title}
      onPointerDown={e => act(e)}
      onMouseDown={e => act(e)}
      onClick={e => act(e) && props.onAction!(action)}
      onDoubleClick={e => {
        // a configured node also runs on a plain double-click (not its dialog)
        if (!act(e) && action === 'execute') {
          e.stopPropagation();
          props.onAction!('execute');
        }
      }}
    >
      <StatusLight state={state} />
      <span className="verknode-light-icon">{LIGHT_ACTIONS[action].icon}</span>
    </div>
  );
}

/** 0.4 ms, 2.5 s, 12 s, 3 min 20 s, 45 min, 1 h 5 min */
export function formatDuration(s: number): string {
  if (s < 0.001) {
    return '<1 ms';
  }
  if (s < 1) {
    return `${Math.round(s * 1000)} ms`;
  }
  if (s < 10) {
    return `${s.toFixed(1)} s`;
  }
  if (s < 60) {
    return `${Math.round(s)} s`;
  }
  if (s >= 600 && s < 3600) {
    return `${Math.round(s / 60)} min`; // (seconds do not matter any more)
  }
  if (s < 3600) {
    const sec = Math.round(s);
    const m = Math.floor(sec / 60);
    return sec % 60 ? `${m} min ${sec % 60} s` : `${m} min`;
  }
  const min = Math.round(s / 60);
  return min % 60 ? `${Math.floor(min / 60)} h ${min % 60} min` : `${min / 60} h`;
}

function formatPerRow(s: number): string {
  const sig = (v: number) => String(Number(v.toPrecision(2)));
  return s < 1e-6 ? '<1 µs/row' : s < 1e-3 ? `${sig(s * 1e6)} µs/row` : s < 1 ? `${sig(s * 1e3)} ms/row` : `${sig(s)} s/row`;
}

/** Time bars use a log scale: 1 ms (nearly empty) to 1 h (full); faint ticks at 1 s and 1 min. */
const LOG_MIN = Math.log10(0.001);
const LOG_MAX = Math.log10(3600);
const logFraction = (s: number) => Math.min(Math.max((Math.log10(Math.max(s, 1e-4)) - LOG_MIN) / (LOG_MAX - LOG_MIN), 0.04), 1);
const TICKS = [1, 60].map(logFraction);

/** Local clock time in s seconds (with seconds when it is within the hour). */
const clockAt = (s: number) =>
  new Date(Date.now() + s * 1000).toLocaleTimeString(
    [],
    s < 3600 ? { hour: '2-digit', minute: '2-digit', second: '2-digit' } : { hour: '2-digit', minute: '2-digit' }
  );

const pct = (f: number) => `${Math.round(f * 100)} %`;

/** The numbers behind a node's time bar. */
function timeTooltip(t: NodeTiming, state: NodeState): string {
  const lines: string[] = [];
  const known = t.eta !== null && (t.eta > 0 || !t.etaPartial);
  const plus = t.etaPartial ? '+ (some nodes never ran)' : '';
  if (state === 'EXECUTING') {
    const el = t.elapsed ?? 0;
    lines.push(t.estimate ? `Running ${formatDuration(el)} of about ${formatDuration(t.estimate)} (last run)` : `Running ${formatDuration(el)} (never ran before)`);
    if (t.progress !== null && t.progress !== undefined) {
      lines.push(`${pct(t.progress)} done`);
    }
  } else if (state === 'QUEUED') {
    lines.push(t.estimate ? `Queued; takes about ${formatDuration(t.estimate)} (last run)` : 'Queued (never ran before)');
  } else if (state === 'EXECUTED' && t.time !== null) {
    lines.push(`Run time ${formatDuration(t.time)}`);
    if (t.rows !== null && t.perRow !== null && t.rows > 1) {
      lines.push(`${t.rows} rows · ${formatPerRow(t.perRow)}`);
    }
  } else if (state === 'CONFIGURED' && known) {
    lines.push(`Estimated ${formatDuration(t.eta!)}${plus}`, 'to run it with the not executed nodes before it (from their last runs)');
  }
  if (t.overdue) {
    const over = (t.elapsed ?? 0) - (t.estimate ?? 0);
    lines.push(
      `⚠ Taking longer than expected (last run ${formatDuration(t.estimate ?? 0)})`,
      `Expected to be done at ${clockAt(-over)}, ${formatDuration(over)} ago`
    );
  } else if ((state === 'EXECUTING' || state === 'QUEUED') && known) {
    lines.push(`Done at about ${clockAt(t.eta!)} (in ${formatDuration(t.eta!)}${plus})`);
  }
  if (t.slow && t.estimate) {
    const expected = Math.min((t.elapsed ?? 0) / t.estimate, 1);
    lines.push(`⚠ Progress slower than expected (${pct(t.progress ?? 0)} done, ${pct(expected)} expected by now)`);
  }
  if (t.late) {
    lines.push('⚠ Starts later than expected: a node before it takes longer');
  }
  if (t.total !== null && t.totalKind && (t.total > 0 || !t.totalPartial)) {
    const what = t.totalKind === 'workflow' ? 'Whole workflow from this start node' : 'Everything up to this end node';
    lines.push(`${what}: ${t.totalEstimated ? '≈ ' : ''}${formatDuration(t.total)}${t.totalPartial ? '+' : ''}`);
  }
  lines.push('(bar: log scale, 1 ms to 1 h; ticks at 1 s and 1 min)');
  return lines.join('\n');
}

/**
 * A tiny bar under the status light. Idle: the node's run time (solid) or,
 * configured, the estimated time to run it with the not executed nodes
 * before it (faint), on a log scale. Running: its progress (from the node,
 * else elapsed against its last run), with the expected end in local time
 * below; queued nodes show that end too. Amber / red: slower than expected
 * or past the expected time (or waiting on such a node). The numbers are in
 * the tooltip.
 */
function NodeTimeBar({ t, state }: { t: NodeTiming; state: NodeState }) {
  const running = state === 'EXECUTING' || state === 'QUEUED';
  const known = t.eta !== null && (t.eta > 0 || !t.etaPartial);
  let kind: 'done' | 'estimate' | 'progress' | 'queued' | null = null;
  let fraction = 0;
  let indeterminate = false;
  if (state === 'EXECUTING') {
    kind = 'progress';
    if (t.overdue) {
      fraction = 1;
    } else if (t.progress !== null && t.progress !== undefined) {
      fraction = t.progress;
    } else if (t.estimate) {
      fraction = Math.min((t.elapsed ?? 0) / t.estimate, 1);
    } else {
      indeterminate = true;
    }
  } else if (state === 'QUEUED') {
    kind = 'queued';
  } else if (state === 'EXECUTED' && t.time !== null) {
    kind = 'done';
    fraction = logFraction(t.time);
  } else if (state === 'CONFIGURED' && known) {
    kind = 'estimate';
    fraction = logFraction(t.eta!);
  }
  if (!kind) {
    return null;
  }
  const warn = t.overdue ? ' verknode-timebar-overdue' : t.slow || t.late ? ' verknode-timebar-slow' : '';
  // past its expected end: that end stays (in the past) instead of moving along
  const ets = t.overdue ? (t.estimate ?? 0) - (t.elapsed ?? 0) : t.eta;
  return (
    <div className={`verknode-timebar verknode-timebar-${kind}${warn}`} title={timeTooltip(t, state)}>
      <div className="verknode-timebar-track">
        {(kind === 'done' || kind === 'estimate') && TICKS.map(f => <i key={f} style={{ left: `${f * 100}%` }} />)}
        <div
          className={`verknode-timebar-fill${indeterminate ? ' verknode-timebar-indeterminate' : ''}`}
          style={indeterminate ? undefined : { width: `${fraction * 100}%` }}
        />
      </div>
      {running && (known || t.overdue) && ets !== null && (
        <div className="verknode-timebar-ets">
          {warn && <b>! </b>}
          {clockAt(ets)}
        </div>
      )}
    </div>
  );
}

export function StatusLight({ state }: { state: NodeState }) {
  if (state === 'EXECUTING') {
    return (
      <div className="verknode-status verknode-status-progress" title="Executing">
        <div />
      </div>
    );
  }
  if (state === 'QUEUED') {
    return (
      <div className="verknode-status verknode-status-queued" title="Queued">
        queued
      </div>
    );
  }
  const on = state === 'EXECUTED' ? 'g' : state === 'CONFIGURED' ? 'y' : 'r';
  const title = { r: 'Not configured', y: 'Configured', g: 'Executed' }[on];
  // three segments; the editor's light style (traffic / LED) decides which light up
  return (
    <div className={`verknode-status verknode-status-${on}`} title={title}>
      {['r', 'y', 'g'].map(c => (
        <span key={c} className={`verknode-light verknode-light-${c}${c === on ? ' verknode-on' : ''}`} />
      ))}
    </div>
  );
}

/**
 * A workflow node: name above, colored icon with ports at the
 * sides (black triangles = tables, red circles = flow variables, the
 * implicit flow variable ports at the top corners), traffic light and
 * annotation below.
 */
export const WorkflowNode = memo(({ data, selected, positionAbsoluteX, positionAbsoluteY }: NodeProps<WorkflowNodeType>) => {
  const n = data.node;
  const ins = n.inPorts.filter(p => !p.implicit);
  const outs = n.outPorts.filter(p => !p.implicit);
  // metanodes have no implicit flow variable ports
  const implicitIn = n.inPorts.find(p => p.implicit);
  const implicitOut = n.outPorts.find(p => p.implicit);
  return (
    <div
      className={`verknode-node verknode-state-${n.state}${selected ? ' verknode-selected' : ''}${n.inactive ? ' verknode-node-is-inactive' : ''}${n.kind === 'metanode' ? ' verknode-node-metanode' : ''}`}
      title={n.kind === 'metanode' ? 'Metanode - double-click to open' : undefined}
    >
      {data.navHints && (
        <div className="verknode-nav-hints">
          {data.navHints.keys.map(k => (
            <span key={k} className="verknode-nav-hint">
              {k}
            </span>
          ))}
          {data.navHints.spatial.map(k => (
            <span key={k} className="verknode-nav-hint verknode-nav-spatial">
              {k}
            </span>
          ))}
        </div>
      )}
      <div className="verknode-node-name">{n.name}</div>
      <div className="verknode-node-body">
        <NodeIcon nodeType={n.nodeType} icon={n.icon} category={n.category} />
        <span className="verknode-node-id">
          #{n.id}
        </span>
        {implicitIn && (
          <PortHandle
            kind="target"
            port={implicitIn}
            top="0%"
            connected={data.connectedIn.includes(0)}
            onInspect={data.onInspect && (a => data.onInspect!(n.id, 'in', 0, a))}
          />
        )}
        {implicitOut && (
          <PortHandle
            kind="source"
            port={implicitOut}
            top="0%"
            connected={data.connectedOut.includes(0)}
            inactive={n.inactivePorts.includes(0)}
            onOpen={data.onOpenPort && (tab => data.onOpenPort!(n.id, 0, tab))}
            onInspect={data.onInspect && (a => data.onInspect!(n.id, 'out', 0, a))}
          />
        )}
        {ins.map((p, i) => (
          <PortHandle
            key={p.index}
            kind="target"
            port={p}
            top={portTop(i, ins.length)}
            connected={data.connectedIn.includes(p.index)}
            onInspect={data.onInspect && (a => data.onInspect!(n.id, 'in', p.index, a))}
          />
        ))}
        {outs.map((p, i) => (
          <PortHandle
            key={p.index}
            kind="source"
            port={p}
            top={portTop(i, outs.length)}
            connected={data.connectedOut.includes(p.index)}
            inactive={n.inactivePorts.includes(p.index)}
            onOpen={data.onOpenPort && (tab => data.onOpenPort!(n.id, p.index, tab))}
            onInspect={data.onInspect && (a => data.onInspect!(n.id, 'out', p.index, a))}
          />
        ))}
        {outs.map((p, i) => {
          const rows = n.outRows?.[p.index];
          return rows !== null && rows !== undefined && !n.inactivePorts.includes(p.index) ? (
            <span key={`r${p.index}`} className="verknode-port-rows" style={{ top: portTop(i, outs.length) }} title={`${rows} rows`}>
              {formatRows(rows)}
            </span>
          ) : null;
        })}
        {n.message && (
          <span className={`verknode-node-badge verknode-badge-${n.message.level}`} title={n.message.text}>
            {n.message.level === 'ERROR' ? '✖' : n.message.level === 'INFO' ? 'i' : '!'}
          </span>
        )}
        {n.loopIteration && (
          <span
            className="verknode-loop-pill"
            title={`Loop iteration ${n.loopIteration.iteration}${n.loopIteration.total ? ` of ${n.loopIteration.estimated ? 'about ' : ''}${n.loopIteration.total}${n.loopIteration.estimated ? ' (as the last run)' : ''}` : ''}`}
          >
            {n.loopIteration.iteration}
            {n.loopIteration.total ? `/${n.loopIteration.estimated ? '~' : ''}${n.loopIteration.total}` : ''}
          </span>
        )}
        {n.usesVariables && (
          <span className="verknode-node-varbadge" title="Settings controlled by flow variables">
            v
          </span>
        )}
      </div>
      <NodeLight node={n} onAction={data.onLight && (action => data.onLight!(n.id, action))} />
      {n.timing && <NodeTimeBar t={n.timing} state={n.state} />}
      {n.widget && (
        <WidgetPanel
          widget={n.widget}
          layout={n.widgetLayout ?? {}}
          id={n.id}
          at={{ x: positionAbsoluteX, y: positionAbsoluteY }}
          tails={data.tails}
          selection={data.widgetSelection}
          groupStarts={data.widgetStarts}
          onGroupMoved={data.onWidgetsMoved}
          onAnchor={value => data.onAnchor?.(n.id, value)}
          dockEnv={data.dockEnv}
          onDropped={data.onWidgetDropped && (layout => data.onWidgetDropped!(n.id, layout))}
          name={n.name}
          executing={n.state === 'EXECUTING' || n.state === 'QUEUED'}
          busy={!!data.busy}
          onChange={v => data.onWidget?.(n.id, v)}
          onSelect={sel => data.onSelect?.(n.id, sel)}
          onSend={v => data.onSend?.(n.id, v)}
          onLayout={layout => data.onWidgetLayout?.(n.id, layout) ?? Promise.resolve()}
          onSketch={() => data.onSketch?.(n.id)}
        />
      )}
      <div className="verknode-node-annotation">{n.annotation}</div>
    </div>
  );
});

/**
 * Port bar inside a metanode: the metanode's input ports (left bar, sources
 * for the nodes inside) or output ports (right bar, targets).
 */
export const MetanodeBar = memo(({ data }: NodeProps<WorkflowNodeType>) => {
  const n = data.node;
  const isIn = n.kind === 'inbar';
  const ports = isIn ? n.outPorts : n.inPorts;
  return (
    <div
      className={`verknode-metabar verknode-metabar-${isIn ? 'in' : 'out'}`}
      style={{ height: data.barHeight ?? 160 }}
      title={`Metanode ${isIn ? 'input' : 'output'} ports (right-click to add one)`}
    >
      <div className="verknode-metabar-label">{isIn ? 'in' : 'out'}</div>
      {ports.map((p, i) =>
        isIn ? (
          <PortHandle
            key={p.index}
            kind="source"
            port={p}
            top={portTop(i, ports.length)}
            connected={data.connectedOut.includes(p.index)}
            onOpen={data.onOpenPort && (tab => data.onOpenPort!(n.id, p.index, tab))}
          />
        ) : (
          <PortHandle key={p.index} kind="target" port={p} top={portTop(i, ports.length)} connected={data.connectedIn.includes(p.index)} />
        )
      )}
    </div>
  );
});
