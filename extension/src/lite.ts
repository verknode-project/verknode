/**
 * verknode in the browser (JupyterLite): no Jupyter server, so the editor's REST calls go to the workflow
 * engine in a Pyodide kernel of its own (jupyterlab_verknode/lite.py), started on the first call.
 *
 * Each call runs `await __vkn.acall(method, endpoint, body)` in that kernel; the answer comes back as an
 * application/json display. The kernel runs one call at a time, and a run executes before its call returns.
 * The long poll of the server (sessions/<id>/wait) becomes polling here.
 */
import { PageConfig } from '@jupyterlab/coreutils';
import { Notification } from '@jupyterlab/apputils';
import { Kernel, ServiceManager } from '@jupyterlab/services';

let services: ServiceManager.IManager | null = null;
let kernel: Promise<Kernel.IKernelConnection> | null = null;

/** The app's services (index.ts), for the kernel. */
export function setLiteServices(manager: ServiceManager.IManager): void {
  services = manager;
}

/** Running in JupyterLite (no Jupyter server to ask)? */
export function isLite(): boolean {
  const name = (PageConfig.getOption('appName') || '').toLowerCase();
  return name.includes('jupyterlite') || !!PageConfig.getOption('litePluginSettings');
}

/** Python that makes the engine: the packages (numpy, polars from Pyodide; verknode's wheel without its
 * server dependencies), then the bridge on the drive (the browser's files). */
const BOOT = `
import piplite
await piplite.install(["numpy", "polars", "pyarrow", "pyodide-http"], keep_going=True)
await piplite.install(["jupyterlab_verknode"], deps=False)
from jupyterlab_verknode import lite as __vkn
__vkn.bridge("/drive")
print("verknode ready")
`;

function run(k: Kernel.IKernelConnection, code: string): Promise<any> {
  return new Promise((resolve, reject) => {
    const future = k.requestExecute({ code, silent: false, store_history: false, allow_stdin: false });
    let result: any = undefined;
    let error: string | null = null;
    future.onIOPub = msg => {
      const type = msg.header.msg_type;
      const content: any = msg.content;
      if ((type === 'display_data' || type === 'execute_result') && content.data?.['application/json'] !== undefined) {
        result = content.data['application/json'];
      } else if (type === 'error') {
        error = `${content.ename}: ${content.evalue}`;
      }
    };
    future.done.then(
      () => (error ? reject(new Error(error)) : resolve(result)),
      e => reject(e)
    );
  });
}

function engine(): Promise<Kernel.IKernelConnection> {
  if (kernel) {
    return kernel;
  }
  kernel = (async () => {
    if (!services) {
      throw new Error('verknode: no services to start the engine kernel with');
    }
    await services.ready;
    const k = await services.kernels.startNew({ name: 'python' });
    await run(k, BOOT);
    return k;
  })();
  void Notification.promise(kernel.then(() => null), {
    pending: { message: 'Starting the verknode engine in the browser (Python packages load the first time)…', options: { autoClose: false } },
    success: { message: () => 'The verknode engine runs in the browser', options: { autoClose: 3000 } },
    error: { message: (e: any) => `The verknode engine did not start: ${e?.message ?? e}`, options: { autoClose: false } }
  });
  kernel.catch(() => {
    kernel = null; // (the next call tries again)
  });
  return kernel;
}

const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));
const py = (s: string) => JSON.stringify(s); // (a JSON string is a Python string literal)

/** One REST call, answered by the engine in the kernel: {status, data}. */
async function callEngine(method: string, endPoint: string, body: string): Promise<{ status: number; data: any }> {
  const k = await engine();
  const out = await run(k, `await __vkn.acall(${py(method)}, ${py(endPoint)}, ${py(body)})`);
  if (!out) {
    throw new Error(`verknode: no answer to ${method} ${endPoint}`);
  }
  return out;
}

/** (for checks from the browser's console: window.verknodeLite.run(python)) */
if (isLite()) {
  (window as any).verknodeLite = {
    run: async (code: string) => run(await engine(), code),
    request: (endPoint: string, init?: RequestInit) => liteRequest(endPoint, init)
  };
}

/** requestAPI in the browser: the same endpoints, answered by the kernel's engine. */
export async function liteRequest<T>(endPoint: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method || 'GET').toUpperCase();
  const body = typeof init.body === 'string' ? init.body : '';
  const wait = endPoint.match(/^sessions\/([0-9a-f]+)\/wait\?(.*)$/);
  if (wait) {
    // the long poll: ask until the version changes or the time is up
    const q = new URLSearchParams(wait[2]);
    const version = Number(q.get('version') ?? '-1');
    const until = Date.now() + Math.min(Number(q.get('timeout') ?? '20'), 60) * 1000;
    const view = `sessions/${wait[1]}?log_since=${q.get('log_since') ?? '0'}`;
    for (;;) {
      const r = await callEngine('GET', view, '');
      if (r.status >= 400 || r.data?.version !== version || Date.now() > until) {
        return unpack<T>(r);
      }
      await sleep(400);
    }
  }
  return unpack<T>(await callEngine(method, endPoint, body));
}

function unpack<T>(r: { status: number; data: any }): T {
  if (r.status >= 400) {
    throw new Error(r.data?.message || `verknode: error ${r.status}`);
  }
  return (r.status === 204 ? undefined : r.data) as T;
}
