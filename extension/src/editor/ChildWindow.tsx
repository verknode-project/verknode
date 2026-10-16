import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

import { Modal } from './Modal';

/**
 * Open an empty same-origin browser window for a view: a separate popup
 * window (movable to another screen, resizable) or, with `tab`, a new tab.
 * Must be called from a user gesture. Returns null if the browser blocked it.
 */
export function openChildWindow(tab: boolean, width = 1000, height = 720, from: Window = window): Window | null {
  // `from`: the window that got the click (a child window can only open windows itself)
  const features = tab
    ? undefined
    : `popup=yes,width=${width},height=${height},left=${window.screenX + 60},top=${window.screenY + 60}`;
  return from.open('', '_blank', features);
}

function copyStyles(from: Document, to: Document): void {
  to.head.innerHTML = '<meta charset="utf-8">';
  from.querySelectorAll('style, link[rel="stylesheet"]').forEach(node => {
    const clone = node.cloneNode(true) as HTMLElement;
    if (node instanceof HTMLLinkElement) {
      (clone as HTMLLinkElement).href = node.href; // absolute: about:blank has no base
    }
    to.head.appendChild(clone);
  });
  // theme (data-jp-theme-light, data-jp-theme-name, ...)
  for (const attr of Array.from(from.body.attributes)) {
    to.body.setAttribute(attr.name, attr.value);
  }
}

/**
 * Renders children into a child browser window (React portal), so the view
 * keeps updating with the workflow. Closing the window calls onClose;
 * unmounting closes the window.
 */
export function ChildWindow(props: { win: Window; title: string; onClose: () => void; children: React.ReactNode }) {
  const { win } = props;
  const [container, setContainer] = useState<HTMLElement | null>(null);
  const onClose = useRef(props.onClose);
  onClose.current = props.onClose;

  useLayoutEffect(() => {
    const doc = win.document;
    copyStyles(document, doc);
    doc.body.innerHTML = '';
    doc.body.style.margin = '0';
    const div = doc.createElement('div');
    div.className = 'verknode-editor verknode-window';
    doc.body.appendChild(div);
    setContainer(div);
    const closed = () => onClose.current();
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && win.close();
    win.addEventListener('pagehide', closed);
    win.addEventListener('keydown', onKey);
    return () => {
      win.removeEventListener('pagehide', closed);
      win.removeEventListener('keydown', onKey);
      if (!win.closed) {
        win.close();
      }
    };
  }, [win]);

  useEffect(() => {
    win.document.title = props.title;
  }, [win, props.title]);

  return container ? createPortal(props.children, container) : null;
}

/**
 * Frame of a view: in-editor dialog, or full page inside a child window.
 */
/** Where a view is shown: editor dialog, child browser window, or JupyterLab tab. */
export type ViewFrameKind = 'modal' | 'window' | 'panel';

/** Where a view can be shown. */
export type ViewPlace = 'dialog' | 'window' | 'tab' | 'jupyter';

/** A button that moves a view elsewhere. */
export interface ViewMove {
  label: string;
  title: string;
  action: () => void;
}

const PLACES: Record<ViewPlace, { label: string; title: string }> = {
  window: { label: '⧉ Window', title: 'Show this view in a separate browser window' },
  tab: { label: '⇗ Browser tab', title: 'Show this view in a new browser tab' },
  jupyter: { label: '⇱ JupyterLab tab', title: 'Show this view in a JupyterLab tab (dock, split, keep it next to the editor)' },
  dialog: { label: '▣ Dialog', title: 'Show this view in a dialog inside the editor' }
};

/** Buttons moving a view from `current` to the other places in `available`. */
export function viewMoves(current: ViewPlace, available: ViewPlace[], move: (to: ViewPlace) => void): ViewMove[] {
  return available.filter(p => p !== current).map(p => ({ ...PLACES[p], action: () => move(p) }));
}

export function ViewFrame(props: {
  frame: ViewFrameKind;
  title: string;
  className?: string;
  onClose: () => void;
  onTitle?: (title: string) => void;
  /** other places the view can move to (window, browser tab, JupyterLab tab, dialog) */
  moves?: ViewMove[];
  children: React.ReactNode;
}) {
  useEffect(() => {
    props.onTitle?.(props.title);
  }, [props.title]);
  const dock = props.moves?.length ? (
    <div className="verknode-view-dock">
      {props.moves.map(m => (
        <button key={m.label} className="verknode-tb" onClick={m.action} title={m.title}>
          {m.label}
        </button>
      ))}
    </div>
  ) : null;
  if (props.frame === 'modal') {
    return (
      <Modal title={props.title} onClose={props.onClose} className={props.className}>
        {dock}
        {props.children}
      </Modal>
    );
  }
  if (props.frame === 'panel') {
    // the JupyterLab tab shows the title
    return (
      <div className={`verknode-window-root verknode-panel-root ${props.className ?? ''}`}>
        {dock && <div className="verknode-panel-bar">{dock}</div>}
        <div className="verknode-window-body">{props.children}</div>
      </div>
    );
  }
  return (
    <div className="verknode-window-root">
      <div className="verknode-window-title">
        {props.title}
        {dock}
      </div>
      <div className="verknode-window-body">{props.children}</div>
    </div>
  );
}

/** The workflow version, re-rendering on every change (views outside the editor's React tree). */
export function useWorkflowVersion(controller: { view: { version: number } | null; changed: any }): number {
  const [version, setVersion] = useState(controller.view?.version ?? 0);
  useEffect(() => {
    const on = (_: unknown, v: { version: number }) => setVersion(v.version);
    controller.changed.connect(on);
    return () => {
      controller.changed.disconnect(on);
    };
  }, [controller]);
  return version;
}
