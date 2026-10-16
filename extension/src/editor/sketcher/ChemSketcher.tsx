import React, { useEffect, useMemo, useRef, useState } from 'react';

import { structure } from './api';
import {
  addAtom,
  addBond,
  addChain,
  addRing,
  Atom,
  atomAt,
  atomById,
  atomOrNew,
  BOND,
  Bond,
  bondAt,
  bondsOf,
  cloneMol,
  cycleBond,
  deleteAtom,
  deleteBond,
  dist,
  emptyMol,
  fromStructure,
  growBond,
  implicitH,
  Mol,
  neighbors,
  parseLabel,
  Point,
  polar,
  setStereo,
  showLabel,
  snapAngle,
  toMolfile
} from './model';

/**
 * Built-in structure sketcher:
 *
 * - bond tool: click an atom to grow a bond at 120° (zigzag), click empty
 *   space for a new bond, drag to draw a bond in 15° steps or onto another
 *   atom; click a bond to cycle single → double → triple (double/triple/
 *   wedge/hash tools set that bond type; wedge again flips it);
 * - chain tool: drag to draw a zigzag chain (bond count shown);
 * - ring tools: click empty space, an atom (shares it) or a bond (fused);
 * - hover hotkeys: over an atom C N O S P F I H, l = Cl, b = Br, + / -
 *   charge; over a bond 1 2 3 bond order, w wedge, h hash; Delete removes
 *   what is under the pointer (or the selection);
 * - double-click an atom (or empty space) to type a label like NH2, O-, Cl;
 * - select tool: drag a rectangle, drag selected atoms to move them;
 * - wheel zooms, middle-drag pans; Ctrl+Z / Ctrl+Y undo / redo.
 */
type Tool =
  | { kind: 'bond'; order: 1 | 2 | 3; stereo: 'wedge' | 'hash' | null }
  | { kind: 'chain' }
  | { kind: 'ring'; size: number; aromatic: boolean }
  | { kind: 'element'; element: string }
  | { kind: 'charge'; delta: 1 | -1 }
  | { kind: 'erase' }
  | { kind: 'select' };

type Drag =
  | { mode: 'bond'; from: number | null; start: Point; cur: Point; moved: boolean }
  | { mode: 'chain'; from: number | null; start: Point; cur: Point; moved: boolean }
  | { mode: 'bondclick'; bond: number; moved: boolean; start: Point }
  | { mode: 'move'; start: Point; cur: Point; atoms: Set<number>; moved: boolean }
  | { mode: 'band'; start: Point; cur: Point }
  | { mode: 'pan'; start: Point; origin: { x: number; y: number } };

const HOTKEY_ELEMENTS: Record<string, string> = {
  c: 'C',
  n: 'N',
  o: 'O',
  s: 'S',
  p: 'P',
  f: 'F',
  i: 'I',
  h: 'H',
  l: 'Cl',
  b: 'Br'
};
const PALETTE = ['C', 'N', 'O', 'S', 'P', 'F', 'Cl', 'Br', 'I', 'H'];
const DEG = Math.PI / 180;
const CHAIN_STEP = BOND * Math.cos(30 * DEG);

/** Heteroatom colours (CPK-like, muted for print-like drawings). */
const COLORS: Record<string, string> = { N: '#2a4fd6', O: '#d42a2a', S: '#b8930b', P: '#d97a00', F: '#2a9d4b', Cl: '#2a9d4b', Br: '#9b3b1b', I: '#7b2fa8' };

export interface ChemSketcherHandle {
  molfile: () => string;
}

