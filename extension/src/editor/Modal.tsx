import React, { useEffect, useRef } from 'react';

// open dialogs, innermost last: only the innermost one closes on Escape
const openDialogs: number[] = [];
let nextDialog = 0;

/**
 * Dialog overlay inside the workflow editor.
 */
export function Modal(props: {
  title: string;
  onClose: () => void;
  footer?: React.ReactNode;
  className?: string;
  children: React.ReactNode;
}) {
  const id = useRef(++nextDialog);
  useEffect(() => {
    openDialogs.push(id.current);
    return () => {
      openDialogs.splice(openDialogs.indexOf(id.current), 1);
    };
  }, []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // (areas marked data-keep-escape use Escape themselves, e.g. to cancel a drawing action)
      const own = (e.target as Element | null)?.closest?.('[data-keep-escape]');
      if (e.key === 'Escape' && !own && openDialogs[openDialogs.length - 1] === id.current) {
        e.stopPropagation();
        props.onClose();
      }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [props.onClose]);
  return (
    <div className="verknode-modal-backdrop" onMouseDown={e => e.target === e.currentTarget && props.onClose()}>
      <div
        className={`verknode-modal ${props.className ?? ''}`}
        onKeyDown={e => e.stopPropagation()}
        onContextMenu={e => e.stopPropagation()}
      >
        <div className="verknode-modal-title">
          <span>{props.title}</span>
          <button className="verknode-icon-btn" onClick={props.onClose} title="Close">
            ✕
          </button>
        </div>
        <div className="verknode-modal-body">{props.children}</div>
        {props.footer && <div className="verknode-modal-footer">{props.footer}</div>}
      </div>
    </div>
  );
}
