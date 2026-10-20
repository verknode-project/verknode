import { Dialog, showDialog } from '@jupyterlab/apputils';
import React, { useEffect, useRef, useState } from 'react';

import { WorkflowController } from '../controller';
import { ColumnSpec, DialogData, FieldInfo, FlowVariableInfo, TableSpec, VariableBinding } from '../types';
import { Modal } from './Modal';
import { EditorSettings } from '../settings';
import { MoleculeList } from './sketcher/MoleculeList';
import { NotebookField } from './NotebookField';
import { Description } from './Repository';

const TYPE_BADGE: Record<string, string> = {
  string: 'S',
  integer: 'I',
  long: 'L',
  double: 'D',
  boolean: 'B',
  date: 'Dt',
  datetime: 'DT',
  time: 'T',
  duration: 'Δ',
  list: '[ ]',
  struct: '{ }',
  binary: '01',
  molecule: 'M',
  null: '?',
  other: '?'
};
const VAR_BADGE: Record<string, string> = { STRING: 'S', INTEGER: 'I', DOUBLE: 'D', BOOLEAN: 'B' };

export function TypeBadge({ kind }: { kind: string }) {
  return <span className={`verknode-type-badge verknode-kind-${kind}`}>{TYPE_BADGE[kind] ?? '?'}</span>;
}

export function VarBadge({ type }: { type: string }) {
  return <span className="verknode-var-badge">{VAR_BADGE[type] ?? '?'}</span>;
}

export interface FieldContext {
  inSpecs: (TableSpec | null)[];
  flowVariables: FlowVariableInfo[];
  editorSettings?: EditorSettings;
  /** node-specific dialog data from the server */
  extra?: any;
  /** the dialog's (unsaved) settings and a way to change another one */
  settings?: Record<string, any>;
  setSetting?: (key: string, value: any) => void;
  setExtra?: (extra: any) => void;
  /** run a node dialog action on the server (e.g. create the notebook) */
  action?: (name: string, args?: Record<string, any>) => Promise<any>;
}

function columnsOf(ctx: FieldContext, port = 1, kinds?: string[]): ColumnSpec[] {
  const cols = ctx.inSpecs[port - 1]?.columns ?? [];
  return kinds ? cols.filter(c => kinds.includes(c.kind)) : cols;
}

function visible(f: FieldInfo, settings: Record<string, any>): boolean {
  if (f.type === 'widget') {
    return false; // edited on the canvas (e.g. an input widget's value)
  }
  if (!f.show_if) {
    return true;
  }
  return Object.entries(f.show_if).every(([k, v]) => (Array.isArray(v) ? v.includes(settings[k]) : settings[k] === v));
}

