import React, { useEffect, useRef } from 'react';

// open dialogs, innermost last: only the innermost one closes on Escape
const openDialogs: number[] = [];
let nextDialog = 0;

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), ' +
  'textarea:not([disabled]), iframe, [tabindex]:not([tabindex="-1"])';
// inputs where Enter means "done", as in any dialog (not buttons, text areas or lists, which use it)
const SUBMIT_INPUTS = ['text', 'number', 'search', 'email', 'url', 'password', 'tel', 'date', 'datetime-local', 'time', 'checkbox', 'radio', 'range', 'color'];

/** The elements of a dialog that Tab goes through, in order (visible ones only). */
export function focusables(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(el => el.offsetParent !== null || el === root.ownerDocument.activeElement);
}

/**
 * Dialog overlay inside the workflow editor.
 */
export function Modal(props: {
  title: string;
  onClose: () => void;
  /** the default button: Ctrl+Enter anywhere, Enter in a field (a dialog's OK) */
  onSubmit?: () => void;
  footer?: React.ReactNode;
  className?: string;
  children: React.ReactNode;
}) {
  const id = useRef(++nextDialog);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    openDialogs.push(id.current);
    // the keyboard goes into the dialog (unless a field took it, autoFocus), and back where it was after
    const doc = box.current?.ownerDocument;
    const before = doc?.activeElement as HTMLElement | null;
    if (box.current && !box.current.contains(doc?.activeElement ?? null)) {
      box.current.focus({ preventScroll: true });
    }
    return () => {
      openDialogs.splice(openDialogs.indexOf(id.current), 1);
      if (before?.isConnected && before !== doc?.body) {
        before.focus({ preventScroll: true });
      }
    };
  }, []);
  const onKeyDown = (e: React.KeyboardEvent) => {
    e.stopPropagation();
    if (e.defaultPrevented || !box.current) {
      return; // (a field used the key, e.g. Enter in a list)
    }
    const target = e.target as HTMLElement;
    if (e.key === 'Tab' && !e.ctrlKey && !e.altKey && !e.metaKey) {
      // Tab stays in the dialog: from the last element to the first, and back with Shift
      const all = focusables(box.current);
      if (!all.length) {
        return;
      }
      const i = all.indexOf(target);
      if (e.shiftKey && (i <= 0)) {
        e.preventDefault();
        all[all.length - 1].focus();
      } else if (!e.shiftKey && (i === -1 || i === all.length - 1)) {
        e.preventDefault();
        all[0].focus();
      }
    } else if (e.key === 'Enter' && props.onSubmit && !e.shiftKey && !e.altKey && !e.ctrlKey && !e.metaKey) {
      const field =
        (target.tagName === 'INPUT' && SUBMIT_INPUTS.includes((target as HTMLInputElement).type)) || target.tagName === 'SELECT';
      if (field) {
        e.preventDefault();
        props.onSubmit();
      }
    }
  };
  // Ctrl+Enter is OK from anywhere, also from fields that keep their keys (code editors): caught first
  const onKeyDownCapture = (e: React.KeyboardEvent) => {
    // (a dialog opened from this one has its own OK)
    if ((e.target as HTMLElement).closest('.verknode-modal') !== box.current) {
      return;
    }
    if (props.onSubmit && e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey) {
      e.preventDefault();
      e.stopPropagation();
      props.onSubmit();
    }
  };
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
        ref={box}
        role="dialog"
        aria-modal="true"
        aria-label={props.title}
        tabIndex={-1}
        className={`verknode-modal ${props.className ?? ''}`}
        onKeyDown={onKeyDown}
        onKeyDownCapture={onKeyDownCapture}
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
