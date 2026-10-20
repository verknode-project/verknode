import React, { useEffect, useState } from 'react';

import { postJSON, requestAPI } from '../api';
import { FieldInfo } from '../types';
import { FieldRow } from './ConfigureDialog';

/** A section of global node defaults (engine/defaults.py), e.g. the saved LLM servers. */
interface DefaultsSection {
  key: string;
  title: string;
  description: string;
  fields: FieldInfo[];
  values: Record<string, any>;
}

/**
 * Settings › Node defaults: setup that nodes share across workflows, saved per user on the server
 * (~/.verknode/defaults.json), not in the workflow.
 */
export function NodeDefaultsPanel() {
  const [sections, setSections] = useState<DefaultsSection[] | null>(null);
  const [values, setValues] = useState<Record<string, Record<string, any>>>({});
  const [status, setStatus] = useState<{ error?: string; saved?: boolean } | null>(null);
  const show = (secs: DefaultsSection[]) => {
    setSections(secs);
    setValues(Object.fromEntries(secs.map(s => [s.key, JSON.parse(JSON.stringify(s.values))])));
  };
  useEffect(() => {
    requestAPI<DefaultsSection[]>('defaults')
      .then(show)
      .catch(e => setStatus({ error: e.message }));
  }, []);
  if (!sections) {
    return <div className="verknode-empty">{status?.error ?? 'Loading…'}</div>;
  }
  if (!sections.length) {
    return <div className="verknode-empty">No node has defaults to set up.</div>;
  }
  const dirty = sections.some(s => JSON.stringify(s.values) !== JSON.stringify(values[s.key]));
  const save = () =>
    postJSON<DefaultsSection[]>('defaults', { values })
      .then(secs => {
        show(secs);
        setStatus({ saved: true });
      })
      .catch(e => setStatus({ error: e.message }));
  return (
    <div className="verknode-node-defaults">
      <p className="verknode-field-help">
        Setup that nodes share across all workflows, saved for you on the Jupyter server
        (~/.verknode/defaults.json), not in the workflow. Store names of environment variables here, never keys.
      </p>
      {sections.map(sec => {
        const vals = values[sec.key] ?? {};
        return (
          <div key={sec.key} className="verknode-node-defaults-section">
            <div className="verknode-field-section">
              {sec.title}
              {sec.description && <div className="verknode-field-section-note">{sec.description}</div>}
            </div>
            {sec.fields.map(f => (
              <FieldRow
                key={f.key}
                field={f}
                value={vals[f.key]}
                ctx={{ inSpecs: [], flowVariables: [], settings: vals }}
                onChange={v => {
                  setStatus(null);
                  setValues(all => ({ ...all, [sec.key]: { ...all[sec.key], [f.key]: v } }));
                }}
              />
            ))}
          </div>
        );
      })}
      <div className="verknode-node-defaults-actions">
        <button className="jp-Dialog-button jp-mod-accept jp-mod-styled" disabled={!dirty} onClick={() => void save()}>
          <div className="jp-Dialog-buttonLabel">Save</div>
        </button>
        {status?.saved && !dirty && <span className="verknode-field-help">Saved.</span>}
        {status?.error && <span className="verknode-error-text">{status.error}</span>}
      </div>
    </div>
  );
}
