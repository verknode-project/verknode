import React, { useEffect, useState } from 'react';

import { structure } from '../sketcher/api';
import { HtmlFrame } from './HtmlFrame';
import { InputControlProps } from './registry';

/**
 * Canvas controls of input widget nodes. Text and numbers are committed on
 * Enter or when the field loses focus (Escape reverts), sliders when they are
 * released, everything else at once. Interactive parts carry "nodrag" so
 * React Flow does not move the node.
 */

/** A draft of the value, reset whenever the stored value changes. */
function useDraft<T>(value: T): [T, (v: T) => void] {
  const [draft, setDraft] = useState<T>(value);
  useEffect(() => setDraft(value), [JSON.stringify(value)]);
  return [draft, setDraft];
}

function TextControl({ value, onChange, disabled }: InputControlProps) {
  const [draft, setDraft] = useDraft<string>(value ?? '');
  const commit = () => draft !== value && onChange(draft);
  return (
    <input
      className="verknode-widget-input nodrag"
      value={draft}
      disabled={disabled}
      onChange={e => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={e => {
        if (e.key === 'Enter') {
          commit();
        } else if (e.key === 'Escape') {
          setDraft(value ?? '');
          e.currentTarget.blur();
        }
      }}
    />
  );
}

function TextAreaControl({ value, onChange, disabled }: InputControlProps) {
  const [draft, setDraft] = useDraft<string>(value ?? '');
  const commit = () => draft !== value && onChange(draft);
  return (
    <textarea
      className="verknode-widget-input verknode-widget-textarea nodrag nowheel"
      value={draft}
      rows={4}
      disabled={disabled}
      title="Ctrl+Enter or click elsewhere to apply"
      onChange={e => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={e => {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
          commit();
        } else if (e.key === 'Escape') {
          setDraft(value ?? '');
          e.currentTarget.blur();
        }
      }}
    />
  );
}

function numberControl(integer: boolean) {
  return function NumberControl({ value, onChange, disabled }: InputControlProps) {
    const [draft, setDraft] = useDraft<string>(String(value ?? 0));
    const commit = () => {
      const n = Number(draft);
      if (draft.trim() === '' || !Number.isFinite(n)) {
        setDraft(String(value ?? 0));
      } else if ((integer ? Math.round(n) : n) !== value) {
        onChange(integer ? Math.round(n) : n);
      }
    };
    return (
      <input
        className="verknode-widget-input nodrag"
        type="number"
        step={integer ? 1 : 'any'}
        value={draft}
        disabled={disabled}
        onChange={e => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={e => {
          if (e.key === 'Enter') {
            commit();
          } else if (e.key === 'Escape') {
            setDraft(String(value ?? 0));
            e.currentTarget.blur();
          }
        }}
      />
    );
  };
}

const formatNumber = (v: number) => (Number.isInteger(v) ? String(v) : String(Number(v.toPrecision(6))));

function SliderControl({ value, props, onChange, disabled }: InputControlProps) {
  const [draft, setDraft] = useDraft<number>(value ?? 0);
  const commit = () => draft !== value && onChange(draft);
  return (
    <div className="verknode-widget-slider">
      <input
        className="nodrag"
        type="range"
        min={props.min}
        max={props.max}
        step={props.step}
        value={draft}
        disabled={disabled}
        onChange={e => setDraft(Number(e.target.value))}
        onPointerUp={commit}
        onKeyUp={commit}
        onBlur={commit}
      />
      <span className="verknode-widget-slider-value">{formatNumber(draft)}</span>
    </div>
  );
}

function CheckboxControl({ value, onChange, disabled, name }: InputControlProps) {
  return (
    <label className="verknode-widget-check nodrag">
      <input type="checkbox" checked={!!value} disabled={disabled} onChange={e => onChange(e.target.checked)} />
      {name}
    </label>
  );
}

function DropdownControl({ value, props, onChange, disabled }: InputControlProps) {
  const options: string[] = props.options ?? [];
  return (
    <select className="verknode-widget-input nodrag" value={value ?? ''} disabled={disabled || !options.length} onChange={e => onChange(e.target.value)}>
      {!options.length && <option value="">(no choices - see the dialog)</option>}
      {options.map(o => (
        <option key={o} value={o}>
          {o}
        </option>
      ))}
    </select>
  );
}

interface TableValue {
  columns: { name: string; type: string }[];
  rows: string[][];
}

function TableControl({ value, onChange, disabled }: InputControlProps) {
  const table: TableValue = value ?? { columns: [], rows: [] };
  const [rows, setRows] = useDraft<string[][]>(table.rows ?? []);
  const commit = (next: string[][]) => JSON.stringify(next) !== JSON.stringify(table.rows) && onChange({ ...table, rows: next });
  const setCell = (i: number, j: number, text: string) =>
    setRows(rows.map((r, k) => (k === i ? table.columns.map((_, c) => (c === j ? text : (r[c] ?? ''))) : r)));
  return (
    <div className="verknode-widget-table-edit">
      <div className="verknode-widget-scroll nowheel">
        <table>
          <thead>
            <tr>
              {table.columns.map(c => (
                <th key={c.name} title={c.type}>
                  {c.name}
                </th>
              ))}
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                {table.columns.map((c, j) => (
                  <td key={j}>
                    <input
                      className="nodrag"
                      value={r[j] ?? ''}
                      disabled={disabled}
                      placeholder="?"
                      onChange={e => setCell(i, j, e.target.value)}
                      onBlur={() => commit(rows)}
                      onKeyDown={e => e.key === 'Enter' && commit(rows)}
                    />
                  </td>
                ))}
                <td>
                  <button className="verknode-widget-mini nodrag" disabled={disabled} title="Remove row" onClick={() => commit(rows.filter((_, k) => k !== i))}>
                    ×
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <button
        className="verknode-widget-mini verknode-widget-addrow nodrag"
        disabled={disabled || !table.columns.length}
        onClick={() => commit([...rows, table.columns.map(() => '')])}
      >
        + row
      </button>
    </div>
  );
}

function MoleculeControl({ value, onSketch, disabled }: InputControlProps) {
  const [svg, setSvg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    if (!value?.trim()) {
      setSvg(null);
      setError(null);
      return;
    }
    structure(value, false, 170, 110).then(
      s => live && (setSvg(s.svg), setError(s.error ?? null)),
      e => live && setError(String(e))
    );
    return () => {
      live = false;
    };
  }, [value]);
  return (
    <div
      className={`verknode-widget-molecule nodrag${disabled ? ' verknode-disabled' : ''}`}
      title="Double-click to draw or edit the structure"
      onDoubleClick={() => !disabled && onSketch?.()}
    >
      {error ? (
        <span className="verknode-widget-error">{error}</span>
      ) : svg ? (
        <span dangerouslySetInnerHTML={{ __html: svg }} />
      ) : (
        <span className="verknode-widget-empty">Double-click to draw</span>
      )}
    </div>
  );
}

function HtmlControl({ value, html, props, onChange, width, height }: InputControlProps) {
  return <HtmlFrame html={html ?? ''} value={value} width={width ?? props.width ?? 200} height={height ?? props.height ?? 40} onValue={onChange} />;
}

export const BUILTIN_CONTROLS: Record<string, React.ComponentType<InputControlProps>> = {
  text: TextControl,
  textarea: TextAreaControl,
  number: numberControl(false),
  integer: numberControl(true),
  slider: SliderControl,
  checkbox: CheckboxControl,
  dropdown: DropdownControl,
  table: TableControl,
  molecule: MoleculeControl,
  html: HtmlControl
};
