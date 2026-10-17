import React, { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react';

import { ketcherAvailable, ketcherUrl } from './api';

export interface KetcherHandle {
  /** the drawn structure as a V2000 molfile */
  getMolfile(): Promise<string>;
}

/**
 * Ketcher (EPAM) in an iframe served by the verknode server extension; the
 * editor is reached through the frame's `window.ketcher` API (same origin).
 */
export const KetcherFrame = forwardRef<KetcherHandle, { initial: string; version?: number }>((props, ref) => {
  const frame = useRef<HTMLIFrameElement>(null);
  const [state, setState] = useState<'checking' | 'loading' | 'ready' | 'missing' | 'failed'>('checking');
  const api = () => (frame.current?.contentWindow as any)?.ketcher;

  useEffect(() => {
    void ketcherAvailable().then(ok => setState(ok ? 'loading' : 'missing'));
  }, []);

  // a new structure for the running editor (next molecule, pasted text): `version` changes
  const shown = useRef(props.version);
  useEffect(() => {
    const k = api();
    if (state !== 'ready' || !k || shown.current === props.version) {
      return;
    }
    shown.current = props.version;
    Promise.resolve(props.initial.trim() ? k.setMolecule(props.initial) : k.editor?.clear?.() ?? k.setMolecule('')).catch(
      () => undefined
    );
  }, [props.version, state]);

  const onLoad = () => {
    // Ketcher initializes asynchronously after the page has loaded
    const started = Date.now();
    const wait = () => {
      const k = api();
      if (k?.editor && typeof k.setMolecule === 'function') {
        const init = props.initial.trim() ? k.setMolecule(props.initial) : Promise.resolve();
        Promise.resolve(init)
          .catch(() => undefined)
          .then(() => setState('ready'));
      } else if (Date.now() - started > 60000) {
        setState('failed');
      } else {
        window.setTimeout(wait, 200);
      }
    };
    wait();
  };

  useImperativeHandle(ref, () => ({
    async getMolfile() {
      const k = api();
      if (!k) {
        throw new Error('Ketcher is not ready');
      }
      try {
        return await k.getMolfile('v2000');
      } catch {
        return await k.getMolfile();
      }
    }
  }));

  if (state === 'missing') {
    return (
      <div className="verknode-ketcher-missing">
        Ketcher is not installed on the server. Rebuild the image (<code>make -C docker build</code>) - docker/Dockerfile
        downloads it to <code>/opt/ketcher</code> - or use the built-in sketcher.
      </div>
    );
  }
  return (
    <div className="verknode-ketcher">
      {state !== 'checking' && <iframe ref={frame} src={ketcherUrl()} title="Ketcher" onLoad={onLoad} />}
      {(state === 'checking' || state === 'loading') && <div className="verknode-ketcher-wait">Loading Ketcher…</div>}
      {state === 'failed' && <div className="verknode-ketcher-wait">Ketcher did not start.</div>}
    </div>
  );
});