export function ChemSketcher(props: {
  /** structure to start with (MolBlock or SMILES) */
  initial: string;
  onChange: (molfile: string, empty: boolean) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const svg = useRef<SVGSVGElement>(null);
  const [mol, setMol] = useState<Mol>(emptyMol);
  const history = useRef<{ undo: Mol[]; redo: Mol[] }>({ undo: [], redo: [] });
  const [tool, setTool] = useState<Tool>({ kind: 'bond', order: 1, stereo: null });
  const [hover, setHover] = useState<{ atom?: number; bond?: number }>({});
  const [drag, setDrag] = useState<Drag | null>(null);
  const [selection, setSelection] = useState<Set<number>>(new Set());
  const [label, setLabel] = useState<{ atom: number | null; at: Point; text: string } | null>(null);
  const [view, setView] = useState({ x: 0, y: 0, k: 1 });
  const [info, setInfo] = useState<{ smiles: string; formula: string; error?: string }>({ smiles: '', formula: '' });
  const [otherElement, setOtherElement] = useState('');

  const size = () => {
    const r = box.current?.getBoundingClientRect();
    return { w: r?.width ?? 600, h: r?.height ?? 400 };
  };
  /** screen (client) point -> model coordinates */
  const toModel = (e: { clientX: number; clientY: number }): Point => {
    const r = svg.current!.getBoundingClientRect();
    return { x: (e.clientX - r.left - view.x) / view.k, y: (e.clientY - r.top - view.y) / view.k };
  };

  // ------------------------------------------------------------ load / report
  const load = (text: string, clean = false) => {
    if (!text.trim()) {
      setMol(emptyMol());
      return Promise.resolve();
    }
    return structure(text, clean).then(s => {
      if (s.error) {
        setInfo({ smiles: '', formula: '', error: s.error });
        return;
      }
      const { w, h } = size();
      setView({ x: 0, y: 0, k: 1 });
      setMol(fromStructure(s, { x: w / 2, y: h / 2 }));
    });
  };
  useEffect(() => {
    void load(props.initial);
    box.current?.focus();
  }, []);

  const molfile = useMemo(() => toMolfile(mol), [mol]);
  useEffect(() => {
    props.onChange(molfile, mol.atoms.length === 0);
    if (!mol.atoms.length) {
      setInfo({ smiles: '', formula: '' });
      return;
    }
    const t = window.setTimeout(() => {
      structure(molfile, false, 10, 10)
        .then(s => setInfo({ smiles: s.smiles ?? '', formula: s.formula ?? '', error: s.error }))
        .catch(e => setInfo({ smiles: '', formula: '', error: String(e.message ?? e) }));
    }, 250);
    return () => window.clearTimeout(t);
  }, [molfile]);

  /** apply an edit to a copy of the molecule and record it for undo */
  const edit = (fn: (m: Mol) => void) => {
    const next = cloneMol(mol);
    fn(next);
    history.current.undo.push(mol);
    history.current.redo = [];
    setMol(next);
  };
  const undo = () => {
    const prev = history.current.undo.pop();
    if (prev) {
      history.current.redo.push(mol);
      setMol(prev);
      setSelection(new Set());
    }
  };
  const redo = () => {
    const next = history.current.redo.pop();
    if (next) {
      history.current.undo.push(mol);
      setMol(next);
    }
  };
  const clean = () => {
    if (mol.atoms.length) {
      const before = mol;
      void load(molfile, true).then(() => {
        history.current.undo.push(before);
        history.current.redo = [];
      });
    }
  };

  // ------------------------------------------------------------ pointer
  const onPointerDown = (e: React.PointerEvent) => {
    box.current?.focus();
    if (label) {
      return;
    }
    const p = toModel(e);
    if (e.button === 1 || (e.button === 0 && e.altKey)) {
      setDrag({ mode: 'pan', start: { x: e.clientX, y: e.clientY }, origin: { x: view.x, y: view.y } });
      return;
    }
    if (e.button !== 0) {
      return;
    }
    (e.target as Element).setPointerCapture?.(e.pointerId);
    const atom = atomAt(mol, p);
    const bond = atom ? undefined : bondAt(mol, p);
    switch (tool.kind) {
      case 'bond':
        if (bond) {
          setDrag({ mode: 'bondclick', bond: bond.id, moved: false, start: p });
        } else {
          setDrag({ mode: 'bond', from: atom?.id ?? null, start: atom ?? p, cur: p, moved: false });
        }
        break;
      case 'chain':
        setDrag({ mode: 'chain', from: atom?.id ?? null, start: atom ?? p, cur: p, moved: false });
        break;
      case 'ring':
        edit(m => addRing(m, tool.size, tool.aromatic, atom ? { atom: atom.id } : bond ? { bond: bond.id } : { point: p }));
        break;
      case 'element':
        edit(m => {
          if (atom) {
            const a = atomById(m, atom.id)!;
            a.element = tool.element;
          } else {
            addAtom(m, p, tool.element);
          }
        });
        break;
      case 'charge':
        if (atom) {
          edit(m => {
            atomById(m, atom.id)!.charge += tool.delta;
          });
        }
        break;
      case 'erase':
        if (atom || bond) {
          edit(m => (atom ? deleteAtom(m, atom.id) : deleteBond(m, bond!.id)));
        }
        break;
      case 'select': {
        const hit = atom ? [atom.id] : bond ? [bond.a, bond.b] : [];
        if (hit.length) {
          const chosen = hit.every(id => selection.has(id)) ? selection : new Set(hit);
          setSelection(chosen);
          setDrag({ mode: 'move', start: p, cur: p, atoms: chosen, moved: false });
        } else {
          setSelection(new Set());
          setDrag({ mode: 'band', start: p, cur: p });
        }
        break;
      }
    }
  };

  const onPointerMove = (e: React.PointerEvent) => {
    const p = toModel(e);
    if (!drag) {
      const atom = atomAt(mol, p);
      const bond = atom ? undefined : bondAt(mol, p);
      if (atom?.id !== hover.atom || bond?.id !== hover.bond) {
        setHover({ atom: atom?.id, bond: bond?.id });
      }
      return;
    }
    if (drag.mode === 'pan') {
      setView(v => ({ ...v, x: drag.origin.x + e.clientX - drag.start.x, y: drag.origin.y + e.clientY - drag.start.y }));
    } else if (drag.mode === 'bondclick') {
      if (dist(p, drag.start) > 4) {
        setDrag({ ...drag, moved: true });
      }
    } else {
      const moved = 'moved' in drag && (drag.moved || dist(p, drag.start) > 4);
      setDrag({ ...drag, cur: p, ...(drag.mode !== 'band' ? { moved } : {}) } as Drag);
    }
  };

  /** End of a drawn bond: an atom under the pointer, else a fixed-length bond at a 15° angle. */
  const bondEnd = (start: Point, cur: Point, from: number | null): { atom?: Atom; point: Point } => {
    const target = atomAt(mol, cur, 12, from !== null ? new Set([from]) : undefined);
    if (target) {
      return { atom: target, point: target };
    }
    const angle = snapAngle(Math.atan2(cur.y - start.y, cur.x - start.x));
    return { point: polar(start, angle) };
  };
  const chainPlan = (start: Point, cur: Point) => {
    const angle = snapAngle(Math.atan2(cur.y - start.y, cur.x - start.x), 30 * DEG);
    const count = Math.max(1, Math.round(dist(start, cur) / CHAIN_STEP));
    return { angle, count };
  };

  const onPointerUp = (e: React.PointerEvent) => {
    const d = drag;
    setDrag(null);
    if (!d) {
      return;
    }
    const p = toModel(e);
    if (d.mode === 'bondclick' && !d.moved && tool.kind === 'bond') {
      edit(m => {
        const b = m.bonds.find(x => x.id === d.bond)!;
        if (tool.stereo) {
          setStereo(m, b.id, tool.stereo);
        } else if (tool.order === 1) {
          cycleBond(m, b.id);
        } else {
          b.order = tool.order;
          b.stereo = null;
        }
      });
    } else if (d.mode === 'bond' && tool.kind === 'bond') {
      edit(m => {
        if (!d.moved) {
          if (d.from !== null) {
            growBond(m, d.from, tool.order, tool.stereo);
          } else {
            const a = addAtom(m, d.start);
            addBond(m, a, atomOrNew(m, polar(d.start, -30 * DEG), new Set([a])), tool.order, tool.stereo);
          }
          return;
        }
        const end = bondEnd(d.start, p, d.from);
        const a = d.from ?? atomOrNew(m, d.start);
        const b = end.atom?.id ?? atomOrNew(m, end.point, new Set([a]));
        if (a !== b) {
          addBond(m, a, b, tool.order, tool.stereo);
        }
      });
    } else if (d.mode === 'chain') {
      edit(m => {
        if (!d.moved) {
          if (d.from !== null) {
            growBond(m, d.from);
          } else {
            addChain(m, d.start, 0, 3);
          }
          return;
        }
        const { angle, count } = chainPlan(d.start, p);
        addChain(m, d.from ?? d.start, angle, count);
      });
    } else if (d.mode === 'move' && d.moved) {
      const dx = p.x - d.start.x;
      const dy = p.y - d.start.y;
      edit(m => {
        for (const a of m.atoms) {
          if (d.atoms.has(a.id)) {
            a.x += dx;
            a.y += dy;
          }
        }
        // a moved atom dropped onto another atom merges with it
        for (const id of d.atoms) {
          const a = atomById(m, id);
          const target = a && atomAt(m, a, 6, d.atoms);
          if (a && target) {
            for (const b of bondsOf(m, id)) {
              const other = b.a === id ? b.b : b.a;
              if (other !== target.id && !m.bonds.some(x => x !== b && ((x.a === other && x.b === target.id) || (x.b === other && x.a === target.id)))) {
                if (b.a === id) {
                  b.a = target.id;
                } else {
                  b.b = target.id;
                }
              }
            }
            deleteAtom(m, id);
          }
        }
      });
    } else if (d.mode === 'band') {
      const x0 = Math.min(d.start.x, d.cur.x);
      const x1 = Math.max(d.start.x, d.cur.x);
      const y0 = Math.min(d.start.y, d.cur.y);
      const y1 = Math.max(d.start.y, d.cur.y);
      setSelection(new Set(mol.atoms.filter(a => a.x >= x0 && a.x <= x1 && a.y >= y0 && a.y <= y1).map(a => a.id)));
    }
  };

  const onDoubleClick = (e: React.MouseEvent) => {
    const p = toModel(e);
    const atom = atomAt(mol, p);
    const text = atom ? atom.element + (atom.charge ? (atom.charge > 0 ? '+' : '-').repeat(Math.abs(atom.charge)) : '') : '';
    setLabel({ atom: atom?.id ?? null, at: atom ?? p, text });
  };
  const applyLabel = (text: string) => {
    const l = label;
    setLabel(null);
    box.current?.focus();
    const parsed = parseLabel(text);
    if (!l || !parsed) {
      return;
    }
    edit(m => {
      const id = l.atom ?? addAtom(m, l.at);
      const a = atomById(m, id)!;
      a.element = parsed.element;
      a.charge = parsed.charge;
      a.isotope = parsed.isotope;
    });
  };

  const onWheel = (e: React.WheelEvent) => {
    const r = svg.current!.getBoundingClientRect();
    const sx = e.clientX - r.left;
    const sy = e.clientY - r.top;
    const k = Math.min(4, Math.max(0.3, view.k * Math.exp(-e.deltaY * 0.0015)));
    setView({ k, x: sx - ((sx - view.x) * k) / view.k, y: sy - ((sy - view.y) * k) / view.k });
  };

  // ------------------------------------------------------------ keyboard
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (label) {
      return;
    }
    const key = e.key;
    const stop = () => {
      e.preventDefault();
      e.stopPropagation();
    };
    if ((e.ctrlKey || e.metaKey) && key.toLowerCase() === 'z') {
      stop();
      if (e.shiftKey) {
        redo();
      } else {
        undo();
      }
      return;
    }
    if ((e.ctrlKey || e.metaKey) && key.toLowerCase() === 'y') {
      stop();
      redo();
      return;
    }
    if ((e.ctrlKey || e.metaKey) && key.toLowerCase() === 'a') {
      stop();
      setSelection(new Set(mol.atoms.map(a => a.id)));
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) {
      return;
    }
    if (key === 'Escape') {
      if (drag || selection.size) {
        stop();
        setDrag(null);
        setSelection(new Set());
      }
      return;
    }
    if (key === 'Delete' || key === 'Backspace') {
      stop();
      if (selection.size) {
        edit(m => selection.forEach(id => deleteAtom(m, id)));
        setSelection(new Set());
      } else if (hover.atom !== undefined) {
        edit(m => deleteAtom(m, hover.atom!));
      } else if (hover.bond !== undefined) {
        edit(m => deleteBond(m, hover.bond!));
      }
      setHover({});
      return;
    }
    if (hover.atom !== undefined) {
      const element = HOTKEY_ELEMENTS[key.toLowerCase()];
      if (element) {
        stop();
        edit(m => {
          atomById(m, hover.atom!)!.element = element;
        });
      } else if (key === '+' || key === '=' || key === '-') {
        stop();
        edit(m => {
          atomById(m, hover.atom!)!.charge += key === '-' ? -1 : 1;
        });
      }
      return;
    }
    if (hover.bond !== undefined) {
      const b = hover.bond;
      if (key === '1' || key === '2' || key === '3') {
        stop();
        edit(m => {
          const x = m.bonds.find(y => y.id === b)!;
          x.order = Number(key) as 1 | 2 | 3;
          x.stereo = null;
        });
      } else if (key.toLowerCase() === 'w' || key.toLowerCase() === 'h') {
        stop();
        edit(m => setStereo(m, b, key.toLowerCase() === 'w' ? 'wedge' : 'hash'));
      }
    }
  };

  // ------------------------------------------------------------ drawing
  const drawing = useMemo(() => renderMol(mol), [mol]);
  const preview = (() => {
    if (!drag) {
      return null;
    }
    if (drag.mode === 'bond' && drag.moved) {
      const end = bondEnd(drag.start, drag.cur, drag.from);
      return <line className="verknode-sk-preview" x1={drag.start.x} y1={drag.start.y} x2={end.point.x} y2={end.point.y} />;
    }
    if (drag.mode === 'chain' && drag.moved) {
      const { angle, count } = chainPlan(drag.start, drag.cur);
      const pts = [drag.start];
      let sign = 1;
      for (let i = 0; i < count; i++) {
        pts.push(polar(pts[pts.length - 1], angle + sign * 30 * DEG));
        sign = -sign;
      }
      const last = pts[pts.length - 1];
      return (
        <>
          <polyline className="verknode-sk-preview" points={pts.map(q => `${q.x},${q.y}`).join(' ')} fill="none" />
          <text className="verknode-sk-count" x={last.x + 8} y={last.y - 8}>
            {count}
          </text>
        </>
      );
    }
    if (drag.mode === 'move' && drag.moved) {
      return null;
    }
    if (drag.mode === 'band') {
      return (
        <rect
          className="verknode-sk-band"
          x={Math.min(drag.start.x, drag.cur.x)}
          y={Math.min(drag.start.y, drag.cur.y)}
          width={Math.abs(drag.cur.x - drag.start.x)}
          height={Math.abs(drag.cur.y - drag.start.y)}
        />
      );
    }
    return null;
  })();
  // atoms being moved are drawn at their dragged position
  const shown =
    drag?.mode === 'move' && drag.moved
      ? (() => {
          const m = cloneMol(mol);
          m.atoms.forEach(a => {
            if (drag.atoms.has(a.id)) {
              a.x += drag.cur.x - drag.start.x;
              a.y += drag.cur.y - drag.start.y;
            }
          });
          return renderMol(m);
        })()
      : drawing;

  const isTool = (t: Partial<Tool> & { kind: Tool['kind'] }) =>
    tool.kind === t.kind && Object.entries(t).every(([k, v]) => (tool as any)[k] === v);
  const toolButton = (t: Tool, title: string, icon: React.ReactNode) => (
    <button className={`verknode-sk-tool ${isTool(t) ? 'verknode-sk-active' : ''}`} title={title} onClick={() => setTool(t)}>
      {icon}
    </button>
  );
  const hoverAtom = hover.atom !== undefined ? atomById(mol, hover.atom) : undefined;
  const hoverBond = hover.bond !== undefined ? mol.bonds.find(b => b.id === hover.bond) : undefined;

  return (
    <div className="verknode-sketcher">
      <div className="verknode-sk-toolbar">
        {toolButton({ kind: 'select' }, 'Select / move (drag a rectangle; drag selected atoms)', <Icon d="M4 3 L4 15 L7.5 11.5 L10 17 L12 16 L9.5 10.5 L14 10.5 Z" fill />)}
        {toolButton({ kind: 'bond', order: 1, stereo: null }, 'Single bond (click a bond: single → double → triple)', <Icon d="M3 16 L17 4" />)}
        {toolButton({ kind: 'bond', order: 2, stereo: null }, 'Double bond', <Icon d="M3 14 L15 3 M5 17 L17 6" />)}
        {toolButton({ kind: 'bond', order: 3, stereo: null }, 'Triple bond', <Icon d="M2 13 L13 2 M4 16 L16 4 M7 18 L18 7" />)}
        {toolButton({ kind: 'bond', order: 1, stereo: 'wedge' }, 'Wedge bond (narrow end at the stereocentre; again flips)', <Icon d="M3 16 L16 2 L18 5 Z" fill />)}
        {toolButton({ kind: 'bond', order: 1, stereo: 'hash' }, 'Hashed wedge bond', <Icon d="M4 15 L5 16 M6 12 L8 14 M8 9 L11 12 M10 6 L14 10 M12 3 L17 8" />)}
        {toolButton({ kind: 'chain' }, 'Chain (drag; the bond count is shown)', <Icon d="M2 14 L6 6 L10 14 L14 6 L18 14" />)}
        <span className="verknode-sk-sep" />
        {[3, 4, 5, 6, 7, 8].map(n =>
          toolButton({ kind: 'ring', size: n, aromatic: false }, `${n}-membered ring (click empty space, an atom or a bond)`, <RingIcon n={n} />)
        )}
        {toolButton({ kind: 'ring', size: 6, aromatic: true }, 'Benzene', <RingIcon n={6} aromatic />)}
        <span className="verknode-sk-sep" />
        {PALETTE.map(el =>
          toolButton(
            { kind: 'element', element: el },
            `${el} (click an atom, or empty space for a new atom; hotkey over an atom: ${Object.entries(HOTKEY_ELEMENTS).find(([, v]) => v === el)?.[0] ?? '-'})`,
            <span className="verknode-sk-el" style={{ color: COLORS[el] }}>
              {el}
            </span>
          )
        )}
        <input
          className="verknode-sk-other"
          placeholder="…"
          title="Other element: type a symbol and press Enter"
          value={otherElement}
          onChange={e => setOtherElement(e.target.value)}
          onKeyDown={e => {
            e.stopPropagation();
            const parsed = parseLabel(otherElement);
            if (e.key === 'Enter' && parsed) {
              setTool({ kind: 'element', element: parsed.element });
            }
          }}
        />
        {toolButton({ kind: 'charge', delta: 1 }, 'Increase charge (hotkey over an atom: +)', <span className="verknode-sk-el">⊕</span>)}
        {toolButton({ kind: 'charge', delta: -1 }, 'Decrease charge (hotkey over an atom: -)', <span className="verknode-sk-el">⊖</span>)}
        {toolButton({ kind: 'erase' }, 'Eraser (or Delete over an atom / bond)', <Icon d="M7 17 L3 13 L11 5 L17 11 L11 17 Z M7 17 L17 17" />)}
        <span className="verknode-sk-sep" />
        <button className="verknode-sk-tool" title="Undo (Ctrl+Z)" onClick={undo}>
          ↶
        </button>
        <button className="verknode-sk-tool" title="Redo (Ctrl+Shift+Z / Ctrl+Y)" onClick={redo}>
          ↷
        </button>
        <button className="verknode-sk-tool verknode-sk-text" title="Clean up the 2D layout (RDKit)" onClick={clean}>
          Clean
        </button>
        <button
          className="verknode-sk-tool verknode-sk-text"
          title="Remove everything"
          onClick={() => mol.atoms.length && edit(m => Object.assign(m, emptyMol()))}
        >
          Clear
        </button>
      </div>
      <div
        className="verknode-sk-canvas"
        ref={box}
        tabIndex={0}
        data-keep-escape
        onKeyDown={onKeyDown}
        onMouseEnter={() => box.current?.focus({ preventScroll: true })}
      >
        <svg
          ref={svg}
          className={`verknode-sk-svg verknode-sk-tool-${tool.kind}`}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerLeave={() => !drag && setHover({})}
          onDoubleClick={onDoubleClick}
          onWheel={onWheel}
          onContextMenu={e => e.preventDefault()}
        >
          <g transform={`translate(${view.x} ${view.y}) scale(${view.k})`}>
            {selection.size > 0 &&
              mol.atoms
                .filter(a => selection.has(a.id))
                .map(a => <circle key={`s${a.id}`} className="verknode-sk-selected" cx={a.x} cy={a.y} r={10} />)}
            {hoverBond && <BondHighlight m={mol} b={hoverBond} />}
            {hoverAtom && <circle className="verknode-sk-hover" cx={hoverAtom.x} cy={hoverAtom.y} r={10} />}
            {shown}
            {preview}
          </g>
        </svg>
        {label && (
          <input
            className="verknode-sk-label-input"
            autoFocus
            style={{ left: label.at.x * view.k + view.x - 24, top: label.at.y * view.k + view.y - 12 }}
            value={label.text}
            placeholder="e.g. N, OH, Cl, O-"
            onChange={e => setLabel({ ...label, text: e.target.value })}
            onKeyDown={e => {
              e.stopPropagation();
              if (e.key === 'Enter') {
                applyLabel(label.text);
              } else if (e.key === 'Escape') {
                setLabel(null);
                box.current?.focus();
              }
            }}
            onBlur={() => applyLabel(label.text)}
          />
        )}
        {!mol.atoms.length && (
          <div className="verknode-sk-empty">
            Click to draw a bond, drag for a direction; pick rings and elements above.
            <br />
            Hover an atom and type N, O, S, l (Cl), b (Br)… or + / −; hover a bond and type 1, 2, 3.
          </div>
        )}
      </div>
      <div className="verknode-sk-status">
        {info.error ? (
          <span className="verknode-sk-error">{info.error}</span>
        ) : (
          <>
            <span className="verknode-sk-formula">{info.formula}</span>
            <span className="verknode-sk-smiles" title="SMILES">
              {info.smiles}
            </span>
          </>
        )}
      </div>
    </div>
  );
}

