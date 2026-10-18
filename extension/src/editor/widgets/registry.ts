import type React from 'react';

/** Props of a canvas control of input widget nodes (InputWidget.control). */
export interface InputControlProps {
  value: any;
  /** InputWidget.control_props */
  props: Record<string, any>;
  /** column name of the value */
  name: string;
  /** page of an "html" control */
  html?: string;
  disabled: boolean;
  /** store a new value (null: back to the default) */
  onChange: (value: any) => void;
  /** open the structure sketcher (molecule control) */
  onSketch?: () => void;
  /** size the user gave the widget (undefined: the control's own size) */
  width?: number;
  height?: number;
}

/** Props of a canvas display of output widget nodes (OutputWidget.renderer). */
export interface OutputRendererProps {
  /** what OutputWidget.render returned */
  data: any;
  width: number;
  height: number;
  /** interactive displays (OutputWidget.interactive): the selection and how to change it (null: clear) */
  selection?: any;
  onSelect?: (selection: any) => void;
  /** send the node something while it runs and waits for it (ExecContext.wait_for_input), e.g. a chat message */
  onSend?: (value: any) => void;
  /** the workflow is executing */
  running?: boolean;
}

/**
 * Controls and displays by name. The built-in ones are registered by
 * WidgetPanel; Python widget nodes pick one with `control` / `renderer` (or
 * use "html" for a page of their own). Frontend code can add more names.
 */
export const INPUT_CONTROLS = new Map<string, React.ComponentType<InputControlProps>>();
export const OUTPUT_RENDERERS = new Map<string, React.ComponentType<OutputRendererProps>>();

export function registerInputControl(name: string, component: React.ComponentType<InputControlProps>): void {
  INPUT_CONTROLS.set(name, component);
}

export function registerOutputRenderer(name: string, component: React.ComponentType<OutputRendererProps>): void {
  OUTPUT_RENDERERS.set(name, component);
}
