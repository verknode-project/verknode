import React, { useRef, useState } from 'react';

import { Modal } from '../Modal';
import { emptyMolfile, structure } from './api';
import { ChemSketcher } from './ChemSketcher';
import { KetcherFrame, KetcherHandle } from './KetcherFrame';

export type SketcherKind = 'ketcher' | 'builtin';

export interface SketchItem {
  name: string;
  /** MolBlock or SMILES (empty for a new structure) */
  structure: string;
  /** shown before the name, e.g. the output index */
  label?: string;
}

/** Canonical SMILES of a structure (to tell whether it was changed). */
const canonical = (text: string) =>
  text.trim() ? structure(text, false, 10, 10).then(s => (s.error ? null : s.smiles)) : Promise.resolve('');

/**
 * Draw or edit structures with Ketcher or the built-in sketcher; the
 * structure moves along when switching editors. Several structures can be
 * edited in a row: Previous / Next (or PageUp / PageDown)
 * keep the changes of the current one and open the next. SMILES or
 * MolBlock text can be pasted in.
 */
export function SketcherDialog(props: {
  items: SketchItem[];
  start: number;
  editor: SketcherKind;
  onEditor: (kind: SketcherKind) => void;
  /** store item `index`; molfile null = structure unchanged (only the name may have changed) */
  onSave: (index: number, name: string, molfile: string | null) => void;
  onClose: () => void;
}) {
  const [index, setIndex] = useState(props.start);
  const item = props.items[index];
  const [name, setName] = useState(item.name);
  const [editor, setEditor] = useState<SketcherKind>(props.editor);
  // structure handed to an editor when it (re)starts
  const [initial, setInitial] = useState(item.structure);
  const [generation, setGeneration] = useState(0);
  const builtin = useRef({ molfile: '', empty: true });
  const ketcher = useRef<KetcherHandle>(null);
  const [paste, setPaste] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  /** the structure in the active editor */
  const current = async (): Promise<string> =>
    editor === 'ketcher' && ketcher.current ? ketcher.current.getMolfile() : builtin.current.empty ? '' : builtin.current.molfile;

  const switchTo = async (kind: SketcherKind) => {
    if (kind === editor) {
      return;
    }
    try {
      setInitial(await current());
    } catch {
      // keep the previous structure
    }
    setEditor(kind);
    setGeneration(g => g + 1);
    props.onEditor(kind);
  };

  const load = async () => {
    setError(null);
    const s = await structure(paste);
    if (s.error) {
      setError(s.error);
      return;
    }
    setInitial(s.molfile);
    setGeneration(g => g + 1);
    setPaste('');
  };

  /** Keep the current item's changes; false if its structure is not valid. */
  const commit = async (): Promise<boolean> => {
    const molfile = await current();
    if (emptyMolfile(molfile)) {
      setError('Draw a structure first');
      return false;
    }
    const [now, before] = await Promise.all([canonical(molfile), canonical(item.structure)]);
    if (now === null) {
      setError('Not a valid structure (check valences and charges)');
      return false;
    }
    const changed = now !== before;
    if (changed || (name.trim() || item.name) !== item.name) {
      props.onSave(index, name.trim() || item.name, changed ? molfile : null);
    }
    return true;
  };

  const run = async (fn: () => Promise<void>) => {
    if (busy) {
      return;
    }
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

  const go = (to: number) =>
    run(async () => {
      if (to < 0 || to >= props.items.length || !(await commit())) {
        return;
      }
      setIndex(to);
      setName(props.items[to].name);
      setInitial(props.items[to].structure);
      setGeneration(g => g + 1);
    });

  const save = () =>
    run(async () => {
      if (await commit()) {
        props.onClose();
      }
    });

  const many = props.items.length > 1;
  const title = `Molecule Sketcher — ${many ? `${index + 1} / ${props.items.length} · ` : ''}${
    item.label ? `${item.label} ` : ''
  }${name || item.name}`;

  return (
    <Modal
      title={title}
      onClose={props.onClose}
      className="verknode-sketcher-dialog"
      footer={
        <>
          {many && (
            <div className="verknode-sketcher-nav">
              <button className="jp-Dialog-button jp-mod-styled" disabled={index === 0 || busy} onClick={() => void go(index - 1)} title="Previous molecule (PageUp)">
                <div className="jp-Dialog-buttonLabel">◀ Previous</div>
              </button>
              <span>
                {index + 1} / {props.items.length}
              </span>
              <button
                className="jp-Dialog-button jp-mod-styled"
                disabled={index === props.items.length - 1 || busy}
                onClick={() => void go(index + 1)}
                title="Next molecule (PageDown)"
              >
                <div className="jp-Dialog-buttonLabel">Next ▶</div>
              </button>
            </div>
          )}
          {error && <div className="verknode-dialog-error">{error}</div>}
          <button className="jp-Dialog-button jp-mod-accept jp-mod-styled" disabled={busy} onClick={() => void save()}>
            <div className="jp-Dialog-buttonLabel">OK</div>
          </button>
          <button className="jp-Dialog-button jp-mod-reject jp-mod-styled" onClick={props.onClose}>
            <div className="jp-Dialog-buttonLabel">Cancel</div>
          </button>
        </>
      }
    >
      <div
        className="verknode-sketcher-frame"
        onKeyDown={e => {
          if (many && (e.key === 'PageDown' || e.key === 'PageUp')) {
            e.preventDefault();
            e.stopPropagation();
            void go(index + (e.key === 'PageDown' ? 1 : -1));
          }
        }}
      >
        <div className="verknode-sketcher-head">
          <div className="verknode-sketcher-tabs">
            <button className={editor === 'builtin' ? 'verknode-active' : ''} onClick={() => void switchTo('builtin')}>
              Built-in sketcher
            </button>
            <button className={editor === 'ketcher' ? 'verknode-active' : ''} onClick={() => void switchTo('ketcher')}>
              Ketcher
            </button>
          </div>
          <label className="verknode-sketcher-name">
            {item.label && <span className="verknode-sketcher-label">{item.label}</span>}
            Name <input value={name} onChange={e => setName(e.target.value)} />
          </label>
          <input
            className="verknode-sketcher-paste"
            placeholder="Paste SMILES or MolBlock…"
            value={paste}
            onChange={e => setPaste(e.target.value)}
            onKeyDown={e => {
              e.stopPropagation();
              if (e.key === 'Enter' && paste.trim()) {
                void load();
              }
            }}
          />
          <button className="verknode-sketcher-load" disabled={!paste.trim()} onClick={() => void load()}>
            Load
          </button>
        </div>
        <div className="verknode-sketcher-body">
          {editor === 'ketcher' ? (
            // kept running between molecules (it takes a moment to start)
            <KetcherFrame ref={ketcher} initial={initial} version={generation} />
          ) : (
            <ChemSketcher
              key={generation}
              initial={initial}
              onChange={(molfile, empty) => (builtin.current = { molfile, empty })}
            />
          )}
        </div>
      </div>
    </Modal>
  );
}
