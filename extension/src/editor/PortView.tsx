import React, { useEffect, useState } from 'react';

import { WorkflowController } from '../controller';
import { PortData } from '../types';
import { HtmlFrame } from './widgets/HtmlFrame';
import { TypeBadge, VarBadge } from './ConfigureDialog';
import { useWorkflowVersion, ViewFrame, ViewFrameKind, ViewMove } from './ChildWindow';

const PAGE = 100;

export function Cell({ value, kind }: { value: any; kind: string }) {
  if (value === null || value === undefined) {
    return <td className="verknode-missing-cell">?</td>;
  }
  if (typeof value === 'object' && value.__molecule__) {
    return (
      <td className="verknode-mol-cell" title={value.smiles}>
        {value.svg ? <div dangerouslySetInnerHTML={{ __html: value.svg }} /> : value.smiles}
      </td>
    );
  }
  const numeric = kind === 'integer' || kind === 'long' || kind === 'double';
  const full = typeof value === 'object' ? JSON.stringify(value) : String(value);
  // doubles shown rounded (full precision in the tooltip)
  const text = kind === 'double' && typeof value === 'number' ? String(Number(value.toPrecision(10))) : full;
  return (
    <td className={numeric ? 'verknode-num' : ''} title={full.length > 40 || full !== text ? full : undefined}>
      {text}
    </td>
  );
}

/**
 * Output port view: data table (paged), column spec and flow variables.
 */
