import { NotebookActions, NotebookPanel } from '@jupyterlab/notebook';
import { Kernel } from '@jupyterlab/services';

/**
 * Running a Jupyter Notebook node in the notebook open in JupyterLab: the
 * workflow asks (a "frontend request"), an open editor claims it, gives the
 * kernel the node's inputs, runs all cells visibly, writes the outputs and
 * reports back with the executed notebook.
 */
export interface NotebookRequest {
  id: string;
  kind: 'notebook';
  path: string;
  node: number;
  /** code giving the kernel the node's inputs */
  pre: string;
  /** code writing the outputs */
  post: string;
  status: 'pending' | 'claimed' | 'cancel';
}

export interface NotebookResult {
  ok: boolean;
  error?: string;
  notebook?: unknown;
}

let finder: ((path: string) => NotebookPanel | null) | null = null;

/** Registered by the plugin: the open notebook panel of a file (relative to the Jupyter root). */
export function setNotebookFinder(f: (path: string) => NotebookPanel | null) {
  finder = f;
}

export const findNotebook = (path: string) => finder?.(path) ?? null;

async function run(kernel: Kernel.IKernelConnection, code: string) {
  const reply = await kernel.requestExecute({ code, silent: false, store_history: false }).done;
  const content = reply.content as any;
  if (content.status === 'error') {
    throw new Error(`${content.ename}: ${content.evalue}`);
  }
}

/** The first error output of the notebook's cells ("Name: message"). */
function cellError(panel: NotebookPanel): string | null {
  const cells = panel.content.model?.cells;
  for (let i = 0; i < (cells?.length ?? 0); i++) {
    const json = cells!.get(i).toJSON() as any;
    for (const o of json.outputs ?? []) {
      if (o.output_type === 'error') {
        return `cell ${i + 1}: ${o.ename}: ${o.evalue}`;
      }
    }
  }
  return null;
}

/** Run the notebook in its panel with the node's inputs; `onCancel` gets a function to interrupt. */
export async function runInNotebook(
  panel: NotebookPanel,
  req: NotebookRequest,
  onCancel: (cancel: () => void) => void
): Promise<NotebookResult> {
  await panel.sessionContext.ready;
  const kernel = panel.sessionContext.session?.kernel;
  if (!kernel) {
    return { ok: false, error: 'The notebook has no running kernel' };
  }
  let canceled = false;
  onCancel(() => {
    canceled = true;
    void kernel.interrupt();
  });
  try {
    await run(kernel, req.pre);
    const ok = await NotebookActions.runAll(panel.content, panel.sessionContext);
    if (canceled) {
      return { ok: false, error: 'canceled' };
    }
    if (!ok) {
      await run(kernel, 'import verknode as _verknode\n_verknode._live_inputs = None').catch(() => undefined);
      return { ok: false, error: cellError(panel) ?? 'a cell failed', notebook: panel.content.model?.toJSON() };
    }
    await run(kernel, req.post);
    return { ok: true, notebook: panel.content.model?.toJSON() };
  } catch (e) {
    return { ok: false, error: (e as Error).message, notebook: panel.content.model?.toJSON() };
  }
}
