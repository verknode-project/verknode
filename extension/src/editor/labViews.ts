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
  /** the files and folders of a folder (path relative to the Jupyter root) */
  listFolder(path: string): Promise<FolderEntry[]>;
}

export interface FolderEntry {
  name: string;
  isFolder: boolean;
  size?: number | null;
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

/** The files and folders of a folder (relative to the Jupyter root): folders first, hidden ones left out. */
export async function listFolder(path: string): Promise<FolderEntry[]> {
  if (!host) {
    return [];
  }
  const found = await host.listFolder(path);
  return found
    .filter(e => !e.name.startsWith('.'))
    .sort((a, b) => Number(b.isFolder) - Number(a.isFolder) || a.name.localeCompare(b.name));
}
