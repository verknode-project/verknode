import type React from 'react';

/**
 * Colour themes of the workflow canvas and its nodes (the panels around it
 * follow the JupyterLab theme).
 *
 * - verkaround (default): nodes coloured by what they do - input, transform,
 *   domain (chemistry, statistics), ML, output - in light tints with dark
 *   glyphs, on a light slate canvas;
 * - knife: the colours of a popular workflow engine, by node type (Source
 *   orange, Manipulator yellow, ...), on the JupyterLab background;
 * - butter: halfway between knife and haze - the knife hues a little muted,
 *   on a warm cream canvas;
 * - haze: the knife colours muted (half the saturation) on a soft off-white
 *   canvas, with softer text and connections;
 * - knightmare: the knife colours on a dark canvas;
 * - pipeline cowboy: the look of a classic workflow engine - tiles by
 *   component role (readers green, writers red, manipulators blue, filters
 *   gold, viewers purple) in lighter tints, and heavy pipes on light grey.
 *
 * Node icons use CSS variables: --vk-group-<group> (themes colouring by
 * group) falling back to --vk-type-<node type>.
 */
export type ThemeName = 'verkaround' | 'knife' | 'butter' | 'haze' | 'knightmare' | 'pipeline-cowboy';

export interface CanvasTheme {
  label: string;
  dark: boolean;
  /** icon colour per node type (Source, Sink, Manipulator, ...) */
  types: Record<string, string>;
  /** icon colour per node group (see nodeGroup); overrides the type colours */
  groups?: Record<string, string>;
  canvas: string;
  dots: string;
  /** node name, node label (annotation) */
  text: string;
  subtext: string;
  /** table connections, table ports, flow variable connections / ports */
  edge: string;
  port: string;
  flowVariable: string;
  selection: string;
  /** zoom controls and minimap */
  panel: string;
  panelText: string;
  panelBorder: string;
  /** minimap area outside the visible part */
  minimapMask: string;
  /** glyph on node icons, and its shadow */
  iconText: string;
  iconShadow: string;
  /** width of connections (px) */
  edgeWidth?: number;
}

/** node type colours of a popular workflow engine (knife, knightmare) */
const POPULAR_TYPES: Record<string, string> = {
  Source: '#f1933b',
  Sink: '#e05a4f',
  Manipulator: '#fcd94d',
  Learner: '#b5d46e',
  Predictor: '#6fc1d9',
  Visualizer: '#5b8fd1',
  LoopStart: '#2fa8c4',
  LoopEnd: '#2fa8c4',
  ScopeStart: '#8ea0cc',
  ScopeEnd: '#8ea0cc',
  Metanode: '#a7adb5',
  Other: '#7d93df'
};