function Icon({ d, fill = false }: { d: string; fill?: boolean }) {
  return (
    <svg viewBox="0 0 20 20" width="18" height="18">
      <path d={d} fill={fill ? 'currentColor' : 'none'} stroke="currentColor" strokeWidth={fill ? 0.5 : 1.6} strokeLinejoin="round" />
    </svg>
  );
}

function RingIcon({ n, aromatic = false }: { n: number; aromatic?: boolean }) {
  const pts = Array.from({ length: n }, (_, i) => {
    const t = -Math.PI / 2 + (i * 2 * Math.PI) / n;
    return [10 + 7.5 * Math.cos(t), 10.5 + 7.5 * Math.sin(t)];
  });
  return (
    <svg viewBox="0 0 20 20" width="18" height="18">
      <polygon points={pts.map(p => p.join(',')).join(' ')} fill="none" stroke="currentColor" strokeWidth={1.4} />
      {aromatic && <circle cx={10} cy={10.5} r={4} fill="none" stroke="currentColor" strokeWidth={1.1} />}
    </svg>
  );
}

function BondHighlight({ m, b }: { m: Mol; b: Bond }) {
  const p = atomById(m, b.a)!;
  const q = atomById(m, b.b)!;
  return <line className="verknode-sk-hover-bond" x1={p.x} y1={p.y} x2={q.x} y2={q.y} />;
}

