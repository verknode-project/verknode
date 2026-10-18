export type PortType = 'table' | 'flowvariable' | 'model' | 'image' | 'dbsession' | 'dbdata';
export type NodeState = 'IDLE' | 'CONFIGURED' | 'EXECUTED' | 'EXECUTING' | 'QUEUED';
export type VarType = 'STRING' | 'INTEGER' | 'DOUBLE' | 'BOOLEAN';

export interface PortInfo {
  index: number;
  type: PortType;
  name: string;
  description: string;
  optional: boolean;
  implicit: boolean;
}

export interface FieldInfo {
  key: string;
  type: string;
  label: string;
  default: any;
  description: string;
  options?: string[];
  port?: number;
  kinds?: string[];
  fields?: FieldInfo[];
  language?: string;
  insertables?: boolean;
  path?: boolean;
  show_if?: Record<string, any>;
  /** sketcher fields: settings whose change invalidates edits ("structure" / "name") */
  depends?: Record<string, 'structure' | 'name'>;
}

export interface NodeDescription {
  factory: string;
  name: string;
  category: string;
  nodeType: string;
  icon: string;
  shortDescription: string;
  description: string;
  inPorts: PortInfo[];
  outPorts: PortInfo[];
  fields: FieldInfo[];
  hasDialog: boolean;
  hasView: boolean;
  /** factories of nodes closing the scope this node opens (loop end, End IF, Catch ...) */
  closedBy: string[];
  custom?: boolean;
  source?: string;
}

export interface CustomReport {
  loaded: { factory: string; name: string; category: string; file: string }[];
  errors: { file: string; error: string }[];
  time: string;
  reason: string;
}

export interface NodeMessage {
  level: 'ERROR' | 'WARNING' | 'INFO';
  text: string;
}

export interface NodeInfo {
  id: number;
  factory: string;
  name: string;
  nodeType: string;
  icon: string;
  category: string;
  x: number;
  y: number;
  annotation: string;
  state: NodeState;
  inactive: boolean;
  inactivePorts: number[];
  message: NodeMessage | null;
  inPorts: PortInfo[];
  outPorts: PortInfo[];
  hasDialog: boolean;
  hasView: boolean;
  viewAvailable: boolean;
  loopRole: 'start' | 'end' | null;
  /** runs, like the output widgets, when an input widget before it changes (e.g. Workflow Script) */
  runsWithInputs?: boolean;
  usesVariables: boolean;
  /** rows of each table output, by port index (when executed) */
  outRows?: (number | null)[];
  /** metanode containing the node */
  parent?: number | null;
  /** canvas widget (input / output widget nodes): what the node shows and edits on the canvas */
  widget?: WidgetInfo;
  widgetLayout?: WidgetLayout;
  /** run times (seconds) shown under the status light */
  timing?: NodeTiming | null;
  /** loop start / end of a running loop: the iteration (1-based) of total (null: unknown; estimated: as the last run) */
  loopIteration?: { iteration: number; total: number | null; estimated: boolean } | null;
  /** set on the hidden pass-through node of a metanode port */
  boundary?: { metanode: number; side: 'in' | 'out'; index: number } | null;
  /** metanode or metanode port bar (only in a level view, see editor/metanodes.ts) */
  kind?: 'metanode' | 'inbar' | 'outbar';
}

/** Run times of a node (engine: Workflow._timings). */
export interface NodeTiming {
  /** of its execution, and per row it worked on (rows of its first input table, else first output) */
  time: number | null;
  rows: number | null;
  perRow: number | null;
  /**
   * configured: expected time to run it with the not executed nodes before it;
   * executing / queued: expected time until it is done (nodes run one at a time)
   */
  eta: number | null;
  /** some of those nodes never ran: no estimate for them */
  etaPartial: boolean;
  /** start nodes ("workflow"): it and everything after it; end nodes ("branch"): it and everything before */
  total: number | null;
  totalKind: 'workflow' | 'branch' | null;
  totalEstimated: boolean;
  totalPartial: boolean;
  /** while executing / queued: its own expected time (last run), time so far, progress (0..1, if the node tells) */
  estimate?: number | null;
  elapsed?: number | null;
  progress?: number | null;
  /** past its expected time; progress behind the expected; (queued) waiting on such a node */
  overdue?: boolean;
  slow?: boolean;
  late?: boolean;
}

