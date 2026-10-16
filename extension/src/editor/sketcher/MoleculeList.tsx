import { Dialog, showDialog } from '@jupyterlab/apputils';
import React, { useEffect, useRef, useState } from 'react';

import { structure } from './api';
import { SketcherDialog, SketcherKind } from './SketcherDialog';

/**
 * A sketch of the Molecule Sketcher node: an edit of input row `row` (a
 * rename only when it has no structure), or an extra sketch (no `row`).
 * Structures are MolBlocks (or SMILES from the Python API).
 */
export interface SketchedMolecule {
  row?: number;
  name: string;
  molfile?: string;
  smiles?: string;
}

/** Input molecules from the server (MoleculeSketcher.dialog_data). */
export interface SketcherInput {
  connected: boolean;
  column?: string;
  nameColumn?: string | null;
  rows: { row: number; name: string | null; molfile: string | null }[];
  total: number;
  error?: string;
}

const thumbnails = new Map<string, Promise<string | null>>();
function thumbnail(text: string): Promise<string | null> {
  if (!thumbnails.has(text)) {
    thumbnails.set(
      text,
      structure(text, false, 150, 100).then(s => s.svg ?? null, () => null)
    );
  }
  return thumbnails.get(text)!;
}

/** Structure drawing, fetched once it scrolls into view (long input tables). */
function Thumbnail({ text, onOpen }: { text: string; onOpen?: () => void }) {
  const [svg, setSvg] = useState<string | null>(null);
  const [visible, setVisible] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const observer = new IntersectionObserver(entries => entries.some(e => e.isIntersecting) && setVisible(true));
    if (box.current) {
      observer.observe(box.current);
    }
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    let live = true;
    setSvg(null);
    if (text && visible) {
      void thumbnail(text).then(s => live && setSvg(s));
    }
    return () => {
      live = false;
    };
  }, [text, visible]);
  const open = { onDoubleClick: onOpen, title: onOpen ? 'Double-click to edit' : undefined };
  return svg ? (
    <div className="verknode-mol-thumb verknode-mol-thumb-open" {...open} dangerouslySetInnerHTML={{ __html: svg }} />
  ) : (
    <div ref={box} className="verknode-mol-thumb verknode-mol-thumb-empty verknode-mol-thumb-open" {...open}>
      {text ? '…' : 'no structure'}
    </div>
  );
}

const structureOf = (m: SketchedMolecule | undefined) => m?.molfile || m?.smiles || '';

/**
 * Settings editor of the Molecule Sketcher. With an input table: its
 * molecules, each to edit, rename or reset (exactly these rows come out).
 * Without: the drawn sketches (draw, paste SMILES, duplicate, reorder).
 * Both show the output Index and Name.
 */
