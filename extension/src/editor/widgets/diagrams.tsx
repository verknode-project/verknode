/**
 * Diagrams and maps on the canvas: Mermaid (the Mermaid Display node, and ```mermaid blocks of Markdown
 * in chats and Text Display) and Leaflet maps (Map Display). Both libraries are loaded the first time
 * one is shown.
 */
import React, { useEffect, useRef, useState } from 'react';

import type * as Leaflet from 'leaflet';

import { formatValue } from './charts';
import { OutputRendererProps } from './registry';

// ---------------------------------------------------------------------------------------------------
// Mermaid
// ---------------------------------------------------------------------------------------------------
let mermaidLib: Promise<any> | null = null;
let queue: Promise<unknown> = Promise.resolve();
let counter = 0;

/** SVG of Mermaid code (one render at a time: Mermaid keeps global state). */
function renderMermaid(code: string, dark: boolean, container?: HTMLElement): Promise<string> {
  mermaidLib ??= import('mermaid').then(m => m.default);
  const job = queue.then(async () => {
    const mermaid = await mermaidLib!;
    mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: dark ? 'dark' : 'default', fontFamily: 'inherit' });
    // (drawn off the canvas, which is scaled by its zoom - Mermaid measures text there - at the
    // display's own width, which Gantt charts take)
    const host = document.createElement('div');
    host.style.cssText = `position:absolute;left:-10000px;top:0;visibility:hidden;width:${container?.offsetWidth || 600}px`;
    if (container) {
      host.style.font = getComputedStyle(container).font;
    }
    document.body.appendChild(host);
    try {
      const { svg } = await mermaid.render(`verknode-mermaid-${++counter}`, code, host);
      return svg as string;
    } finally {
      host.remove();
    }
  });
  queue = job.catch(() => undefined);
  return job;
}

const isDark = (el: Element | null) =>
  !!el?.closest('.verknode-theme-dark') || document.body.getAttribute('data-jp-theme-light') === 'false';

/** A Mermaid diagram; its code with the error if Mermaid cannot read it. */
export function MermaidDiagram({ code }: { code: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const sizer = useRef<HTMLDivElement>(null); // (its width and font: those of the diagram)
  const [svg, setSvg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    setError(null);
    renderMermaid(code, isDark(ref.current), sizer.current ?? undefined).then(
      s => live && setSvg(s),
      e => {
        if (live) {
          setSvg(null);
          setError(String(e?.message ?? e).split('\n').slice(0, 4).join('\n'));
        }
      }
    );
    return () => {
      live = false;
    };
  }, [code]);
  return (
    <div ref={ref} className="verknode-mermaid">
      <div ref={sizer} className="verknode-mermaid-sizer" />
      {error ? (
        <div className="verknode-mermaid-error">
          <div>Not a diagram Mermaid can draw:</div>
          <pre>{error}</pre>
          <pre className="verknode-chat-code">
            <code>{code}</code>
          </pre>
        </div>
      ) : svg ? (
        <div className="verknode-mermaid-svg" dangerouslySetInnerHTML={{ __html: svg }} />
      ) : (
        <span className="verknode-widget-placeholder">Drawing…</span>
      )}
    </div>
  );
}

