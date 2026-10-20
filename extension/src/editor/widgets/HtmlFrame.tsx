import React, { useEffect, useMemo, useRef } from 'react';
import { withBaseUrl } from './frames';

/**
 * A page of a custom widget node ("html" control or display), run in a
 * sandboxed frame (scripts allowed, no access to JupyterLab). The page gets
 * `verknode.value` (the widget's value) and `verknode.setValue(v)` to change
 * it (input widgets).
 */
export function HtmlFrame(props: {
  html: string;
  value?: any;
  width: number;
  height: number;
  onValue?: (v: any) => void;
  /** fill the parent (port views) instead of width x height */
  fill?: boolean;
  /** the page keeps running when the value changes (it made the change itself, e.g. an editor's placement) */
  keep?: boolean;
}) {
  const frame = useRef<HTMLIFrameElement>(null);
  const onValue = useRef(props.onValue);
  onValue.current = props.onValue;
  const first = useRef(props.value);
  const shownValue = props.keep ? first.current : props.value;
  const doc = useMemo(() => {
    const value = JSON.stringify(shownValue ?? null).replace(/</g, '\\u003c');
    return (
      '<!doctype html><meta charset="utf-8"><script>window.verknode = { value: ' +
      value +
      ', setValue: function (v) { parent.postMessage({ verknodeWidget: true, value: v }, "*"); } };</script>' +
      withBaseUrl(props.html)
    );
  }, [props.html, JSON.stringify(shownValue)]);
  useEffect(() => {
    const listen = (e: MessageEvent) => {
      if (e.source === frame.current?.contentWindow && e.data?.verknodeWidget) {
        onValue.current?.(e.data.value);
      }
    };
    window.addEventListener('message', listen);
    return () => window.removeEventListener('message', listen);
  }, []);
  return (
    <iframe
      ref={frame}
      className="verknode-widget-frame nodrag nowheel"
      sandbox="allow-scripts"
      srcDoc={doc}
      style={props.fill ? { width: '100%', height: '100%', border: 0 } : { width: props.width, height: props.height }}
      title="Widget"
    />
  );
}
