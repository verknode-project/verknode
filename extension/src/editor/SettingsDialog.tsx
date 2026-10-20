import React, { useEffect, useState } from 'react';

import { WorkflowController } from '../controller';
import { EditorPreferences, EditorSettings } from '../settings';
import { CustomNodesPanel } from './CustomNodesPanel';
import { NodeDefaultsPanel } from './NodeDefaultsPanel';
import { Modal } from './Modal';

type Option<T> = { value: T; label: string };

const DRAG: Option<EditorPreferences['dragBehavior']>[] = [
  { value: 'pan', label: 'Move the view (Shift+drag draws a selection rectangle)' },
  { value: 'select', label: 'Draw a selection rectangle (Ctrl+drag moves the view)' }
];
const NAVIGATION: Option<EditorPreferences['arrowNavigation']>[] = [
  { value: 'approximate', label: 'Approximate: closest parent / child, siblings above or below by position' },
  { value: 'strict', label: 'Strict: first parent, first child and sibling order by ports' }
];
const WHEEL: Option<EditorPreferences['wheelBehavior']>[] = [
  { value: 'zoom', label: 'Zoom in / out (pan with Ctrl+drag or middle-drag)' },
  { value: 'pan', label: 'Scroll the canvas (Ctrl+wheel zooms)' }
];
const VIEWS: Option<EditorPreferences['viewTarget']>[] = [
  { value: 'window', label: 'Separate browser window' },
  { value: 'tab', label: 'Browser tab' },
  { value: 'dialog', label: 'Dialog inside the editor' },
  { value: 'jupyter', label: 'JupyterLab tab (dockable next to the editor)' }
];
const THEME_OPTIONS: Option<EditorPreferences['theme']>[] = [
  { value: 'verkaround', label: 'Verkaround - coloured by what nodes do' },
  { value: 'knife', label: 'Knife - popular workflow engine colours, light' },
  { value: 'butter', label: 'Butter - Knife, a little muted, on cream' },
  { value: 'haze', label: 'Haze - Knife, muted' },
  { value: 'knightmare', label: 'Knightmare - popular workflow engine colours, dark' },
  { value: 'pipeline-cowboy', label: 'Pipeline Cowboy - classic workflow engine look' }
];
const LIGHTS: Option<EditorPreferences['statusLight']>[] = [
  { value: 'led', label: 'LED - _/_ red, / / yellow, /// green' },
  { value: 'led-orange', label: 'LED orange - as LED, with reddish orange for red' },
  { value: 'traffic', label: 'Traffic light - one red, yellow or green dot' }
];
const SKETCHERS: Option<EditorPreferences['sketcher']>[] = [
  { value: 'builtin', label: 'Built-in sketcher (hotkeys, fixed geometry)' },
  { value: 'ketcher', label: 'Ketcher (EPAM)' }
];

function Row(props: { label: string; help?: string; children: React.ReactNode }) {
  return (
    <div className="verknode-field">
      <label>{props.label}</label>
      <div className="verknode-field-editor">
        {props.children}
        {props.help && <div className="verknode-field-help">{props.help}</div>}
      </div>
    </div>
  );
}

/**
 * Editor settings (per user, stored in JupyterLab's settings and applied to
 * all workflow editors) and the custom node setup of this workflow.
 */