/** The display of the Mermaid Display node. */
export function MermaidRenderer({ data, width, height }: OutputRendererProps) {
  const diagrams: { code: string; title?: string }[] = data?.diagrams ?? [];
  return (
    <div className="verknode-widget-scroll verknode-mermaid-display nowheel" style={{ width, height, maxHeight: 'none' }}>
      {diagrams.length ? (
        diagrams.map((d, i) => <MermaidDiagram key={i} code={d.code} />)
      ) : (
        <span className="verknode-widget-placeholder">No diagram</span>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------------------------------
// Leaflet map
// ---------------------------------------------------------------------------------------------------
let leafletLib: Promise<typeof Leaflet> | null = null;
const loadLeaflet = () => (leafletLib ??= import('leaflet').then(m => ((m as any).default ?? m) as typeof Leaflet));

interface MapItem {
  row: number;
  label?: string | null;
  group?: number | null;
  size?: number | null;
  tip: [string, any][];
  lat?: number;
  lon?: number;
  geometry?: any;
}

const escapeHtml = (s: string) => s.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]!);

function tipHtml(it: MapItem): string {
  const head = it.label ? `<b>${escapeHtml(it.label)}</b><br>` : '';
  return head + it.tip.map(([k, v]) => `<span class="verknode-map-key">${escapeHtml(k)}</span> ${escapeHtml(String(formatValue(v)))}`).join('<br>');
}

/** The display of the Map Display node: dots and shapes on map tiles; click / Ctrl-click / Shift-drag selects. */
export function MapRenderer({ data, width, height, selection, onSelect }: OutputRendererProps) {
  const box = useRef<HTMLDivElement>(null);
  const map = useRef<Leaflet.Map | null>(null);
  const layers = useRef<{ L: typeof Leaflet; group: Leaflet.FeatureGroup; marks: Map<number, Leaflet.Path[]> } | null>(null);
  const sel = useRef<{ selection: any; onSelect?: (s: any) => void }>({ selection, onSelect });
  sel.current = { selection, onSelect };
  const [ready, setReady] = useState(0); // (a new map each time: its count)
  const [failed, setFailed] = useState<string | null>(null);
  const groups: string[] = data?.groups ?? [];

  const colorOf = (g: number | null | undefined) => {
    const css = box.current ? getComputedStyle(box.current) : null;
    const v = (i: number) => css?.getPropertyValue(`--vk-series-${i}`).trim() || '#2a78d6';
    return g === null || g === undefined ? (groups.length ? '#8a8a8a' : v(1)) : v((g % 8) + 1);
  };

  // the map itself (once)
  useEffect(() => {
    let gone = false;
    loadLeaflet().then(
      L => {
        if (gone || !box.current) {
          return;
        }
        const m = L.map(box.current, { boxZoom: false, attributionControl: true, zoomSnap: 0.5, preferCanvas: false });
        if (data?.tiles) {
          L.tileLayer(data.tiles, { maxZoom: 19, attribution: data.attribution || '' }).addTo(m);
        }
        m.setView([20, 0], 1);
        const group = L.featureGroup().addTo(m);
        layers.current = { L, group, marks: new Map() };
        map.current = m;
        // Shift-drag: select the dots in a box
        let start: Leaflet.LatLng | null = null;
        let rect: Leaflet.Rectangle | null = null;
        m.on('mousedown', (e: Leaflet.LeafletMouseEvent) => {
          if (!e.originalEvent.shiftKey || !sel.current.onSelect) {
            return;
          }
          start = e.latlng;
          m.dragging.disable();
          rect = L.rectangle(L.latLngBounds(start, start), { weight: 1, dashArray: '4', fillOpacity: 0.08, interactive: false }).addTo(m);
        });
        m.on('mousemove', (e: Leaflet.LeafletMouseEvent) => start && rect?.setBounds(L.latLngBounds(start, e.latlng)));
        m.on('mouseup', (e: Leaflet.LeafletMouseEvent) => {
          if (!start || !rect) {
            return;
          }
          const b = L.latLngBounds(start, e.latlng);
          rect.remove();
          start = rect = null;
          m.dragging.enable();
          const add = e.originalEvent.ctrlKey || e.originalEvent.metaKey;
          const picked = new Set<number>(add ? sel.current.selection?.rows ?? [] : []);
          for (const it of (data?.points ?? []) as MapItem[]) {
            if (b.contains([it.lat!, it.lon!])) {
              picked.add(it.row);
            }
          }
          sel.current.onSelect?.(picked.size ? { rows: [...picked].sort((a, z) => a - z) } : null);
        });
        setReady(n => n + 1);
      },
      e => setFailed(String(e?.message ?? e))
    );
    return () => {
      gone = true;
      map.current?.remove();
      map.current = null;
      layers.current = null;
    };
    // (a new map when the tiles change)
  }, [data?.tiles]);

  // the dots and shapes
  useEffect(() => {
    const ls = layers.current;
    if (!ready || !ls || !map.current) {
      return;
    }
    const { L, group, marks } = ls;
    group.clearLayers();
    marks.clear();
    const click = (row: number) => (e: Leaflet.LeafletMouseEvent) => {
      L.DomEvent.stopPropagation(e);
      const { selection: cur, onSelect: send } = sel.current;
      if (!send) {
        return;
      }
      const rows: number[] = cur?.rows ?? [];
      const add = e.originalEvent.ctrlKey || e.originalEvent.metaKey;
      const next = add ? (rows.includes(row) ? rows.filter(r => r !== row) : [...rows, row].sort((a, z) => a - z)) : rows.length === 1 && rows[0] === row ? [] : [row];
      send(next.length ? { rows: next } : null);
    };
    const keep = (row: number, layer: Leaflet.Path) => {
      marks.set(row, [...(marks.get(row) ?? []), layer]);
      layer.bindTooltip(tipHtml(itemOf.get(row)!), { sticky: true });
      layer.on('click', click(row));
      group.addLayer(layer);
    };
    const itemOf = new Map<number, MapItem>();
    for (const it of [...(data?.shapes ?? []), ...(data?.points ?? [])] as MapItem[]) {
      itemOf.set(it.row, it);
    }
    for (const it of (data?.shapes ?? []) as MapItem[]) {
      const c = colorOf(it.group);
      const gj = L.geoJSON(it.geometry, {
        style: { color: c, weight: 2, fillColor: c, fillOpacity: 0.25 },
        pointToLayer: (_f, ll) => L.circleMarker(ll, { radius: 5 })
      });
      gj.eachLayer(l => keep(it.row, l as Leaflet.Path));
    }
    for (const it of (data?.points ?? []) as MapItem[]) {
      const c = colorOf(it.group);
      const r = it.size === null || it.size === undefined ? 5 : 4 + 12 * Math.sqrt(it.size);
      keep(it.row, L.circleMarker([it.lat!, it.lon!], { radius: r, color: '#ffffff', weight: 1, fillColor: c, fillOpacity: 0.85 }));
    }
    const b = group.getBounds();
    map.current.invalidateSize({ animate: false }); // (its size first: the fit depends on it)
    if (b.isValid()) {
      map.current.fitBounds(b.pad(0.08), { maxZoom: 15, animate: false });
    }
  }, [ready, data]);

  // the selection
  useEffect(() => {
    const ls = layers.current;
    if (!ready || !ls) {
      return;
    }
    const picked = new Set<number>(selection?.rows ?? []);
    for (const [row, list] of ls.marks) {
      for (const l of list) {
        const on = picked.has(row);
        l.setStyle(l instanceof ls.L.CircleMarker ? { color: on ? '#111111' : '#ffffff', weight: on ? 3 : 1 } : { weight: on ? 4 : 2, dashArray: on ? '' : undefined });
        if (on) {
          l.bringToFront();
        }
      }
    }
  }, [ready, data, selection]);

  // the display's size
  useEffect(() => {
    map.current?.invalidateSize({ animate: false });
  }, [width, height, ready]);

  return (
    <div className="verknode-map-display" style={{ width, height }}>
      <div ref={box} className="verknode-map nowheel nodrag" style={{ width, height }} />
      {failed && <div className="verknode-widget-placeholder">The map could not load: {failed}</div>}
      {groups.length > 0 && (
        <div className="verknode-map-legend">
          {data.color && <div className="verknode-map-legend-title">{data.color}</div>}
          {groups.map((g, i) => (
            <div key={i}>
              <span className="verknode-map-swatch" style={{ background: colorOf(i) }} />
              {g}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