export const THEMES: Record<ThemeName, CanvasTheme> = {
  verkaround: {
    label: 'Verkaround',
    dark: false,
    types: { ...POPULAR_TYPES, Metanode: '#64748b', Other: '#64748b' },
    // light tints of the group hues (emerald, cyan, indigo, cobalt, violet,
    // amethyst, amber, rose, teal, slate) with dark glyphs, easy to read
    groups: {
      input: '#6ee7b7', // Input / ingestion: emerald
      'input-db': '#67e8f9', // ... databases, file handling: cyan
      transform: '#a5b4fc', // Transformation / clean: indigo
      'transform-row': '#93c5fd', // ... row operations: cobalt blue
      script: '#c7d2fe', // scripting: light indigo
      chemistry: '#c4b5fd', // Domain / specialised: violet
      statistics: '#d8b4fe', // ... statistics: amethyst
      learner: '#fcd34d', // ML / models: warm amber
      predictor: '#fda4af', // ... predictors: rose
      output: '#5eead4', // Output / sinks: neutral teal
      view: '#99f6e4', // views and charts: light teal
      flow: '#cbd5e1', // flow control (loops, switches, variables): slate
      metanode: '#e2e8f0',
      other: '#e2e8f0'
    },
    canvas: '#f8fafc',
    dots: '#cbd5e1',
    text: '#1e293b',
    subtext: '#64748b',
    edge: '#64748b',
    port: '#334155',
    flowVariable: '#dc2626',
    selection: '#4f46e5',
    panel: '#ffffff',
    panelText: '#475569',
    panelBorder: '#cbd5e1',
    minimapMask: 'rgb(241 245 249 / 70%)',
    iconText: '#1e293b',
    iconShadow: 'none'
  },
  knife: {
    label: 'Knife',
    dark: false,
    types: POPULAR_TYPES,
    canvas: 'var(--jp-layout-color0)',
    dots: '#91919a',
    text: 'var(--jp-ui-font-color0)',
    subtext: 'var(--jp-ui-font-color1)',
    edge: 'var(--jp-ui-font-color1)',
    port: 'var(--jp-ui-font-color0)',
    flowVariable: '#d4242b',
    selection: 'var(--jp-brand-color1)',
    panel: 'var(--jp-layout-color1)',
    panelText: 'var(--jp-ui-font-color1)',
    panelBorder: 'var(--jp-border-color1)',
    minimapMask: 'rgb(240 240 240 / 60%)',
    iconText: '#ffffff',
    iconShadow: '0 0 2px rgb(0 0 0 / 45%)'
  },
  butter: {
    label: 'Butter',
    dark: false,
    // halfway between knife and haze: the knife hues with saturation and lightness between the two
    types: {
      Source: '#db9d63',
      Sink: '#d17871',
      Manipulator: '#e6cf73',
      Learner: '#b7cb88',
      Predictor: '#89bfcf',
      Visualizer: '#789bc7',
      LoopStart: '#4fa7bc',
      LoopEnd: '#4fa7bc',
      ScopeStart: '#a0acc9',
      ScopeEnd: '#a0acc9',
      Metanode: '#b1b5ba',
      Other: '#95a3d6'
    },
    canvas: '#faf8f1',
    dots: '#ddd8c8',
    text: '#33363b',
    subtext: '#6f7379',
    edge: '#7c8087',
    port: '#474b51',
    flowVariable: '#cb4a4e',
    selection: '#4a83c6',
    panel: '#fdfcf8',
    panelText: '#4f5359',
    panelBorder: '#e0dccf',
    minimapMask: 'rgb(240 237 228 / 62%)',
    iconText: '#2b2f36',
    iconShadow: 'none'
  },
  haze: {
    label: 'Haze',
    dark: false,
    // the knife colours with about half the saturation, a little lighter
    types: {
      Source: '#caa887',
      Sink: '#c5938f',
      Manipulator: '#d5c895',
      Learner: '#b9c59f',
      Predictor: '#a1bfc8',
      Visualizer: '#93a6bf',
      LoopStart: '#73a4b0',
      LoopEnd: '#73a4b0',
      ScopeStart: '#b0b7c7',
      ScopeEnd: '#b0b7c7',
      Metanode: '#babcbf',
      Other: '#aab3cf'
    },
    canvas: '#f6f6f4',
    dots: '#d9d9d5',
    text: '#3c4046',
    subtext: '#7b8088',
    edge: '#8c9097',
    port: '#5d6269',
    flowVariable: '#c27070',
    selection: '#7391bd',
    panel: '#fbfbfa',
    panelText: '#585c63',
    panelBorder: '#dcdcd8',
    minimapMask: 'rgb(235 235 232 / 65%)',
    iconText: '#2b2f36',
    iconShadow: 'none'
  },
  knightmare: {
    label: 'Knightmare',
    dark: true,
    types: { ...POPULAR_TYPES, Metanode: '#6b7280' },
    canvas: '#121418',
    dots: '#2e333b',
    text: '#e6e8eb',
    subtext: '#9aa3ad',
    edge: '#8b949e',
    port: '#c9d1d9',
    flowVariable: '#f05252',
    selection: '#58a6ff',
    panel: '#1c1f24',
    panelText: '#c9d1d9',
    panelBorder: '#30363d',
    minimapMask: 'rgb(0 0 0 / 55%)',
    iconText: '#ffffff',
    iconShadow: '0 0 2px rgb(0 0 0 / 45%)'
  },
  'pipeline-cowboy': {
    label: 'Pipeline Cowboy',
    dark: false,
    types: { ...POPULAR_TYPES, Metanode: '#a6b6ca', Other: '#b8bcc2' },
    // component roles of a classic workflow engine, in lighter tints with dark glyphs
    groups: {
      input: '#6cc26c', // readers: green
      'input-db': '#5fb89a', // database / file readers: sea green
      transform: '#6a9fe0', // manipulators: blue
      'transform-row': '#f2c94c', // filters: gold
      script: '#8b96d6', // scripting: indigo blue
      chemistry: '#5bbcc4', // chemistry: cyan
      statistics: '#9b95d4', // statistics: blue violet
      learner: '#f5a65b', // learners: orange
      predictor: '#e88a63', // predictors: burnt orange
      output: '#e57368', // writers: red
      view: '#bb83d2', // viewers / reporting: purple
      flow: '#a3b1bf', // flow control: steel
      metanode: '#a6b6ca', // subprotocols: grey blue
      other: '#b8bcc2'
    },
    canvas: '#f2f2f2',
    dots: '#d9d9d9',
    text: '#222222',
    subtext: '#5f6368',
    edge: '#5c5c5c',
    port: '#3a3a3a',
    flowVariable: '#c62828',
    selection: '#1f6fd1',
    panel: '#fafafa',
    panelText: '#3a3a3a',
    panelBorder: '#cfcfcf',
    minimapMask: 'rgb(225 225 225 / 65%)',
    iconText: '#1f2328',
    iconShadow: 'none',
    edgeWidth: 2
  }
};

