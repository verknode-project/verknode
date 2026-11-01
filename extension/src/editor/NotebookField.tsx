import { Dialog, showDialog } from '@jupyterlab/apputils';
import React, { useState } from 'react';

import { FieldInfo } from '../types';
import type { FieldContext } from './ConfigureDialog';
import { labAvailable, openFileInLab } from './labViews';
import { BrowseButton } from './PathField';

/** What the Jupyter Notebook node tells its dialog (JupyterNotebook.dialog_data). */
interface NotebookInfo {
  exists: boolean;
  rootPath?: string;
  suggested?: string;
  kernel?: string;
  parametersCell?: boolean;
  cells?: number;
  inputs?: number;
  outputs?: number;
  error?: string;
}

/**
 * Notebook of the Jupyter Notebook node: its path, whether it exists, and
 * buttons to create it from a template (inputs, outputs, imports, kernel)
 * and to open it in JupyterLab for editing and interactive runs.
 */
export function NotebookField(props: {
  field: FieldInfo;
  value: string;
  onChange: (v: string) => void;
  ctx: FieldContext;
  disabled?: boolean;
}) {
  const info: NotebookInfo = props.ctx.extra ?? { exists: false };
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const create = async () => {
    setError(null);
    if (info.exists) {
      const answer = await showDialog({
        title: 'Re-create the notebook?',
        body: `${props.value} will be replaced by a new notebook from the template; its cells are lost.`,
        buttons: [Dialog.cancelButton(), Dialog.warnButton({ label: 'Replace' })]
      });
      if (!answer.button.accept) {
        return;
      }
    }
    setBusy(true);
    try {
      const result = await props.ctx.action!('create', { overwrite: info.exists });
      props.onChange(result.path);
      props.ctx.setExtra?.(result.data);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const status = !props.value
    ? `No notebook yet - "Create notebook" makes ${info.suggested ?? 'one'} next to the workflow.`
    : info.error
      ? info.error
      : info.exists
        ? `Found: ${info.cells ?? '?'} cells, kernel ${info.kernel ?? '?'}, ${info.inputs ?? '?'} input / ${info.outputs ?? '?'} output ports${
            info.parametersCell ? '' : ' - no cell tagged "parameters": the inputs are injected at the top'
          }`
        : 'Not found - create it, or check the path (relative to the workflow folder).';

  return (
    <div className="verknode-notebook-field">
      <div className="verknode-path-field">
        <input
          className="jp-mod-styled"
          type="text"
          value={props.value ?? ''}
          placeholder={info.suggested ?? 'analysis.ipynb'}
          disabled={props.disabled}
          onChange={e => props.onChange(e.target.value)}
        />
        <BrowseButton field={props.field} value={props.value} onChange={props.onChange} ctx={props.ctx} disabled={props.disabled} />
      </div>
      <div className={`verknode-notebook-status ${info.exists ? 'verknode-ok' : ''}`}>{status}</div>
      <div className="verknode-notebook-actions">
        <button className="jp-mod-styled" disabled={busy || props.disabled} onClick={() => void create()}>
          {info.exists ? 'Re-create from template…' : 'Create notebook'}
        </button>
        <button
          className="jp-mod-styled"
          disabled={!info.exists || !info.rootPath || !labAvailable()}
          title="Open the notebook in JupyterLab to edit and run it (it reads the node's inputs from the workflow)"
          onClick={() => info.rootPath && openFileInLab(info.rootPath)}
        >
          Open in JupyterLab
        </button>
      </div>
      {error && <div className="verknode-dialog-error">{error}</div>}
      <div className="verknode-field-help">
        The node's inputs are <code>input_table_1</code>, … and <code>flow_variables</code>, injected
        after the cell tagged <code>parameters</code>; assign <code>output_table_1</code>, … The node's ports follow the
        notebook (the highest <code>input_table_N</code> / <code>output_table_N</code> it uses). Editing the notebook resets
        the node.
      </div>
    </div>
  );
}