export function SettingsDialog(props: { controller: WorkflowController; settings: EditorSettings; onClose: () => void }) {
  const { settings } = props;
  const [values, setValues] = useState(settings.values);
  const [tab, setTab] = useState<'editor' | 'defaults' | 'nodes'>('editor');
  useEffect(() => {
    const onChange = (_: unknown, v: EditorPreferences) => setValues(v);
    settings.changed.connect(onChange);
    return () => {
      settings.changed.disconnect(onChange);
    };
  }, [settings]);
  const set = <K extends keyof EditorPreferences>(key: K, value: EditorPreferences[K]) => void settings.set(key, value);
  const check = (
    key: 'showFlowVariablePorts' | 'showScopeBlocks' | 'snapToGrid' | 'showMinimap' | 'showPythonCommands',
    label: string
  ) => (
    <label className="verknode-settings-check">
      <input type="checkbox" checked={values[key]} onChange={e => set(key, e.target.checked)} /> {label}
    </label>
  );

  return (
    <Modal
      title="Settings"
      onClose={props.onClose}
      className="verknode-settings-dialog"
      footer={
        <button className="jp-Dialog-button jp-mod-reject jp-mod-styled" onClick={props.onClose}>
          <div className="jp-Dialog-buttonLabel">Close</div>
        </button>
      }
    >
      <div className="verknode-tabs">
        <button className={`verknode-tab ${tab === 'editor' ? 'verknode-tab-active' : ''}`} onClick={() => setTab('editor')}>
          Editor
        </button>
        <button className={`verknode-tab ${tab === 'defaults' ? 'verknode-tab-active' : ''}`} onClick={() => setTab('defaults')}>
          Node defaults
        </button>
        <button className={`verknode-tab ${tab === 'nodes' ? 'verknode-tab-active' : ''}`} onClick={() => setTab('nodes')}>
          Custom nodes
        </button>
      </div>
      {tab === 'defaults' && (
        <div className="verknode-tab-body">
          <NodeDefaultsPanel />
        </div>
      )}
      {tab === 'editor' && (
        <div className="verknode-tab-body">
          <p className="verknode-field-help">
            These settings apply to all workflow editors and are saved with your JupyterLab settings (also editable in
            Settings › Settings Editor › Verknode Workflow Editor).
          </p>
          <Row label="Left-drag on empty canvas">
            {DRAG.map(o => (
              <label key={o.value} className="verknode-settings-check">
                <input
                  type="radio"
                  checked={values.dragBehavior === o.value}
                  onChange={() => set('dragBehavior', o.value)}
                />{' '}
                {o.label}
              </label>
            ))}
          </Row>
          <Row
            label="Arrow keys"
            help="Arrow keys select the nearest node in that direction. Hold Alt to follow the connections instead (lineage: ← / → an upstream / downstream node, ↑ / ↓ a sibling); double-tap Alt to stay in lineage mode until Alt is tapped again or nothing is selected. Shift extends the selection. This setting picks how lineage chooses among several candidates."
          >
            {NAVIGATION.map(o => (
              <label key={o.value} className="verknode-settings-check">
                <input
                  type="radio"
                  checked={values.arrowNavigation === o.value}
                  onChange={() => set('arrowNavigation', o.value)}
                />{' '}
                {o.label}
              </label>
            ))}
          </Row>
          <Row label="Mouse wheel">
            {WHEEL.map(o => (
              <label key={o.value} className="verknode-settings-check">
                <input
                  type="radio"
                  checked={values.wheelBehavior === o.value}
                  onChange={() => set('wheelBehavior', o.value)}
                />{' '}
                {o.label}
              </label>
            ))}
          </Row>
          <Row label="Open port and node views in" help="Holding Ctrl (Cmd) switches between window and tab.">
            <select
              className="jp-mod-styled"
              value={values.viewTarget}
              onChange={e => set('viewTarget', e.target.value as EditorPreferences['viewTarget'])}
            >
              {VIEWS.map(o => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </Row>
          <Row label="Theme" help="Colours of the canvas and nodes.">
            <select className="jp-mod-styled" value={values.theme} onChange={e => set('theme', e.target.value as EditorPreferences['theme'])}>
              {THEME_OPTIONS.map(o => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </Row>
          <Row label="Status lights" help="How a node shows its state (not configured, configured, executed).">
            <select
              className="jp-mod-styled"
              value={values.statusLight}
              onChange={e => set('statusLight', e.target.value as EditorPreferences['statusLight'])}
            >
              {LIGHTS.map(o => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </Row>
          <Row label="Molecule Sketcher" help="Structure editor opened first; the sketcher dialog can switch.">
            <select
              className="jp-mod-styled"
              value={values.sketcher}
              onChange={e => set('sketcher', e.target.value as EditorPreferences['sketcher'])}
            >
              {SKETCHERS.map(o => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </Row>
          <Row label="Canvas">
            {check('showFlowVariablePorts', 'Always show flow variable ports')}
            {check('showScopeBlocks', 'Highlight loops, try/catch and switch blocks')}
            {check('snapToGrid', 'Snap nodes to grid')}
            {check('showMinimap', 'Show minimap')}
          </Row>
          <Row label="Console">{check('showPythonCommands', 'Show the Python command for every edit')}</Row>
        </div>
      )}
      {tab === 'nodes' && (
        <div className="verknode-tab-body">
          <CustomNodesPanel controller={props.controller} />
        </div>
      )}
    </Modal>
  );
}
