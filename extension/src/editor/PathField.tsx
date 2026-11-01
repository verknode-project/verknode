import { PathExt } from '@jupyterlab/coreutils';
import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

import { FieldInfo } from '../types';
import type { FieldContext } from './ConfigureDialog';
import { FolderEntry, labAvailable, listFolder } from './labViews';
import { Modal } from './Modal';

type Kind = 'file' | 'save' | 'folder' | 'any';

const TITLES: Record<Kind, string> = {
  file: 'Choose a file',
  save: 'Save as',
  folder: 'Choose a folder',
  any: 'Choose a file or folder'
};
const OK_LABELS: Record<Kind, string> = { file: 'Open', save: 'Choose', folder: 'Choose this folder', any: 'Choose' };

const kindOf = (f: FieldInfo): Kind => (f.path === 'save' || f.path === 'folder' || f.path === 'any' ? f.path : 'file');

/** Folder (from the Jupyter root) of a path as a node reads it; null if the chooser cannot reach it. */
function inRoot(base: string, value: string): string | null {
  const v = value.trim();
  // (absolute and home paths, URLs and paths from variables: the chooser only sees the Jupyter root)
  if (/^(~|\/|\\|[A-Za-z]:[\\/]|[a-z]+:\/\/)/.test(v) || v.includes('$')) {
    return null;
  }
  const p = PathExt.normalize(PathExt.join(base, v));
  if (p === '.') {
    return '';
  }
  return p === '..' || p.startsWith('../') ? null : p;
}

/** The folder a field's relative paths start from, from the Jupyter root (null: out of reach). */
function baseOf(f: FieldInfo, ctx: FieldContext): string | null {
  const folder = ctx.workflowFolder ?? '';
  return f.path_base ? inRoot(folder, String(ctx.settings?.[f.path_base] ?? '')) : folder;
}

function sizeText(n?: number | null) {
  if (n === null || n === undefined) {
    return '';
  }
  if (n < 1024) {
    return `${n} B`;
  }
  return n < 1024 * 1024 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/**
 * Chooses a file or folder under the Jupyter root. The keyboard works: arrow keys and typing move in the
 * list, Enter opens a folder or takes a file, Backspace goes up.
 */
function PathChooser(props: {
  kind: Kind;
  title: string;
  extensions: string[];
  startFolder: string;
  fallbackFolder: string;
  startName: string;
  onChoose: (rootPath: string) => void;
  onClose: () => void;
}) {
  const { kind, extensions } = props;
  const [folder, setFolder] = useState<string | null>(null);
  const [entries, setEntries] = useState<FolderEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState(props.startName);
  const [selected, setSelected] = useState<string | null>(props.startName || null);
  const [allFiles, setAllFiles] = useState(!extensions.length);
  const list = useRef<HTMLDivElement>(null);
  const nameBox = useRef<HTMLInputElement>(null);

  const open = async (path: string, select: string | null = null, fallback?: string) => {
    try {
      const found = await listFolder(path);
      setEntries(found);
      setFolder(path);
      setSelected(select);
      setError(null);
    } catch (e) {
      if (fallback !== undefined && fallback !== path) {
        await open(fallback, null, fallback === '' ? undefined : '');
      } else {
        setError((e as Error).message);
      }
    }
  };
  useEffect(() => {
    void open(props.startFolder, props.startName || null, props.fallbackFolder);
    (kind === 'save' ? nameBox.current : list.current)?.focus({ preventScroll: true });
    if (kind === 'save') {
      nameBox.current?.select();
    }
  }, []);

  const wanted = (e: FolderEntry) =>
    e.isFolder || allFiles || extensions.some(x => e.name.toLowerCase().endsWith(x.toLowerCase()));
  const shown = entries.filter(e => (kind === 'folder' ? e.isFolder : wanted(e)));
  const here = folder ?? '';
  const crumbs = here ? here.split('/') : [];
  const selectedEntry = shown.find(e => e.name === selected);

  const select = (e: FolderEntry | undefined) => {
    if (!e) {
      return;
    }
    setSelected(e.name);
    if (!e.isFolder && kind !== 'folder') {
      setName(e.name);
    } else if (kind === 'any') {
      setName('');
    }
    list.current?.querySelector(`[data-name="${CSS.escape(e.name)}"]`)?.scrollIntoView({ block: 'nearest' });
  };
  const enter = (e: FolderEntry) => {
    setName('');
    void open(PathExt.join(here, e.name));
  };
  const up = () => {
    if (here) {
      void open(PathExt.dirname(here), PathExt.basename(here));
    }
  };
  /** The OK button: what a dialog of this kind takes from the list and the name box. */
  const accept = () => {
    if (folder === null) {
      return;
    }
    if (selectedEntry?.isFolder) {
      if (kind === 'folder' || kind === 'any') {
        props.onChoose(PathExt.join(here, selectedEntry.name));
      } else {
        enter(selectedEntry);
      }
      return;
    }
    const typed = name.trim();
    if (kind === 'folder' || (kind === 'any' && !typed)) {
      props.onChoose(here);
    } else if (typed) {
      props.onChoose(PathExt.join(here, typed));
    }
  };
  const onListKey = (e: React.KeyboardEvent) => {
    const i = shown.findIndex(x => x.name === selected);
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      select(shown[Math.max(0, Math.min(shown.length - 1, i + (e.key === 'ArrowDown' ? 1 : -1)))]);
    } else if (e.key === 'Home' || e.key === 'End') {
      e.preventDefault();
      select(shown[e.key === 'Home' ? 0 : shown.length - 1]);
    } else if (e.key === 'Backspace' || (e.key === 'ArrowLeft' && e.altKey)) {
      e.preventDefault();
      up();
    } else if (e.key === 'ArrowRight' && selectedEntry?.isFolder) {
      e.preventDefault();
      enter(selectedEntry);
    } else if (e.key === 'Enter' && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      if (selectedEntry?.isFolder) {
        enter(selectedEntry);
      } else {
        accept();
      }
    } else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
      // typing goes to the next name that starts with the letter
      const k = e.key.toLowerCase();
      select([...shown.slice(i + 1), ...shown.slice(0, i + 1)].find(x => x.name.toLowerCase().startsWith(k)));
    }
  };
  const canAccept = folder !== null && (kind === 'folder' || kind === 'any' || !!name.trim() || !!selectedEntry?.isFolder);

  return (
    <Modal
      title={props.title}
      className="verknode-path-chooser"
      onClose={props.onClose}
      onSubmit={accept}
      footer={
        <>
          <button className="jp-Button jp-mod-styled jp-mod-reject" onClick={props.onClose}>
            Cancel
          </button>
          <button className="jp-Button jp-mod-styled jp-mod-accept" disabled={!canAccept} onClick={accept}>
            {OK_LABELS[kind]}
          </button>
        </>
      }
    >
      <div className="verknode-path-crumbs">
        <button className="verknode-icon-btn" disabled={!here} onClick={up} title="Up one folder (Backspace)">
          ↑
        </button>
        <button className="verknode-path-crumb" onClick={() => void open('')} title="The Jupyter root">
          ⌂
        </button>
        {crumbs.map((c, i) => (
          <React.Fragment key={i}>
            <span>/</span>
            <button className="verknode-path-crumb" onClick={() => void open(crumbs.slice(0, i + 1).join('/'))}>
              {c}
            </button>
          </React.Fragment>
        ))}
      </div>
      <div
        ref={list}
        className="verknode-path-list"
        role="listbox"
        tabIndex={0}
        aria-label="Files"
        onKeyDown={onListKey}
      >
        {shown.map(e => (
          <div
            key={e.name}
            data-name={e.name}
            role="option"
            aria-selected={e.name === selected}
            className={`verknode-path-row ${e.name === selected ? 'verknode-selected' : ''}`}
            onClick={() => select(e)}
            onDoubleClick={() => {
              if (e.isFolder) {
                enter(e);
              } else {
                props.onChoose(PathExt.join(here, e.name));
              }
            }}
          >
            <span className="verknode-path-icon">{e.isFolder ? '📁' : '📄'}</span>
            <span className="verknode-path-name">{e.name}</span>
            <span className="verknode-path-size">{e.isFolder ? '' : sizeText(e.size)}</span>
          </div>
        ))}
        {folder !== null && !shown.length && <div className="verknode-path-empty">Nothing here</div>}
      </div>
      {error && <div className="verknode-dialog-error">{error}</div>}
      <div className="verknode-path-bottom">
        {kind !== 'folder' && (
          <input
            ref={nameBox}
            className="jp-mod-styled"
            type="text"
            aria-label="Name"
            placeholder={kind === 'any' ? 'File name (empty: this folder)' : 'File name'}
            value={name}
            onChange={e => {
              setName(e.target.value);
              setSelected(null);
            }}
          />
        )}
        {extensions.length > 0 && kind !== 'folder' && (
          <select className="jp-mod-styled" aria-label="File types" value={allFiles ? 'all' : 'some'} onChange={e => setAllFiles(e.target.value === 'all')}>
            <option value="some">{extensions.join(', ')}</option>
            <option value="all">All files</option>
          </select>
        )}
      </div>
    </Modal>
  );
}

