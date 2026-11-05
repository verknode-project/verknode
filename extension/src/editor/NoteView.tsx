import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

import { WorkflowController } from '../controller';
import { Modal } from './Modal';
import { DocMarkdown } from './widgets/walkthrough';

/**
 * Notes: Markdown attached to a node or a group annotation, saying what it does and why. A node with a
 * note has ¶ after its label, a group ¶ after its title; hovering the mark shows the note, clicking it
 * (or Edit Note… in the node's menu, Edit Group… for a group) edits it. Notes may refer to nodes as #3
 * and use the walkthroughs' vk: links.
 */

/** A note rendered (the walkthroughs' Markdown). */
export function NoteText({ text }: { text: string }) {
  return (
    <div className="verknode-walk verknode-note-text">
      <DocMarkdown text={text} />
    </div>
  );
}

/** Markdown text with its preview beside it. */
export function NoteEditor(props: {
  value: string;
  onChange: (v: string) => void;
  onSubmit?: () => void;
  autoFocus?: boolean;
  placeholder?: string;
}) {
  return (
    <div className="verknode-note-editor">
      <textarea
        autoFocus={props.autoFocus}
        value={props.value}
        spellCheck
        placeholder={props.placeholder ?? 'What it does and why, in Markdown: **bold**, lists, links, #3 for node 3'}
        onChange={e => props.onChange(e.target.value)}
        onKeyDown={e => {
          if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && props.onSubmit) {
            e.preventDefault();
            props.onSubmit();
          }
        }}
      />
      <div className="verknode-note-preview" aria-label="Preview">
        {props.value.trim() ? <NoteText text={props.value} /> : <span className="verknode-note-empty">The preview shows here.</span>}
      </div>
    </div>
  );
}

/** Edit the note of a node. */
export function NoteDialog(props: { controller: WorkflowController; id: number; label: string; note: string; onClose: () => void }) {
  const [text, setText] = useState(props.note);
  const [error, setError] = useState<string | null>(null);
  const save = async (value: string) => {
    try {
      if (value.trim() !== props.note.trim()) {
        await props.controller.op('set_note', { id: props.id, text: value });
      }
      props.onClose();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <Modal
      title={`Note of #${props.id} “${props.label}”`}
      onClose={props.onClose}
      onSubmit={() => void save(text)}
      className="verknode-note-dialog"
      footer={
        <>
          {error && <div className="verknode-dialog-error">{error}</div>}
          {props.note && (
            <button className="jp-Dialog-button jp-mod-warn jp-mod-styled verknode-group-delete" onClick={() => void save('')}>
              <div className="jp-Dialog-buttonLabel">Remove note</div>
            </button>
          )}
          <button className="jp-Dialog-button jp-mod-accept jp-mod-styled" onClick={() => void save(text)}>
            <div className="jp-Dialog-buttonLabel">Save</div>
          </button>
          <button className="jp-Dialog-button jp-mod-reject jp-mod-styled" onClick={props.onClose}>
            <div className="jp-Dialog-buttonLabel">Cancel</div>
          </button>
        </>
      }
    >
      <NoteEditor autoFocus value={text} onChange={setText} onSubmit={() => void save(text)} />
    </Modal>
  );
}

export interface NotePeekTarget {
  kind: 'node' | 'group';
  id: number;
  title: string;
  note: string;
  /** the mark it belongs to, on the screen */
  rect: DOMRect;
}

/**
 * The note of a node or group, shown near its mark while the mouse is on the mark or on the card (its
 * links can be clicked).
 */
export function NotePeek(props: { target: NotePeekTarget; onEnter: () => void; onLeave: () => void; onEdit: () => void }) {
  const { target } = props;
  const box = useRef<HTMLDivElement>(null);
  const [at, setAt] = useState<{ left: number; top: number } | null>(null);
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) {
      return;
    }
    const doc = el.ownerDocument.documentElement;
    const w = el.offsetWidth;
    const h = el.offsetHeight;
    const r = target.rect;
    const left = Math.max(8, Math.min(r.left + r.width / 2 - w / 2, doc.clientWidth - w - 8));
    const below = r.bottom + 6;
    const top = below + h <= doc.clientHeight - 8 ? below : Math.max(8, r.top - h - 6);
    setAt({ left, top });
  }, [target]);
  useEffect(() => {
    const key = (e: KeyboardEvent) => e.key === 'Escape' && props.onLeave();
    window.addEventListener('keydown', key);
    return () => window.removeEventListener('keydown', key);
  }, [props.onLeave]);
  return createPortal(
    <div
      ref={box}
      className="verknode-note-peek"
      style={at ? { left: at.left, top: at.top } : { visibility: 'hidden', left: 0, top: 0 }}
      onMouseEnter={props.onEnter}
      onMouseLeave={props.onLeave}
    >
      <div className="verknode-note-peek-head">
        <span className="verknode-note-peek-title">
          {target.kind === 'node' ? `#${target.id} ` : ''}
          {target.title}
        </span>
        <button className="verknode-icon-btn" title="Edit the note" onClick={props.onEdit}>
          ✎
        </button>
      </div>
      <NoteText text={target.note} />
    </div>,
    document.body
  );
}
