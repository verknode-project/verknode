import { ReactWidget } from '@jupyterlab/apputils';
import { Widget } from '@lumino/widgets';

/**
 * Port and node views as JupyterLab tabs (dockable, splittable like any
 * notebook). The plugin registers how widgets are added to the shell.
 */
export interface LabHost {
  add(widget: Widget): void;
  activate(id: string): void;
  /** open a file (path relative to the Jupyter root) in JupyterLab */
  openFile(path: string): void;
}

let host: LabHost | null = null;

export function setLabHost(h: LabHost) {
  host = h;
}

export const labAvailable = () => host !== null;

let counter = 0;

/** A React view in a JupyterLab tab; `render` gets the widget (title, dispose). */
export class LabViewWidget extends ReactWidget {
  constructor(private readonly renderView: (w: LabViewWidget) => JSX.Element) {
    super();
    this.id = `verknode-view-${++counter}`;
    this.addClass('verknode-lab-view');
    this.title.closable = true;
    this.title.label = 'View';
  }

  render(): JSX.Element {
    return this.renderView(this);
  }
}

/** Show a widget in the JupyterLab main area (split next to the current tab). */
export function openInLab(widget: LabViewWidget): boolean {
  if (!host) {
    return false;
  }
  host.add(widget);
  host.activate(widget.id);
  return true;
}

export function activateInLab(widget: Widget) {
  host?.activate(widget.id);
}

/** Open a file (relative to the Jupyter root) in JupyterLab, e.g. a node's notebook. */
export function openFileInLab(path: string): boolean {
  if (!host) {
    return false;
  }
  host.openFile(path);
  return true;
}
