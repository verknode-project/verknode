import { DocumentRegistry } from '@jupyterlab/docregistry';
import { IDisposable } from '@lumino/disposable';
import { ISignal, Signal } from '@lumino/signaling';

import { postJSON, requestAPI } from './api';
import { findNotebook, NotebookRequest, runInNotebook } from './liveNotebook';
import { DialogData, LogEntry, NodeDescription, NodeViewData, PortData, WorkflowView } from './types';

const POLL_INTERVAL = 300;
const MAX_LOG = 1000;

/**
 * Connects a .vkf document to a workflow session in the server-side engine.
 *
 * The engine owns the workflow; the document model only tracks the dirty
 * state. When JupyterLab saves the document (Ctrl+S, autosave, save-as) the
 * engine writes the zip archive to the document path and the model is
 * reverted so it matches the file on disk.
 */
export class WorkflowController implements IDisposable {
  constructor(readonly context: DocumentRegistry.Context) {}

  get changed(): ISignal<this, WorkflowView> {
    return this._changed;
  }

  get failed(): ISignal<this, string> {
    return this._failed;
  }

  get view(): WorkflowView | null {
    return this._view;
  }

  get logs(): LogEntry[] {
    return this._logs;
  }

  get isDisposed(): boolean {
    return this._isDisposed;
  }

  async start(): Promise<void> {
    await this.context.ready;
    try {
      const view = await postJSON<WorkflowView>('sessions', { path: this.context.path });
      this._session = view.session;
      this._setView(view);
      void this._watch();
    } catch (e) {
      this._failed.emit(`Could not open workflow: ${(e as Error).message}`);
      return;
    }
    this.context.saveState.connect(this._onSaveState, this);
    this.context.pathChanged.connect(this._onPathChanged, this);
    this.context.model.contentChanged.connect(this._onContentChanged, this);
  }

  async op(op: string, args: Record<string, any> = {}): Promise<WorkflowView> {
    const view = await postJSON<WorkflowView>(this._url('ops'), { op, ...args, log_since: this._logSeq });
    this._setView(view);
    return view;
  }

  async refresh(): Promise<void> {
    if (!this._session) {
      return;
    }
    const view = await requestAPI<WorkflowView>(`${this._url()}?log_since=${this._logSeq}`);
    this._setView(view);
  }

  dialog(nodeId: number): Promise<DialogData> {
    return requestAPI<DialogData>(this._url(`nodes/${nodeId}/dialog`));
  }

  /** A node's dialog action (e.g. create its notebook) with the unsaved dialog settings. */
  nodeAction(nodeId: number, name: string, settings: Record<string, any>, args: Record<string, any> = {}): Promise<any> {
    return requestAPI<any>(this._url(`nodes/${nodeId}/action`), { method: 'POST', body: JSON.stringify({ name, settings, args }) });
  }

  /** Node-specific dialog data for unsaved dialog settings. */
  dialogData(nodeId: number, settings: Record<string, any>): Promise<any> {
    return requestAPI<any>(this._url(`nodes/${nodeId}/dialog_data`), { method: 'POST', body: JSON.stringify({ settings }) });
  }

  port(nodeId: number, port: number, offset = 0, limit = 100, structures: string[] = []): Promise<PortData> {
    const render = encodeURIComponent(structures.join('\t'));
    return requestAPI<PortData>(
      this._url(`nodes/${nodeId}/ports/${port}?offset=${offset}&limit=${limit}&structures=${render}`)
    );
  }

  /** Node repository of this workflow: built-in plus its custom nodes. */
  nodeTypes(): Promise<NodeDescription[]> {
    return requestAPI<NodeDescription[]>(this._url('nodes'));
  }

  /** Python script that rebuilds the workflow, and the line to attach to it. */
  python(): Promise<{ script: string; hint: string }> {
    return requestAPI<{ script: string; hint: string }>(this._url('python'));
  }

  nodeView(nodeId: number): Promise<NodeViewData> {
    return requestAPI<NodeViewData>(this._url(`nodes/${nodeId}/view`));
  }

  async save(): Promise<void> {
    if (!this._session || this._saving) {
      return;
    }
    this._saving = true;
    try {
      const view = await postJSON<WorkflowView>(this._url('save'), { path: this.context.path });
      await this.context.revert();
      this.context.model.dirty = false;
      this._setView(view);
    } finally {
      this._saving = false;
    }
  }

  clearLog(): void {
    this._logs = [];
    if (this._view) {
      this._changed.emit(this._view);
    }
  }

  dispose(): void {
    if (this._isDisposed) {
      return;
    }
    this._isDisposed = true;
    window.clearTimeout(this._pollTimer);
    if (this._session) {
      void requestAPI(this._url(), { method: 'DELETE' }).catch(() => undefined);
    }
    Signal.clearData(this);
  }