/* ------------------------------------------------------------------ */
/* simple editors                                                      */
/* ------------------------------------------------------------------ */
function ScalarEditor(props: {
  field: FieldInfo;
  value: any;
  onChange: (v: any) => void;
  ctx: FieldContext;
  disabled?: boolean;
}) {
  const { field: f, value, onChange, ctx, disabled } = props;
  switch (f.type) {
    case 'bool':
      return (
        <input type="checkbox" checked={!!value} disabled={disabled} onChange={e => onChange(e.target.checked)} />
      );
    case 'int':
    case 'float':
      return (
        <input
          className="jp-mod-styled"
          type="number"
          step={f.type === 'int' ? 1 : 'any'}
          value={value ?? ''}
          disabled={disabled}
          onChange={e => {
            const raw = e.target.value;
            onChange(raw === '' ? 0 : f.type === 'int' ? parseInt(raw, 10) : parseFloat(raw));
          }}
        />
      );
    case 'enum': {
      // (extra.options: choices the server works out, e.g. the saved LLM servers; options_from: the names
      // of a records setting, as edited)
      let options: string[] = ctx.extra?.options?.[f.key] ?? f.options ?? [];
      if (f.options_from) {
        const rows: any[] = ctx.settings?.[f.options_from.field] ?? [];
        const names = rows.map(r => String(r?.[f.options_from!.key] ?? '').trim()).filter(n => n && !options.includes(n));
        options = [...options, ...new Set(names)];
      }
      if (value && !options.includes(value)) {
        options = [...options, value];
      }
      return (
        <select className="jp-mod-styled" value={value ?? ''} disabled={disabled} onChange={e => onChange(e.target.value)}>
          {options.map(o => (
            <option key={o} value={o}>
              {o === '' ? '(default)' : o}
            </option>
          ))}
        </select>
      );
    }
    case 'variable': {
      const missing = value && !ctx.flowVariables.some(v => v.name === value);
      return (
        <select className="jp-mod-styled" value={value ?? ''} disabled={disabled} onChange={e => onChange(e.target.value)}>
          <option value="">— select variable —</option>
          {missing && <option value={value}>{value} (not available)</option>}
          {ctx.flowVariables.map(v => (
            <option key={v.name} value={v.name}>
              [{VAR_BADGE[v.type]}] {v.name}
            </option>
          ))}
        </select>
      );
    }
    case 'column': {
      const cols = columnsOf(ctx, f.port, f.kinds);
      const missing = value && !cols.some(c => c.name === value);
      return (
        <select
          className="jp-mod-styled"
          value={value ?? ''}
          disabled={disabled}
          onChange={e => onChange(e.target.value || null)}
        >
          <option value="">{cols.length ? '— select column —' : '— no input columns —'}</option>
          {missing && <option value={value}>{value} (not in input)</option>}
          {cols.map(c => (
            <option key={c.name} value={c.name}>
              [{TYPE_BADGE[c.kind] ?? '?'}] {c.name}
            </option>
          ))}
        </select>
      );
    }
    default: {
      // values to pick from that the node's dialog data offers (e.g. the models of an LLM server)
      const suggestions: string[] = ctx.extra?.suggest?.[f.key] ?? [];
      const list = suggestions.length ? `verknode-suggest-${f.key}` : undefined;
      return (
        <>
          <input
            className="jp-mod-styled"
            type="text"
            value={value ?? ''}
            disabled={disabled}
            list={list}
            placeholder={f.path ? 'path/to/file (relative to the workflow)' : (ctx.extra?.placeholder?.[f.key] ?? '')}
            onChange={e => onChange(e.target.value)}
          />
          {list && (
            <datalist id={list}>
              {suggestions.map(v => (
                <option key={v} value={v} />
              ))}
            </datalist>
          )}
        </>
      );
    }
  }
}

/* ------------------------------------------------------------------ */
/* twin list (column / variable include-exclude)                       */
/* ------------------------------------------------------------------ */
interface TwinValue {
  include: string[];
  exclude: string[];
  enforce: 'inclusion' | 'exclusion';
}

const TWIN_DEFAULT: TwinValue = { include: [], exclude: [], enforce: 'exclusion' };

const TWIN_DRAG = 'application/x-verknode-twin';

/**
 * One side of a twin list: every item visible at once (no native select,
 * which some browsers collapse into a picker popup). Click selects, Ctrl/Cmd
 * toggles, Shift extends; double-click or Enter moves the selection to the
 * other side, and items can be dragged across.
 */