// ------------------------------------------------------------------ rendering
const LABEL_GAP = 8;
const DOUBLE_GAP = 4.5;

function labelText(m: Mol, a: Atom): { main: string; h: number; hLeft: boolean; charge: string } {
  const h = implicitH(m, a);
  // H goes to the side without bonds ("HO" when bonds leave to the right)
  const sx = neighbors(m, a.id).reduce((s, n) => s + (n.x - a.x), 0);
  const charge = a.charge ? `${Math.abs(a.charge) > 1 ? Math.abs(a.charge) : ''}${a.charge > 0 ? '+' : '−'}` : '';
  return { main: a.element, h, hLeft: sx > 1, charge };
}

function renderMol(m: Mol): React.ReactNode {
  const labelled = new Set(m.atoms.filter(a => showLabel(m, a)).map(a => a.id));
  const bonds = m.bonds.map(b => {
    const p = atomById(m, b.a)!;
    const q = atomById(m, b.b)!;
    const len = dist(p, q) || 1;
    const ux = (q.x - p.x) / len;
    const uy = (q.y - p.y) / len;
    // shorten at labelled atoms
    const s = labelled.has(p.id) ? LABEL_GAP : 0;
    const e = labelled.has(q.id) ? LABEL_GAP : 0;
    const x1 = p.x + ux * s;
    const y1 = p.y + uy * s;
    const x2 = q.x - ux * e;
    const y2 = q.y - uy * e;
    const nx = -uy;
    const ny = ux;
    if (b.stereo === 'wedge') {
      const w = 3.6;
      return <polygon key={b.id} className="verknode-sk-wedge" points={`${x1},${y1} ${x2 + nx * w},${y2 + ny * w} ${x2 - nx * w},${y2 - ny * w}`} />;
    }
    if (b.stereo === 'hash') {
      const n = Math.max(4, Math.round(len / 5));
      const lines = [];
      for (let i = 1; i <= n; i++) {
        const t = i / n;
        const w = 0.6 + 3.2 * t;
        const cx = x1 + (x2 - x1) * t;
        const cy = y1 + (y2 - y1) * t;
        lines.push(<line key={i} x1={cx + nx * w} y1={cy + ny * w} x2={cx - nx * w} y2={cy - ny * w} />);
      }
      return (
        <g key={b.id} className="verknode-sk-bond">
          {lines}
        </g>
      );
    }
    if (b.order === 1) {
      return <line key={b.id} className="verknode-sk-bond" x1={x1} y1={y1} x2={x2} y2={y2} />;
    }
    if (b.order === 3) {
      return (
        <g key={b.id} className="verknode-sk-bond">
          <line x1={x1} y1={y1} x2={x2} y2={y2} />
          <line x1={x1 + nx * DOUBLE_GAP} y1={y1 + ny * DOUBLE_GAP} x2={x2 + nx * DOUBLE_GAP} y2={y2 + ny * DOUBLE_GAP} />
          <line x1={x1 - nx * DOUBLE_GAP} y1={y1 - ny * DOUBLE_GAP} x2={x2 - nx * DOUBLE_GAP} y2={y2 - ny * DOUBLE_GAP} />
        </g>
      );
    }
    // double bond: the second line on the side of the other substituents (inside rings),
    // centred for terminal double bonds like C=O
    const others = [...neighbors(m, p.id), ...neighbors(m, q.id)].filter(a => a.id !== p.id && a.id !== q.id);
    const terminal = bondsOf(m, p.id).length === 1 || bondsOf(m, q.id).length === 1;
    const side = others.reduce((acc, a) => acc + Math.sign((a.x - p.x) * nx + (a.y - p.y) * ny), 0);
    if (terminal || side === 0) {
      const g = DOUBLE_GAP / 2;
      return (
        <g key={b.id} className="verknode-sk-bond">
          <line x1={x1 + nx * g} y1={y1 + ny * g} x2={x2 + nx * g} y2={y2 + ny * g} />
          <line x1={x1 - nx * g} y1={y1 - ny * g} x2={x2 - nx * g} y2={y2 - ny * g} />
        </g>
      );
    }
    const sg = Math.sign(side) * DOUBLE_GAP;
    const inset = 0.14 * len;
    const a1 = s ? 0 : inset;
    const a2 = e ? 0 : inset;
    return (
      <g key={b.id} className="verknode-sk-bond">
        <line x1={x1} y1={y1} x2={x2} y2={y2} />
        <line
          x1={x1 + ux * a1 + nx * sg}
          y1={y1 + uy * a1 + ny * sg}
          x2={x2 - ux * a2 + nx * sg}
          y2={y2 - uy * a2 + ny * sg}
        />
      </g>
    );
  });
  const labels = m.atoms
    .filter(a => labelled.has(a.id))
    .map(a => {
      const t = labelText(m, a);
      const hText = t.h ? 'H' : '';
      const hSub = t.h > 1 ? String(t.h) : '';
      const color = COLORS[a.element];
      return (
        <g key={a.id} className="verknode-sk-label" style={{ fill: color }}>
          <rect className="verknode-sk-label-bg" x={a.x - 7} y={a.y - 8} width={14} height={16} rx={3} />
          <text x={a.x} y={a.y} textAnchor={t.hLeft ? 'end' : 'start'} dx={t.hLeft ? 5 : -5}>
            {t.hLeft && hText && (
              <>
                <tspan>{hText}</tspan>
                {hSub && <tspan className="verknode-sk-sub" dy={3}>{hSub}</tspan>}
                {hSub && <tspan dy={-3} />}
              </>
            )}
            {a.isotope ? <tspan className="verknode-sk-sup" dy={-5}>{a.isotope}</tspan> : null}
            <tspan dy={a.isotope ? 5 : 0}>{t.main}</tspan>
            {!t.hLeft && hText && (
              <>
                <tspan>{hText}</tspan>
                {hSub && <tspan className="verknode-sk-sub" dy={3}>{hSub}</tspan>}
              </>
            )}
            {t.charge && (
              <tspan className="verknode-sk-sup" dy={hSub && !t.hLeft ? -8 : -5}>
                {t.charge}
              </tspan>
            )}
          </text>
        </g>
      );
    });
  return (
    <>
      <g>{bonds}</g>
      <g>{labels}</g>
    </>
  );
}
