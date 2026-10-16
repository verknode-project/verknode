import { ISettingRegistry } from '@jupyterlab/settingregistry';
import { ISignal, Signal } from '@lumino/signaling';

/** User preferences of the workflow editor (schema/plugin.json). */
export interface EditorPreferences {
  dragBehavior: 'pan' | 'select';
  arrowNavigation: 'approximate' | 'strict';
  wheelBehavior: 'zoom' | 'pan';
  showFlowVariablePorts: boolean;
  showScopeBlocks: boolean;
  snapToGrid: boolean;
  showMinimap: boolean;
  viewTarget: 'window' | 'tab' | 'dialog' | 'jupyter';
  showPythonCommands: boolean;
  /** structure editor of the Molecule Sketcher */
  sketcher: 'ketcher' | 'builtin';
  /** node status lights: three slashes (LED, or LED with reddish orange for red) or traffic light dots */
  statusLight: 'led' | 'led-orange' | 'traffic';
  /** colour theme of the canvas and nodes (themes.ts) */
  theme: 'verkaround' | 'knife' | 'butter' | 'haze' | 'knightmare' | 'pipeline-cowboy';
}

export const DEFAULT_PREFERENCES: EditorPreferences = {
  dragBehavior: 'pan',
  arrowNavigation: 'approximate',
  wheelBehavior: 'zoom',
  showFlowVariablePorts: false,
  showScopeBlocks: true,
  snapToGrid: true,
  showMinimap: true,
  viewTarget: 'window',
  showPythonCommands: true,
  sketcher: 'ketcher',
  theme: 'verkaround',
  statusLight: 'led-orange'
};

/**
 * Editor preferences backed by the JupyterLab settings registry (so they are
 * stored per user and shown in the Settings Editor); shared by all editors.
 */
export class EditorSettings {
  constructor(settings: ISettingRegistry.ISettings | null) {
    this._settings = settings;
    if (settings) {
      this._read();
      settings.changed.connect(() => {
        this._read();
        this._changed.emit(this._values);
      });
    }
  }

  get values(): EditorPreferences {
    return this._values;
  }

  get changed(): ISignal<this, EditorPreferences> {
    return this._changed;
  }

  async set<K extends keyof EditorPreferences>(key: K, value: EditorPreferences[K]): Promise<void> {
    if (this._settings) {
      await this._settings.set(key, value as any);
    } else {
      this._values = { ...this._values, [key]: value };
      this._changed.emit(this._values);
    }
  }

  private _read(): void {
    const composite = (this._settings?.composite ?? {}) as Partial<EditorPreferences>;
    this._values = { ...DEFAULT_PREFERENCES, ...composite };
  }

  private _settings: ISettingRegistry.ISettings | null;
  private _values: EditorPreferences = { ...DEFAULT_PREFERENCES };
  private _changed = new Signal<this, EditorPreferences>(this);
}