/** The Browse button of a path field: a chooser, whose choice goes in the field as the node reads it. */
export function BrowseButton(props: {
  field: FieldInfo;
  value: string;
  onChange: (v: string) => void;
  ctx: FieldContext;
  disabled?: boolean;
}) {
  const { field, ctx } = props;
  const [start, setStart] = useState<{ folder: string; name: string } | null>(null);
  const button = useRef<HTMLButtonElement>(null);
  if (!labAvailable()) {
    return null;
  }
  const base = baseOf(field, ctx);
  const kind = kindOf(field);
  const open = () => {
    const at = inRoot(base ?? '', String(props.value ?? ''));
    if (at === null || !String(props.value ?? '').trim()) {
      setStart({ folder: base ?? '', name: '' });
    } else if (kind === 'folder') {
      setStart({ folder: at, name: '' });
    } else {
      setStart({ folder: PathExt.dirname(at), name: PathExt.basename(at) });
    }
  };
  // (in the editor, not in the dialog of the field: a dialog over the whole editor)
  const host = button.current?.closest('.verknode-editor');
  return (
    <>
      <button
        ref={button}
        type="button"
        className="jp-Button jp-mod-styled verknode-path-browse"
        disabled={props.disabled || base === null}
        title={base === null ? 'The chooser only reaches folders under the Jupyter root: type the path' : 'Choose in a list of files'}
        onClick={open}
      >
        Browse…
      </button>
      {start &&
        host &&
        createPortal(
          <PathChooser
            kind={kind}
            title={field.label === 'File' ? TITLES[kind] : `${TITLES[kind]} - ${field.label}`}
            extensions={field.extensions ?? []}
            startFolder={start.folder}
            fallbackFolder={base ?? ''}
            startName={start.name}
            onClose={() => setStart(null)}
            onChoose={p => {
              setStart(null);
              const rel = PathExt.relative(base ?? '', p);
              props.onChange(rel === '' ? '.' : rel);
            }}
          />,
          host
        )}
    </>
  );
}