function TwinListBox(props: {
  side: 'exclude' | 'include';
  items: string[];
  selected: string[];
  onSelect: (s: string[]) => void;
  label: (n: string) => string;
  tag: (n: string) => string | undefined;
  missing: (n: string) => boolean;
  onMove: (names: string[]) => void;
  onDrop: (names: string[]) => void;
  disabled?: boolean;
}) {
  const { items, selected, onSelect, disabled } = props;
  const anchor = useRef<string | null>(null);
  const [over, setOver] = useState(false);
  const sel = new Set(selected);

  const click = (e: React.MouseEvent, n: string) => {
    if (disabled) {
      return;
    }
    if (e.shiftKey && anchor.current && items.includes(anchor.current)) {
      const a = items.indexOf(anchor.current);
      const b = items.indexOf(n);
      const range = items.slice(Math.min(a, b), Math.max(a, b) + 1);
      onSelect(e.ctrlKey || e.metaKey ? [...new Set([...selected, ...range])] : range);
      return;
    }
    anchor.current = n;
    if (e.ctrlKey || e.metaKey) {
      onSelect(sel.has(n) ? selected.filter(x => x !== n) : [...selected, n]);
    } else {
      onSelect([n]);
    }
  };
  const key = (e: React.KeyboardEvent) => {
    if (disabled || !items.length) {
      return;
    }
    if (e.key === 'Enter' && selected.length) {
      e.preventDefault();
      props.onMove(selected);
    } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') {
      e.preventDefault();
      onSelect([...items]);
    } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const cur = anchor.current && items.includes(anchor.current) ? items.indexOf(anchor.current) : -1;
      const next = items[Math.max(0, Math.min(items.length - 1, cur + (e.key === 'ArrowDown' ? 1 : -1)))];
      anchor.current = next;
      onSelect(e.shiftKey ? [...new Set([...selected, next])] : [next]);
    }
  };
  return (
    <div
      role="listbox"
      aria-multiselectable
      tabIndex={disabled ? -1 : 0}
      className={`verknode-twin-list ${over ? 'verknode-twin-over' : ''} ${disabled ? 'verknode-disabled' : ''}`}
      onKeyDown={key}
      onDragOver={e => {
        if (!disabled && e.dataTransfer.types.includes(TWIN_DRAG)) {
          e.preventDefault();
          setOver(true);
        }
      }}
      onDragLeave={() => setOver(false)}
      onDrop={e => {
        setOver(false);
        const data = e.dataTransfer.getData(TWIN_DRAG);
        if (!data) {
          return;
        }
        e.preventDefault();
        const { from, names } = JSON.parse(data) as { from: string; names: string[] };
        if (from !== props.side) {
          props.onDrop(names);
        }
      }}
    >
      {items.map(n => (
        <div
          key={n}
          role="option"
          aria-selected={sel.has(n)}
          draggable={!disabled}
          className={`verknode-twin-item ${sel.has(n) ? 'verknode-selected' : ''} ${props.missing(n) ? 'verknode-missing' : ''}`}
          onMouseDown={e => e.shiftKey && e.preventDefault()}
          onClick={e => click(e, n)}
          onDoubleClick={() => !disabled && props.onMove([n])}
          onDragStart={e => {
            const names = sel.has(n) ? selected : [n];
            if (!sel.has(n)) {
              onSelect([n]);
            }
            e.dataTransfer.setData(TWIN_DRAG, JSON.stringify({ from: props.side, names }));
            e.dataTransfer.effectAllowed = 'move';
          }}
          title={props.label(n)}
        >
          {props.tag(n) ? <span className="verknode-twin-tag">{props.tag(n)}</span> : null}
          <span className="verknode-twin-name">{props.label(n)}</span>
        </div>
      ))}
      {!items.length ? <div className="verknode-twin-empty">{props.side === 'include' ? 'Nothing included' : 'Nothing excluded'}</div> : null}
    </div>
  );
}

