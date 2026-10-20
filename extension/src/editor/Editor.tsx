import { Dialog, InputDialog, showDialog } from '@jupyterlab/apputils';
import {
  Background,
  BackgroundVariant,
  Connection,
  ControlButton,
  Controls,
  Edge,
  IsValidConnection,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useReactFlow,
  ViewportPortal
} from '@xyflow/react';
import React, { useCallback, useDeferredValue, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';

import { ExportOptions, WorkflowController } from '../controller';
import { FlowVariableInfo, GroupInfo, NodeDescription, NodeInfo, VarType, WidgetLayout, WorkflowView } from '../types';
import { ConfigureDialog, VarBadge } from './ConfigureDialog';
import { EditorPreferences, EditorSettings } from '../settings';
import { SettingsDialog } from './SettingsDialog';
import { Modal } from './Modal';
import { MetanodeBar, WorkflowNode, WorkflowNodeType } from './NodeView';
import { nodeColor, themeOf, themeStyle } from './themes';
import { flatEnd, innerNodes, levelPath, projectLevel } from './metanodes';
import { Link, planAutoConnect, planBridge, planInsert } from './wiring';
import { ChildWindow, openChildWindow, ViewPlace, viewMoves } from './ChildWindow';
import { activateInLab, labAvailable, LabViewWidget, openFileInLab, openInLab } from './labViews';
import { KEY_DIRECTIONS, NavigationGraph, navigationHints, navigationTarget, parentsOf } from './navigation';
import { QuickAdd } from './QuickAdd';
import { bubbleShapes, findScopeBlocks, layoutBlocks, Rect, ScopeBlock } from './scopes';
import { GroupDialog, groupTitle } from './GroupDialog';
import { compatibleInput, recordTransition, suggestNodes } from './hinting';
import { copyText, getFragment, hasFragment, setFragment, trackCopy } from './clipboard';
import { NodeViewDialog } from './NodeViewDialog';
import { PortView } from './PortView';
import { Description, NODE_DRAG_MIME, Repository } from './Repository';
import { SketcherDialog } from './sketcher/SketcherDialog';
import { useWidgetSelection, WidgetSelection } from './widgets/selection';
import { dockGroup, outlinePath, Rect as WidgetRect } from './widgets/snap';
import { TailStore, useTails } from './widgets/tails';
import { DisplayLoader } from './widgets/frames';
import { CanvasLink, CanvasLinkContext } from './widgets/registry';
import { InspectLayer, Inspection, INSPECT_H, INSPECT_W, obstacles, place, portPoint } from './Inspector';

const nodeTypes = { node: WorkflowNode, metabar: MetanodeBar };

type Menu = { x: number; y: number; nodeId: number | null; groupId?: number | null; inspection?: string };

/** A port or node view shown in a child browser window. */
type External = { key: number; win: Window; kind: 'port' | 'view'; node: number; port: number; tab: boolean };

function parseHandle(h: string | null | undefined): number {
  return h ? parseInt(h.split('-')[1], 10) : 0;
}

function edgeId(c: { sourceID: number; sourcePort: number; destID: number; destPort: number }): string {
  return `${c.sourceID}:${c.sourcePort}-${c.destID}:${c.destPort}`;
}

function useView(controller: WorkflowController): WorkflowView | null {
  const [view, setView] = useState<WorkflowView | null>(controller.view);
  useEffect(() => {
    const onChange = (_: unknown, v: WorkflowView) => setView(v);
    controller.changed.connect(onChange);
    return () => {
      controller.changed.disconnect(onChange);
    };
  }, [controller]);
  return view;
}

/* ------------------------------------------------------------------ */
/** Speech-bubble tails of the canvas widgets: one layer under all nodes and widgets (above bubbles). */
function WidgetTails({ store }: { store: TailStore }) {
  const tails = useTails(store);
  return tails.size ? (
    <svg className="verknode-widget-tails" width="1" height="1">
      {[...tails].map(([id, points]) => (
        <polygon key={id} points={points} />
      ))}
    </svg>
  ) : null;
}

/**
 * What dragging the hovered widget title bar would move: one outline around
 * those widgets (a docked group as a whole), and a dotted one around the
 * widgets of its group that would stay.
 */
function WidgetHints({ selection, rects }: { selection: WidgetSelection; rects: () => Map<number, WidgetRect> }) {
  useWidgetSelection(selection);
  const hint = selection.hint;
  if (!hint) {
    return null;
  }
  const all = rects();
  const pick = (ids: number[]) => ids.map(id => all.get(id)).filter((r): r is WidgetRect => !!r);
  return (
    <svg className="verknode-widget-hints" width="1" height="1">
      <path className="verknode-hint-staying" d={outlinePath(pick(hint.staying), 4)} />
      <path className="verknode-hint-moving" d={outlinePath(pick(hint.moving), 4)} />
    </svg>
  );
}

/* ------------------------------------------------------------------ */
function ContextMenu(props: {
  menu: Menu;
  items: ({ label: string; shortcut?: string; disabled?: boolean; action: (e: React.MouseEvent) => void } | '-')[];
  onClose: () => void;
}) {
  useEffect(() => {
    const close = () => props.onClose();
    window.addEventListener('mousedown', close);
    window.addEventListener('blur', close);
    return () => {
      window.removeEventListener('mousedown', close);
      window.removeEventListener('blur', close);
    };
  }, []);
  // (kept inside the window: moved up / left by what would stick out)
  const ref = useRef<HTMLDivElement>(null);
  const [shift, setShift] = useState({ x: 0, y: 0 });
  useLayoutEffect(() => {
    const r = ref.current?.getBoundingClientRect();
    if (r) {
      const margin = 4;
      setShift({
        x: Math.min(0, window.innerWidth - margin - r.right),
        y: r.height > window.innerHeight - 2 * margin ? margin - r.top : Math.min(0, window.innerHeight - margin - r.bottom)
      });
    }
  }, [props.menu.x, props.menu.y]);
  return (
    <div
      ref={ref}
      className="verknode-menu"
      style={{ left: props.menu.x + shift.x, top: props.menu.y + shift.y, maxHeight: window.innerHeight - 8, overflowY: 'auto' }}
      onMouseDown={e => e.stopPropagation()}
    >
      {props.items.map((it, i) =>
        it === '-' ? (
          <div key={i} className="verknode-menu-sep" />
        ) : (
          <div
            key={i}
            className={`verknode-menu-item ${it.disabled ? 'verknode-disabled' : ''}`}
            onClick={e => {
              if (!it.disabled) {
                props.onClose();
                it.action(e);
              }
            }}
          >
            <span>{it.label}</span>
            {it.shortcut && <span className="verknode-menu-shortcut">{it.shortcut}</span>}
          </div>
        )
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
function WorkflowVariablesDialog(props: { controller: WorkflowController; onClose: () => void }) {
  const [vars, setVars] = useState<FlowVariableInfo[]>(
    JSON.parse(JSON.stringify(props.controller.view?.workflowVariables ?? []))
  );
  const [error, setError] = useState<string | null>(null);
  const set = (i: number, patch: Partial<FlowVariableInfo>) => setVars(vars.map((v, j) => (j === i ? { ...v, ...patch } : v)));
  const ok = async () => {
    try {
      await props.controller.op('set_workflow_variables', { variables: vars });
      props.onClose();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <Modal
      title="Workflow Variables"
      onClose={props.onClose}
      className="verknode-wfvars"
      footer={
        <>
          {error && <div className="verknode-dialog-error">{error}</div>}
          <button className="jp-Dialog-button jp-mod-accept jp-mod-styled" onClick={ok}>
            <div className="jp-Dialog-buttonLabel">OK</div>
          </button>
          <button className="jp-Dialog-button jp-mod-reject jp-mod-styled" onClick={props.onClose}>
            <div className="jp-Dialog-buttonLabel">Cancel</div>
          </button>
        </>
      }
    >
      <p className="verknode-field-help">
        Workflow variables are available to all nodes. Changing them resets the workflow.
      </p>
      <table className="verknode-fv-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Type</th>
            <th>Value</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {vars.map((v, i) => (
            <tr key={i}>
              <td>
                <input className="jp-mod-styled" value={v.name} onChange={e => set(i, { name: e.target.value })} />
              </td>
              <td>
                <select className="jp-mod-styled" value={v.type} onChange={e => set(i, { type: e.target.value as VarType })}>
                  {['STRING', 'INTEGER', 'DOUBLE', 'BOOLEAN'].map(t => (
                    <option key={t}>{t}</option>
                  ))}
                </select>
              </td>
              <td>
                <input className="jp-mod-styled" value={String(v.value)} onChange={e => set(i, { value: e.target.value })} />
              </td>
              <td>
                <button className="verknode-icon-btn" onClick={() => setVars(vars.filter((_, j) => j !== i))}>
                  ✕
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <button
        className="jp-Button jp-mod-styled verknode-add-btn"
        onClick={() => setVars([...vars, { name: `var${vars.length + 1}`, type: 'STRING', value: '' }])}
      >
        + Add
      </button>
    </Modal>
  );
}

/* ------------------------------------------------------------------ */
function Console(props: {
  controller: WorkflowController;
  open: boolean;
  onToggle: () => void;
  onSelectNode: (id: number) => void;
  onPython: () => void;
  python: boolean;
  setPython: (on: boolean) => void;
}) {
  const [debug, setDebug] = useState(false);
  const { python, setPython } = props;
  const ref = useRef<HTMLDivElement>(null);
  const logs = props.controller.logs.filter(
    l => (debug || l.level !== 'DEBUG') && (python || l.level !== 'PY')
  );
  const copyCommands = () =>
    void copyText(
      props.controller.logs
        .filter(l => l.level === 'PY')
        .map(l => l.text)
        .join('\n')
    );
  useEffect(() => {
    ref.current?.scrollTo({ top: ref.current.scrollHeight });
  }, [logs.length, props.open]);
  const errors = props.controller.logs.filter(l => l.level === 'ERROR').length;
  return (
    <div className={`verknode-console ${props.open ? 'verknode-console-open' : ''}`}>
      <div className="verknode-console-head">
        <span className="verknode-console-title" onClick={props.onToggle}>
          {props.open ? '▾' : '▸'} Console {errors > 0 && <span className="verknode-console-errors">{errors} error(s)</span>}
        </span>
        {props.open && (
          <span className="verknode-console-tools">
            <label title="Show the verknode Python command for every edit">
              <input type="checkbox" checked={python} onChange={e => setPython(e.target.checked)} /> python
            </label>
            <label>
              <input type="checkbox" checked={debug} onChange={e => setDebug(e.target.checked)} /> debug
            </label>
            <button className="jp-Button" onClick={copyCommands} title="Copy all Python commands">
              Copy commands
            </button>
            <button className="jp-Button" onClick={props.onPython} title="Python script that rebuilds this workflow">
              Export as Python…
            </button>
            <button className="jp-Button" onClick={() => props.controller.clearLog()}>
              Clear
            </button>
          </span>
        )}
      </div>
      {props.open && (
        <div className="verknode-console-body" ref={ref}>
          {logs.map(l => (
            <div key={l.seq} className={`verknode-log verknode-log-${l.level}`}>
              <span className="verknode-log-time">{l.time}</span>
              <span className="verknode-log-level">{l.level === 'PY' ? '>>>' : l.level}</span>
              {l.node !== null && (
                <a className="verknode-log-node" onClick={() => props.onSelectNode(l.node!)}>
                  #{l.node}
                </a>
              )}
              <span
                className="verknode-log-text"
                title={l.level === 'PY' ? 'Click to copy' : undefined}
                onClick={l.level === 'PY' ? () => void copyText(l.text) : undefined}
              >
                {l.text}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
function PythonDialog(props: { controller: WorkflowController; groups: GroupInfo[]; selected: number[]; onClose: () => void }) {
  const [tab, setTab] = useState<'live' | 'standalone'>('live');
  // what to export: '' (all), 'group:<text>', 'selected'
  const [part, setPart] = useState('');
  const [opts, setOpts] = useState({ headless: true, snapshot: false, data: true, docker: false, api: false });
  const [exported, setExported] = useState<{ exported: boolean; exists?: boolean; folder: string; text?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const options: ExportOptions = {
    group: part.startsWith('group:') ? part.slice(6) : null,
    nodes: part === 'selected' ? props.selected : null,
    headless: opts.headless,
    snapshot: !!part && opts.snapshot
  };
  const [data, setData] = useState<{ script: string; hint: string } | null>(null);
  const [standalone, setStandalone] = useState<{ script: string; path: string; notes?: string[]; problems?: string[] } | null>(null);
  const [saved, setSaved] = useState<{ saved: boolean; exists?: boolean; path: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    props.controller
      .python()
      .then(setData)
      .catch(e => setError(e.message));
  }, []);
  useEffect(() => {
    setError(null);
    props.controller
      .standalonePython(options)
      .then(setStandalone)
      .catch(e => setError(e.message));
  }, [part, opts.headless]);
  const script = tab === 'live' ? data?.script : standalone?.script;
  const save = (overwrite: boolean) =>
    props.controller
      .saveStandalonePython(overwrite, options)
      .then(setSaved)
      .catch(e => setError(e.message));
  const exportFolder = (overwrite: boolean) => {
    setBusy(true);
    setError(null);
    props.controller
      .exportFolder({ ...options, data: opts.data, docker: opts.docker, api: opts.api, overwrite })
      .then(setExported)
      .catch(e => setError(e.message))
      .finally(() => setBusy(false));
  };
  const flag = (key: keyof typeof opts, label: string, title: string) => (
    <label title={title}>
      <input type="checkbox" checked={opts[key]} onChange={e => setOpts({ ...opts, [key]: e.target.checked })} /> {label}
    </label>
  );
  const file = standalone?.path.split('/').pop() ?? 'workflow.py';
  return (
    <Modal
      title="Python"
      onClose={props.onClose}
      className="verknode-python-dialog"
      footer={
        <>
          {error && <div className="verknode-dialog-error">{error}</div>}
          {tab === 'standalone' && (
            <button
              className="jp-Dialog-button jp-mod-accept jp-mod-styled"
              disabled={busy}
              title="A folder that runs on its own: the script, the parts of verknode it uses, its data, requirements.txt (only what it needs) - and a Dockerfile / API server if chosen"
              onClick={() => exportFolder(!!exported?.exists && !exported.exported)}
            >
              <div className="jp-Dialog-buttonLabel">
                {busy ? 'Exporting…' : exported?.exists && !exported.exported ? 'Replace the folder' : 'Export folder'}
              </div>
            </button>
          )}
          {tab === 'standalone' && (
            <button
              className="jp-Dialog-button jp-mod-reject jp-mod-styled"
              disabled={!standalone}
              title={`Save it as ${standalone?.path ?? ''}, next to the workflow`}
              onClick={() => void save(!!saved?.exists)}
            >
              <div className="jp-Dialog-buttonLabel">{saved?.exists && !saved.saved ? `Replace ${file}` : `Save as ${file}`}</div>
            </button>
          )}
          <button
            className={`jp-Dialog-button jp-mod-styled ${tab === 'live' ? 'jp-mod-accept' : 'jp-mod-reject'}`}
            disabled={!script}
            onClick={() => script && void copyText(script).then(() => setCopied(true))}
          >
            <div className="jp-Dialog-buttonLabel">{copied ? 'Copied' : 'Copy script'}</div>
          </button>
          <button className="jp-Dialog-button jp-mod-reject jp-mod-styled" onClick={props.onClose}>
            <div className="jp-Dialog-buttonLabel">Close</div>
          </button>
        </>
      }
    >
      <div className="verknode-python-tabs">
        <button className={tab === 'live' ? 'verknode-python-tab-on' : ''} onClick={() => (setTab('live'), setCopied(false))}>
          Live, in this JupyterLab
        </button>
        <button className={tab === 'standalone' ? 'verknode-python-tab-on' : ''} onClick={() => (setTab('standalone'), setCopied(false))}>
          Standalone script
        </button>
      </div>
      {tab === 'live' ? (
        <>
          <p className="verknode-field-help">
            Control this live workflow from a notebook or console in this JupyterLab (changes show up here
            immediately). Every edit you make in the editor is also shown as a <code>&gt;&gt;&gt;</code> command in
            the console below the canvas.
          </p>
          {data && <pre className="verknode-python-hint">{data.hint}</pre>}
          <p className="verknode-field-help">Script that rebuilds this workflow:</p>
          {data && <pre className="verknode-python-script">{data.script}</pre>}
          <p className="verknode-field-help">
            API: <code>wf.add_node(name, x, y, **settings)</code>, <code>wf.connect(a, b, src_port=1, dst_port=1)</code>,{' '}
            <code>node.configure(**settings)</code>, <code>node.bind(setting, variable)</code>, <code>wf.execute()</code>,{' '}
            <code>node.table()</code>, <code>node.help()</code>, <code>verknode.nodes("search")</code>, <code>wf.undo()</code>,{' '}
            <code>wf.save()</code>.
          </p>
        </>
      ) : (
        <>
          <p className="verknode-field-help">
            The workflow as a Python script that rebuilds and runs it <b>without Jupyter</b> - from a terminal, cron, CI, a
            container or another program: <code>--var NAME=VALUE</code> (workflow variables), <code>--out DIR</code> (write the
            result tables; <code>--format parquet</code>), <code>--save copy.vkf</code>. <b>Save as</b> writes the script
            (it needs the jupyterlab-verknode package); <b>Export folder</b> writes a folder that runs on its own - the
            script, the parts of verknode it uses, its data and a requirements.txt of only what it needs (measured by
            running it) - with a Dockerfile and an HTTP API server if you like.
          </p>
          <div className="verknode-export-options">
            <label>
              Export{' '}
              <select value={part} onChange={e => setPart(e.target.value)}>
                <option value="">the whole workflow</option>
                {props.groups
                  .filter(g => g.text)
                  .map(g => (
                    <option key={g.id} value={`group:${g.text}`}>
                      the group "{g.text}"
                    </option>
                  ))}
                {props.selected.length > 0 && <option value="selected">the {props.selected.length} selected node(s)</option>}
              </select>
            </label>
            {flag('headless', 'Headless', 'Leave out what only the canvas uses: displays, views and nodes no result needs; input widgets become workflow variables (--var)')}
            {part && flag('snapshot', 'Snapshot the data coming in', 'Instead of the nodes before the part: their tables, saved in the folder (export folder only)')}
            <span className="verknode-export-sep">Folder:</span>
            {flag('data', 'Data files', 'Copy the files the workflow reads into the folder (data/)')}
            {flag('docker', 'Dockerfile', 'A Dockerfile to run it in a container')}
            {flag('api', 'API server', 'server.py: an HTTP API that runs the workflow with the variables of a request')}
          </div>
          {standalone?.notes && standalone.notes.length > 0 && (
            <ul className="verknode-export-notes">
              {standalone.notes.map((n, i) => (
                <li key={i}>{n}</li>
              ))}
            </ul>
          )}
          {standalone?.problems?.map((p, i) => (
            <p key={i} className="verknode-dialog-error">
              {p}
            </p>
          ))}
          {exported?.exported && (
            <>
              <p className="verknode-field-help">
                Exported to <code>{exported.folder}/</code>.{' '}
                <button className="verknode-chat-link" onClick={() => openFileInLab(`${exported.folder}/README.md`)}>
                  Open its README
                </button>
              </p>
              <pre className="verknode-python-hint verknode-export-report">{exported.text}</pre>
            </>
          )}
          {exported?.exists && !exported.exported && (
            <p className="verknode-dialog-error">
              {exported.folder}/ exists already: <b>Replace the folder</b> exports again over it.
            </p>
          )}
          <pre className="verknode-python-hint">{`python ${file} --out results/`}</pre>
          {saved?.saved && (
            <p className="verknode-field-help">
              Saved <code>{saved.path}</code>.{' '}
              <button className="verknode-chat-link" onClick={() => openFileInLab(saved.path)}>
                Open it
              </button>
            </p>
          )}
          {saved?.exists && !saved.saved && (
            <p className="verknode-dialog-error">
              {saved.path} exists already: <b>Replace {file}</b> overwrites it.
            </p>
          )}
          {standalone && <pre className="verknode-python-script">{standalone.script}</pre>}
          <p className="verknode-field-help">
            A <code>.vkf</code> file also runs as it is: <code>python -m jupyterlab_verknode.standalone workflow.vkf --var NAME=VALUE --out results/</code>.
          </p>
        </>
      )}
    </Modal>
  );
}

/* ------------------------------------------------------------------ */
function usePreferences(settings: EditorSettings): EditorPreferences {
  const [values, setValues] = useState(settings.values);
  useEffect(() => {
    const onChange = (_: unknown, v: EditorPreferences) => setValues(v);
    settings.changed.connect(onChange);
    return () => {
      settings.changed.disconnect(onChange);
    };
  }, [settings]);
  return values;
}

function EditorCanvas({ controller, settings }: { controller: WorkflowController; settings: EditorSettings }) {
  const prefs = usePreferences(settings);
  const theme = themeOf(prefs.theme);
  const rawView = useView(controller);
  // the editor shows one level: the workflow (null) or the inside of a metanode
  const [level, setLevel] = useState<number | null>(null);
  const view = useMemo(() => (rawView ? projectLevel(rawView, level) : null), [rawView, level]);
  useEffect(() => {
    // the shown metanode is gone (deleted, expanded, undone): show the closest existing level
    if (rawView && level !== null && !(rawView.metanodes ?? []).some(m => m.id === level)) {
      setLevel(null);
    }
  }, [rawView, level]);
  const flow = useReactFlow();
  const flashing = useRef<Set<string>>(new Set()); // (nodes a display points at: canvasLink.highlight)
  // inspection widgets (Inspector.tsx): the one shown while a port is hovered ("peek") and kept ones
  const [inspections, setInspections] = useState<Inspection[]>([]);
  const inspectRef = useRef<
    (node: number, kind: 'in' | 'out', port: number, action: 'enter' | 'leave' | 'pin', near?: { x: number; y: number; w: number }) => void
  >();
  const inspectNodeRef = useRef<(node: number | null, pin?: boolean, near?: DOMRect) => void>();
  const tails = useMemo(() => new TailStore(), []);
  // selected canvas widgets (marquee, Ctrl+click, or with their nodes unless anchored)
  const widgetSel = useMemo(() => new WidgetSelection(), []);
  const widgetOps = useRef({
    starts: () => new Map<number, { x: number; y: number }>(),
    moved: (_: Map<number, { x: number; y: number }>) => undefined as void,
    anchor: (_: number, __: boolean) => undefined as void,
    env: () => ({ rects: new Map<number, { x: number; y: number; w: number; h: number }>(), members: new Set<number>() }),
    dropped: (_: number, __: WidgetLayout) => Promise.resolve() as Promise<unknown>
  });
  const wrapper = useRef<HTMLDivElement>(null);
  /** Fit to screen: the nodes and their canvas widgets (React Flow's own fit only knows the nodes). */
  const fitAll = (duration = 0) => {
    const el = wrapper.current;
    const boxes = el
      ? Array.from(el.querySelectorAll('.react-flow__node, .verknode-widget-dock')).map(e => e.getBoundingClientRect()).filter(b => b.width && b.height)
      : [];
    if (!boxes.length) {
      void flow.fitView({ padding: 0.25, maxZoom: 1, duration });
      return;
    }
    const a = flow.screenToFlowPosition({ x: Math.min(...boxes.map(b => b.left)), y: Math.min(...boxes.map(b => b.top)) });
    const b = flow.screenToFlowPosition({ x: Math.max(...boxes.map(b => b.right)), y: Math.max(...boxes.map(b => b.bottom)) });
    void flow.fitBounds({ x: a.x, y: a.y, width: b.x - a.x, height: b.y - a.y }, { padding: 0.06, duration }).then(() => {
      if (flow.getZoom() > 1) {
        void flow.zoomTo(1);
      }
    });
  };
  const [nodes, setNodes, onNodesChange] = useNodesState<WorkflowNodeType>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [repo, setRepo] = useState<NodeDescription[]>([]);
  const [described, setDescribed] = useState<NodeDescription | null>(null);
  const [configure, setConfigure] = useState<number | null>(null);
  const [portView, setPortView] = useState<{ node: number; port: number } | null>(null);
  const [nodeView, setNodeView] = useState<number | null>(null);
  const pendingViews = useRef<Set<number>>(new Set());
  const [externals, setExternals] = useState<External[]>([]);
  const externalKey = useRef(0);
  const externalsRef = useRef<External[]>([]);
  externalsRef.current = externals;
  const [menu, setMenu] = useState<Menu | null>(null);
  const [wfVars, setWfVars] = useState(false);
  const [pythonOpen, setPythonOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const editorRef = useRef<HTMLDivElement>(null);
  // keyboard focus was last inside the editor (not moved elsewhere on purpose)
  const focusInside = useRef(false);
  const selectionBeforeClick = useRef<Set<string>>(new Set());
  const [panning, setPanning] = useState(false);

  const [band, setBand] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null);

  /**
   * Shift + drag on the empty canvas draws a selection rectangle (with Ctrl:
   * added to the selection). Done here so it works whatever has the keyboard
   * focus in JupyterLab.
   */
  const startBand = (e: React.PointerEvent) => {
    const target = e.target as HTMLElement;
    const canvas = wrapper.current;
    if (e.button !== 0 || !canvas || !target.closest('.react-flow__pane') || target.closest('.react-flow__node')) {
      return false;
    }
    e.preventDefault();
    e.stopPropagation();
    const r = canvas.getBoundingClientRect();
    const start = { x: e.clientX, y: e.clientY };
    const keep = e.ctrlKey || e.metaKey ? new Set(nodes.filter(n => n.selected).map(n => n.id)) : new Set<string>();
    setBand({ x0: start.x - r.left, y0: start.y - r.top, x1: start.x - r.left, y1: start.y - r.top });
    const move = (ev: PointerEvent) =>
      setBand({ x0: start.x - r.left, y0: start.y - r.top, x1: ev.clientX - r.left, y1: ev.clientY - r.top });
    const up = (ev: PointerEvent) => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      setBand(null);
      // the click that ends the drag must not reach the pane (it clears the selection)
      const swallow = (ce: MouseEvent) => {
        ce.stopPropagation();
        ce.preventDefault();
      };
      window.addEventListener('click', swallow, { capture: true, once: true });
      window.setTimeout(() => window.removeEventListener('click', swallow, true), 0);
      const a = flow.screenToFlowPosition({ x: Math.min(start.x, ev.clientX), y: Math.min(start.y, ev.clientY) });
      const b = flow.screenToFlowPosition({ x: Math.max(start.x, ev.clientX), y: Math.max(start.y, ev.clientY) });
      const hit = new Set(keep);
      for (const n of flow.getNodes()) {
        const w = n.measured?.width ?? 0;
        const h = n.measured?.height ?? 0;
        if (n.position.x < b.x && n.position.x + w > a.x && n.position.y < b.y && n.position.y + h > a.y) {
          hit.add(n.id);
        }
      }
      setNodes(ns => ns.map(n => ({ ...n, selected: hit.has(n.id) })));
      setEdges(es => es.map(x => ({ ...x, selected: false })));
      widgetSel.setExplicit(widgetsIn(start.x, start.y, ev.clientX, ev.clientY), keep.size > 0 || e.ctrlKey || e.metaKey);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    return true;
  };

  /**
   * Ctrl/Cmd + drag on the empty canvas pans the view. Done here because
   * React Flow never pans while Ctrl is held (reserved for ctrl+wheel zoom)
   * and misses key presses while focus is elsewhere in JupyterLab.
   */
  const startCtrlPan = (e: React.PointerEvent) => {
    const target = e.target as HTMLElement;
    if (e.button !== 0 || !target.closest('.react-flow__pane') || target.closest('.react-flow__node')) {
      return false;
    }
    e.preventDefault();
    e.stopPropagation();
    const start = { x: e.clientX, y: e.clientY, viewport: flow.getViewport() };
    setPanning(true);
    const move = (ev: PointerEvent) =>
      flow.setViewport({
        x: start.viewport.x + ev.clientX - start.x,
        y: start.viewport.y + ev.clientY - start.y,
        zoom: start.viewport.zoom
      });
    const up = () => {
      setPanning(false);
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    return true;
  };
  useEffect(() => {
    // modifier cursors: Ctrl (Cmd) = pan, Shift = selection rectangle. The
    // classes are toggled directly so key presses don't re-render the canvas.
    const set = (ctrl: boolean, shift: boolean) => {
      editorRef.current?.classList.toggle('verknode-pan-mode', ctrl && !shift);
      editorRef.current?.classList.toggle('verknode-select-mode', shift);
      editorRef.current?.classList.toggle('verknode-ctrl', ctrl);
    };
    const update = (e: KeyboardEvent) => set(e.ctrlKey || e.metaKey, e.shiftKey);
    const off = () => set(false, false);
    window.addEventListener('keydown', update);
    window.addEventListener('keyup', update);
    window.addEventListener('blur', off);
    return () => {
      window.removeEventListener('keydown', update);
      window.removeEventListener('keyup', update);
      window.removeEventListener('blur', off);
    };
  }, []);

  // Arrow keys move by position; holding Alt follows the connections (lineage)
  // instead. A double tap on Alt locks lineage mode until Alt is tapped again or
  // nothing is selected any more.
  const lineageLock = useRef(false);
  const setLineageLock = (on: boolean) => {
    lineageLock.current = on;
    editorRef.current?.classList.toggle('verknode-lineage-lock', on);
  };
  useEffect(() => {
    const TAP = 350; // ms: longest press that counts as a tap, longest gap of a double tap
    let down: number | null = null; // when Alt went down, while no other key was pressed
    let lastTap = 0;
    const setHeld = (held: boolean) => editorRef.current?.classList.toggle('verknode-alt', held);
    const keydown = (e: KeyboardEvent) => {
      if (e.key === 'Alt') {
        if (!e.repeat) {
          down = e.ctrlKey || e.metaKey || e.shiftKey ? null : performance.now();
        }
        setHeld(true);
      } else {
        down = null; // Alt+key is not a tap
        setHeld(e.altKey);
      }
    };
    const keyup = (e: KeyboardEvent) => {
      if (e.key !== 'Alt') {
        setHeld(e.altKey);
        return;
      }
      setHeld(false);
      const now = performance.now();
      const tap = down !== null && now - down < TAP && focusInside.current;
      down = null;
      if (!tap) {
        return;
      }
      e.preventDefault(); // (keeps some browsers from focusing their menu bar)
      if (lineageLock.current) {
        setLineageLock(false);
        lastTap = 0;
      } else if (now - lastTap < TAP * 2) {
        setLineageLock(true);
        lastTap = 0;
      } else {
        lastTap = now;
      }
    };
    const off = () => {
      down = null;
      setHeld(false);
    };
    // (capture: the editor stops the keys it handles from bubbling)
    window.addEventListener('keydown', keydown, true);
    window.addEventListener('keyup', keyup, true);
    window.addEventListener('blur', off);
    return () => {
      window.removeEventListener('keydown', keydown, true);
      window.removeEventListener('keyup', keyup, true);
      window.removeEventListener('blur', off);
    };
  }, []);
  const pendingSelect = useRef<Set<string> | null>(null);
  const [consoleOpen, setConsoleOpen] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const fitted = useRef(false);

  // node repository (built-in + custom nodes); reloaded when custom nodes change
  const registryVersion = view?.registryVersion;
  useEffect(() => {
    if (registryVersion === undefined) {
      return;
    }
    controller
      .nodeTypes()
      .then(setRepo)
      .catch(e => setError(`Could not load node repository: ${e.message}`));
  }, [controller, registryVersion]);

  // clear the "custom node file(s) failed" banner once a reload succeeded
  const customErrors = view?.customReport?.errors.length ?? 0;
  useEffect(() => {
    if (!customErrors) {
      setError(e => (e && e.includes('custom node file') ? null : e));
    }
  }, [customErrors, view?.customReport?.time]);

  useEffect(() => {
    const onFail = (_: unknown, msg: string) => setError(msg);
    controller.failed.connect(onFail);
    return () => {
      controller.failed.disconnect(onFail);
    };
  }, [controller]);

  const byId = useMemo(() => new Map((view?.nodes ?? []).map(n => [n.id, n])), [view]);
  const repoByFactory = useMemo(() => new Map(repo.map(r => [r.factory, r])), [repo]);

  // a focused node that disappeared (deleted, undone) takes keyboard focus with it: give
  // it back to the editor so shortcuts keep working
  useEffect(() => {
    const doc = editorRef.current?.ownerDocument;
    const active = doc?.activeElement;
    const editor = editorRef.current;
    if (focusInside.current && editor && (!active || active === doc?.body || (active !== editor && active.contains(editor)))) {
      editor.focus({ preventScroll: true });
    }
  });

  // backend view -> React Flow nodes / edges (keep selection)
  useEffect(() => {
    if (!view) {
      return;
    }
    const connIn = new Map<number, number[]>();
    const connOut = new Map<number, number[]>();
    for (const c of view.connections) {
      connIn.set(c.destID, [...(connIn.get(c.destID) ?? []), c.destPort]);
      connOut.set(c.sourceID, [...(connOut.get(c.sourceID) ?? []), c.sourcePort]);
    }
    setNodes(prev => {
      const prevById = new Map(prev.map(n => [n.id, n]));
      const levelYs = view.nodes.filter(n => n.id > 0).map(n => n.y);
      const barHeight = levelYs.length ? Math.max(...levelYs) - Math.min(...levelYs) + 140 : 160;
      return view.nodes.map(n => {
        const old = prevById.get(String(n.id));
        const bar = n.kind === 'inbar' || n.kind === 'outbar';
        return {
          id: String(n.id),
          type: (bar ? 'metabar' : 'node') as 'metabar' | 'node',
          ...(bar ? { draggable: false, deletable: false, selectable: false } : {}),
          // widgets float above plain nodes (a widget moved over a node stays visible)
          ...(n.widget ? { zIndex: 1 } : {}),
          position: old?.dragging ? old.position : { x: n.x, y: n.y },
          // keep the measured size: React Flow hides nodes it thinks are unmeasured, and a
          // hidden editor tab (e.g. while its notebook runs) cannot measure them again
          ...(old?.measured ? { measured: old.measured } : {}),
          selected: pendingSelect.current ? pendingSelect.current.has(String(n.id)) : (old?.selected ?? false),
          // (nodes a display points at flash for a moment, also while the view changes)
          ...(flashing.current.has(String(n.id)) ? { className: 'verknode-flash' } : {}),
          dragging: old?.dragging,
          data: {
            node: n,
            connectedIn: connIn.get(n.id) ?? [],
            connectedOut: connOut.get(n.id) ?? [],
            onOpenPort,
            onInspect: (node: number, kind: 'in' | 'out', port: number, action: 'enter' | 'leave' | 'pin') =>
              inspectRef.current?.(node, kind, port, action),
            onLight: (id: number, action: 'execute' | 'reset' | 'cancel') => lightRef.current?.(id, action),
            onWidget: (id: number, value: any) => widgetRef.current?.(id, value),
            onSelect: (id: number, selection: any) => selectRef.current?.(id, selection),
            onSend: (id: number, value: any) => sendRef.current?.(id, value),
            onWidgetLayout: (id: number, layout: WidgetLayout) => controller.op('set_widget_layouts', { layouts: [{ id, layout }] }),
            onSketch: setSketch,
            tails,
            widgetSelection: widgetSel,
            widgetStarts: () => widgetOps.current.starts(),
            onWidgetsMoved: (moved: Map<number, { x: number; y: number }>) => widgetOps.current.moved(moved),
            onAnchor: (id: number, anchored: boolean) => widgetOps.current.anchor(id, anchored),
            dockEnv: () => widgetOps.current.env(),
            onWidgetDropped: (id: number, layout: WidgetLayout) => widgetOps.current.dropped(id, layout),
            busy: view.executing,
            navHints: hintsRef.current.get(n.id),
            barHeight
          }
        };
      });
    });
    setEdges(prev => {
      const selected = new Set(prev.filter(e => e.selected).map(e => e.id));
      return view.connections.map(c => {
        const id = edgeId(c);
        return {
          id,
          source: String(c.sourceID),
          sourceHandle: `out-${c.sourcePort}`,
          target: String(c.destID),
          targetHandle: `in-${c.destPort}`,
          selected: selected.has(id),
          className: `verknode-edge-${c.type}${c.type !== 'table' && c.type !== 'flowvariable' ? ' verknode-edge-square' : ''}${c.inactive ? ' verknode-edge-inactive' : ''}${id === insertEdge.current ? ' verknode-edge-insert' : ''}`,
          data: { ...c }
        };
      });
    });
    for (const id of [...pendingViews.current]) {
      const n = view.nodes.find(x => x.id === id);
      if (!n || (n.state !== 'EXECUTING' && n.state !== 'QUEUED')) {
        pendingViews.current.delete(id);
        if (n?.viewAvailable) {
          setNodeView(id);
        }
      }
    }
    if (!fitted.current && view.nodes.some(n => n.id > 0)) {
      fitted.current = true;
      // (after the widgets are drawn: they count too)
      window.requestAnimationFrame(() => window.requestAnimationFrame(() => fitAll()));
    }
  }, [view]);

  const run = useCallback(
    async (fn: () => Promise<unknown>) => {
      try {
        setError(null);
        await fn();
      } catch (e) {
        setError((e as Error).message);
      } finally {
        // keep keyboard shortcuts working when the focused node was removed (undo, cut, ...)
        window.requestAnimationFrame(() => {
          // (focus falls back to the body or to a container around the editor)
          const active = editorRef.current?.ownerDocument.activeElement;
          if (!active || (active !== editorRef.current && active.contains(editorRef.current))) {
            editorRef.current?.focus({ preventScroll: true });
          }
        });
      }
    },
    [setError]
  );

  const selectedIds = (): number[] => nodes.filter(n => n.selected).map(n => Number(n.id));
  // the selection now and the one before (clicking a chat on the canvas selects its own node: a chat
  // refers to what was selected before it)
  const recentSelection = useRef<[number[], number[]]>([[], []]);
  useEffect(() => {
    const sel = nodes.filter(n => n.selected && n.type === 'node').map(n => Number(n.id));
    const [cur] = recentSelection.current;
    if (sel.join() !== cur.join()) {
      recentSelection.current = [sel, cur];
    }
  }, [nodes]);
  const viewRef = useRef(rawView);
  viewRef.current = rawView;
  const canvasLink = useMemo<CanvasLink>(
    () => ({
      selection: except => {
        const [cur, prev] = recentSelection.current;
        const ids = cur.length === 1 && cur[0] === except ? prev : cur;
        const byNode = new Map((viewRef.current?.nodes ?? []).map(n => [n.id, n]));
        return ids
          .filter(id => id !== except && byNode.has(id))
          .map(id => ({ id, name: byNode.get(id)!.name, label: byNode.get(id)!.annotation }));
      },
      node: id => {
        const n = viewRef.current?.nodes.find(x => x.id === id);
        return n ? { id, name: n.name, label: n.annotation } : null;
      },
      inspect: (id, pin, near) => inspectNodeRef.current?.(id, pin, near),
      highlight: (ids, fit = true) => {
        const hidden = (id: number) => {
          const el = wrapper.current?.querySelector(`.react-flow__node[data-id="${id}"]`)?.getBoundingClientRect();
          const box = wrapper.current?.getBoundingClientRect();
          return !el || !box || el.right < box.left || el.left > box.right || el.bottom < box.top || el.top > box.bottom;
        };
        const want = new Set(ids.map(String));
        flashing.current = want;
        // (fit false: a hover - only the flash, the selection stays)
        setNodes(ns => ns.map(n => ({ ...n, selected: fit ? want.has(n.id) : n.selected, className: want.has(n.id) ? 'verknode-flash' : undefined })));
        window.setTimeout(() => {
          if (flashing.current === want) {
            flashing.current = new Set();
            setNodes(ns => ns.map(n => (want.has(n.id) ? { ...n, className: undefined } : n)));
          }
        }, 2600);
        const shown = ids.filter(id => flow.getNode(String(id)));
        if (shown.length && (fit === true || (fit === 'if-hidden' && shown.some(hidden)))) {
          void flow.fitView({ nodes: shown.map(id => ({ id: String(id) })), padding: 0.4, maxZoom: 1, duration: 500 });
        }
      }
    }),
    [flow]
  );

  /** Select a node together with all nodes downstream (or upstream) of it. */
  const selectConnected = (id: number, direction: 'down' | 'up') => {
    const next = new Map<number, number[]>();
    for (const c of view?.connections ?? []) {
      const [from, to] = direction === 'down' ? [c.sourceID, c.destID] : [c.destID, c.sourceID];
      next.set(from, [...(next.get(from) ?? []), to]);
    }
    const chosen = new Set<number>([id]);
    const todo = [id];
    while (todo.length) {
      for (const n of next.get(todo.pop()!) ?? []) {
        if (!chosen.has(n)) {
          chosen.add(n);
          todo.push(n);
        }
      }
    }
    setNodes(ns => ns.map(n => ({ ...n, selected: chosen.has(Number(n.id)) })));
    setEdges(es => es.map(e => ({ ...e, selected: false })));
  };

  // ------------------------------------------------------------- keyboard navigation
  // the "current" node: latest clicked / navigated; arrow keys move from it
  const [cursor, setCursor] = useState<number | null>(null);
  const navGraph = useMemo<NavigationGraph>(
    () => ({
      // (without the port bars inside a metanode)
      connections: (view?.connections ?? []).filter(c => c.sourceID > 0 && c.destID > 0),
      // node centers (icon) from the workflow positions
      positions: new Map((view?.nodes ?? []).filter(n => n.id > 0).map(n => [n.id, { x: n.x + 48, y: n.y + 40 }]))
    }),
    [view]
  );
  const hints = useMemo(
    () =>
      cursor !== null && byId.has(cursor)
        ? navigationHints(navGraph, cursor, prefs.arrowNavigation)
        : new Map<number, { keys: string[]; spatial: string[] }>(),
    [navGraph, cursor, prefs.arrowNavigation, byId]
  );
  const hintsRef = useRef(hints);
  hintsRef.current = hints;
  useEffect(() => {
    setNodes(ns => ns.map(n => ({ ...n, data: { ...n.data, navHints: hints.get(Number(n.id)) } })));
  }, [hints]);

  // leaving lineage mode when the selection is cleared
  const anySelected = nodes.some(n => n.selected);
  useEffect(() => {
    if (!anySelected && lineageLock.current) {
      setLineageLock(false);
    }
  }, [anySelected]);

  const navigate = (dir: 'left' | 'right' | 'up' | 'down', lineage: boolean, extend: boolean) => {
    const selected = nodes.filter(n => n.selected).map(n => Number(n.id));
    const from: number | undefined =
      cursor !== null && selected.includes(cursor) ? cursor : selected[selected.length - 1];
    let target: number | null;
    if (from === undefined) {
      // nothing selected: start at the top-left node
      const first = [...(view?.nodes ?? [])].sort((a, b) => a.x + a.y - (b.x + b.y))[0];
      target = first ? first.id : null;
    } else {
      target = navigationTarget(navGraph, from, dir, lineage ? prefs.arrowNavigation : 'spatial');
    }
    if (target === null) {
      return;
    }
    const keep = new Set(extend ? selected : []);
    keep.add(target);
    setNodes(ns => ns.map(n => ({ ...n, selected: keep.has(Number(n.id)) })));
    setEdges(es => es.map(x => ({ ...x, selected: false })));
    setCursor(target);
    const t = byId.get(target);
    if (t) {
      setDescribed(repoByFactory.get(t.factory) ?? null);
      // keep the target in view
      const r = wrapper.current?.getBoundingClientRect();
      const vp = flow.getViewport();
      const sx = (t.x + 48) * vp.zoom + vp.x;
      const sy = (t.y + 40) * vp.zoom + vp.y;
      if (r && (sx < 60 || sy < 60 || sx > r.width - 60 || sy > r.height - 60)) {
        void flow.setCenter(t.x + 48, t.y + 40, { zoom: vp.zoom, duration: 200 });
      }
    }
  };

  // ------------------------------------------------------------- scope bubbles
  const groupsById = useMemo(() => new Map((view?.groups ?? []).map(g => [g.id, g])), [view]);
  // automatic scope bubbles (loops, try/catch, switches) and group annotations, layered together
  const scopeBlocks = useMemo(() => {
    if (!view) {
      return [];
    }
    const scopes = prefs.showScopeBlocks ? findScopeBlocks(view.nodes, view.connections, repo).map(b => ({ ...b })) : [];
    const pos = new Map(view.nodes.map(n => [n.id, n]));
    const groups: ScopeBlock[] = (view.groups ?? [])
      .map(g => ({ g, members: g.nodes.filter(n => pos.has(n)) }))
      .filter(({ members }) => members.length > 0)
      .map(({ g, members }) => {
        // label above the top-left node
        const start = [...members].sort((a, b) => pos.get(a)!.y - pos.get(b)!.y || pos.get(a)!.x - pos.get(b)!.x)[0];
        return { start, end: start, members, kind: 'group' as const, depth: 0, groupId: g.id };
      });
    return layoutBlocks([...scopes, ...groups]);
  }, [prefs.showScopeBlocks, view, repo]);
  // bubbles follow the nodes with low priority, so dragging stays smooth
  const bubbleNodes = useDeferredValue(nodes);
  const scopeBubbles = useMemo(() => {
    const rects = new Map<number, Rect>(
      // (just the nodes: canvas widgets stay outside the bubbles)
      bubbleNodes.map(n => [Number(n.id), { x: n.position.x, y: n.position.y, w: n.measured?.width ?? 96, h: n.measured?.height ?? 90 }])
    );
    const shapes = bubbleShapes(scopeBlocks, rects);
    return scopeBlocks
      .map((b, i) => {
        const shape = shapes[i];
        return shape ? { key: b.groupId !== undefined ? `g${b.groupId}` : `${b.start}-${b.end}`, block: b, shape } : null;
      })
      .filter((b): b is NonNullable<typeof b> => b !== null)
      .sort((a, b) => b.block.depth - a.block.depth); // outer first, inner drawn on top
  }, [scopeBlocks, bubbleNodes, view]);
  const SCOPE_LABEL = { loop: 'loop', try: 'try / catch', switch: 'switch', group: 'group' } as const;

  // ------------------------------------------------------------- group annotations
  const [groupDialog, setGroupDialog] = useState<{ id: number | null; nodes: number[] } | null>(null);
  const hitContext = useMemo(() => document.createElement('canvas').getContext('2d'), []);
  /**
   * Innermost bubble under a screen point (its label counts too); `groupsOnly`
   * looks at group annotations only.
   */
  const bubbleAt = (clientX: number, clientY: number, groupsOnly = false): ScopeBlock | null => {
    const candidates = scopeBubbles.filter(b => !groupsOnly || b.block.groupId !== undefined);
    for (const el of Array.from(wrapper.current?.querySelectorAll<HTMLElement>('.verknode-scope-label[data-bubble]') ?? [])) {
      const r = el.getBoundingClientRect();
      const hit = candidates.find(b => b.key === el.dataset.bubble);
      if (hit && clientX >= r.left && clientX <= r.right && clientY >= r.top && clientY <= r.bottom) {
        return hit.block;
      }
    }
    const p = flow.screenToFlowPosition({ x: clientX, y: clientY });
    const hits = candidates.filter(b => hitContext?.isPointInPath(new Path2D(b.shape.path), p.x, p.y, 'evenodd'));
    hits.sort((a, b) => a.block.depth - b.block.depth || a.shape.box.w * a.shape.box.h - b.shape.box.w * b.shape.box.h);
    return hits.length ? hits[0].block : null;
  };
  const groupAt = (clientX: number, clientY: number): number | null =>
    bubbleAt(clientX, clientY, true)?.groupId ?? null;
  const selectMembers = (ids: number[]) => {
    const chosen = new Set(ids.map(String));
    setNodes(ns => ns.map(n => ({ ...n, selected: chosen.has(n.id) })));
  };
  const groupsOf = (id: number) => (view?.groups ?? []).filter(g => g.nodes.includes(id));
  const createGroup = (ids: number[]) => ids.length && setGroupDialog({ id: null, nodes: ids });
  const editGroup = (g: GroupInfo) => setGroupDialog({ id: g.id, nodes: g.nodes });
  const selectGroup = (g: GroupInfo) => selectMembers(g.nodes);
  const removeFromGroup = (g: GroupInfo, ids: number[]) => {
    const rest = g.nodes.filter(n => !ids.includes(n));
    void run(() =>
      rest.length ? controller.op('update_group', { id: g.id, nodes: rest }) : controller.op('remove_group', { id: g.id })
    );
  };
  /** Nodes and metanodes of a group on the shown level. */
  const levelMembers = (g: GroupInfo) => g.nodes.filter(id => id > 0 && byId.has(id));
  const groupMenuItems = (g: GroupInfo) => [
    { label: `Edit Group “${groupTitle(g)}”…`, action: () => editGroup(g) },
    { label: 'Select Group Nodes', action: () => selectGroup(g) },
    {
      label: 'Collapse Group into Metanode…',
      disabled: !levelMembers(g).length,
      action: () => void collapse(levelMembers(g))
    },
    { label: 'Delete Group Annotation', action: () => void run(() => controller.op('remove_group', { id: g.id })) }
  ];

  // ------------------------------------------------------------- Tab quick-add
  const [repoQuery, setRepoQuery] = useState('');
  const savedRepoQuery = useRef('');
  const [quick, setQuick] = useState<{
    source: number | null;
    portIdx: number;
    query: string;
    index: number;
    x: number;
    y: number;
    /** where the picked node goes (flow coordinates), when there is no source: the pointer's place */
    at?: { x: number; y: number };
  } | null>(null);
  /** the pointer's last place over the canvas (client coordinates), null when it is elsewhere */
  const pointer = useRef<{ x: number; y: number } | null>(null);
  const quickSource = quick && quick.source !== null ? (byId.get(quick.source) ?? null) : null;
  // output ports of the source: explicit ports first, the flow variable port last
  const quickPorts = useMemo(
    () =>
      quickSource
        ? [...quickSource.outPorts.filter(p => !p.implicit), ...quickSource.outPorts.filter(p => p.implicit)]
        : [],
    [quickSource]
  );
  const quickPort = quickPorts.length ? quickPorts[(quick?.portIdx ?? 0) % quickPorts.length] : null;
  const quickItems = useMemo(
    () =>
      quick
        ? suggestNodes({
            source: quickSource,
            portType: quickPort?.type ?? null,
            query: quick.query,
            connections: view?.connections ?? [],
            nodes: view?.nodes ?? [],
            repository: repo
          })
        : [],
    [quick?.query, quickSource, quickPort, repo, view]
  );
  // the highlighted candidate is described in the Node Description panel
  const quickCandidate = quick ? quickItems[quick.index] : undefined;
  useEffect(() => {
    if (quickCandidate) {
      setDescribed(quickCandidate);
    }
  }, [quickCandidate]);

  const openQuickAdd = () => {
    const selected = nodes.filter(n => n.selected).map(n => Number(n.id));
    const sourceId = cursor !== null && selected.includes(cursor) ? cursor : (selected[selected.length - 1] ?? null);
    const source = sourceId !== null ? byId.get(sourceId) : undefined;
    const r = wrapper.current?.getBoundingClientRect();
    if (!r) {
      return;
    }
    let x = r.width / 2 - 140;
    let y = r.height / 3;
    let at: { x: number; y: number } | undefined;
    if (source) {
      const p = flow.flowToScreenPosition({ x: source.x + 110, y: source.y });
      x = p.x - r.left;
      y = p.y - r.top;
    } else if (!selected.length && pointer.current) {
      // nothing selected: the list opens at the pointer, and the node goes there
      x = pointer.current.x - r.left;
      y = pointer.current.y - r.top;
      at = flow.screenToFlowPosition(pointer.current);
    }
    savedRepoQuery.current = repoQuery;
    setQuick({
      source: source ? source.id : null,
      portIdx: 0,
      query: '',
      index: 0,
      x: Math.max(8, Math.min(x, r.width - 300)),
      y: Math.max(8, Math.min(y, r.height - 330)),
      at
    });
    setRepoQuery('');
  };

  const closeQuickAdd = () => {
    setQuick(null);
    setRepoQuery(savedRepoQuery.current);
    window.requestAnimationFrame(() => editorRef.current?.focus({ preventScroll: true }));
  };

  const pickQuickAdd = (node: NodeDescription) => {
    const source = quickSource;
    const port = quickPort;
    const at = quick?.at;
    closeQuickAdd();
    void run(async () => {
      let x: number;
      let y: number;
      if (source) {
        x = source.x + 160;
        y = source.y;
        const taken = (px: number, py: number) =>
          (view?.nodes ?? []).some(n => Math.abs(n.x - px) < 90 && Math.abs(n.y - py) < 80);
        while (taken(x, y)) {
          y += 110;
        }
      } else if (at) {
        x = at.x - 45;
        y = at.y - 30;
      } else {
        const r = wrapper.current!.getBoundingClientRect();
        const p = flow.screenToFlowPosition({ x: r.left + r.width / 2, y: r.top + r.height / 3 });
        x = p.x - 45;
        y = p.y - 30;
      }
      const v = await controller.op('add_node', { factory: node.factory, x: Math.round(x), y: Math.round(y), parent: level });
      const id: number = v.result.id;
      if (source && port) {
        const destPort = compatibleInput(node, port.type);
        if (destPort !== null) {
          await connectOp(source.id, port.index, id, destPort);
        }
        recordTransition(source.factory, node.factory);
      }
      pendingSelect.current = new Set([String(id)]);
      setNodes(ns => ns.map(n => ({ ...n, selected: n.id === String(id) })));
      window.setTimeout(() => (pendingSelect.current = null), 500);
      setCursor(id);
      setDescribed(node);
    });
  };

  const selectOnly = (id: number) => {
    setCursor(id);
    setNodes(ns => ns.map(n => ({ ...n, selected: n.id === String(id) })));
    const n = byId.get(id);
    if (n) {
      setDescribed(repoByFactory.get(n.factory) ?? null);
    }
  };

  /** Ctrl+A: every node of the shown level, with the widgets on the canvas. */
  const selectAll = () => {
    setNodes(ns => ns.map(n => ({ ...n, selected: true })));
    widgetSel.setExplicit(
      Array.from(wrapper.current?.querySelectorAll<HTMLElement>('.verknode-widget-dock[data-widget]') ?? []).map(el =>
        Number(el.dataset.widget)
      )
    );
  };

  /** The Reset button: the selected nodes; Ctrl/Cmd+click resets every node, once confirmed. */
  const resetButton = async (e: React.MouseEvent) => {
    const ids = selectedIds();
    if (ids.length && !(e.ctrlKey || e.metaKey)) {
      return reset(ids);
    }
    const result = await showDialog({
      title: 'Reset all nodes?',
      body: 'This resets every node of the workflow and discards their results.',
      buttons: [Dialog.cancelButton(), Dialog.warnButton({ label: 'Reset all' })]
    });
    if (result.button.accept) {
      await reset();
    }
  };

  // ---------------------------------------------------------------- actions
  /** Connect in the shown level (metanode ports and port bars are translated). */
  const connectOp = (sourceID: number, sourcePort: number, destID: number, destPort: number) => {
    const src = flatEnd(rawView!, level, sourceID, sourcePort, 'out');
    const dst = flatEnd(rawView!, level, destID, destPort, 'in');
    return controller.op('connect', { sourceID: src.id, sourcePort: src.port, destID: dst.id, destPort: dst.port });
  };
  /** Links of the shown level as workflow connections (metanode ports translated). */
  const flatLinks = (links: Link[]) =>
    links.map(l => {
      const src = flatEnd(rawView!, level, l.sourceID, l.sourcePort, 'out');
      const dst = flatEnd(rawView!, level, l.destID, l.destPort, 'in');
      return { sourceID: src.id, sourcePort: src.port, destID: dst.id, destPort: dst.port };
    });
  /** Tab with several nodes selected: connect nearby selected nodes through free compatible ports. */
  const autoConnect = (ids: number[]) => {
    const chosen = ids.map(id => byId.get(id)).filter((n): n is NodeInfo => !!n && n.id > 0);
    const links = planAutoConnect(chosen, view?.connections ?? []);
    if (!links.length) {
      setError('Nothing to connect: the selected nodes have no free compatible ports (inputs are fed from the left)');
      return;
    }
    void run(() => controller.op('rewire', { add: flatLinks(links) }));
  };

  // ------------------------------------------------ drop a node onto a connection
  // edges under the dragged nodes: sampled once per drag (only edges not touching them)
  const insertEdge = useRef<string | null>(null);
  const dragProbe = useRef<{ samples: Map<string, { x: number; y: number }[]>; icons: Map<string, { x: number; y: number }> } | null>(
    null
  );
  const setInsertEdge = (id: string | null) => {
    if (insertEdge.current !== id) {
      insertEdge.current = id;
      setEdges(es =>
        es.map(e => ({
          ...e,
          className: `${(e.className ?? '').replace(' verknode-edge-insert', '')}${e.id === id ? ' verknode-edge-insert' : ''}`
        }))
      );
    }
  };
  const onDragStart = (dragged: { id: string; position: { x: number; y: number } }[]) => {
    const ids = new Set(dragged.map(n => Number(n.id)));
    const samples = new Map<string, { x: number; y: number }[]>();
    for (const c of view?.connections ?? []) {
      if (ids.has(c.sourceID) || ids.has(c.destID)) {
        continue;
      }
      const id = edgeId(c);
      const path = wrapper.current?.querySelector<SVGPathElement>(`[data-id="${id}"] path.react-flow__edge-path, [data-testid="rf__edge-${id}"] path.react-flow__edge-path`);
      if (path) {
        const length = path.getTotalLength();
        const pts = [];
        for (let t = 0; t <= length; t += 6) {
          const p = path.getPointAtLength(t);
          pts.push({ x: p.x, y: p.y });
        }
        samples.set(id, pts);
      }
    }
    // icon centres relative to the node positions (flow coordinates)
    const icons = new Map<string, { x: number; y: number }>();
    for (const n of dragged) {
      const r = wrapper.current?.querySelector(`.react-flow__node[data-id="${n.id}"] .verknode-node-icon`)?.getBoundingClientRect();
      if (r) {
        const c = flow.screenToFlowPosition({ x: r.left + r.width / 2, y: r.top + r.height / 2 });
        icons.set(n.id, { x: c.x - n.position.x, y: c.y - n.position.y });
      }
    }
    dragProbe.current = { samples, icons };
    const movable = widgetSel.ids
      .map(id => byId.get(id))
      .filter((n): n is NodeInfo => !!n?.widget && n.widgetLayout?.side === 'free' && !n.widgetLayout.anchored);
    if (movable.length && dragged.length) {
      widgetSel.startMove(new Map(movable.map(n => [n.id, { x: n.widgetLayout!.x ?? 0, y: n.widgetLayout!.y ?? 0 }])));
      dragOrigin.current = { id: dragged[0].id, x: dragged[0].position.x, y: dragged[0].position.y };
    }
  };
  const dragOrigin = useRef<{ id: string; x: number; y: number } | null>(null);
  const followDrag = (dragged: { id: string; position: { x: number; y: number } }[]) => {
    const o = dragOrigin.current;
    const n = o && dragged.find(d => d.id === o.id);
    if (o && n) {
      widgetSel.moveBy(Math.round(n.position.x - o.x), Math.round(n.position.y - o.y));
    }
  };
  /** Layouts of the widgets that moved with a selection (and the end of that move, once shown). */
  const movedLayouts = () =>
    [...widgetSel.moved].map(([id, p]) => ({ id, layout: { ...(byId.get(id)?.widgetLayout ?? {}), side: 'free' as const, x: p.x, y: p.y } }));
  const endWidgetMove = () => window.requestAnimationFrame(() => window.requestAnimationFrame(() => widgetSel.endMove()));
  /** Connection under the dragged nodes' icons that they can be inserted into. */
  const edgeUnder = (dragged: { id: string; position: { x: number; y: number } }[]): string | null => {
    const probe = dragProbe.current;
    if (!probe) {
      return null;
    }
    const boxes = dragged
      .map(n => {
        const o = probe.icons.get(n.id);
        return o ? { x: n.position.x + o.x, y: n.position.y + o.y } : null;
      })
      .filter((c): c is { x: number; y: number } => !!c);
    const r = 26;
    const draggedInfo = dragged.map(n => byId.get(Number(n.id))).filter((n): n is NodeInfo => !!n);
    for (const [id, pts] of probe.samples) {
      if (pts.some(p => boxes.some(b => Math.abs(p.x - b.x) <= r && Math.abs(p.y - b.y) <= r))) {
        const c = view?.connections.find(x => edgeId(x) === id);
        if (c && planInsert(draggedInfo, c, view?.connections ?? [])) {
          return id;
        }
      }
    }
    return null;
  };
  const onDragStop = (dragged: { id: string; position: { x: number; y: number } }[]) => {
    const positions = dragged.map(n => ({ id: Number(n.id), x: Math.round(n.position.x), y: Math.round(n.position.y) }));
    const id = edgeUnder(dragged);
    dragProbe.current = null;
    setInsertEdge(null);
    const edge = id ? view?.connections.find(x => edgeId(x) === id) : undefined;
    const draggedInfo = dragged.map(n => byId.get(Number(n.id))).filter((n): n is NodeInfo => !!n);
    const links = edge ? planInsert(draggedInfo, edge, view?.connections ?? []) : null;
    const layouts = dragOrigin.current ? movedLayouts() : [];
    dragOrigin.current = null;
    void run(async () => {
      if (edge && links) {
        await controller.op('rewire', { positions, remove: [edge.flat ?? edge], add: flatLinks(links) });
        if (layouts.length) {
          await controller.op('set_widget_layouts', { layouts });
        }
      } else {
        await controller.op('move', { positions, layouts });
      }
    }).finally(endWidgetMove);
  };
  /** Where a widget is on the canvas (its top left, as a free layout's x / y). */
  const widgetPosition = (id: number): { x: number; y: number } | null => {
    const node = wrapper.current?.querySelector(`.react-flow__node[data-id="${id}"]`);
    const dock = node?.querySelector('.verknode-widget-dock');
    const root = node?.querySelector<HTMLElement>('.verknode-node');
    if (!dock || !root) {
      return null;
    }
    const r = dock.getBoundingClientRect();
    const p = flow.screenToFlowPosition({ x: r.left, y: r.top }, { snapToGrid: false });
    return { x: Math.round(p.x - root.clientLeft), y: Math.round(p.y - root.clientTop) };
  };
  widgetOps.current = {
    // a selected widget dragged: every selected one that is not anchored comes along
    starts: () => {
      const out = new Map<number, { x: number; y: number }>();
      for (const id of widgetSel.ids) {
        const n = byId.get(id);
        if (!n?.widget || n.widgetLayout?.anchored) {
          continue;
        }
        const l = n.widgetLayout ?? {};
        const p = l.side === 'free' ? { x: l.x ?? 0, y: l.y ?? 0 } : widgetPosition(id);
        if (p) {
          out.set(id, p);
        }
      }
      return out;
    },
    moved: () => {
      const layouts = movedLayouts();
      const moved = new Map(layouts.map(l => [l.id, { x: l.layout.x, y: l.layout.y }]));
      void run(() => controller.op('set_widget_layouts', { layouts: [...layouts, ...pinsAfter(moved)] })).finally(endWidgetMove);
    },
    // one widget dropped somewhere new
    dropped: (id, layout) =>
      run(() => controller.op('set_widget_layouts', { layouts: [{ id, layout }, ...pinsAfter(new Map([[id, { x: layout.x ?? 0, y: layout.y ?? 0 }]]))] })),
    // every widget's box (canvas coordinates, x / y as a free layout's); free, unanchored ones dock to each other
    env: () => {
      const rects = new Map<number, { x: number; y: number; w: number; h: number }>();
      const members = new Set<number>();
      const zoom = flow.getZoom();
      for (const el of Array.from(wrapper.current?.querySelectorAll<HTMLElement>('.verknode-widget-dock[data-widget]') ?? [])) {
        const id = Number(el.dataset.widget);
        const root = el.closest('.verknode-node') as HTMLElement | null;
        const r = el.getBoundingClientRect();
        const p = flow.screenToFlowPosition({ x: r.left, y: r.top }, { snapToGrid: false });
        rects.set(id, { x: p.x - (root?.clientLeft ?? 0), y: p.y - (root?.clientTop ?? 0), w: r.width / zoom, h: r.height / zoom });
        const l = byId.get(id)?.widgetLayout;
        // (widgets still beside their nodes count too: docking to one pins it, see pinsAfter)
        if (!l?.anchored) {
          members.add(id);
        }
      }
      return { rects, members };
    },
    // the anchor button: this widget, or all selected ones (set to the same state)
    anchor: (id, anchored) => {
      const ids = widgetSel.has(id) && widgetSel.ids.length > 1 ? widgetSel.ids : [id];
      const layouts = ids
        .map(i => byId.get(i))
        .filter((n): n is NodeInfo => !!n?.widget)
        .map(n => {
          const l = n.widgetLayout ?? {};
          // a docked widget is pinned where it is now
          const p = anchored && l.side !== 'free' ? widgetPosition(n.id) : null;
          return { id: n.id, layout: { ...l, ...(p ? { side: 'free' as const, x: p.x, y: p.y } : {}), anchored: anchored || undefined } };
        });
      void run(() => controller.op('set_widget_layouts', { layouts }));
    }
  };
  /**
   * Widgets that moved widgets docked to and that still sit beside their nodes: pinned as free
   * widgets where they are, so the group stays together when those nodes move.
   */
  const pinsAfter = (moved: Map<number, { x: number; y: number }>) => {
    const { rects, members } = widgetOps.current.env();
    for (const [id, p] of moved) {
      const r = rects.get(id);
      if (r) {
        rects.set(id, { ...r, x: p.x, y: p.y });
      }
    }
    const pins = new Map<number, { x: number; y: number }>();
    for (const id of moved.keys()) {
      for (const g of dockGroup(id, rects, members)) {
        const l = byId.get(g)?.widgetLayout;
        if (!moved.has(g) && l?.side !== 'free') {
          pins.set(g, { x: Math.round(rects.get(g)!.x), y: Math.round(rects.get(g)!.y) });
        }
      }
    }
    return [...pins].map(([g, p]) => ({ id: g, layout: { ...(byId.get(g)?.widgetLayout ?? {}), side: 'free' as const, x: p.x, y: p.y } }));
  };
  // the widgets of the selected nodes are selected too, unless anchored
  useEffect(() => {
    widgetSel.setDerived(
      nodes
        .filter(n => n.selected)
        .map(n => byId.get(Number(n.id)))
        .filter((n): n is NodeInfo => !!n?.widget && !n.widgetLayout?.anchored)
        .map(n => n.id)
    );
  }, [nodes, byId]);
  /** Widgets inside a marquee (client coordinates). */
  const widgetsIn = (x0: number, y0: number, x1: number, y1: number): number[] =>
    Array.from(wrapper.current?.querySelectorAll<HTMLElement>('.verknode-widget-dock[data-widget]') ?? [])
      .filter(el => {
        const r = el.getBoundingClientRect();
        return r.left < Math.max(x0, x1) && r.right > Math.min(x0, x1) && r.top < Math.max(y0, y1) && r.bottom > Math.min(y0, y1);
      })
      .map(el => Number(el.dataset.widget));
  const selectionStart = useRef<{ x: number; y: number } | null>(null);

  const enterLevel = (id: number | null) => {
    setLevel(id);
    setMenu(null);
    setCursor(null);
    fitted.current = false;
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => fitAll()));
  };
  const collapse = async (ids: number[]) => {
    // default name: the group annotation made of exactly the selected nodes (on this level;
    // the most specific one if several groups qualify)
    const group = (rawView?.groups ?? [])
      .filter(g => {
        const members = levelMembers(g);
        return g.text.trim() && members.length === ids.length && ids.every(id => members.includes(id));
      })
      .sort((a, b) => a.nodes.length - b.nodes.length)[0];
    const name = group ? groupTitle(group) : 'Metanode';
    const result = await InputDialog.getText({ title: 'Collapse into Metanode', label: 'Name', text: name });
    if (result.button.accept) {
      await run(async () => {
        const v = await controller.op('collapse', { ids, name: result.value || name });
        selectOnly(v.result.id);
      });
    }
  };
  const renameMetanode = async (id: number) => {
    const m = rawView?.metanodes.find(x => x.id === id);
    const result = await InputDialog.getText({ title: 'Rename Metanode', text: m?.name ?? '' });
    if (result.button.accept && result.value) {
      await run(() => controller.op('rename_metanode', { id, name: result.value }));
    }
  };
  const addPort = (id: number, side: 'in' | 'out', type: string) =>
    run(() => controller.op('add_metanode_port', { id, side, type }));
  const execute = (ids?: number[]) => run(() => controller.op('execute', ids ? { ids } : {}));
  // Ctrl+click on a traffic light (see NodeLight)
  const lightRef = useRef<(id: number, action: 'execute' | 'reset' | 'cancel') => void>();
  // canvas widgets: store an input's new value, then run the output widgets after it
  const [sketch, setSketch] = useState<number | null>(null);
  const widgetRef = useRef<(id: number, value: any) => void>();
  const selectRef = useRef<(id: number, selection: any) => void>();
  const sendRef = useRef<(id: number, value: any) => void>();
  /** Output widgets and Workflow Scripts after a node (and the node itself when there are none, or it is an output widget). */
  /** Is node `to` downstream of node `from`? */
  const reachable = (from: number, to: number): boolean => {
    const seen = new Set<number>([from]);
    const todo = [from];
    while (todo.length) {
      const n = todo.pop()!;
      for (const c of rawView?.connections ?? []) {
        if (c.sourceID === n && !seen.has(c.destID)) {
          if (c.destID === to) {
            return true;
          }
          seen.add(c.destID);
          todo.push(c.destID);
        }
      }
    }
    return false;
  };
  const widgetsAfter = (id: number): number[] => {
    const next = new Map<number, number[]>();
    for (const c of rawView?.connections ?? []) {
      next.set(c.sourceID, [...(next.get(c.sourceID) ?? []), c.destID]);
    }
    const after = new Set<number>();
    const todo = [id];
    while (todo.length) {
      for (const n of next.get(todo.pop()!) ?? []) {
        if (!after.has(n)) {
          after.add(n);
          todo.push(n);
        }
      }
    }
    // (nodes that say so run too: Workflow Script - e.g. a checkbox that switches a path)
    const outputs = (rawView?.nodes ?? [])
      .filter(n => after.has(n.id) && (n.widget?.type === 'output' || n.runsWithInputs))
      .map(n => n.id);
    const self = rawView?.nodes.find(n => n.id === id)?.widget?.type === 'output';
    return self || !outputs.length ? [id, ...outputs] : outputs;
  };
  widgetRef.current = (id, value) =>
    void run(async () => {
      await controller.op('set_settings', { id, settings: { value } });
      const widget = rawView?.nodes.find(n => n.id === id)?.widget;
      if (widget?.type === 'input' && widget.autoRun) {
        // (without output widgets downstream, the input itself runs: its flow variable is set)
        await controller.op('execute', { ids: widgetsAfter(id) });
      }
    });
  // a selection on a display: it runs again (its "Selection" port), with the displays after it
  selectRef.current = (id, selection) =>
    void run(async () => {
      await controller.op('set_settings', { id, settings: { selection } });
      await controller.op('execute', { ids: widgetsAfter(id) });
    });
  // a running node waits for what its widget sends (a chat message); {start: true}: run its loop afresh
  // ({new: true}: the node is told to forget the conversation it shows); {sandbox}: a checkbox of the chat -
  // for the running node ({live: true}), else its setting
  sendRef.current = (id, value) => {
    if (value?.start) {
      const after = new Set(widgetsAfter(id));
      const ends = (rawView?.nodes ?? []).filter(n => n.loopRole === 'end' && reachable(id, n.id)).map(n => n.id);
      void run(async () => {
        await controller.op('reset', { ids: [id] });
        if (value.new) {
          await controller.op('deliver', { id, value: { new: true } });
        }
        await controller.op('execute', { ids: ends.length ? ends : [id, ...after] });
      });
      return;
    }
    if (value?.setting && !value.live) {
      // (a checkbox of a chat that does not run: its setting)
      void run(async () => {
        await controller.op('set_settings', { id, settings: value.setting });
      });
      return;
    }
    // (not through run(): the chat box keeps the keyboard focus)
    controller.op('deliver', { id, value }).catch(e => setError((e as Error).message));
  };
  lightRef.current = (id, action) => void (action === 'execute' ? execute([id]) : action === 'reset' ? reset([id]) : cancel());
  /**
   * Port and node views open in a child browser window (a new tab with
   * Ctrl/Cmd) so they can be moved and resized freely; the in-editor dialog
   * is the fallback when the browser blocks the window.
   */
  const openExternal = (kind: External['kind'], node: number, port: number, tab: boolean, from: Window = window): boolean => {
    const existing = externalsRef.current.find(x => x.kind === kind && x.node === node && x.port === port && !x.win.closed);
    if (existing && existing.tab === tab && !tab) {
      existing.win.focus();
      return true;
    }
    const win = kind === 'view' ? openChildWindow(tab, 1100, 800, from) : openChildWindow(tab, 1000, 720, from);
    if (!win) {
      return false;
    }
    setExternals(xs => [...xs, { key: ++externalKey.current, win, kind, node, port, tab }]);
    return true;
  };
  /**
   * Move a view to another place (window, browser tab, JupyterLab tab or
   * dialog); `close` removes it from where it is, `from` is the window the
   * click came from (needed to open browser windows from a child window).
   */
  const moveView = (kind: External['kind'], node: number, port: number, to: ViewPlace, close: () => void, from: Window = window) => {
    let done = false;
    if (to === 'window' || to === 'tab') {
      done = openExternal(kind, node, port, to === 'tab', from);
    } else if (to === 'jupyter') {
      close();
      done = openLab(kind, node, port);
      if (done) {
        return;
      }
    }
    close();
    if (!done) {
      // dialog (or the browser blocked the window)
      if (kind === 'port') {
        setPortView({ node, port });
      } else {
        setNodeView(node);
      }
    }
  };
  const movesFor = (kind: External['kind'], node: number, port: number, current: ViewPlace, close: () => void, from?: Window) =>
    viewMoves(current, ['window', 'tab', ...(labAvailable() ? ['jupyter' as const] : []), 'dialog'], to =>
      moveView(kind, node, port, to, close, from)
    );
  /** Where a view opens: the preference, with Ctrl/Cmd switching window <-> tab. */
  const viewTarget = (ctrl: boolean): 'window' | 'tab' | 'dialog' | 'jupyter' => {
    const pref = settings.values.viewTarget;
    if (pref === 'dialog') {
      return ctrl ? 'tab' : 'dialog';
    }
    if (pref === 'jupyter') {
      return ctrl ? 'window' : labAvailable() ? 'jupyter' : 'dialog';
    }
    return (pref === 'tab') !== ctrl ? 'tab' : 'window';
  };
  /**
   * Port / node views in JupyterLab tabs (one per port or node; opening it
   * again focuses it). They live outside the editor and follow the workflow.
   */
  const labViews = useRef(new Map<string, LabViewWidget>());
  const openLab = (kind: External['kind'], node: number, port: number): boolean => {
    const key = `${kind}:${node}:${port}`;
    const existing = labViews.current.get(key);
    if (existing && !existing.isDisposed) {
      activateInLab(existing);
      return true;
    }
    const widget = new LabViewWidget(w => {
      const moves = movesFor(kind, node, port, 'jupyter', () => w.dispose());
      return kind === 'port' ? (
        <PortView controller={controller} nodeId={node} port={port} frame="panel" onTitle={t => (w.title.label = t)} onClose={() => w.dispose()} moves={moves} />
      ) : (
        <NodeViewDialog controller={controller} nodeId={node} frame="panel" onTitle={t => (w.title.label = t)} onClose={() => w.dispose()} moves={moves} />
      );
    });
    widget.disposed.connect(() => labViews.current.delete(key));
    labViews.current.set(key, widget);
    return openInLab(widget);
  };
  const openPort = (shownNode: number, shownPort: number, ctrl = false) => {
    // a metanode's output / the input bar shows the data passing through the port node
    const { id: node, port } = flatEnd(rawView!, level, shownNode, shownPort, 'out');
    const target = viewTarget(ctrl);
    if (target === 'jupyter' && openLab('port', node, port)) {
      return;
    }
    if (target === 'dialog' || target === 'jupyter' || !openExternal('port', node, port, target === 'tab')) {
      setPortView({ node, port });
    }
  };
  const openNodeView = (node: number, ctrl = false) => {
    const target = viewTarget(ctrl);
    if (target === 'jupyter' && openLab('view', node, 0)) {
      return;
    }
    if (target === 'dialog' || target === 'jupyter' || !openExternal('view', node, 0, target === 'tab')) {
      setNodeView(node);
    }
  };
  // "Execute and Open View": the window opens right away (inside the
  // user gesture) and shows the view once the node has executed
  const executeAndOpenView = (id: number, ctrl = false) => {
    const executed = byId.get(id)?.state === 'EXECUTED';
    const target = viewTarget(ctrl);
    if (target === 'dialog' || !openExternal('view', id, 0, target === 'tab')) {
      // window blocked: in-editor dialog, after execution if necessary
      if (executed) {
        setNodeView(id);
      } else {
        pendingViews.current.add(id);
      }
    }
    if (!executed) {
      void execute([id]);
    }
  };
  const openPortRef = useRef(openPort);
  openPortRef.current = openPort;
  const onOpenPort = useCallback((node: number, port: number, tab: boolean) => openPortRef.current(node, port, tab), []);
  // hovering a port: a look at its data after a moment; leaving it (and the widget): gone; Ctrl+click: kept
  const peekTimer = useRef<number | undefined>();
  const hideTimer = useRef<number | undefined>();
  const hidePeek = (delay = 250) => {
    window.clearTimeout(hideTimer.current);
    hideTimer.current = window.setTimeout(() => setInspections(list => list.filter(i => i.pinned)), delay);
  };
  inspectRef.current = (node, kind, port, action, near) => {
    window.clearTimeout(peekTimer.current);
    if (action === 'leave') {
      hidePeek();
      return;
    }
    // the output the data comes from (an input: the one connected to it)
    const c = kind === 'in' ? rawView?.connections.find(x => x.destID === node && x.destPort === port) : undefined;
    const src = kind === 'out' ? { node, port } : c ? { node: c.sourceID, port: c.sourcePort } : null;
    if (!src || rawView?.nodes.find(n => n.id === src.node)?.state !== 'EXECUTED') {
      return;
    }
    const here = portPoint(flow, node, kind, port);
    // (a free spot by the port - or by what points at the node, e.g. a chat: near, its box on the screen,
    // in canvas coordinates - not over nodes, canvas widgets or other inspection widgets)
    const taken = obstacles(flow, wrapper.current, []);
    const size = { w: INSPECT_W, h: INSPECT_H };
    let at = here ? place(here, kind === 'out' ? 'right' : 'left', size, taken) : { x: 0, y: 0 };
    if (near) {
      // left of it if that is in view, else right of it
      const box = wrapper.current?.getBoundingClientRect();
      const left = box ? flow.screenToFlowPosition({ x: box.left, y: box.top }).x : -Infinity;
      const l = place({ x: near.x, y: near.y }, 'left', size, taken);
      at = l.x + INSPECT_W <= near.x && l.x >= left ? l : place({ x: near.x + near.w, y: near.y }, 'right', size, taken);
    }
    const same = (i: Inspection) => i.node === src.node && i.port === src.port;
    if (action === 'pin') {
      window.clearTimeout(hideTimer.current);
      setInspections(list =>
        list.some(i => i.pinned && same(i))
          ? list.filter(i => i.pinned)
          : [
              ...list.filter(i => i.pinned),
              { ...(list.find(i => !i.pinned && same(i)) ?? { node: src.node, port: src.port, at }), key: `i${Date.now()}`, pinned: true }
            ]
      );
      return;
    }
    window.clearTimeout(hideTimer.current);
    peekTimer.current = window.setTimeout(
      () =>
        setInspections(list => [
          ...list.filter(i => i.pinned),
          ...(list.some(i => i.pinned && same(i)) ? [] : [{ key: 'peek', node: src.node, port: src.port, at, pinned: false }])
        ]),
      350
    );
  };
  // a display pointing at a node (a #id in a chat): a look at its first output while hovered, kept on a click
  inspectNodeRef.current = (id, pin, near) => {
    const info = id === null ? undefined : rawView?.nodes.find(n => n.id === id);
    const port = info?.outPorts.find(p => !p.implicit)?.index;
    if (id === null || !info || port === undefined) {
      hidePeek();
      return;
    }
    // (near: the box of what points at it on the screen - the widget is put beside it, where it can be seen)
    const box = near && {
      ...flow.screenToFlowPosition({ x: near.left, y: near.top }),
      w: flow.screenToFlowPosition({ x: near.right, y: near.top }).x - flow.screenToFlowPosition({ x: near.left, y: near.top }).x
    };
    inspectRef.current?.(id, 'out', port, pin ? 'pin' : 'enter', box || undefined);
  };
  /** Kept inspection widgets for the outputs of these nodes (executed ones), right of each node. */
  const inspectNodes = (ids: number[]) => {
    const add: Inspection[] = [];
    const taken = obstacles(flow, wrapper.current, []);
    for (const id of ids) {
      const info = rawView?.nodes.find(n => n.id === id);
      const node = flow.getInternalNode(String(id));
      if (!info || info.state !== 'EXECUTED' || !node) {
        continue;
      }
      const ports = info.outPorts.filter(p => !p.implicit).map(p => p.index);
      const p = node.internals.positionAbsolute;
      const w = node.measured?.width ?? 80;
      for (const port of ports.length ? ports : [0]) {
        const anchor = portPoint(flow, id, 'out', port) ?? { x: p.x + w, y: p.y + 30 };
        const at = place(anchor, 'right', { w: INSPECT_W, h: INSPECT_H }, taken);
        taken.push({ x: at.x, y: at.y, w: INSPECT_W, h: INSPECT_H });
        add.push({ key: `i${Date.now()}-${id}-${port}`, node: id, port, pinned: true, at });
      }
    }
    setInspections(list => [...list.filter(i => i.pinned && !add.some(a => a.node === i.node && a.port === i.port)), ...add]);
  };

  // close child windows with the editor (and when the page unloads)
  useEffect(() => {
    const closeAll = () => {
      externalsRef.current.forEach(x => x.win.close());
      labViews.current.forEach(w => w.dispose());
    };
    window.addEventListener('beforeunload', closeAll);
    return () => {
      window.removeEventListener('beforeunload', closeAll);
      closeAll();
    };
  }, []);
  const reset = (ids?: number[]) => run(() => controller.op('reset', ids ? { ids } : {}));
  const cancel = () => run(() => controller.op('cancel'));
  /** Nearest surviving upstream node of the deleted ones (selected after a delete). */
  const upstreamSurvivor = (ids: number[]): number | null => {
    const gone = new Set(ids);
    const start = cursor !== null && gone.has(cursor) ? cursor : ids[ids.length - 1];
    const seen = new Set<number>([start]);
    const queue = [start];
    while (queue.length) {
      const cur = queue.shift()!;
      const p = navGraph.positions.get(cur);
      const parents = parentsOf(navGraph, cur).sort((a, b) => {
        const pa = navGraph.positions.get(a);
        const pb = navGraph.positions.get(b);
        const d = (q?: { x: number; y: number }) => (p && q ? Math.abs(q.y - p.y) * 2 + Math.abs(q.x - p.x) : 0);
        return d(pa) - d(pb);
      });
      for (const parent of parents) {
        if (!gone.has(parent)) {
          return parent;
        }
        if (!seen.has(parent)) {
          seen.add(parent);
          queue.push(parent);
        }
      }
    }
    return null;
  };
  // after deleting, the nearest upstream node becomes selected / current
  /**
   * Delete nodes and select the nearest upstream node that remains; with
   * `reconnect` (Shift+Del) the nodes around the gap are connected again.
   */
  const remove = (ids: number[], reconnect = false) =>
    ids.length &&
    run(async () => {
      const next = upstreamSurvivor(ids);
      if (reconnect) {
        const links = planBridge(new Set(ids), view?.connections ?? []);
        await controller.op('rewire', { delete: ids, add: flatLinks(links) });
      } else {
        await controller.op('remove_nodes', { ids });
      }
      if (next !== null) {
        pendingSelect.current = new Set([String(next)]);
        setNodes(ns => ns.map(n => ({ ...n, selected: n.id === String(next) })));
        window.setTimeout(() => (pendingSelect.current = null), 500);
        setCursor(next);
      } else {
        setCursor(null);
      }
    });
  /**
   * Detach nodes (Alt+Del): remove their connections to the rest of the
   * workflow, reconnect the gap like Shift+Del, and move them down out of the
   * way of the new connection.
   */
  const detach = (ids: number[]) =>
    ids.length &&
    run(async () => {
      const chosen = new Set(ids);
      const connections = view?.connections ?? [];
      const cut = connections.filter(c => chosen.has(c.sourceID) !== chosen.has(c.destID));
      if (!cut.length) {
        setError('The selected nodes are not connected to other nodes');
        return;
      }
      const links = planBridge(chosen, connections);
      // move out of the line to the nearest free spot (below first, then above)
      const moved = (view?.nodes ?? []).filter(n => chosen.has(n.id));
      const others = (view?.nodes ?? []).filter(n => !chosen.has(n.id) && n.id > 0);
      const overlaps = (dy: number) =>
        moved.some(m => others.some(o => Math.abs(o.x - m.x) < 110 && Math.abs(o.y - (m.y + dy)) < 95));
      let dy = 110;
      for (let step = 110; step < 2000 && overlaps(dy); step += 55) {
        dy = overlaps(step) ? -step : step;
      }
      await controller.op('rewire', {
        positions: moved.map(n => ({ id: n.id, x: n.x, y: n.y + dy })),
        remove: cut.map(c => c.flat ?? c),
        add: flatLinks(links)
      });
    });
  const reloadNodes = () =>
    run(async () => {
      const v = await controller.op('reload_nodes');
      if (v.result.report.errors.length) {
        setError(`${v.result.report.errors.length} custom node file(s) failed to load — see Custom nodes… or the console`);
      }
    });
  const undo = () => run(() => controller.op('undo'));
  const redo = () => run(() => controller.op('redo'));
  const copySelection = (ids = selectedIds()) =>
    ids.length
      ? run(() =>
          trackCopy(
            controller.op('copy', { ids }).then(v => {
              setFragment(v.result.fragment);
            })
          )
        )
      : undefined;
  const cutSelection = (ids = selectedIds()) =>
    ids.length
      ? run(async () => {
          await copySelection(ids);
          await remove(ids);
        })
      : undefined;
  const paste = () =>
    run(async () => {
      const clip = await getFragment();
      if (!clip) {
        return;
      }
      const v = await controller.op('paste', { fragment: clip.fragment, dx: clip.offset, dy: clip.offset, parent: level });
      const ids = new Set<string>(v.result.ids.map(String));
      pendingSelect.current = ids;
      setNodes(ns => ns.map(n => ({ ...n, selected: ids.has(n.id) })));
      window.setTimeout(() => (pendingSelect.current = null), 500);
    });
  const openConfigure = (id: number) => {
    const n = byId.get(id);
    if (n?.hasDialog) {
      setConfigure(id);
    }
  };
  const editLabel = async (id: number) => {
    const n = byId.get(id);
    if (n?.kind === 'metanode') {
      return renameMetanode(id);
    }
    const result = await InputDialog.getText({ title: 'Edit Node Label', text: n?.annotation ?? '' });
    if (result.button.accept && result.value !== null) {
      await run(() => controller.op('set_annotation', { id, text: result.value }));
    }
  };

  const addNode = async (factory: string, position?: { x: number; y: number }) => {
    const sel = selectedIds();
    const anchor = sel.length === 1 ? byId.get(sel[0]) : undefined;
    let pos = position;
    if (!pos) {
      if (anchor) {
        pos = { x: anchor.x + 160, y: anchor.y };
      } else {
        const r = wrapper.current?.getBoundingClientRect();
        pos = r ? flow.screenToFlowPosition({ x: r.left + r.width / 2, y: r.top + r.height / 3 }) : { x: 0, y: 0 };
      }
    }
    await run(async () => {
      const v = await controller.op('add_node', { factory, x: Math.round(pos!.x), y: Math.round(pos!.y), parent: level });
      const newId: number = v.result.id;
      // adding a node while one node is selected connects them
      if (anchor && !position) {
        const target = repoByFactory.get(factory);
        const out = anchor.outPorts.find(p => !p.implicit && target?.inPorts.some(ip => ip.type === p.type));
        const inp = target?.inPorts.find(ip => ip.type === out?.type);
        if (out && inp) {
          await connectOp(anchor.id, out.index, newId, inp.index);
        }
      }
      selectOnly(newId);
    });
  };

  // ---------------------------------------------------------------- flow callbacks
  const portType = (nodeId: string | null, handle: string | null | undefined, dir: 'in' | 'out') => {
    const n = byId.get(Number(nodeId));
    const ports = dir === 'in' ? n?.inPorts : n?.outPorts;
    return ports?.find(p => p.index === parseHandle(handle))?.type;
  };

  const isValidConnection: IsValidConnection = c =>
    c.source !== c.target &&
    portType(c.source, c.sourceHandle, 'out') !== undefined &&
    portType(c.source, c.sourceHandle, 'out') === portType(c.target, c.targetHandle, 'in');

  const onConnect = (c: Connection) =>
    run(() => {
      const from = byId.get(Number(c.source));
      const to = byId.get(Number(c.target));
      if (from && to) {
        recordTransition(from.factory, to.factory);
      }
      return connectOp(Number(c.source), parseHandle(c.sourceHandle), Number(c.target), parseHandle(c.targetHandle));
    });

  const onEdgesDelete = (deleted: Edge[]) =>
    run(async () => {
      for (const e of deleted) {
        const c = e.data as any;
        await controller.op('disconnect', c.flat ?? c);
      }
    });

  const onDrop = (e: React.DragEvent) => {
    const factory = e.dataTransfer.getData(NODE_DRAG_MIME);
    if (!factory) {
      return;
    }
    e.preventDefault();
    const p = flow.screenToFlowPosition({ x: e.clientX, y: e.clientY });
    void addNode(factory, { x: p.x - 45, y: p.y - 30 });
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    const target = e.target as HTMLElement;
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) {
      return;
    }
    const sel = selectedIds();
    const handled = () => {
      e.preventDefault();
      e.stopPropagation();
    };
    if ((e.key === 'Delete' || e.key === 'Backspace') && e.altKey && sel.length) {
      handled();
      void detach(sel);
    } else if ((e.key === 'Delete' || e.key === 'Backspace') && e.shiftKey && sel.length) {
      // (stopping the event here keeps React Flow from deleting without reconnecting)
      handled();
      void remove(sel, true);
    } else if (e.key === 'Tab' && !e.ctrlKey && !e.metaKey && !e.altKey) {
      handled();
      if (sel.length > 1) {
        autoConnect(sel);
      } else {
        openQuickAdd();
      }
    } else if (KEY_DIRECTIONS[e.key]) {
      handled();
      navigate(KEY_DIRECTIONS[e.key], e.altKey || lineageLock.current, e.shiftKey);
    } else if (e.key === 'F6' && sel.length === 1) {
      handled();
      openConfigure(sel[0]);
    } else if (e.key === 'F7') {
      handled();
      void execute(e.shiftKey || !sel.length ? undefined : sel);
    } else if (e.key === 'F8') {
      handled();
      void reset(e.shiftKey || !sel.length ? undefined : sel);
    } else if (e.key === 'F10' && e.shiftKey && sel.length === 1) {
      handled();
      executeAndOpenView(sel[0], e.ctrlKey || e.metaKey);
    } else if (e.key === 'F9') {
      handled();
      void cancel();
    } else if (e.ctrlKey || e.metaKey) {
      const key = e.key.toLowerCase();
      if (key === 'a') {
        handled();
        selectAll();
      } else if (key === 'c') {
        handled();
        void copySelection();
      } else if (key === 'x') {
        handled();
        void cutSelection();
      } else if (key === 'v') {
        handled();
        void paste();
      } else if (key === 'z' && !e.shiftKey) {
        handled();
        void undo();
      } else if ((key === 'z' && e.shiftKey) || key === 'y') {
        handled();
        void redo();
      } else if (key === 'g' && sel.length) {
        handled();
        createGroup(sel);
      }
    }
  };

  // ---------------------------------------------------------------- menus
  const nodeMenuItems = (n: NodeInfo) => {
    const busy = n.state === 'EXECUTING' || n.state === 'QUEUED';
    return [
      { label: 'Configure…', shortcut: 'F6', disabled: !n.hasDialog || busy, action: () => openConfigure(n.id) },
      { label: 'Execute', shortcut: 'F7', disabled: busy || n.state === 'EXECUTED', action: () => execute([n.id]) },
      { label: 'Cancel', shortcut: 'F9', disabled: !busy, action: () => cancel() },
      { label: 'Reset', shortcut: 'F8', disabled: n.state !== 'EXECUTED', action: () => reset([n.id]) },
      ...(n.hasView
        ? [
            {
              label: 'Execute and Open View',
              shortcut: 'Shift+F10',
              disabled: busy,
              action: (e: React.MouseEvent) => executeAndOpenView(n.id, e.ctrlKey || e.metaKey)
            },
            {
              label: 'Open View',
              disabled: !n.viewAvailable,
              action: (e: React.MouseEvent) => openNodeView(n.id, e.ctrlKey || e.metaKey)
            }
          ]
        : []),
      ...(n.factory === 'verknode.scripting.JupyterNotebook'
        ? [
            {
              label: 'Open Notebook in JupyterLab',
              action: () =>
                void run(async () => {
                  const d = await controller.dialog(n.id);
                  if (!d.extra?.exists || !d.extra?.rootPath || !openFileInLab(d.extra.rootPath)) {
                    throw new Error('No notebook yet: create one in the node dialog (Configure…)');
                  }
                })
            }
          ]
        : []),
      ...(n.widget ? ['-' as const, ...widgetMenuItems(n)] : []),
      '-' as const,
      { label: 'Edit Node Label…', action: () => void editLabel(n.id) },
      {
        label: 'Collapse into Metanode…',
        disabled: busy,
        action: () => void collapse(selectedIds().length ? selectedIds() : [n.id])
      },
      {
        label: 'Create Group Annotation…',
        shortcut: 'Ctrl+G',
        action: () => createGroup(selectedIds().length ? selectedIds() : [n.id])
      },
      ...groupsOf(n.id).flatMap(g => [
        { label: `Edit Group “${groupTitle(g)}”…`, action: () => editGroup(g) },
        {
          label: `Remove from Group “${groupTitle(g)}”`,
          action: () => removeFromGroup(g, selectedIds().length ? selectedIds() : [n.id])
        }
      ]),
      { label: 'Cut', shortcut: 'Ctrl+X', disabled: busy, action: () => cutSelection(selectedIds().length ? selectedIds() : [n.id]) },
      { label: 'Copy', shortcut: 'Ctrl+C', action: () => copySelection(selectedIds().length ? selectedIds() : [n.id]) },
      { label: 'Paste', shortcut: 'Ctrl+V', disabled: !hasFragment(), action: () => paste() },
      { label: 'Delete', shortcut: 'Del', disabled: busy, action: () => remove(selectedIds().length ? selectedIds() : [n.id]) },
      {
        label: 'Delete and Reconnect',
        shortcut: 'Shift+Del',
        disabled: busy,
        action: () => remove(selectedIds().length ? selectedIds() : [n.id], true)
      },
      {
        label: 'Detach and Reconnect',
        shortcut: 'Alt+Del',
        disabled: busy,
        action: () => detach(selectedIds().length ? selectedIds() : [n.id])
      },
      '-' as const,
      {
        label: selectedIds().length > 1 && selectedIds().includes(n.id) ? `Inspect Outputs (${selectedIds().length} nodes)` : 'Inspect Outputs',
        disabled: !(selectedIds().includes(n.id) ? selectedIds() : [n.id]).some(id => byId.get(id)?.state === 'EXECUTED'),
        action: () => inspectNodes(selectedIds().includes(n.id) ? selectedIds() : [n.id])
      },
      {
        label: 'Close Inspections',
        disabled: !inspections.some(i => (selectedIds().includes(n.id) ? selectedIds() : [n.id]).includes(i.node)),
        action: () => {
          const ids = selectedIds().includes(n.id) ? selectedIds() : [n.id];
          setInspections(list => list.filter(i => !ids.includes(i.node)));
        }
      },
      ...n.outPorts
        .filter(p => !p.implicit)
        .map(p => ({
          label: `${p.index}: ${p.name}`,
          action: (e: React.MouseEvent) => openPort(n.id, p.index, e.ctrlKey || e.metaKey)
        })),
      { label: 'Flow Variables', action: (e: React.MouseEvent) => openPort(n.id, 0, e.ctrlKey || e.metaKey) }
    ];
  };

  /** Placement of canvas widgets: of the selected widget nodes when n is one of them. */
  const widgetMenuItems = (n: NodeInfo) => {
    const sel = selectedIds();
    const targets = (sel.includes(n.id) ? sel : [n.id]).map(id => byId.get(id)).filter((x): x is NodeInfo => !!x?.widget);
    const layout = n.widgetLayout ?? {};
    const side = layout.side ?? (n.widget!.type === 'input' ? 'left' : 'right');
    const update = (change: (l: WidgetLayout, x: NodeInfo) => WidgetLayout) =>
      void run(() => controller.op('set_widget_layouts', { layouts: targets.map(x => ({ id: x.id, layout: change(x.widgetLayout ?? {}, x) })) }));
    const dock = (to: 'left' | 'right' | 'above' | 'below') => ({
      label: `${side === to ? '✓ ' : ''}Widget ${to === 'left' || to === 'right' ? 'on the ' : ''}${to[0].toUpperCase()}${to.slice(1)}`,
      action: () => update(l => ({ ...l, side: to, x: undefined, y: undefined, anchored: undefined }))
    });
    return [
      dock('left'),
      dock('right'),
      dock('above'),
      dock('below'),
      { label: layout.minimized ? 'Restore Widget' : 'Minimize Widget', action: () => update(l => ({ ...l, minimized: !layout.minimized })) },
      {
        label: 'Default Widget Size',
        disabled: layout.w === undefined && layout.h === undefined,
        action: () => update(l => ({ ...l, w: undefined, h: undefined }))
      }
    ];
  };

  const metanodeMenuItems = (n: NodeInfo) => {
    const m = rawView?.metanodes.find(x => x.id === n.id);
    const busy = innerNodes(rawView!, n.id).some(x => x.state === 'EXECUTING' || x.state === 'QUEUED');
    const ids = () => (selectedIds().length ? selectedIds() : [n.id]);
    return [
      { label: 'Open Metanode', action: () => enterLevel(n.id) },
      { label: 'Execute', shortcut: 'F7', disabled: busy || n.state === 'EXECUTED', action: () => execute([n.id]) },
      { label: 'Cancel', shortcut: 'F9', disabled: !busy, action: () => cancel() },
      { label: 'Reset', shortcut: 'F8', disabled: busy, action: () => reset([n.id]) },
      '-' as const,
      { label: 'Rename Metanode…', action: () => void renameMetanode(n.id) },
      { label: 'Expand Metanode', disabled: busy, action: () => void run(() => controller.op('expand', { id: n.id })) },
      { label: 'Collapse into Metanode…', disabled: busy, action: () => void collapse(ids()) },
      { label: 'Add Input Port (table)', action: () => addPort(n.id, 'in', 'table') },
      { label: 'Add Input Port (flow variables)', action: () => addPort(n.id, 'in', 'flowvariable') },
      { label: 'Add Output Port (table)', action: () => addPort(n.id, 'out', 'table') },
      ...(m?.inputs ?? []).map((_, k) => ({
        label: `Remove Input Port ${k + 1}`,
        action: () => void run(() => controller.op('remove_metanode_port', { id: n.id, side: 'in', index: k + 1 }))
      })),
      ...(m?.outputs ?? []).map((_, k) => ({
        label: `Remove Output Port ${k + 1}`,
        action: () => void run(() => controller.op('remove_metanode_port', { id: n.id, side: 'out', index: k + 1 }))
      })),
      '-' as const,
      { label: 'Create Group Annotation…', shortcut: 'Ctrl+G', action: () => createGroup(ids()) },
      { label: 'Cut', shortcut: 'Ctrl+X', disabled: busy, action: () => cutSelection(ids()) },
      { label: 'Copy', shortcut: 'Ctrl+C', action: () => copySelection(ids()) },
      { label: 'Delete', shortcut: 'Del', disabled: busy, action: () => remove(ids()) },
      { label: 'Delete and Reconnect', shortcut: 'Shift+Del', disabled: busy, action: () => remove(ids(), true) },
      { label: 'Detach and Reconnect', shortcut: 'Alt+Del', disabled: busy, action: () => detach(ids()) },
      '-' as const,
      ...n.outPorts.map(p => ({
        label: `${p.index}: ${p.name}`,
        action: (e: React.MouseEvent) => openPort(n.id, p.index, e.ctrlKey || e.metaKey)
      }))
    ];
  };

  // inside a metanode: leave it, add ports
  const levelMenuItems =
    level !== null
      ? [
          { label: 'Leave Metanode', action: () => enterLevel(rawView?.metanodes.find(m => m.id === level)?.parent ?? null) },
          { label: 'Add Input Port (table)', action: () => addPort(level, 'in', 'table') },
          { label: 'Add Input Port (flow variables)', action: () => addPort(level, 'in', 'flowvariable') },
          { label: 'Add Output Port (table)', action: () => addPort(level, 'out', 'table') },
          '-' as const
        ]
      : [];

  const paneMenuItems = [
    ...levelMenuItems,
    { label: 'Undo', shortcut: 'Ctrl+Z', disabled: !view?.canUndo, action: () => undo() },
    { label: 'Redo', shortcut: 'Ctrl+Shift+Z', disabled: !view?.canRedo, action: () => redo() },
    { label: 'Paste', shortcut: 'Ctrl+V', disabled: !hasFragment(), action: () => paste() },
    '-' as const,
    { label: 'Execute All', shortcut: 'Shift+F7', action: () => execute() },
    { label: 'Cancel All', shortcut: 'F9', disabled: !view?.executing, action: () => cancel() },
    { label: 'Reset All', shortcut: 'Shift+F8', action: () => reset() },
    '-' as const,
    { label: 'Workflow Variables…', action: () => setWfVars(true) },
    { label: 'Export as Python…', action: () => setPythonOpen(true) },
    { label: 'Fit to Screen', action: () => fitAll(200) },
    ...(inspections.length
      ? ['-' as const, { label: `Close All Inspections (${inspections.length})`, action: () => setInspections([]) }]
      : [])
  ];
  // the menu of an inspection widget
  const inspectionMenuItems = (key: string) => [
    { label: 'Close', action: () => setInspections(list => list.filter(i => i.key !== key)) },
    {
      label: 'Close Others',
      disabled: inspections.length < 2,
      action: () => setInspections(list => list.filter(i => i.key === key))
    },
    { label: `Close All Inspections (${inspections.length})`, action: () => setInspections([]) }
  ];

  const localPos = (e: React.MouseEvent | MouseEvent) => {
    const r = wrapper.current!.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };

  const sel = selectedIds();
  const single = sel.length === 1 ? byId.get(sel[0]) : undefined;
  const counts = useMemo(() => {
    const all = (rawView?.nodes ?? []).filter(n => !n.boundary);
    return { executed: all.filter(n => n.state === 'EXECUTED').length, total: all.length };
  }, [rawView]);

  return (
    <CanvasLinkContext.Provider value={canvasLink}>
    <div
      ref={editorRef}
      className={`verknode-editor verknode-theme-${prefs.theme} verknode-lights-${prefs.statusLight} ${prefs.statusLight.startsWith('led-') ? 'verknode-lights-led' : ''} ${theme.dark ? 'verknode-theme-dark' : ''} ${prefs.showFlowVariablePorts ? 'verknode-show-flow-ports' : ''} ${panning ? 'verknode-panning' : ''}`}
      style={themeStyle(theme)}
      onKeyDown={onKeyDown}
      onPointerDownCapture={e => {
        if (e.shiftKey && startBand(e)) {
          return;
        }
        if ((e.ctrlKey || e.metaKey) && startCtrlPan(e)) {
          return;
        }
        // remember the selection before React Flow changes it, for Ctrl/Shift+click
        if (e.ctrlKey || e.metaKey || e.shiftKey) {
          selectionBeforeClick.current = new Set(nodes.filter(n => n.selected).map(n => n.id));
        }
      }}
      tabIndex={-1}
      onFocus={() => (focusInside.current = true)}
      onBlur={e => {
        // focus moved to another element on purpose; a null target means the focused
        // element went away (e.g. a deleted node), which should not end keyboard use
        if (e.relatedTarget && !editorRef.current?.contains(e.relatedTarget as Node)) {
          focusInside.current = false;
        }
      }}
    >
      <div className="verknode-lineage-badge" title="Arrow keys follow the connections. Tap Alt (or clear the selection) to go back to moving by position.">
        Lineage navigation · tap Alt to leave
      </div>
      <div className="verknode-toolbar">
        <span className="verknode-brand" title="verknode">
          <svg viewBox="133 399 928 381" height="13" aria-hidden="true">
            <path d="M133 399H239L375 656L512 399H615L400 780H334Z" fill="#5DA9E9" />
            <path d="M665 400H762L625 640H527Z" fill="#F52D1A" />
            <path d="M815 400H912L772 641H675Z" fill="#FFC400" />
            <path d="M963 400H1061L920 644H823Z" fill="#00B86A" />
          </svg>
          Verknode
        </span>
        <span className="verknode-tb-sep" />
        <button className="verknode-tb" disabled={!view?.canUndo} onClick={undo} title="Undo (Ctrl+Z)">
          ↶
        </button>
        <button className="verknode-tb" disabled={!view?.canRedo} onClick={redo} title="Redo (Ctrl+Shift+Z / Ctrl+Y)">
          ↷
        </button>
        <span className="verknode-tb-sep" />
        <button className="verknode-tb" disabled={!single?.hasDialog} onClick={() => single && openConfigure(single.id)} title="Configure selected node (F6)">
          ⚙ Configure
        </button>
        <button className="verknode-tb" disabled={!sel.length} onClick={() => execute(sel)} title="Execute selected nodes (F7)">
          <span className="verknode-tb-run">▶</span> Execute
        </button>
        <button className="verknode-tb" onClick={() => execute()} title="Execute all nodes (Shift+F7)">
          <span className="verknode-tb-run">⏩</span> Execute all
        </button>
        <button className="verknode-tb" disabled={!view?.executing} onClick={cancel} title="Cancel all (F9)">
          <span className="verknode-tb-stop">■</span> Cancel
        </button>
        <button
          className="verknode-tb"
          disabled={!sel.length && !view?.nodes.some(n => n.state === 'EXECUTED')}
          onClick={e => void resetButton(e)}
          title="Reset selected nodes (F8); Ctrl+click, or with nothing selected: reset all nodes (Shift+F8)"
        >
          ↺ Reset
        </button>
        <span className="verknode-tb-sep" />
        <button className="verknode-tb" onClick={() => setWfVars(true)} title="Edit workflow variables">
          <VarBadge type="STRING" /> Workflow variables
        </button>
        <button className="verknode-tb" onClick={() => setPythonOpen(true)} title="Python script / API for this workflow">
          🐍 Python
        </button>
        {!!view?.nodePaths.length && (
          <button
            className="verknode-tb"
            onClick={reloadNodes}
            title={`Reload custom nodes from ${view.nodePaths.join(', ')}`}
          >
            ⟳ Reload nodes
            {view.customReport?.errors.length ? <span className="verknode-tb-errors">{view.customReport.errors.length}</span> : null}
          </button>
        )}
        <span className="verknode-tb-spacer" />
        <span className="verknode-tb-status">
          {view?.executing ? 'Executing…' : `${counts.executed}/${counts.total} executed`}
          {view?.dirty ? ' • unsaved' : ''}
        </span>
        <button className="verknode-tb" onClick={() => setSettingsOpen(true)} title="Editor settings and custom nodes">
          ≡ Settings
        </button>
      </div>
      {error && (
        <div className="verknode-error-banner">
          <span>{error}</span>
          <button className="verknode-icon-btn" onClick={() => setError(null)}>
            ✕
          </button>
        </div>
      )}
      <div className="verknode-main">
        <div className="verknode-side">
          <Repository
            nodes={repo}
            onAdd={f => void addNode(f)}
            onSelect={setDescribed}
            query={repoQuery}
            onQuery={setRepoQuery}
            highlight={quick ? (quickItems[quick.index]?.factory ?? null) : null}
          />
          <Description node={described} />
        </div>
        <div
          className={`verknode-canvas verknode-drag-${prefs.dragBehavior}`}
          ref={wrapper}
          onMouseMove={e => (pointer.current = { x: e.clientX, y: e.clientY })}
          onMouseLeave={() => (pointer.current = null)}
          onDoubleClickCapture={e => {
            // on the blank canvas inside a bubble (or on its label): Ctrl+double-click selects
            // the nodes of the innermost bubble (loop, try/catch, switch or group), double-click
            // edits a group annotation; nodes, edges and ports keep their own double-click
            // (React Flow nests them inside the pane element)
            if (!(e.target as HTMLElement).classList.contains('react-flow__pane')) {
              return;
            }
            if (e.ctrlKey || e.metaKey) {
              const b = bubbleAt(e.clientX, e.clientY);
              if (b) {
                e.stopPropagation();
                selectMembers(b.members);
              }
            } else if (!e.shiftKey) {
              const g = groupsById.get(groupAt(e.clientX, e.clientY) ?? -1);
              if (g) {
                e.stopPropagation();
                editGroup(g);
              }
            }
          }}
          onDragOver={e => e.preventDefault()}
          onDrop={onDrop}
        >
          {level !== null && rawView && (
            <div className="verknode-breadcrumb" onMouseDown={e => e.stopPropagation()}>
              {levelPath(rawView, level).map((m, i, all) => (
                <React.Fragment key={m?.id ?? 'top'}>
                  {i > 0 && <span className="verknode-breadcrumb-sep">›</span>}
                  {i < all.length - 1 ? (
                    <button className="verknode-breadcrumb-link" onClick={() => enterLevel(m?.id ?? null)}>
                      {m ? m.name : 'Workflow'}
                    </button>
                  ) : (
                    <span className="verknode-breadcrumb-current">⊞ {m?.name}</span>
                  )}
                </React.Fragment>
              ))}
            </div>
          )}
          {quick && (
            <QuickAdd
              x={quick.x}
              y={quick.y}
              source={quickSource}
              port={quickPort}
              portCount={quickPorts.length}
              query={quick.query}
              items={quickItems}
              index={Math.min(quick.index, Math.max(0, quickItems.length - 1))}
              onQuery={q => {
                setQuick({ ...quick, query: q, index: 0 });
                setRepoQuery(q);
              }}
              onIndex={i => setQuick(qa => (qa ? { ...qa, index: i } : qa))}
              onCyclePort={step =>
                setQuick(qa =>
                  qa ? { ...qa, portIdx: (qa.portIdx + step + quickPorts.length) % Math.max(1, quickPorts.length), index: 0 } : qa
                )
              }
              onPick={pickQuickAdd}
              onClose={() => quick && closeQuickAdd()}
            />
          )}
          {band && (
            <div
              className="verknode-band"
              style={{
                left: Math.min(band.x0, band.x1),
                top: Math.min(band.y0, band.y1),
                width: Math.abs(band.x1 - band.x0),
                height: Math.abs(band.y1 - band.y0)
              }}
            />
          )}
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            colorMode={theme.dark ? 'dark' : 'light'}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            isValidConnection={isValidConnection}
            onNodesDelete={ns => void remove(ns.map(n => Number(n.id)))}
            onEdgesDelete={onEdgesDelete}
            onNodeDragStart={(_, __, dragged) => onDragStart(dragged)}
            onNodeDrag={(_, __, dragged) => {
              setInsertEdge(edgeUnder(dragged));
              followDrag(dragged);
            }}
            onSelectionStart={e => (selectionStart.current = { x: e.clientX, y: e.clientY })}
            onSelectionEnd={e => {
              const s = selectionStart.current;
              selectionStart.current = null;
              if (s) {
                widgetSel.setExplicit(widgetsIn(s.x, s.y, e.clientX, e.clientY));
              }
            }}
            onNodeDragStop={(_, __, dragged) => onDragStop(dragged)}
            onNodeClick={(e, n) => {
              setCursor(Number(n.id));
              // a plain click on a node (not on its widget) replaces the widget selection
              if (!(e.ctrlKey || e.metaKey || e.shiftKey) && !(e.target as HTMLElement).closest('.verknode-widget-dock')) {
                widgetSel.setExplicit([]);
              }
              setDescribed(repoByFactory.get((n.data as any).node.factory) ?? null);
              // Ctrl/Cmd/Shift + click toggles the node in the selection
              if (e.ctrlKey || e.metaKey || e.shiftKey) {
                const before = selectionBeforeClick.current;
                const next = new Set(before);
                if (before.has(n.id)) {
                  next.delete(n.id);
                } else {
                  next.add(n.id);
                }
                setNodes(ns => ns.map(x => ({ ...x, selected: next.has(x.id) })));
              }
            }}
            onNodeDoubleClick={(e, n) => {
              // Ctrl+double-click selects downstream, Shift+double-click upstream
              if (e.ctrlKey || e.metaKey) {
                selectConnected(Number(n.id), 'down');
              } else if (e.shiftKey) {
                selectConnected(Number(n.id), 'up');
              } else if (byId.get(Number(n.id))?.kind === 'metanode') {
                enterLevel(Number(n.id));
              } else {
                openConfigure(Number(n.id));
              }
            }}
            onNodeContextMenu={(e, n) => {
              e.preventDefault();
              if (!n.selected) {
                selectOnly(Number(n.id));
              }
              setMenu({ ...localPos(e), nodeId: Number(n.id) });
            }}
            onPaneContextMenu={e => {
              e.preventDefault();
              const ev = e as React.MouseEvent;
              setMenu({ ...localPos(ev), nodeId: null, groupId: groupAt(ev.clientX, ev.clientY) });
            }}
            onPaneClick={() => {
              setMenu(null);
              setCursor(null);
              widgetSel.setExplicit([]);
            }}
            deleteKeyCode={['Delete', 'Backspace']}
            // left-drag pans by default (Shift+drag selects, see startBand);
            // the "select" preference restores drag-to-select
            selectionOnDrag={prefs.dragBehavior === 'select'}
            panOnDrag={prefs.dragBehavior === 'pan' ? [0, 1] : [1]}
            selectionKeyCode={null}
            disableKeyboardA11y
            panActivationKeyCode={null}
            zoomOnScroll={prefs.wheelBehavior === 'zoom'}
            zoomOnDoubleClick={false}
            panOnScroll={prefs.wheelBehavior === 'pan'}
            zoomActivationKeyCode={prefs.wheelBehavior === 'pan' ? 'Control' : null}
            multiSelectionKeyCode={null}
            minZoom={0.2}
            maxZoom={2.5}
            snapToGrid={prefs.snapToGrid}
            snapGrid={[5, 5]}
            connectionRadius={24}
            connectOnClick={false}
            proOptions={{ hideAttribution: true }}
          >
            <Background variant={BackgroundVariant.Dots} gap={20} size={1} />
            <ViewportPortal>
              {scopeBubbles.map(({ key, block, shape }) => {
                const cls = `verknode-scope-${block.kind} ${block.end === null ? 'verknode-scope-open' : ''}`;
                const group = block.groupId !== undefined ? groupsById.get(block.groupId) : undefined;
                const color = group ? ({ '--scope': group.color } as React.CSSProperties) : {};
                const label = (
                  <span
                    className={`verknode-scope-label ${cls}`}
                    style={{ ...color, transform: `translate(${shape.label.x}px, ${shape.label.y}px)` }}
                    data-group={group?.id}
                    data-bubble={key}
                  >
                    {group ? groupTitle(group) : SCOPE_LABEL[block.kind]}
                    {block.end === null ? ' …' : ''}
                  </span>
                );
                const m = 3;
                const { x, y, w, h } = shape.box;
                return (
                  <React.Fragment key={key}>
                    <svg
                      className={`verknode-scope-svg ${cls}`}
                      data-group={group?.id}
                      data-bubble={key}
                      style={{ ...color, transform: `translate(${x - m}px, ${y - m}px)` }}
                      width={w + 2 * m}
                      height={h + 2 * m}
                      viewBox={`${x - m} ${y - m} ${w + 2 * m} ${h + 2 * m}`}
                    >
                      <path d={shape.path} />
                    </svg>
                    {label}
                  </React.Fragment>
                );
              })}
              <WidgetTails store={tails} />
              <WidgetHints selection={widgetSel} rects={() => widgetOps.current.env().rects} />
            </ViewportPortal>
            <InspectLayer
              controller={controller}
              tails={tails}
              inspections={inspections}
              onMenu={(key, e) => setMenu({ ...localPos(e), nodeId: null, inspection: key })}
              onChange={(key, patch) =>
                setInspections(list =>
                  list.map(i => (i.key !== key ? i : { ...i, ...patch, ...(patch.pinned && !i.pinned ? { key: `i${Date.now()}` } : {}) }))
                )
              }
              onClose={key => setInspections(list => list.filter(i => i.key !== key))}
              onHover={(key, inside) => {
                if (key !== 'peek') {
                  return;
                }
                if (inside) {
                  window.clearTimeout(hideTimer.current);
                } else {
                  hidePeek();
                }
              }}
              onOpen={(node, port) => openPort(node, port)}
            />
            <Controls showInteractive={false} showFitView={false}>
              <ControlButton onClick={() => fitAll(200)} title="Fit view" aria-label="fit view" className="react-flow__controls-fitview">
                <svg viewBox="0 0 32 30" xmlns="http://www.w3.org/2000/svg">
                  <path d="M3.692 4.63c0-.53.4-.938.939-.938h5.215V0H4.708C2.13 0 0 2.054 0 4.63v5.216h3.692V4.631zM27.354 0h-5.2v3.692h5.17c.53 0 .984.4.984.939v5.215H32V4.631A4.624 4.624 0 0027.354 0zm.954 24.83c0 .532-.4.94-.939.94h-5.215v3.768h5.215c2.577 0 4.631-2.13 4.631-4.707v-5.139h-3.692v5.139zm-23.677.94c-.531 0-.939-.4-.939-.94v-5.138H0v5.139c0 2.577 2.13 4.707 4.708 4.707h5.138V25.77H4.631z" />
                </svg>
              </ControlButton>
            </Controls>
            {prefs.showMinimap && (
              <MiniMap
                pannable
                zoomable
                nodeColor={n => nodeColor(theme, (n.data as any)?.node?.nodeType ?? 'Other', (n.data as any)?.node?.category)}
                maskColor={theme.minimapMask}
                className="verknode-minimap"
              />
            )}
          </ReactFlow>
          {view && view.nodes.length === 0 && (
            <div className="verknode-canvas-hint">
              Drag nodes from the Node Repository onto the canvas, or double-click them.
              <br />
              Connect black triangle ports (tables) and red circle ports (flow variables).
            </div>
          )}
          {menu && (
            <ContextMenu
              menu={menu}
              items={
                menu.inspection
                  ? inspectionMenuItems(menu.inspection)
                  : menu.nodeId !== null && byId.get(menu.nodeId)?.kind === 'metanode'
                  ? metanodeMenuItems(byId.get(menu.nodeId)!)
                  : menu.nodeId !== null && byId.get(menu.nodeId) && menu.nodeId > 0
                  ? nodeMenuItems(byId.get(menu.nodeId)!)
                  : menu.groupId != null && groupsById.get(menu.groupId)
                    ? [...groupMenuItems(groupsById.get(menu.groupId)!), '-' as const, ...paneMenuItems]
                    : paneMenuItems
              }
              onClose={() => setMenu(null)}
            />
          )}
        </div>
      </div>
      <Console
        controller={controller}
        open={consoleOpen}
        onToggle={() => setConsoleOpen(!consoleOpen)}
        onSelectNode={selectOnly}
        onPython={() => setPythonOpen(true)}
        python={prefs.showPythonCommands}
        setPython={on => void settings.set('showPythonCommands', on)}
      />
      {pythonOpen && (
        <PythonDialog controller={controller} groups={rawView?.groups ?? []} selected={selectedIds()} onClose={() => setPythonOpen(false)} />
      )}
      {settingsOpen && (
        <SettingsDialog controller={controller} settings={settings} onClose={() => setSettingsOpen(false)} />
      )}
      {configure !== null && (
        <ConfigureDialog controller={controller} nodeId={configure} onClose={() => setConfigure(null)} editorSettings={settings} />
      )}
      {portView && (
        <PortView
          controller={controller}
          nodeId={portView.node}
          port={portView.port}
          onClose={() => setPortView(null)}
          moves={movesFor('port', portView.node, portView.port, 'dialog', () => setPortView(null))}
        />
      )}
      {externals.map(x => (
        <ChildWindow
          key={x.key}
          win={x.win}
          title={x.kind === 'view' ? 'View' : 'Port view'}
          onClose={() => setExternals(xs => xs.filter(y => y.key !== x.key))}
        >
          {x.kind === 'port' ? (
            <PortView
              controller={controller}
              nodeId={x.node}
              port={x.port}
              frame="window"
              onTitle={t => (x.win.document.title = t)}
              onClose={() => x.win.close()}
              moves={movesFor('port', x.node, x.port, x.tab ? 'tab' : 'window', () => x.win.close(), x.win)}
            />
          ) : (
            <NodeViewDialog
              controller={controller}
              nodeId={x.node}
              frame="window"
              onTitle={t => (x.win.document.title = t)}
              onClose={() => x.win.close()}
              moves={movesFor('view', x.node, 0, x.tab ? 'tab' : 'window', () => x.win.close(), x.win)}
            />
          )}
        </ChildWindow>
      ))}
      {sketch !== null &&
        (() => {
          const w = rawView?.nodes.find(n => n.id === sketch)?.widget;
          return w?.type === 'input' ? (
            <SketcherDialog
              items={[{ name: w.label || w.name, structure: w.value ?? '' }]}
              start={0}
              editor={prefs.sketcher}
              onEditor={kind => void settings.set('sketcher', kind)}
              onSave={(_, __, molfile) => molfile !== null && widgetRef.current?.(sketch, molfile)}
              onClose={() => setSketch(null)}
            />
          ) : null;
        })()}
      {nodeView !== null && (
        <NodeViewDialog
          controller={controller}
          nodeId={nodeView}
          onClose={() => setNodeView(null)}
          moves={movesFor('view', nodeView, 0, 'dialog', () => setNodeView(null))}
        />
      )}
      {wfVars && <WorkflowVariablesDialog controller={controller} onClose={() => setWfVars(false)} />}
      {groupDialog && view && (
        <GroupDialog
          controller={controller}
          group={groupDialog.id !== null ? groupsById.get(groupDialog.id) ?? null : null}
          nodes={groupDialog.nodes}
          colors={view.groupColors ?? []}
          nodeName={id => byId.get(id)?.annotation || byId.get(id)?.name || `Node ${id}`}
          selection={selectedIds}
          onClose={() => setGroupDialog(null)}
        />
      )}
    </div>
    </CanvasLinkContext.Provider>
  );
}

export function WorkflowEditor({ controller, settings }: { controller: WorkflowController; settings: EditorSettings }) {
  const loadDisplay = useCallback((nodeId: number) => controller.widgetDisplay(nodeId), [controller]);
  return (
    <ReactFlowProvider>
      <DisplayLoader.Provider value={loadDisplay}>
        <EditorCanvas controller={controller} settings={settings} />
      </DisplayLoader.Provider>
    </ReactFlowProvider>
  );
}
