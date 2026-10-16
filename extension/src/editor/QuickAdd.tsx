import React, { useEffect, useRef } from 'react';

import { NodeDescription, NodeInfo, PortInfo } from '../types';
import { NodeIcon } from './NodeView';

const VISIBLE = 9;

/**
 * Tab quick-add: what the user types (top), the matching nodes below with
 * the selection highlighted; Tab cycles the output port of the source node,
 * Enter adds and connects the selected node, Escape cancels.
 */
export function QuickAdd(props: {
  x: number;
  y: number;
  source: NodeInfo | null;
  port: PortInfo | null;
  portCount: number;
  query: string;
  items: NodeDescription[];
  index: number;
  onQuery: (q: string) => void;
  onIndex: (i: number) => void;
  onCyclePort: (step: number) => void;
  onPick: (node: NodeDescription) => void;
  onClose: () => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    input.current?.focus();
  }, []);
  const { items, index } = props;
  const first = Math.min(Math.max(0, index - Math.floor(VISIBLE / 2)), Math.max(0, items.length - VISIBLE));
  const shown = items.slice(first, first + VISIBLE);

  const onKeyDown = (e: React.KeyboardEvent) => {
    e.stopPropagation();
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      props.onIndex(Math.min(items.length - 1, index + 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      props.onIndex(Math.max(0, index - 1));
    } else if (e.key === 'PageDown') {
      e.preventDefault();
      props.onIndex(Math.min(items.length - 1, index + VISIBLE));
    } else if (e.key === 'PageUp') {
      e.preventDefault();
      props.onIndex(Math.max(0, index - VISIBLE));
    } else if (e.key === 'Tab') {
      e.preventDefault();
      props.onCyclePort(e.shiftKey ? -1 : 1);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (items[index]) {
        props.onPick(items[index]);
      }
    } else if (e.key === 'Escape') {
      e.preventDefault();
      props.onClose();
    }
  };

  const port = props.port;
  return (
    <div
      className="verknode-quickadd"
      style={{ left: props.x, top: props.y }}
      onMouseDown={e => e.stopPropagation()}
      onPointerDown={e => e.stopPropagation()}
    >
      <div className="verknode-quickadd-head">
        {props.source ? (
          <span title={props.portCount > 1 ? 'Tab / Shift+Tab: other output port' : undefined}>
            from <b>{props.source.name}</b>
            {port && (
              <span className={`verknode-quickadd-port verknode-quickadd-port-${port.type}`}>
                {port.implicit ? 'flow variables' : `${port.index}: ${port.name}`}
                {props.portCount > 1 && <span className="verknode-quickadd-tab"> Tab ⇆</span>}
              </span>
            )}
          </span>
        ) : (
          <span>add node</span>
        )}
      </div>
      <input
        ref={input}
        className="verknode-quickadd-input"
        placeholder="type to search nodes…"
        value={props.query}
        onChange={e => props.onQuery(e.target.value)}
        onKeyDown={onKeyDown}
        onBlur={() => window.setTimeout(props.onClose, 120)}
        spellCheck={false}
      />
      <div className="verknode-quickadd-list">
        {shown.map((n, i) => (
          <div
            key={n.factory}
            className={`verknode-quickadd-item ${first + i === index ? 'verknode-quickadd-active' : ''}`}
            onMouseEnter={() => props.onIndex(first + i)}
            onMouseDown={e => {
              e.preventDefault();
              props.onPick(n);
            }}
          >
            <NodeIcon nodeType={n.nodeType} icon={n.icon} size={18} category={n.category} />
            <span className="verknode-quickadd-name">{n.name}</span>
            <span className="verknode-quickadd-cat">{n.category.split('/').slice(-1)[0]}</span>
          </div>
        ))}
        {!items.length && <div className="verknode-quickadd-empty">No matching node</div>}
      </div>
      <div className="verknode-quickadd-foot">
        {items.length ? `${index + 1}/${items.length}` : ''} · ↑↓ select · Enter add{props.portCount > 1 ? ' · Tab port' : ''} ·
        Esc
      </div>
    </div>
  );
}