export function MoleculeList(props: {
  value: SketchedMolecule[];
  onChange: (v: SketchedMolecule[]) => void;
  input?: SketcherInput | null;
  editor: SketcherKind;
  onEditor: (kind: SketcherKind) => void;
}) {
  const all = props.value ?? [];
  const input = props.input;
  const edits = new Map(all.filter(m => m.row !== undefined && m.row !== null).map(m => [m.row!, m]));
  const extras = all.filter(m => m.row === undefined || m.row === null);
  const inputMode = !!input?.connected;
  // what the sketcher dialog is editing
  const [editing, setEditing] = useState<{ kind: 'input'; row: number } | { kind: 'extra'; index: number } | { kind: 'new' } | null>(null);
  const [bulk, setBulk] = useState<string | null>(null);

  const setExtras = (next: SketchedMolecule[]) => props.onChange([...all.filter(m => m.row !== undefined && m.row !== null), ...next]);
  /** Store the edit of an input row; a name equal to the input's is not an edit. */
  const setEdit = (row: number, m: SketchedMolecule | null) => {
    const original = input?.rows.find(r => r.row === row);
    const kept: Partial<SketchedMolecule> = { ...(m ?? {}) };
    if (kept.name === undefined || kept.name === (original?.name || `Row ${row + 1}`)) {
      delete kept.name;
    }
    const empty = !kept.name && !kept.molfile && !kept.smiles;
    props.onChange([...all.filter(x => x.row !== row), ...(m && !empty ? [{ ...kept, row } as SketchedMolecule] : [])]);
  };
  const duplicate = (name: string, text: string) =>
    setExtras([...extras, { name: `${name} (copy)`, ...(text.includes('M  END') ? { molfile: text } : { smiles: text }) }]);
  const moveExtra = (i: number, d: number) => {
    const next = [...extras];
    [next[i], next[i + d]] = [next[i + d], next[i]];
    setExtras(next);
  };
  const addSmiles = () => {
    const added = (bulk ?? '')
      .split('\n')
      .map(l => l.trim())
      .filter(Boolean)
      .map((l, k) => {
        const [smiles, ...rest] = l.split(/\s+/);
        return { name: rest.join(' ') || `Molecule ${extras.length + k + 1}`, smiles };
      });
    setExtras([...extras, ...added]);
    setBulk(null);
  };

  const resetAll = async () => {
    const result = await showDialog({
      title: 'Reset all edits?',
      body: `All ${edits.size} edited input row${edits.size > 1 ? 's go' : ' goes'} back to the input molecule and name.`,
      buttons: [Dialog.cancelButton(), Dialog.warnButton({ label: 'Reset all' })]
    });
    if (result.button.accept) {
      props.onChange(extras);
    }
  };

  // the structure and name shown for an input row
  const inputRow = (r: SketcherInput['rows'][number]) => {
    const e = edits.get(r.row);
    return {
      name: e?.name || r.name || `Row ${r.row + 1}`,
      text: structureOf(e) || r.molfile || '',
      edited: !!structureOf(e),
      renamed: !!e?.name && e.name !== r.name
    };
  };

  const dialog = (() => {
    if (!editing) {
      return null;
    }
    if (editing.kind === 'new') {
      const n = extras.length + 1;
      return (
        <SketcherDialog
          items={[{ name: `Molecule ${n}`, structure: '', label: `#${n}` }]}
          start={0}
          editor={props.editor}
          onEditor={props.onEditor}
          onClose={() => setEditing(null)}
          onSave={(_, name, molfile) => molfile && setExtras([...extras, { name, molfile }])}
        />
      );
    }
    // the whole list, so the sketcher can go from one molecule to the next
    const inputMode = editing.kind === 'input';
    const rows = input?.rows ?? [];
    const items = inputMode
      ? rows.map(r => {
          const shown = inputRow(r);
          return { name: shown.name, structure: shown.text, label: `#${r.row + 1}` };
        })
      : extras.map((m, i) => ({ name: m.name, structure: structureOf(m), label: `#${i + 1}` }));
    const start = editing.kind === 'input' ? rows.findIndex(r => r.row === editing.row) : editing.index;
    return (
      <SketcherDialog
        items={items}
        start={Math.max(0, start)}
        editor={props.editor}
        onEditor={props.onEditor}
        onClose={() => setEditing(null)}
        onSave={(i, name, molfile) => {
          if (inputMode) {
            const row = rows[i].row;
            const e = edits.get(row);
            setEdit(row, { ...(e ?? {}), name, ...(molfile ? { molfile, smiles: undefined } : {}) });
          } else {
            setExtras(extras.map((m, j) => (j === i ? { ...m, name, ...(molfile ? { molfile, smiles: undefined } : {}) } : m)));
          }
        }}
      />
    );
  })();

  return (
    <div className="verknode-mol-list">
      {input?.connected && (
        <>
          <div className="verknode-mol-section verknode-mol-input-head">
            <span>
              Input molecules{input.total ? ` (${input.total})` : ''}
              {input.column && (
                <span className="verknode-mol-cols">
                  structures from <b>{input.column}</b>, names from{' '}
                  {input.nameColumn ? <b>{input.nameColumn}</b> : <i>row numbers</i>}
                </span>
              )}
            </span>
            <button disabled={!edits.size} onClick={() => void resetAll()} title="Undo all edits of input rows">
              Reset all
            </button>
          </div>
          {input.error && <div className="verknode-dialog-error">{input.error}</div>}
          {!input.error && !input.total && (
            <div className="verknode-mol-empty">Execute the upstream node to see and edit its molecules here.</div>
          )}
          {input.rows.map(r => {
            const shown = inputRow(r);
            const e = edits.get(r.row);
            return (
              <div key={`in${r.row}`} className={`verknode-mol-item ${shown.edited ? 'verknode-mol-edited' : ''}`}>
                <span className="verknode-mol-index" title="Index">
                  {r.row + 1}
                </span>
                <Thumbnail text={shown.text} onOpen={() => setEditing({ kind: 'input', row: r.row })} />
                <input
                  className="verknode-mol-name"
                  value={shown.name}
                  onChange={ev => setEdit(r.row, { ...(e ?? { name: '' }), name: ev.target.value })}
                />
                {shown.edited && <span className="verknode-mol-badge">edited</span>}
                <div className="verknode-mol-actions">
                  <button onClick={() => setEditing({ kind: 'input', row: r.row })}>Edit…</button>
                  <button
                    title="Back to the input molecule and name"
                    disabled={!e}
                    onClick={() => setEdit(r.row, null)}
                  >
                    Reset
                  </button>
                </div>
              </div>
            );
          })}
          {input.total > input.rows.length && (
            <div className="verknode-mol-empty">
              … and {input.total - input.rows.length} more input rows (not listed; they are passed on unchanged).
            </div>
          )}
          {extras.length > 0 && (
            <div className="verknode-mol-empty">
              {extras.length} sketch{extras.length > 1 ? 'es' : ''} drawn without an input {extras.length > 1 ? 'are' : 'is'} kept
              but not used while a table is connected.
            </div>
          )}
        </>
      )}
      {!inputMode && extras.map((m, i) => (
        <div key={`x${i}`} className="verknode-mol-item">
          <span className="verknode-mol-index" title="Index">
            {i + 1}
          </span>
          <Thumbnail text={structureOf(m)} onOpen={() => setEditing({ kind: 'extra', index: i })} />
          <input className="verknode-mol-name" value={m.name} onChange={e => setExtras(extras.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))} />
          <div className="verknode-mol-actions">
            <button onClick={() => setEditing({ kind: 'extra', index: i })}>Edit…</button>
            <button title="Add a copy" onClick={() => duplicate(m.name, structureOf(m))}>
              Duplicate
            </button>
            <button disabled={i === 0} title="Move up" onClick={() => moveExtra(i, -1)}>
              ↑
            </button>
            <button disabled={i === extras.length - 1} title="Move down" onClick={() => moveExtra(i, 1)}>
              ↓
            </button>
            <button title="Remove" onClick={() => setExtras(extras.filter((_, j) => j !== i))}>
              ✕
            </button>
          </div>
        </div>
      ))}
      {!inputMode && !extras.length && <div className="verknode-mol-empty">No structures yet.</div>}
      {!inputMode && (
        <div className="verknode-mol-add">
          <button onClick={() => setEditing({ kind: 'new' })}>Draw structure…</button>
          <button onClick={() => setBulk(bulk === null ? '' : null)}>Add SMILES…</button>
        </div>
      )}
      {!inputMode && bulk !== null && (
        <div className="verknode-mol-bulk">
          <textarea
            rows={4}
            autoFocus
            placeholder={'One structure per line: SMILES, optionally followed by a name\nc1ccccc1O Phenol'}
            value={bulk}
            onChange={e => setBulk(e.target.value)}
          />
          <button disabled={!bulk.trim()} onClick={addSmiles}>
            Add
          </button>
        </div>
      )}
      {dialog}
    </div>
  );
}