function TwinList(props: {
  items: { name: string; tag: string }[];
  value: TwinValue;
  onChange: (v: TwinValue) => void;
  disabled?: boolean;
}) {
  const value: TwinValue = { ...TWIN_DEFAULT, ...props.value };
  const names = props.items.map(i => i.name);
  const isIncluded = (n: string) =>
    value.include.includes(n) || (!value.exclude.includes(n) && value.enforce === 'exclusion');
  const known = new Set(names);
  const unknownIncluded = value.include.filter(n => !known.has(n));
  const included = [...names.filter(isIncluded), ...unknownIncluded];
  const excluded = names.filter(n => !isIncluded(n));
  const [selLeft, setSelLeft] = useState<string[]>([]);
  const [selRight, setSelRight] = useState<string[]>([]);
  const tag = (n: string) => props.items.find(i => i.name === n)?.tag;

  const set = (inc: string[], exc: string[]) => props.onChange({ ...value, include: inc, exclude: exc });
  const moveIn = (list: string[]) =>
    set([...included, ...list.filter(n => !included.includes(n))], excluded.filter(n => !list.includes(n)));
  const moveOut = (list: string[]) =>
    set(included.filter(n => !list.includes(n)), [...excluded, ...list.filter(n => !excluded.includes(n))]);

  const label = (n: string) => (!known.has(n) ? `${n} (missing)` : n);
  const list = (side: 'exclude' | 'include', items: string[], sel: string[], setSel: (s: string[]) => void) => (
    <TwinListBox
      side={side}
      items={items}
      selected={sel}
      onSelect={setSel}
      label={label}
      tag={n => (known.has(n) ? tag(n) : undefined)}
      missing={n => !known.has(n)}
      disabled={props.disabled}
      onMove={names => {
        if (side === 'exclude') {
          moveIn(names);
          setSelLeft([]);
        } else {
          moveOut(names);
          setSelRight([]);
        }
      }}
      onDrop={names => {
        // dropped here from the other list
        if (side === 'include') {
          moveIn(names);
          setSelLeft([]);
        } else {
          moveOut(names);
          setSelRight([]);
        }
      }}
    />
  );
  return (
    <div className="verknode-twin">
      <div className="verknode-twin-cols">
        <div>
          <div className="verknode-twin-head">Exclude ({excluded.length})</div>
          {list('exclude', excluded, selLeft, setSelLeft)}
        </div>
        <div className="verknode-twin-buttons">
          <button className="jp-Button" disabled={props.disabled} onClick={() => (moveIn(selLeft), setSelLeft([]))} title="Add">
            ›
          </button>
          <button className="jp-Button" disabled={props.disabled} onClick={() => moveIn(excluded)} title="Add all">
            »
          </button>
          <button className="jp-Button" disabled={props.disabled} onClick={() => (moveOut(selRight), setSelRight([]))} title="Remove">
            ‹
          </button>
          <button className="jp-Button" disabled={props.disabled} onClick={() => moveOut(included)} title="Remove all">
            «
          </button>
        </div>
        <div>
          <div className="verknode-twin-head">Include ({included.length})</div>
          {list('include', included, selRight, setSelRight)}
        </div>
      </div>
      <div className="verknode-twin-enforce">
        <label>
          <input
            type="radio"
            checked={value.enforce === 'exclusion'}
            disabled={props.disabled}
            onChange={() => props.onChange({ ...value, include: [], exclude: excluded, enforce: 'exclusion' })}
          />{' '}
          Enforce exclusion
        </label>
        <label>
          <input
            type="radio"
            checked={value.enforce === 'inclusion'}
            disabled={props.disabled}
            onChange={() => props.onChange({ ...value, include: included, exclude: [], enforce: 'inclusion' })}
          />{' '}
          Enforce inclusion
        </label>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* records (list of rows with sub-fields)                              */
/* ------------------------------------------------------------------ */
function RecordsEditor(props: { field: FieldInfo; value: any[]; onChange: (v: any[]) => void; ctx: FieldContext }) {
  const { field, ctx } = props;
  const rows = props.value ?? [];
  const sub = field.fields ?? [];
  const blank = () => Object.fromEntries(sub.map(s => [s.key, s.default ?? (s.type === 'column' ? null : '')]));
  const update = (i: number, key: string, v: any) =>
    props.onChange(rows.map((r, j) => (j === i ? { ...r, [key]: v } : r)));
  const move = (i: number, d: number) => {
    const next = [...rows];
    const [r] = next.splice(i, 1);
    next.splice(i + d, 0, r);
    props.onChange(next);
  };
  return (
    <div className="verknode-records">
      <table>
        <thead>
          <tr>
            {sub.map(s => (
              <th key={s.key}>{s.label}</th>
            ))}
            <th />
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              {sub.map(s => (
                <td key={s.key}>
                  <ScalarEditor field={s} value={r[s.key]} ctx={ctx} onChange={v => update(i, s.key, v)} />
                </td>
              ))}
              <td className="verknode-records-actions">
                <button className="verknode-icon-btn" disabled={i === 0} onClick={() => move(i, -1)} title="Move up">
                  ▲
                </button>
                <button className="verknode-icon-btn" disabled={i === rows.length - 1} onClick={() => move(i, 1)} title="Move down">
                  ▼
                </button>
                <button className="verknode-icon-btn" onClick={() => props.onChange(rows.filter((_, j) => j !== i))} title="Remove">
                  ✕
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <button className="jp-Button jp-mod-styled verknode-add-btn" onClick={() => props.onChange([...rows, blank()])}>
        + Add
      </button>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* code editor with column / flow variable insertion                   */
/* ------------------------------------------------------------------ */
function CodeEditor(props: {
  field: FieldInfo;
  value: string;
  onChange: (v: string) => void;
  ctx: FieldContext;
  disabled?: boolean;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const insert = (text: string) => {
    const ta = ref.current;
    const v = props.value ?? '';
    const start = ta?.selectionStart ?? v.length;
    const end = ta?.selectionEnd ?? v.length;
    props.onChange(v.slice(0, start) + text + v.slice(end));
    window.requestAnimationFrame(() => {
      ta?.focus();
      ta?.setSelectionRange(start + text.length, start + text.length);
    });
  };
  const textarea = (
    <textarea
      ref={ref}
      className="verknode-code"
      spellCheck={false}
      value={props.value ?? ''}
      disabled={props.disabled}
      onChange={e => props.onChange(e.target.value)}
      onKeyDown={e => {
        if (e.key === 'Tab') {
          e.preventDefault();
          insert('    ');
        }
        e.stopPropagation();
      }}
    />
  );
  if (!props.field.insertables) {
    return textarea;
  }
  return (
    <div className="verknode-code-wrap">
      <div className="verknode-insert-panel">
        <div className="verknode-twin-head">Column List</div>
        <ul>
          {columnsOf(props.ctx).map(c => (
            <li key={c.name} onDoubleClick={() => insert(`$${c.name}$`)} title="Double-click to insert">
              <TypeBadge kind={c.kind} /> {c.name}
            </li>
          ))}
        </ul>
        <div className="verknode-twin-head">Flow Variable List</div>
        <ul>
          {props.ctx.flowVariables.map(v => (
            <li key={v.name} onDoubleClick={() => insert(`$\${${VAR_BADGE[v.type]}${v.name}}$$`)} title="Double-click to insert">
              <VarBadge type={v.type} /> {v.name}
            </li>
          ))}
        </ul>
      </div>
      {textarea}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Table Creator                                                       */
/* ------------------------------------------------------------------ */
interface CreatorTable {
  columns: { name: string; type: string }[];
  rows: string[][];
}

function TableCreatorEditor(props: { field: FieldInfo; value: CreatorTable; onChange: (v: CreatorTable) => void }) {
  const t: CreatorTable = props.value ?? { columns: [], rows: [] };
  const types = props.field.options ?? ['String'];
  const set = (next: Partial<CreatorTable>) => props.onChange({ ...t, ...next });
  const setCol = (j: number, patch: Partial<{ name: string; type: string }>) =>
    set({ columns: t.columns.map((c, k) => (k === j ? { ...c, ...patch } : c)) });
  const setCell = (i: number, j: number, v: string) =>
    set({
      rows: t.rows.map((r, k) => {
        if (k !== i) {
          return r;
        }
        const row = [...r];
        while (row.length < t.columns.length) {
          row.push('');
        }
        row[j] = v;
        return row;
      })
    });
  const addColumn = () => {
    let n = t.columns.length + 1;
    while (t.columns.some(c => c.name === `column${n}`)) {
      n++;
    }
    set({ columns: [...t.columns, { name: `column${n}`, type: 'String' }], rows: t.rows.map(r => [...r, '']) });
  };
  const removeColumn = (j: number) =>
    set({ columns: t.columns.filter((_, k) => k !== j), rows: t.rows.map(r => r.filter((_, k) => k !== j)) });

  return (
    <div className="verknode-creator">
      <div className="verknode-creator-scroll">
        <table>
          <thead>
            <tr>
              <th className="verknode-rowid">RowID</th>
              {t.columns.map((c, j) => (
                <th key={j}>
                  <input className="jp-mod-styled" value={c.name} onChange={e => setCol(j, { name: e.target.value })} />
                  <div className="verknode-creator-colctl">
                    <select className="jp-mod-styled" value={c.type} onChange={e => setCol(j, { type: e.target.value })}>
                      {types.map(o => (
                        <option key={o}>{o}</option>
                      ))}
                    </select>
                    <button className="verknode-icon-btn" onClick={() => removeColumn(j)} title="Remove column">
                      ✕
                    </button>
                  </div>
                </th>
              ))}
              <th>
                <button className="jp-Button jp-mod-styled" onClick={addColumn} title="Add column">
                  +
                </button>
              </th>
            </tr>
          </thead>
          <tbody>
            {t.rows.map((r, i) => (
              <tr key={i}>
                <td className="verknode-rowid">Row{i}</td>
                {t.columns.map((_, j) => (
                  <td key={j}>
                    <input
                      className="jp-mod-styled"
                      value={r[j] ?? ''}
                      placeholder="?"
                      onChange={e => setCell(i, j, e.target.value)}
                    />
                  </td>
                ))}
                <td>
                  <button className="verknode-icon-btn" onClick={() => set({ rows: t.rows.filter((_, k) => k !== i) })} title="Remove row">
                    ✕
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <button
        className="jp-Button jp-mod-styled verknode-add-btn"
        onClick={() => set({ rows: [...t.rows, t.columns.map(() => '')] })}
      >
        + Add row
      </button>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* field row                                                           */
/* ------------------------------------------------------------------ */
export function FieldRow(props: {
  field: FieldInfo;
  value: any;
  onChange: (v: any) => void;
  ctx: FieldContext;
  boundTo?: string | null;
}) {
  const { field: f, value, onChange, ctx, boundTo } = props;
  const disabled = !!boundTo;
  let editor: React.ReactNode;
  let wide = true;
  switch (f.type) {
    case 'columns':
      editor = (
        <TwinList
          items={columnsOf(ctx, f.port).map(c => ({ name: c.name, tag: TYPE_BADGE[c.kind] ?? '?' }))}
          value={value}
          onChange={onChange}
          disabled={disabled}
        />
      );
      break;
    case 'multienum':
      editor = (
        <TwinList
          items={(f.options ?? []).map(o => ({ name: o, tag: '' }))}
          value={value}
          onChange={onChange}
          disabled={disabled}
        />
      );
      break;
    case 'varlist':
      editor = (
        <TwinList
          items={ctx.flowVariables.map(v => ({ name: v.name, tag: VAR_BADGE[v.type] ?? '?' }))}
          value={value}
          onChange={onChange}
          disabled={disabled}
        />
      );
      break;
    case 'records':
      editor = <RecordsEditor field={f} value={value} onChange={onChange} ctx={ctx} />;
      break;
    case 'code':
      editor = <CodeEditor field={f} value={value} onChange={onChange} ctx={ctx} disabled={disabled} />;
      break;
    case 'tablecreator':
      editor = <TableCreatorEditor field={f} value={value} onChange={onChange} />;
      break;
    case 'notebook':
      editor = <NotebookField field={f} value={value} onChange={onChange} ctx={ctx} disabled={disabled} />;
      break;
    case 'kernel':
      wide = false;
      editor = (
        <select className="jp-mod-styled" value={value ?? ''} disabled={disabled} onChange={e => onChange(e.target.value)}>
          <option value="">The notebook's kernel{ctx.extra?.kernel ? ` (${ctx.extra.kernel})` : ''}</option>
          {(ctx.extra?.kernels ?? []).map((k: { name: string; display: string }) => (
            <option key={k.name} value={k.name}>
              {k.display} ({k.name})
            </option>
          ))}
        </select>
      );
      break;
    case 'sketcher':
      editor = (
        <MoleculeList
          value={value}
          onChange={onChange}
          input={ctx.extra}
          editor={ctx.editorSettings?.values.sketcher ?? 'builtin'}
          onEditor={kind => void ctx.editorSettings?.set('sketcher', kind)}
        />
      );
      break;
    default:
      wide = false;
      editor = <ScalarEditor field={f} value={value} onChange={onChange} ctx={ctx} disabled={disabled} />;
  }
  return (
    <div className={`verknode-field ${wide ? 'verknode-field-wide' : ''} verknode-field-${f.type}`}>
      <label title={f.description}>{f.label}</label>
      <div className="verknode-field-editor">
        {editor}
        {boundTo && (
          <div className="verknode-field-bound">
            The "{f.label}" parameter is controlled by the variable <b>{boundTo}</b>.
          </div>
        )}
        {f.description && !wide && <div className="verknode-field-help">{f.description}</div>}
      </div>
    </div>
  );
}

const BINDABLE = new Set(['string', 'int', 'float', 'bool', 'enum', 'column', 'code', 'variable', 'notebook', 'kernel']);

function FlowVariablesTab(props: {
  fields: FieldInfo[];
  variables: Record<string, VariableBinding>;
  available: FlowVariableInfo[];
  onChange: (v: Record<string, VariableBinding>) => void;
}) {
  const set = (key: string, patch: VariableBinding) =>
    props.onChange({ ...props.variables, [key]: { ...(props.variables[key] ?? {}), ...patch } });
  const fields = props.fields.filter(f => BINDABLE.has(f.type));
  if (!fields.length) {
    return <div className="verknode-empty">This node has no settings that can be controlled by flow variables.</div>;
  }
  return (
    <table className="verknode-fv-table">
      <thead>
        <tr>
          <th>Setting</th>
          <th>Use variable</th>
          <th>Create variable</th>
        </tr>
      </thead>
      <tbody>
        {fields.map(f => {
          const b = props.variables[f.key] ?? {};
          return (
            <tr key={f.key}>
              <td>{f.label}</td>
              <td>
                <select className="jp-mod-styled" value={b.used ?? ''} onChange={e => set(f.key, { used: e.target.value || null })}>
                  <option value="">—</option>
                  {b.used && !props.available.some(v => v.name === b.used) && (
                    <option value={b.used}>{b.used} (not available)</option>
                  )}
                  {props.available.map(v => (
                    <option key={v.name} value={v.name}>
                      [{VAR_BADGE[v.type]}] {v.name} = {String(v.value)}
                    </option>
                  ))}
                </select>
              </td>
              <td>
                <input
                  className="jp-mod-styled"
                  placeholder="variable name"
                  value={b.exposed ?? ''}
                  onChange={e => set(f.key, { exposed: e.target.value || null })}
                />
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/**
 * Node configuration dialog ("Dialog - 3 - CSV Reader") with Settings and
 * Flow Variables tabs, OK / Apply / Cancel.
 */
export function ConfigureDialog(props: {
  controller: WorkflowController;
  nodeId: number;
  onClose: () => void;
  editorSettings?: EditorSettings;
}) {
  const { controller, nodeId, onClose } = props;
  const [data, setData] = useState<DialogData | null>(null);
  const [settings, setSettings] = useState<Record<string, any>>({});
  const [variables, setVariables] = useState<Record<string, VariableBinding>>({});
  const [tab, setTab] = useState<'settings' | 'variables' | 'description'>('settings');
  const [error, setError] = useState<string | null>(null);

  // node-specific data (e.g. the input molecules of the Molecule Sketcher), refreshed
  // when other settings change (e.g. another input column is chosen)
  const [extra, setExtra] = useState<any>(null);
  const load = () =>
    controller
      .dialog(nodeId)
      .then(d => {
        const s = JSON.parse(JSON.stringify(d.settings));
        // show the input column a node picked by itself (empty setting)
        if (d.extra?.column && 'column' in s && !s.column) {
          s.column = d.extra.column;
        }
        setData(d);
        setSettings(s);
        setExtra(d.extra ?? null);
        setVariables(JSON.parse(JSON.stringify(d.variables)));
      })
      .catch(e => setError(e.message));
  const sketcherKeys = (data?.node.fields ?? []).filter(f => f.type === 'sketcher').map(f => f.key);
  const extraInputs = data?.extra
    ? JSON.stringify(Object.entries(settings).filter(([k]) => !sketcherKeys.includes(k)))
    : null;
  const firstExtra = useRef(true);
  useEffect(() => {
    if (extraInputs === null) {
      return;
    }
    if (firstExtra.current) {
      firstExtra.current = false;
      return;
    }
    const t = window.setTimeout(() => {
      controller
        .dialogData(nodeId, settings)
        .then(setExtra)
        .catch(e => setExtra({ error: e.message }));
    }, 250);
    return () => window.clearTimeout(t);
  }, [extraInputs]);

  /**
   * Change a setting; when edits depend on it (e.g. the input molecule
   * column of the Molecule Sketcher) ask before discarding them.
   */
  const change = async (key: string, value: any) => {
    const next = { ...settings, [key]: value };
    const affected: { field: FieldInfo; what: 'structure' | 'name'; count: number }[] = [];
    for (const f of data?.node.fields ?? []) {
      const what = f.depends?.[key];
      if (!what || !Array.isArray(settings[f.key])) {
        continue;
      }
      const count = settings[f.key].filter(
        (m: any) => m.row !== undefined && m.row !== null && (what === 'name' ? m.name : m.molfile || m.smiles)
      ).length;
      if (count) {
        affected.push({ field: f, what, count });
      }
    }
    if (affected.length && settings[key] !== value) {
      const what = affected.map(a => `${a.count} edited ${a.what === 'name' ? 'name' : 'molecule'}${a.count > 1 ? 's' : ''}`).join(' and ');
      const result = await showDialog({
        title: 'Change the input column?',
        body: `This discards ${what}, made for the current column.`,
        buttons: [Dialog.cancelButton({ label: 'Keep edits' }), Dialog.warnButton({ label: 'Change and discard' })]
      });
      if (!result.button.accept) {
        return; // the selection stays as it was
      }
      for (const a of affected) {
        next[a.field.key] = settings[a.field.key]
          .map((m: any) => {
            if (m.row === undefined || m.row === null) {
              return m;
            }
            const copy = { ...m };
            if (a.what === 'name') {
              delete copy.name;
            } else {
              delete copy.molfile;
              delete copy.smiles;
            }
            return copy;
          })
          .filter((m: any) => m.row === undefined || m.row === null || m.name || m.molfile || m.smiles);
      }
    }
    setSettings(next);
  };

  useEffect(() => {
    void load();
  }, [nodeId]);

  const apply = async (close: boolean) => {
    try {
      setError(null);
      await controller.op('set_settings', { id: nodeId, settings, variables });
      if (close) {
        onClose();
      } else {
        await load();
      }
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const ctx: FieldContext = {
    inSpecs: data?.inSpecs ?? [],
    flowVariables: data?.flowVariables ?? [],
    editorSettings: props.editorSettings,
    extra,
    settings,
    setSetting: (key, value) => void change(key, value),
    setExtra,
    action: (name, args) => controller.nodeAction(nodeId, name, settings, args)
  };
  const title = data ? `Dialog - ${nodeId} - ${data.node.name}` : 'Dialog';
  const hasData = !!data && data.node.fields.length > 0;

  return (
    <Modal
      title={title}
      onClose={onClose}
      className="verknode-configure"
      footer={
        <>
          {error && <div className="verknode-dialog-error">{error}</div>}
          <button className="jp-Dialog-button jp-mod-accept jp-mod-styled" onClick={() => apply(true)} disabled={!hasData}>
            <div className="jp-Dialog-buttonLabel">OK</div>
          </button>
          <button className="jp-Dialog-button jp-mod-reject jp-mod-styled" onClick={() => apply(false)} disabled={!hasData}>
            <div className="jp-Dialog-buttonLabel">Apply</div>
          </button>
          <button className="jp-Dialog-button jp-mod-reject jp-mod-styled" onClick={onClose}>
            <div className="jp-Dialog-buttonLabel">Cancel</div>
          </button>
        </>
      }
    >
      {!data ? (
        <div className="verknode-empty">{error ? '' : 'Loading…'}</div>
      ) : (
        <>
          <div className="verknode-tabs">
            {(
              [
                ['settings', 'Settings'],
                ['variables', 'Flow Variables'],
                ['description', 'Description']
              ] as const
            ).map(([k, label]) => (
              <button key={k} className={`verknode-tab ${tab === k ? 'verknode-tab-active' : ''}`} onClick={() => setTab(k)}>
                {label}
              </button>
            ))}
          </div>
          <div className="verknode-tab-body">
            {tab === 'settings' &&
              (data.node.fields.length ? (
                data.node.fields
                  .filter(f => visible(f, settings))
                  .map((f, i, shown) => (
                    <React.Fragment key={f.key}>
                      {f.section && f.section !== shown[i - 1]?.section && (
                        <div className="verknode-field-section">
                          {f.section}
                          {extra?.notes?.[f.section] && <div className="verknode-field-section-note">{extra.notes[f.section]}</div>}
                        </div>
                      )}
                      {/* (extra.hide: settings the node's inputs replace, e.g. an LLM port's connection) */}
                      {!(extra?.hide ?? []).includes(f.key) && (
                        <FieldRow
                          field={f}
                          value={settings[f.key]}
                          ctx={ctx}
                          boundTo={variables[f.key]?.used}
                          onChange={v => void change(f.key, v)}
                        />
                      )}
                    </React.Fragment>
                  ))
              ) : (
                <div className="verknode-empty">This node has no settings.</div>
              ))}
            {tab === 'variables' && (
              <FlowVariablesTab
                fields={data.node.fields}
                variables={variables}
                available={data.flowVariables}
                onChange={setVariables}
              />
            )}
            {tab === 'description' && <Description node={data.node} />}
          </div>
        </>
      )}
    </Modal>
  );
}