export function PortView(props: {
  controller: WorkflowController;
  nodeId: number;
  port: number;
  onClose: () => void;
  frame?: ViewFrameKind;
  onTitle?: (title: string) => void;
  moves?: ViewMove[];
}) {
  const { controller, nodeId, port } = props;
  const version = useWorkflowVersion(controller);
  const [fresh, setFresh] = useState<PortData | null>(null);
  // the last executed results stay visible (marked outdated) while the node is reset or runs
  const [last, setLast] = useState<PortData | null>(null);
  const stale = !!fresh && !fresh.executed && !!last;
  const data = stale ? last : fresh;
  const state = controller.view?.nodes.find(n => n.id === nodeId)?.state;
  const running = state === 'EXECUTING' || state === 'QUEUED';
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [structures, setStructures] = useState<string[]>([]);
  const [tab, setTab] = useState<'table' | 'spec' | 'variables' | 'object'>(port === 0 ? 'variables' : 'table');
  useEffect(() => {
    if (data && tab === 'table' && data.type !== 'table' && port !== 0) {
      setTab(data.type === 'flowvariable' ? 'variables' : 'object');
    }
  }, [data?.type]);

  useEffect(() => {
    controller
      .port(nodeId, port, offset, PAGE, structures)
      .then(d => {
        setError(null);
        setFresh(d);
        if (d.executed) {
          setLast(d);
        }
      })
      .catch(e => setError(e.message));
  }, [nodeId, port, offset, structures, version]);

  const toggleStructure = (name: string) =>
    setStructures(structures.includes(name) ? structures.filter(s => s !== name) : [...structures, name]);

  const columns = data?.spec?.columns ?? [];
  const total = data?.rowCount ?? 0;
  const isTable = data?.type === 'table';
  const isObject = !!data && !isTable && data.type !== 'flowvariable';
  const title = data ? `${port === 0 ? 'Flow Variables' : `${port}: ${data.portName}`} - ${data.nodeName} (#${data.metanode ?? nodeId})` : 'Port view';

  const tabs: [typeof tab, string][] = isTable
    ? [
        ['table', 'Table'],
        ['spec', 'Spec - Columns'],
        ['variables', 'Flow Variables']
      ]
    : isObject
      ? [
          ['object', data!.type === 'image' ? 'Image' : 'Port Object'],
          ['spec', 'Spec - Columns'],
          ['variables', 'Flow Variables']
        ]
      : [['variables', 'Flow Variables']];

  return (
    <ViewFrame
      frame={props.frame ?? 'modal'}
      title={title}
      onClose={props.onClose}
      onTitle={props.onTitle}
      moves={props.moves}
      className="verknode-portview"
    >
      {stale && (
        <div className="verknode-stale-note">
          Outdated: {running ? 'the node is running - the view updates when it has executed.' : 'the node was reset - showing its last results until it runs again.'}
        </div>
      )}
      {error && <div className="verknode-dialog-error">{error}</div>}
      {data && (
        <>
          <div className="verknode-tabs">
            {tabs.map(([k, label]) => (
              <button key={k} className={`verknode-tab ${tab === k ? 'verknode-tab-active' : ''}`} onClick={() => setTab(k)}>
                {label}
              </button>
            ))}
          </div>
          {!data.executed && (
            <div className="verknode-empty">Node is not executed.{columns.length ? ' Showing the expected column spec.' : ''}</div>
          )}
          {data.inactive && <div className="verknode-empty">This port is inactive (branch not taken).</div>}
          {tab === 'table' && data.executed && !data.inactive && (
            <>
              <div className="verknode-port-info">
                Rows: {total} | Columns: {columns.length}
                <span className="verknode-pager">
                  <button className="jp-Button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
                    ‹
                  </button>
                  {total ? `${offset + 1}–${Math.min(offset + PAGE, total)}` : '0'}
                  <button className="jp-Button" disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>
                    ›
                  </button>
                </span>
              </div>
              <div className="verknode-table-scroll">
                <table className="verknode-data-table">
                  <thead>
                    <tr>
                      <th className="verknode-rowid">RowID</th>
                      {columns.map(c => (
                        <th key={c.name} title={`${c.type} (${c.dtype})`}>
                          <TypeBadge kind={c.kind} /> {c.name}
                          {c.kind === 'string' && (
                            <button
                              className={`verknode-struct-toggle ${structures.includes(c.name) ? 'verknode-on' : ''}`}
                              onClick={() => toggleStructure(c.name)}
                              title="Render values (SMILES / MolBlock) as structures"
                            >
                              ⌬
                            </button>
                          )}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {(data.rows ?? []).map((r, i) => (
                      <tr key={i}>
                        <td className="verknode-rowid">Row{offset + i}</td>
                        {r.map((v, j) => (
                          <Cell key={j} value={v} kind={columns[j]?.kind} />
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
          {tab === 'object' && data.executed && !data.inactive && (
            <div className="verknode-table-scroll">
              {data.html && (
                <div className="verknode-port-html">
                  <HtmlFrame html={data.html} width={0} height={0} fill />
                </div>
              )}
              {data.image && (
                <div className="verknode-port-image">
                  <img src={`data:${data.image.mime};base64,${data.image.data}`} alt={data.portName} />
                </div>
              )}
              {data.object && (
                <table className="verknode-data-table verknode-kv-table">
                  <tbody>
                    {Object.entries(data.object).map(([k, v]) => (
                      <tr key={k}>
                        <th>{k}</th>
                        <td className="verknode-mono">{typeof v === 'object' ? JSON.stringify(v) : String(v)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}
          {tab === 'spec' && (
            <div className="verknode-table-scroll">
              <table className="verknode-data-table">
                <thead>
                  <tr>
                    <th>Column</th>
                    <th>Type</th>
                    <th>Storage (polars)</th>
                  </tr>
                </thead>
                <tbody>
                  {columns.map(c => (
                    <tr key={c.name}>
                      <td>{c.name}</td>
                      <td>
                        <TypeBadge kind={c.kind} /> {c.type}
                      </td>
                      <td className="verknode-mono">{c.dtype}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {tab === 'variables' && (
            <div className="verknode-table-scroll">
              <table className="verknode-data-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Value</th>
                  </tr>
                </thead>
                <tbody>
                  {(data.flowVariables ?? []).map(v => (
                    <tr key={v.name}>
                      <td>
                        <VarBadge type={v.type} /> {v.name}
                      </td>
                      <td className="verknode-mono">{String(v.value)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {!data.executed && <div className="verknode-empty">Execute the node to see its flow variables.</div>}
            </div>
          )}
        </>
      )}
    </ViewFrame>
  );
}
