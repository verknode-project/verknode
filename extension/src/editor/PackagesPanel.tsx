import React, { useEffect, useRef, useState } from 'react';

import { postJSON, requestAPI } from '../api';

/** A verknode package (engine/packages.py): installed, or in the index. */
interface Installed {
  name: string;
  title: string;
  version: string;
  dist: string;
  api: string;
  description: string;
  homepage: string;
  status: 'loaded' | 'incompatible' | 'error';
  error: string;
  nodes: number;
  managed: boolean;
}

interface Available {
  name: string;
  title: string;
  description: string;
  homepage: string;
  installed: boolean;
  latest: string | null;
  newest: string | null;
}

interface Job {
  id: string;
  kind: string;
  args: any[];
  state: 'running' | 'done' | 'failed';
  log: string[];
}

interface Status {
  api: string;
  installed: Installed[];
  available: Available[];
  updates: { name: string; current: string; latest: string }[];
  indexError: string;
  indexUrls: string[];
  policy: string;
  env: string;
  server: boolean;
  jobs: Job[];
}

/**
 * Settings › Packages: the verknode packages of the Jupyter server - install from the index (the newest
 * version that fits this verknode) or a pip requirement, update, uninstall; nodes come and go at once.
 */
export function PackagesPanel() {
  const [st, setSt] = useState<Status | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [spec, setSpec] = useState('');
  const logEnd = useRef<HTMLDivElement>(null);
  const load = (refresh = false) =>
    requestAPI<Status>(`packages${refresh ? '?refresh=1' : ''}`)
      .then(s => {
        setSt(s);
        setError(null);
      })
      .catch(e => setError(e.message));
  useEffect(() => {
    void load();
  }, []);
  useEffect(() => {
    if (!job || job.state !== 'running') {
      return;
    }
    const t = window.setTimeout(() => {
      requestAPI<Job>(`packages/jobs/${job.id}`)
        .then(j => {
          setJob(j);
          if (j.state !== 'running') {
            void load();
          }
        })
        .catch(e => setError(e.message));
    }, 800);
    return () => window.clearTimeout(t);
  }, [job]);
  useEffect(() => {
    logEnd.current?.scrollIntoView({ block: 'nearest' });
  }, [job?.log.length]);

  const act = (action: string, name = '') =>
    postJSON<{ job: string }>('packages', { action, name })
      .then(r => setJob({ id: r.job, kind: action, args: [name], state: 'running', log: [] }))
      .catch(e => setError(e.message));
  const busy = job?.state === 'running';

  if (!st) {
    return <div className="verknode-empty">{error ?? 'Loading…'}</div>;
  }
  if (!st.server) {
    return (
      <div className="verknode-empty">
        Packages are installed on a Jupyter server: the browser version has the nodes it was built with.
      </div>
    );
  }
  const update = new Map(st.updates.map(u => [u.name, u]));
  return (
    <div className="verknode-packages">
      <p className="verknode-field-help">
        Packages add nodes, port types, canvas widgets and views (docs/packages.md). They are installed for the Jupyter
        server in {st.env}, and their nodes are there at once. This verknode's node API: {st.api}. Updates:{' '}
        <b>{st.policy}</b> (Settings › Node defaults › Packages).
      </p>
      <div className="verknode-packages-bar">
        <button className="jp-Button jp-mod-styled" disabled={busy} onClick={() => void load(true)}>
          Check for updates
        </button>
        {st.updates.length > 0 && (
          <button className="jp-Button jp-mod-styled jp-mod-accept" disabled={busy} onClick={() => void act('update')}>
            Update all ({st.updates.length})
          </button>
        )}
        {error && <span className="verknode-error-text">{error}</span>}
      </div>

      <div className="verknode-field-section">Installed</div>
      {st.installed.length ? (
        <table className="verknode-data-table verknode-packages-table">
          <tbody>
            {st.installed.map(p => {
              const u = update.get(p.name);
              return (
                <tr key={p.name}>
                  <td>
                    <b>{p.title}</b> <span className="verknode-mono">{p.name}</span>
                    <div className="verknode-field-help">{p.description}</div>
                    {p.status !== 'loaded' && <pre className="verknode-packages-error">{p.error}</pre>}
                  </td>
                  <td className="verknode-mono">{p.version}</td>
                  <td>
                    <span className={`verknode-packages-status verknode-packages-${p.status}`}>{p.status}</span>
                    {p.status === 'loaded' && <div className="verknode-field-help">{p.nodes} nodes</div>}
                  </td>
                  <td className="verknode-packages-actions">
                    {u && (
                      <button className="jp-Button jp-mod-styled jp-mod-accept" disabled={busy} onClick={() => void act('update', p.name)}>
                        Update to {u.latest}
                      </button>
                    )}
                    {p.managed && (
                      <button className="jp-Button jp-mod-styled" disabled={busy} onClick={() => void act('uninstall', p.name)}>
                        Uninstall
                      </button>
                    )}
                    {p.homepage && (
                      <a href={p.homepage} target="_blank" rel="noreferrer">
                        home
                      </a>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : (
        <div className="verknode-field-help">No packages yet.</div>
      )}

      <div className="verknode-field-section">
        Available
        <div className="verknode-field-section-note">
          From {st.indexUrls.join(', ')}
          {st.indexError && <span className="verknode-error-text"> - {st.indexError}</span>}
        </div>
      </div>
      <table className="verknode-data-table verknode-packages-table">
        <tbody>
          {st.available.map(p => (
            <tr key={p.name}>
              <td>
                <b>{p.title}</b> <span className="verknode-mono">{p.name}</span>
                <div className="verknode-field-help">{p.description}</div>
              </td>
              <td className="verknode-mono">
                {p.latest ?? <span title={`newest: ${p.newest}`}>none for this verknode</span>}
              </td>
              <td className="verknode-packages-actions">
                <button
                  className="jp-Button jp-mod-styled jp-mod-accept"
                  disabled={busy || p.installed || !p.latest}
                  onClick={() => void act('install', p.name)}
                >
                  {p.installed ? 'Installed' : 'Install'}
                </button>
                {p.homepage && (
                  <a href={p.homepage} target="_blank" rel="noreferrer">
                    home
                  </a>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="verknode-packages-bar">
        <input
          className="jp-mod-styled"
          placeholder='Or a pip requirement: "name @ git+https://github.com/…", a wheel URL, a folder'
          value={spec}
          onChange={e => setSpec(e.target.value)}
        />
        <button className="jp-Button jp-mod-styled" disabled={busy || !spec.trim()} onClick={() => void act('install', spec.trim())}>
          Install
        </button>
      </div>

      {job && (
        <div className="verknode-packages-job">
          <div className="verknode-field-section">
            {job.kind} {job.args.filter(Boolean).join(' ')}: {job.state === 'running' ? 'working…' : job.state}
          </div>
          <pre className="verknode-packages-log">
            {job.log.join('\n')}
            <div ref={logEnd} />
          </pre>
        </div>
      )}
    </div>
  );
}
