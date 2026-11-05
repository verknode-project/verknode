import React, { useState } from 'react';

import { WorkflowController } from '../controller';
import { GroupInfo } from '../types';
import { Modal } from './Modal';
import { NoteEditor } from './NoteView';

/** Title of a group annotation shown on its bubble (its text; older ones: its first line). */
export function groupTitle(g: GroupInfo): string {
  return g.text.split('\n')[0].trim() || 'group';
}

/**
 * Create or edit a group annotation: its title, note (Markdown: what the nodes do), colour and nodes;
 * existing annotations can also be deleted here.
 */
export function GroupDialog(props: {
  controller: WorkflowController;
  /** null: create a new annotation */
  group: GroupInfo | null;
  nodes: number[];
  colors: string[];
  nodeName: (id: number) => string;
  selection: () => number[];
  onClose: () => void;
}) {
  const { group } = props;
  const [text, setText] = useState(group?.text ?? '');
  const [note, setNote] = useState(group?.note ?? '');
  const [color, setColor] = useState(group?.color ?? props.colors[0] ?? '#e6b422');
  const [nodes, setNodes] = useState(props.nodes);
  const [error, setError] = useState<string | null>(null);
  const selected = props.selection();
  const sameAsSelection = selected.length === nodes.length && selected.every(n => nodes.includes(n));
  const added = selected.filter(n => !nodes.includes(n));

  const act = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      props.onClose();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const ok = () => {
    if (!group) {
      return act(() => props.controller.op('add_group', { nodes, text, color, ...(note.trim() ? { note } : {}) }));
    }
    // only what changed (keeps the console transcript to the point)
    const changes: Record<string, unknown> = {};
    if (text !== group.text) {
      changes.text = text;
    }
    if (note.trim() !== (group.note ?? '').trim()) {
      changes.note = note;
    }
    if (color !== group.color) {
      changes.color = color;
    }
    if (nodes.length !== group.nodes.length || nodes.some((n, i) => n !== group.nodes[i])) {
      changes.nodes = nodes;
    }
    return act(async () => {
      if (Object.keys(changes).length) {
        await props.controller.op('update_group', { id: group.id, ...changes });
      }
    });
  };
  const remove = () => group && act(() => props.controller.op('remove_group', { id: group.id }));

  return (
    <Modal
      title={group ? 'Edit Group Annotation' : 'New Group Annotation'}
      onClose={props.onClose}
      className="verknode-group-dialog"
      footer={
        <>
          {error && <div className="verknode-dialog-error">{error}</div>}
          {group && (
            <button className="jp-Dialog-button jp-mod-warn jp-mod-styled verknode-group-delete" onClick={() => void remove()}>
              <div className="jp-Dialog-buttonLabel">Delete annotation</div>
            </button>
          )}
          <button className="jp-Dialog-button jp-mod-accept jp-mod-styled" disabled={!nodes.length} onClick={() => void ok()}>
            <div className="jp-Dialog-buttonLabel">{group ? 'OK' : 'Create'}</div>
          </button>
          <button className="jp-Dialog-button jp-mod-reject jp-mod-styled" onClick={props.onClose}>
            <div className="jp-Dialog-buttonLabel">Cancel</div>
          </button>
        </>
      }
    >
      <label className="verknode-group-field">
        <span>Title</span>
        <input
          autoFocus
          value={text}
          placeholder="Shown on the bubble, e.g. Load and clean"
          onChange={e => setText(e.target.value.replace(/\n/g, ' '))}
          onKeyDown={e => {
            if (e.key === 'Enter') {
              e.preventDefault();
              void ok();
            }
          }}
        />
      </label>
      <div className="verknode-group-field">
        <span>Note (Markdown: what these nodes do and why; ¶ on the bubble shows it)</span>
        <NoteEditor value={note} onChange={setNote} onSubmit={() => void ok()} />
      </div>
      <div className="verknode-group-field">
        <span>Colour</span>
        <div className="verknode-group-colors">
          {props.colors.map(c => (
            <button
              key={c}
              className={`verknode-group-swatch ${c === color ? 'verknode-group-swatch-active' : ''}`}
              style={{ background: c }}
              title={c}
              onClick={() => setColor(c)}
            />
          ))}
          <input type="color" value={color} title="Other colour" onChange={e => setColor(e.target.value)} />
        </div>
      </div>
      <div className="verknode-group-field">
        <span>Nodes</span>
        <div className="verknode-group-nodes">
          {nodes.map(n => (
            <span key={n} className="verknode-group-node">
              {props.nodeName(n)}
              <button title="Remove from the group" onClick={() => setNodes(nodes.filter(m => m !== n))}>
                ✕
              </button>
            </span>
          ))}
          {added.length > 0 && (
            <button className="verknode-group-use-selection" onClick={() => setNodes([...nodes, ...added])}>
              Add selected ({added.length})
            </button>
          )}
          {selected.length > 0 && !sameAsSelection && (
            <button className="verknode-group-use-selection" onClick={() => setNodes(selected)}>
              Only selected ({selected.length})
            </button>
          )}
        </div>
      </div>
    </Modal>
  );
}