/** Input widget: a control whose value the node outputs (see engine/widgets.py). */
export interface InputWidgetInfo {
  type: 'input';
  /** text | textarea | number | integer | slider | checkbox | dropdown | table | molecule | html */
  control: string;
  label: string;
  name: string;
  value: any;
  /** the value was changed on the canvas (↺ goes back to the default) */
  edited: boolean;
  autoRun: boolean;
  /** control-specific: options (dropdown), min / max / step (slider), width / height (html) */
  props: Record<string, any>;
  /** page of an html control */
  html?: string;
}

/** Output widget: shows the data set while executing (null before). */
export interface OutputWidgetInfo {
  type: 'output';
  /** table | bar | line | scatter | value | image | html */
  renderer: string;
  title: string;
  /** starting size of the display (WidgetLayout w / h once resized) */
  width: number;
  height: number;
  data: any;
  /** the user can select on the display (rows of the "Selection" output port) */
  interactive: boolean;
  /** what is selected, e.g. {rows: [...]} (null: nothing) */
  selection: any;
}

export type WidgetInfo = InputWidgetInfo | OutputWidgetInfo;

/**
 * Where a canvas widget sits: docked at a side of its node (default: inputs
 * left, outputs right) or moved to a place of its own on the canvas (x / y),
 * its size once resized and whether it is minimized to its title bar.
 */
export interface WidgetLayout {
  side?: 'left' | 'right' | 'above' | 'below' | 'free';
  /** canvas position of a moved ("free") widget: it stays there when its node moves */
  x?: number;
  y?: number;
  w?: number;
  h?: number;
  minimized?: boolean;
  /** kept where it is: not dragged, not moved along with its node (or a selection) */
  anchored?: boolean;
}

export interface ConnectionInfo {
  sourceID: number;
  sourcePort: number;
  destID: number;
  destPort: number;
  type: PortType;
  inactive: boolean;
  /** the workflow's connection, when a level view shows it between metanode ports */
  flat?: ConnectionInfo;
}

/** Container of nodes; its ports are pass-through nodes ("inputs"/"outputs", in port order). */
export interface MetanodeInfo {
  id: number;
  name: string;
  parent: number | null;
  x: number;
  y: number;
  inputs: number[];
  outputs: number[];
}

export interface FlowVariableInfo {
  name: string;
  type: VarType;
  value: any;
}

export interface LogEntry {
  seq: number;
  time: string;
  level: string;
  node: number | null;
  text: string;
}

export interface WorkflowView {
  session: string;
  version: number;
  dirty: boolean;
  executing: boolean;
  canUndo: boolean;
  canRedo: boolean;
  nodePaths: string[];
  autoReload: boolean;
  registryVersion: number;
  customReport: CustomReport | null;
  nodes: NodeInfo[];
  connections: ConnectionInfo[];
  workflowVariables: FlowVariableInfo[];
  groups: GroupInfo[];
  groupColors: string[];
  metanodes: MetanodeInfo[];
  /** open requests of the workflow to editors (e.g. run a notebook in its JupyterLab tab) */
  frontendRequests?: { id: string; kind: string; status: string; [key: string]: unknown }[];
  meta: Record<string, any>;
  log: LogEntry[];
  result?: any;
}

/** Group annotation: a bubble with a text around some nodes. */
export interface GroupInfo {
  id: number;
  nodes: number[];
  text: string;
  color: string;
}

export interface ColumnSpec {
  name: string;
  type: string;
  kind: string;
  dtype: string;
}

export interface TableSpec {
  columns: ColumnSpec[];
  inactive?: boolean;
  flowvariable?: boolean;
}

export interface VariableBinding {
  used?: string | null;
  exposed?: string | null;
}

export interface DialogData {
  node: NodeDescription;
  /** node-specific data for the dialog (e.g. input molecules of the Molecule Sketcher) */
  extra?: any;
  id: number;
  annotation: string;
  settings: Record<string, any>;
  variables: Record<string, VariableBinding>;
  inSpecs: (TableSpec | null)[];
  flowVariables: FlowVariableInfo[];
}

export interface PortData {
  node: number;
  nodeName: string;
  /** set when the data passes through a metanode port */
  metanode?: number;
  port: number;
  portName: string;
  type: PortType;
  executed: boolean;
  inactive?: boolean;
  spec?: TableSpec | null;
  rowCount?: number;
  offset?: number;
  rows?: any[][];
  flowVariables?: FlowVariableInfo[];
  object?: Record<string, any>;
  image?: { mime: string; data: string };
}

export interface NodeViewData {
  node: number;
  nodeName: string;
  html: string | null;
  /** the view runs scripts (e.g. a 3D viewer), still in a sandboxed frame */
  scripts?: boolean;
}
