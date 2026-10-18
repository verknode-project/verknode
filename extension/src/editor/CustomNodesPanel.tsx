import React, { useState } from 'react';

import { WorkflowController } from '../controller';
import { copyText } from './clipboard';

/**
 * Custom node folders of the workflow (saved in the .vkf): edit the folder
 * list, reload the Python node files, create an example node file, and see
 * load errors. Shown in the Settings window.
 */
export function CustomNodesPanel(props: { controller: WorkflowController }) {
  const { controller } = props;
  const view = controller.view;
  const [paths, setPaths] = useState<string[]>(view?.nodePaths.length ? [...view.nodePaths] : ['vkn_nodes']);
  const [autoReload, setAutoReload] = useState(view?.autoReload ?? true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const report = controller.view?.customReport;

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const apply = () => run(() => controller.op('set_node_paths', { paths, autoReload }));
  const reload = () => run(() => controller.op('reload_nodes'));
  const example = () =>
    run(async () => {
      const v = await controller.op('create_node_template', { folder: paths[0] || 'vkn_nodes' });
      setPaths(v.nodePaths);
    });

  return (
    <div className="verknode-custom-panel">
      <p className="verknode-field-help">
        Python files in these folders define custom nodes: every <code>NodeModel</code> subclass becomes a node
        (see <code>help(verknode.node)</code> or "Create example node"). Folders are relative to the workflow file
        and are saved with it.
      </p>
      <div className="verknode-records">
        <table>
          <thead>
            <tr>
              <th>Node folder</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {paths.map((p, i) => (
              <tr key={i}>
                <td>
                  <input
                    className="jp-mod-styled"
                    type="text"
                    value={p}
                    onChange={e => setPaths(paths.map((q, j) => (j === i ? e.target.value : q)))}
                  />
                </td>
                <td className="verknode-records-actions">
                  <button className="verknode-icon-btn" onClick={() => setPaths(paths.filter((_, j) => j !== i))}>
                    ✕
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <button className="jp-Button jp-mod-styled verknode-add-btn" onClick={() => setPaths([...paths, ''])}>
          + Add folder
        </button>
      </div>
      <label className="verknode-custom-auto">
        <input type="checkbox" checked={autoReload} onChange={e => setAutoReload(e.target.checked)} /> Reload
        changed node files automatically before execution
      </label>
      <div className="verknode-custom-actions">
        {error && <div className="verknode-dialog-error">{error}</div>}
        <button className="jp-Button jp-mod-styled" disabled={busy} onClick={example}>
          Create example node
        </button>
        <button className="jp-Button jp-mod-styled" disabled={busy} onClick={reload}>
          Reload nodes
        </button>
        <button className="jp-Button jp-mod-styled jp-mod-accept" disabled={busy} onClick={apply}>
          Apply
        </button>
      </div>
      {report && (
        <div className="verknode-custom-report">
          <h4>
            Last load ({report.reason}, {report.time})
          </h4>
          {report.loaded.length ? (
            <table className="verknode-data-table">
              <thead>
                <tr>
                  <th>Node</th>
                  <th>Category</th>
                  <th>Factory</th>
                  <th>File</th>
                </tr>
              </thead>
              <tbody>
                {report.loaded.map(n => (
                  <tr key={n.factory}>
                    <td>{n.name}</td>
                    <td>{n.category}</td>
                    <td className="verknode-mono">{n.factory}</td>
                    <td className="verknode-mono" title={n.file}>
                      {n.file.split('/').pop()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="verknode-empty">No custom nodes loaded.</div>
          )}
          {report.errors.map(e => (
            <div key={e.file} className="verknode-custom-error">
              <div>
                <b>{e.file}</b>{' '}
                <button className="verknode-icon-btn" onClick={() => void copyText(e.error)} title="Copy traceback">
                  copy
                </button>
              </div>
              <pre>{e.error}</pre>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
