import React from 'react';

/** Props of a canvas control of input widget nodes (InputWidget.control). */
export interface InputControlProps {
  value: any;
  /** InputWidget.control_props */
  props: Record<string, any>;
  /** column name of the value */
  name: string;
  /** page of an "html" control */
  html?: string;
  disabled: boolean;
  /** store a new value (null: back to the default) */
  onChange: (value: any) => void;
  /** open the structure sketcher (molecule control) */
  onSketch?: () => void;
  /** size the user gave the widget (undefined: the control's own size) */
  width?: number;
  height?: number;
}

/** Props of a canvas display of output widget nodes (OutputWidget.renderer). */
export interface OutputRendererProps {
  /** what OutputWidget.render returned */
  data: any;
  width: number;
  height: number;
  /** interactive displays (OutputWidget.interactive): the selection and how to change it (null: clear) */
  selection?: any;
  onSelect?: (selection: any) => void;
  /** send the node something while it runs and waits for it (ExecContext.wait_for_input), e.g. a chat message */
  onSend?: (value: any) => void;
  /** the workflow is executing */
  running?: boolean;
  /** the node of the display, and whether it is running itself */
  nodeId?: number;
  nodeRunning?: boolean;
}

/** A node as a display may refer to it (e.g. a chat mentioning the selected nodes). */
export interface CanvasNodeRef {
  id: number;
  name: string;
  label: string;
  /** it has a canvas widget / a dialog / a view; its number of output ports */
  widget?: boolean;
  dialog?: boolean;
  view?: boolean;
  outputs?: number;
}

/** A group annotation of the canvas. */
export interface CanvasGroupRef {
  id: number;
  text: string;
  nodes: number[];
}

/** What a reference of a document (a walkthrough's vk: link) does on the canvas. */
export type CanvasAction =
  | { kind: 'node' | 'view'; node: number }
  | { kind: 'widget'; node: number; quiet?: boolean }
  | { kind: 'group'; group: number }
  | { kind: 'settings'; node: number; field?: string }
  | { kind: 'output'; node: number; port?: number }
  | { kind: 'run'; all?: boolean; group?: number; nodes?: number[] };

/** What displays can do with the canvas they are on. */
export interface CanvasLink {
  /** the nodes selected on the canvas lately - before the display was clicked (without `except`) */
  selection: (except?: number) => CanvasNodeRef[];
  /** show these nodes: select them, bring them into view and make them flash (fit 'if-hidden': move the
   * view only if one is out of it; false: only the flash); keep: an element on the canvas that stays in view
   * too (e.g. the walkthrough that points at them) */
  highlight: (ids: number[], fit?: boolean | 'if-hidden', keep?: Element) => void;
  /** a node of the canvas by its number (#id), or null */
  node: (id: number) => CanvasNodeRef | null;
  /** a look at a node's output data (inspection widget) while hovered (null: gone); pin: kept; near:
   * the screen box of what points at it (the widget goes beside it) */
  inspect: (id: number | null, pin?: boolean, near?: DOMRect) => void;
  /** all nodes (of the canvas level shown) and the groups */
  nodes: () => CanvasNodeRef[];
  groups: () => CanvasGroupRef[];
  /** do what a reference says: show a node or group, open settings / an output / a view, show a widget, run
   * (keep: an element on the canvas that stays in view, e.g. the walkthrough the reference is on; a quiet
   * widget is shown without moving the view) */
  act: (action: CanvasAction, keep?: Element) => void;
  /** change some settings of a node (one undo step) */
  saveSettings: (id: number, settings: Record<string, any>) => Promise<void>;
  /** show a node's walkthrough as a page of its own (where views open: a JupyterLab tab, a window, ...) */
  openWalkthrough: (id: number) => void;
  /** a file next to the workflow (a relative path): its URL, and opening it in JupyterLab */
  fileUrl: (path: string) => string | null;
  openFile: (path: string) => void;
  /** the workflow is executing */
  running: () => boolean;
}

export const CanvasLinkContext = React.createContext<CanvasLink | null>(null);

/**
 * Controls and displays by name. The built-in ones are registered by
 * WidgetPanel; Python widget nodes pick one with `control` / `renderer` (or
 * use "html" for a page of their own). Frontend code can add more names.
 */
export const INPUT_CONTROLS = new Map<string, React.ComponentType<InputControlProps>>();
export const OUTPUT_RENDERERS = new Map<string, React.ComponentType<OutputRendererProps>>();

export function registerInputControl(name: string, component: React.ComponentType<InputControlProps>): void {
  INPUT_CONTROLS.set(name, component);
}

export function registerOutputRenderer(name: string, component: React.ComponentType<OutputRendererProps>): void {
  OUTPUT_RENDERERS.set(name, component);
}