  private _url(suffix = ''): string {
    return `sessions/${this._session}${suffix ? '/' + suffix : ''}`;
  }

  /**
   * Long-poll for changes made elsewhere (other editors, Python clients via
   * `verknode.open(path)`, execution progress) so the editor always shows the
   * live workflow.
   */
  private async _watch(): Promise<void> {
    while (!this._isDisposed && this._session) {
      try {
        const version = this._view?.version ?? -1;
        const view = await requestAPI<WorkflowView>(
          this._url(`wait?editor=1&version=${version}&timeout=20&log_since=${this._logSeq}`)
        );
        if (!this._isDisposed && view.version !== this._view?.version) {
          this._setView(view);
        }
      } catch (e) {
        if (String((e as Error).message).includes('Unknown workflow session')) {
          // server restarted: reattach to the workflow by path
          try {
            const view = await postJSON<WorkflowView>('sessions', { path: this.context.path });
            this._session = view.session;
            this._view = null;
            this._logSeq = 0;
            this._setView(view);
            this._failed.emit('The server was restarted; the workflow was reloaded from its file.');
            continue;
          } catch {
            // fall through to retry
          }
        }
        await new Promise(resolve => window.setTimeout(resolve, 3000));
      }
    }
  }

  /**
   * Requests of the workflow to an open editor: run a Jupyter Notebook node in
   * its notebook when that is open in JupyterLab (claim it, run, report back).
   */
  private _serveRequests(view: WorkflowView): void {
    for (const r of (view.frontendRequests ?? []) as unknown as NotebookRequest[]) {
      if (r.status === 'cancel') {
        this._running.get(r.id)?.();
      }
      if (r.kind !== 'notebook' || r.status !== 'pending' || this._seen.has(r.id)) {
        continue;
      }
      this._seen.add(r.id);
      const panel = findNotebook(r.path);
      if (!panel) {
        continue; // not open here: another editor may have it, else the node runs headless
      }
      void (async () => {
        const reply = (action: string, data?: unknown) =>
          postJSON<{ ok: boolean }>(this._url(`frontend/${r.id}`), { action, data });
        if (!(await reply('claim')).ok) {
          return;
        }
        const result = await runInNotebook(panel, r, cancel => this._running.set(r.id, cancel));
        this._running.delete(r.id);
        await reply('done', result);
      })();
    }
  }
  private _seen = new Set<string>();
  private _running = new Map<string, () => void>();

  private _setView(view: WorkflowView): void {
    if (this._isDisposed) {
      return;
    }
    if (this._view && view.version < this._view.version) {
      return; // stale response (e.g. a long poll answered before a newer edit)
    }
    const fresh = view.log.filter(e => e.seq > this._logSeq);
    if (fresh.length) {
      this._logSeq = fresh[fresh.length - 1].seq;
      this._logs = [...this._logs, ...fresh].slice(-MAX_LOG);
    }
    this._view = view;
    this._serveRequests(view);
    if (view.dirty && !this.context.model.dirty) {
      this.context.model.dirty = true;
    } else if (!view.dirty && this.context.model.dirty && !this._saving) {
      void this._syncSavedElsewhere();
    }
    if (view.executing && this._pollTimer === undefined) {
      this._pollTimer = window.setTimeout(() => {
        this._pollTimer = undefined;
        void this.refresh().catch(e => this._failed.emit((e as Error).message));
      }, POLL_INTERVAL);
    }
    this._changed.emit(view);
  }

  /** Saved by a Python client: re-sync the document with the file on disk. */
  private async _syncSavedElsewhere(): Promise<void> {
    this._saving = true;
    try {
      await this.context.revert();
      this.context.model.dirty = false;
    } catch {
      // ignore: the next save resolves it
    } finally {
      this._saving = false;
    }
  }

  private _onSaveState(_: unknown, state: DocumentRegistry.SaveState): void {
    if (state === 'completed' && !this._saving) {
      void this.save().catch(e => this._failed.emit(`Save failed: ${(e as Error).message}`));
    }
  }

  private _onPathChanged(): void {
    void this.op('set_path', { path: this.context.path }).catch(() => undefined);
  }

  private _onContentChanged(): void {
    // "Reload from disk" / revert: reload the engine from the file
    if (this._saving || !this._session) {
      return;
    }
    void postJSON<WorkflowView>(this._url('reload'), { path: this.context.path })
      .then(view => {
        this._logSeq = 0;
        this.context.model.dirty = false;
        this._setView(view);
      })
      .catch(e => this._failed.emit(`Reload failed: ${(e as Error).message}`));
  }

  private _session: string | null = null;
  private _view: WorkflowView | null = null;
  private _logs: LogEntry[] = [];
  private _logSeq = 0;
  private _saving = false;
  private _pollTimer: number | undefined = undefined;
  private _isDisposed = false;
  private _changed = new Signal<this, WorkflowView>(this);
  private _failed = new Signal<this, string>(this);
}