export const THEME_NAMES = Object.keys(THEMES) as ThemeName[];
export const DEFAULT_THEME: ThemeName = 'verkaround';

export const themeOf = (name: string | undefined): CanvasTheme => THEMES[(name as ThemeName) ?? DEFAULT_THEME] ?? THEMES[DEFAULT_THEME];

/** What a node does, for themes that colour by group (verkaround). */
export function nodeGroup(nodeType: string, category = ''): string {
  const top = category.split('/')[0];
  if (nodeType === 'Metanode') {
    return 'metanode';
  }
  if (['LoopStart', 'LoopEnd', 'ScopeStart', 'ScopeEnd'].includes(nodeType) || top === 'Flow Control') {
    return 'flow';
  }
  if (nodeType === 'Learner') {
    return 'learner';
  }
  if (nodeType === 'Predictor') {
    return 'predictor';
  }
  if (nodeType === 'Source') {
    return top === 'DB' ? 'input-db' : 'input';
  }
  if (nodeType === 'Sink') {
    return 'output';
  }
  if (nodeType === 'Visualizer' || top === 'Views') {
    return 'view';
  }
  if (top === 'Chemistry') {
    return 'chemistry';
  }
  if (category.startsWith('Analytics/Statistics')) {
    return 'statistics';
  }
  if (top === 'Analytics') {
    return 'learner';
  }
  if (top === 'Scripting') {
    return 'script';
  }
  if (category.startsWith('Manipulation/Row')) {
    return 'transform-row';
  }
  if (top === 'IO' || top === 'DB') {
    return 'input-db';
  }
  if (nodeType === 'Manipulator') {
    return 'transform';
  }
  return 'other';
}

/** CSS background of a node icon (resolved by the theme variables on the editor). */
export const iconBackground = (nodeType: string, category?: string) =>
  `var(--vk-group-${nodeGroup(nodeType, category)}, var(--vk-type-${nodeType}, var(--vk-type-Other)))`;

/** Actual icon colour (for places that cannot use CSS variables, e.g. the minimap). */
export function nodeColor(theme: CanvasTheme, nodeType: string, category?: string): string {
  return theme.groups?.[nodeGroup(nodeType, category)] ?? theme.types[nodeType] ?? theme.types.Other;
}

/** CSS variables of a theme, set on the editor element. */
export function themeStyle(theme: CanvasTheme): React.CSSProperties {
  const vars: Record<string, string> = {
    '--vk-canvas': theme.canvas,
    '--vk-node-text': theme.text,
    '--vk-node-subtext': theme.subtext,
    '--vk-edge': theme.edge,
    '--vk-selection': theme.selection,
    '--verknode-port-color': theme.port,
    '--verknode-var-red': theme.flowVariable,
    '--vk-dots': theme.dots,
    '--vk-panel': theme.panel,
    '--vk-panel-text': theme.panelText,
    '--vk-panel-border': theme.panelBorder,
    '--vk-icon-text': theme.iconText,
    '--vk-icon-shadow': theme.iconShadow,
    '--vk-edge-width': `${theme.edgeWidth ?? 1.3}`
  };
  for (const [type, color] of Object.entries(theme.types)) {
    vars[`--vk-type-${type}`] = color;
  }
  for (const [group, color] of Object.entries(theme.groups ?? {})) {
    vars[`--vk-group-${group}`] = color;
  }
  return vars as React.CSSProperties;
}
