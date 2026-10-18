import React, { useMemo, useRef, useState } from 'react';

import { OutputRendererProps } from './registry';

/**
 * Bar, line and scatter charts of output widget nodes, drawn as SVG on the
 * node. Series colours are the categorical slots --vk-series-1..8 (fixed
 * order, light and dark steps in base.css); text uses the panel text
 * colours. Hovering shows the values (a crosshair on line charts).
 * Selecting (interactive displays): click bars, drag across a line chart,
 * drag a rectangle on a scatter plot or click its points; Ctrl adds.
 */

interface Series {
  name: string;
  values: (number | string | null)[];
}

const color = (i: number) => `var(--vk-series-${i + 1})`;
const LEGEND = 18;
const M = { left: 38, right: 10, top: 8, bottom: 20 };

/** Pointer position in chart coordinates (the canvas may be zoomed). */
function local(e: { clientX: number; clientY: number; currentTarget?: any }, svg: SVGSVGElement | null, width: number, height: number) {
  const r = (svg ?? (e.currentTarget as Element)).getBoundingClientRect();
  return { x: ((e.clientX - r.left) * width) / (r.width || 1), y: ((e.clientY - r.top) * height) / (r.height || 1) };
}

/** Click selection of keys: a click picks one (again: none), Ctrl toggles. */
function toggle<T>(current: T[], key: T, add: boolean): T[] {
  if (add) {
    return current.includes(key) ? current.filter(k => k !== key) : [...current, key];
  }
  return current.length === 1 && current[0] === key ? [] : [key];
}

type Box = { x0: number; y0: number; x1: number; y1: number };

/**
 * A rubber band dragged on the chart (selection); a plain click calls
 * onClick instead. Coordinates are chart pixels.
 */
function useBrush(
  svg: React.RefObject<SVGSVGElement>,
  width: number,
  height: number,
  onDone: (b: Box, add: boolean) => void,
  onClick: (p: { x: number; y: number }, add: boolean) => void
) {
  const [box, setBox] = useState<Box | null>(null);
  const start = (e: React.PointerEvent) => {
    if (e.button !== 0) {
      return;
    }
    e.stopPropagation();
    const p0 = local(e, svg.current, width, height);
    const add = e.ctrlKey || e.metaKey || e.shiftKey;
    let b: Box | null = null;
    const move = (ev: PointerEvent) => {
      const p = local(ev, svg.current, width, height);
      if (b || Math.hypot(p.x - p0.x, p.y - p0.y) > 3) {
        setBox((b = { x0: p0.x, y0: p0.y, x1: p.x, y1: p.y }));
      }
    };
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      setBox(null);
      b ? onDone(b, add) : onClick(p0, add);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  };
  return { box, start };
}

const num = (v: any): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const compact = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 });

/** Labels of axis ticks: as many decimals as the step between them needs (3.141, 3.142 - not 3.1, 3.1). */
function tickFormat(ts: number[]): (t: number) => string {
  const step = ts.length > 1 ? Math.abs(ts[1] - ts[0]) : 0;
  const top = Math.max(...ts.map(Math.abs));
  if (!step || top >= 1e4) {
    return t => compact.format(t);
  }
  const digits = Math.min(Math.max(0, -Math.floor(Math.log10(step) + 1e-9)), 10);
  const f = new Intl.NumberFormat(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return t => f.format(t);
}
const precise = new Intl.NumberFormat(undefined, { maximumSignificantDigits: 6 });
export const formatValue = (v: any) => (typeof v === 'number' ? precise.format(v) : v === null || v === undefined ? '–' : String(v));

/** "Nice" axis ticks covering [lo, hi]. */
function ticks(lo: number, hi: number, count = 4): number[] {
  if (lo === hi) {
    const d = Math.abs(lo) || 1;
    lo -= d / 2;
    hi += d / 2;
  }
  const raw = (hi - lo) / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw) ?? 10 * mag;
  const out: number[] = [];
  for (let t = Math.floor(lo / step) * step; t <= hi + step * 1e-9; t += step) {
    out.push(Number(t.toPrecision(12)));
  }
  if (out[out.length - 1] < hi) {
    out.push(Number((out[out.length - 1] + step).toPrecision(12)));
  }
  return out;
}

function extent(values: number[], zero: boolean): [number, number] {
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  if (!values.length) {
    return [0, 1];
  }
  if (zero) {
    lo = Math.min(lo, 0);
    hi = Math.max(hi, 0);
  }
  return [lo, hi];
}

function dateFormatter(span: number) {
  const day = 86400000;
  const opts: Intl.DateTimeFormatOptions =
    span > 2 * 365 * day ? { year: 'numeric' } : span > 60 * day ? { month: 'short', year: '2-digit' } : span > 2 * day ? { month: 'short', day: 'numeric' } : { hour: '2-digit', minute: '2-digit' };
  return (t: number) => new Date(t).toLocaleDateString(undefined, opts);
}

function Legend({ series }: { series: Series[] }) {
  return series.length < 2 ? null : (
    <div className="verknode-chart-legend" style={{ height: LEGEND }}>
      {series.map((s, i) => (
        <span key={s.name}>
          <i style={{ background: color(i) }} />
          {s.name}
        </span>
      ))}
    </div>
  );
}

interface Tip {
  x: number;
  y: number;
  title: string;
  lines: { name: string; value: string; index: number }[];
}

function Tooltip({ tip, width }: { tip: Tip | null; width: number }) {
  if (!tip) {
    return null;
  }
  const left = tip.x > width / 2;
  return (
    <div className="verknode-chart-tip" style={left ? { right: width - tip.x + 8, top: tip.y } : { left: tip.x + 8, top: tip.y }}>
      <b>{tip.title}</b>
      {tip.lines.map(l => (
        <div key={l.name}>
          <i style={{ background: color(l.index) }} />
          {l.name} <span>{l.value}</span>
        </div>
      ))}
    </div>
  );
}

function YAxis({ ts, y, w }: { ts: number[]; y: (v: number) => number; w: number }) {
  const fmt = tickFormat(ts);
  return (
    <g className="verknode-chart-axis">
      {ts.map(t => (
        <g key={t}>
          <line x1={M.left} x2={w - M.right} y1={y(t)} y2={y(t)} className={t === 0 ? 'verknode-chart-zero' : 'verknode-chart-grid'} />
          <text x={M.left - 4} y={y(t)} dy="0.32em" textAnchor="end">
            {fmt(t)}
          </text>
        </g>
      ))}
    </g>
  );
}

/** Path of a bar from the baseline y0 to y1, rounded at the data end. */
function barPath(x: number, w: number, y0: number, y1: number) {
  const r = Math.min(3, w / 2, Math.abs(y1 - y0));
  const up = y1 < y0;
  const end = up ? y1 + r : y1 - r;
  const sweep = up ? 1 : 0;
  return `M${x},${y0} V${end} A${r},${r} 0 0 ${sweep} ${x + r},${y1} H${x + w - r} A${r},${r} 0 0 ${sweep} ${x + w},${end} V${y0} Z`;
}

export function BarChart({ data, width, height, selection, onSelect }: OutputRendererProps) {
  const series: Series[] = data.series ?? [];
  const cats: string[] = data.categories ?? [];
  const [tip, setTip] = useState<Tip | null>(null);
  // a bar per row: select rows; else the x values (all rows with them)
  const byRow: number[] | undefined = data.rows;
  const keyOf = (ci: number) => (byRow ? byRow[ci] : cats[ci]);
  const chosen: any[] = (byRow ? selection?.rows : selection?.categories) ?? [];
  const dim = (ci: number) => chosen.length > 0 && !chosen.includes(keyOf(ci));
  const pick = (ci: number, e: React.MouseEvent) => {
    if (onSelect) {
      const next = toggle(chosen, keyOf(ci), e.ctrlKey || e.metaKey || e.shiftKey);
      onSelect(next.length ? (byRow ? { rows: next } : { categories: next }) : null);
    }
  };
  const h = height - (series.length > 1 ? LEGEND : 0);
  const values = series.flatMap(s => s.values.map(num).filter((v): v is number => v !== null));
  const [lo, hi] = extent(values, true);
  const ts = ticks(lo, hi);
  const y = (v: number) => M.top + ((ts[ts.length - 1] - v) / (ts[ts.length - 1] - ts[0])) * (h - M.top - M.bottom);
  const band = (width - M.left - M.right) / Math.max(cats.length, 1);
  const inner = band * 0.8;
  const bw = Math.max(inner / Math.max(series.length, 1) - 2, 1);
  const labelEvery = Math.max(1, Math.ceil(cats.length / Math.max(1, Math.floor((width - M.left) / 44))));
  return (
    <div className="verknode-chart" onMouseLeave={() => setTip(null)}>
      <Legend series={series} />
      <svg width={width} height={h} className={onSelect ? 'verknode-chart-select nodrag nopan' : ''}>
        <YAxis ts={ts} y={y} w={width} />
        {cats.map((c, ci) => {
          const x0 = M.left + ci * band + (band - inner) / 2;
          return (
            <g key={ci}>
              {series.map((s, si) => {
                const v = num(s.values[ci]);
                return v === null ? null : (
                  <path key={si} d={barPath(x0 + si * (bw + 2), bw, y(0), y(v))} fill={color(si)} className={dim(ci) ? 'verknode-chart-dim' : ''} />
                );
              })}
              {/* hit target: the whole band */}
              <rect
                x={M.left + ci * band}
                y={M.top}
                width={band}
                height={h - M.top - M.bottom}
                fill="transparent"
                onClick={e => pick(ci, e)}
                onMouseEnter={() =>
                  setTip({
                    x: M.left + (ci + 0.5) * band,
                    y: M.top,
                    title: c,
                    lines: series.map((s, si) => ({ name: s.name, value: formatValue(s.values[ci]), index: si }))
                  })
                }
              />
              {ci % labelEvery === 0 && (
                <text className="verknode-chart-xlabel" x={M.left + (ci + 0.5) * band} y={h - M.bottom + 13} textAnchor="middle">
                  {c.length > 10 ? c.slice(0, 9) + '…' : c}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      <Tooltip tip={tip} width={width} />
    </div>
  );
}

/** x positions of line / scatter charts: numbers, dates (as time) or text (row order). */
function xScale(data: any, width: number) {
  const raw: any[] = data.xValues ?? [];
  const kind: string = data.xKind;
  const time = kind === 'date' || kind === 'datetime';
  const numeric = ['integer', 'long', 'double'].includes(kind);
  const xs: (number | null)[] = raw.map((v, i) => (time ? (v === null ? null : Date.parse(v)) : numeric ? num(v) : i));
  const valid = xs.filter((v): v is number => v !== null && Number.isFinite(v));
  const [lo, hi] = extent(valid, false);
  const ts = numeric ? ticks(lo, hi) : [];
  const dlo = numeric ? ts[0] : lo;
  const dhi = numeric ? ts[ts.length - 1] : hi === lo ? lo + 1 : hi;
  const px = (v: number) => M.left + ((v - dlo) / (dhi - dlo || 1)) * (width - M.left - M.right);
  let labels: { at: number; text: string }[];
  if (numeric) {
    const fmt = tickFormat(ts);
    labels = ts.map(t => ({ at: px(t), text: fmt(t) }));
  } else if (time) {
    const fmt = dateFormatter(hi - lo);
    labels = ticks(lo, hi, 3)
      .filter(t => t >= lo && t <= hi)
      .map(t => ({ at: px(t), text: fmt(t) }));
  } else {
    const every = Math.max(1, Math.ceil(raw.length / Math.max(1, Math.floor((width - M.left) / 50))));
    labels = raw.map((v, i) => ({ at: px(i), text: String(v) })).filter((_, i) => i % every === 0);
  }
  const label = (i: number) => (time && raw[i] ? dateFormatter(hi - lo)(Date.parse(raw[i])) : formatValue(raw[i]));
  return { xs, px, labels, label };
}

function XAxis({ labels, h, w }: { labels: { at: number; text: string }[]; h: number; w: number }) {
  return (
    <g className="verknode-chart-axis">
      <line x1={M.left} x2={w - M.right} y1={h - M.bottom} y2={h - M.bottom} className="verknode-chart-zero" />
      {labels.map((l, i) => (
        <text key={i} className="verknode-chart-xlabel" x={l.at} y={h - M.bottom + 13} textAnchor="middle">
          {l.text.length > 12 ? l.text.slice(0, 11) + '…' : l.text}
        </text>
      ))}
    </g>
  );
}

function usePlot(data: any, width: number, height: number) {
  const series: Series[] = data.series ?? [];
  const h = height - (series.length > 1 ? LEGEND : 0);
  return useMemo(() => {
    const xsc = xScale(data, width);
    const values = series.flatMap(s => s.values.map(num).filter((v): v is number => v !== null));
    const [lo, hi] = extent(values, false);
    const ts = ticks(lo, hi);
    const y = (v: number) => M.top + ((ts[ts.length - 1] - v) / (ts[ts.length - 1] - ts[0])) * (h - M.top - M.bottom);
    return { series, h, ts, y, ...xsc };
  }, [data, width, h]);
}

export function LineChart({ data, width, height, selection, onSelect }: OutputRendererProps) {
  const { series, h, ts, y, xs, px, labels, label } = usePlot(data, width, height);
  const [hover, setHover] = useState<number | null>(null);
  const svg = useRef<SVGSVGElement>(null);
  const pts = xs.map(x => (x === null ? null : px(x)));
  const rows: number[] = data.rows ?? [];
  // the selected x range: the points dragged across (the node adds the thinned-out rows between)
  const brush = useBrush(
    svg,
    width,
    h,
    b => {
      const [a, z] = [Math.min(b.x0, b.x1), Math.max(b.x0, b.x1)];
      const inside = rows.filter((_, i) => pts[i] !== null && pts[i]! >= a && pts[i]! <= z);
      onSelect?.(inside.length ? { rows: inside } : null);
    },
    () => selection && onSelect?.(null)
  );
  const chosen = new Set<number>(selection?.rows ?? []);
  const at = pts.filter((p, i) => p !== null && chosen.has(rows[i])) as number[];
  const band = at.length ? [Math.min(...at), Math.max(...at)] : null;
  const nearest = (mx: number) => {
    let best: number | null = null;
    pts.forEach((p, i) => {
      if (p !== null && (best === null || Math.abs(p - mx) < Math.abs(pts[best]! - mx))) {
        best = i;
      }
    });
    return best;
  };
  const tip: Tip | null =
    hover === null
      ? null
      : {
          x: pts[hover]!,
          y: M.top,
          title: label(hover),
          lines: series.map((s, si) => ({ name: s.name, value: formatValue(s.values[hover]), index: si }))
        };
  return (
    <div className="verknode-chart" onMouseLeave={() => setHover(null)}>
      <Legend series={series} />
      <svg ref={svg} width={width} height={h} className={onSelect ? 'verknode-chart-select nodrag nopan' : ''}>
        <YAxis ts={ts} y={y} w={width} />
        <XAxis labels={labels} h={h} w={width} />
        {band && <rect className="verknode-chart-band" x={band[0] - 2} y={M.top} width={band[1] - band[0] + 4} height={Math.max(h - M.top - M.bottom, 0)} />}
        {brush.box && (
          <rect
            className="verknode-chart-brush"
            x={Math.min(brush.box.x0, brush.box.x1)}
            y={M.top}
            width={Math.abs(brush.box.x1 - brush.box.x0)}
            height={Math.max(h - M.top - M.bottom, 0)}
          />
        )}
        {series.map((s, si) => {
          let d = '';
          let pen = false;
          s.values.forEach((v, i) => {
            const n = num(v);
            if (n === null || pts[i] === null) {
              pen = false; // gaps stay gaps
              return;
            }
            d += `${pen ? 'L' : 'M'}${pts[i]!.toFixed(1)},${y(n).toFixed(1)}`;
            pen = true;
          });
          return <path key={si} d={d} fill="none" stroke={color(si)} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />;
        })}
        {hover !== null && (
          <g>
            <line className="verknode-chart-cross" x1={pts[hover]!} x2={pts[hover]!} y1={M.top} y2={h - M.bottom} />
            {series.map((s, si) => {
              const n = num(s.values[hover]);
              return n === null ? null : <circle key={si} cx={pts[hover]!} cy={y(n)} r={4} fill={color(si)} className="verknode-chart-ring" />;
            })}
          </g>
        )}
        <rect
          x={M.left}
          y={M.top}
          width={Math.max(width - M.left - M.right, 0)}
          height={Math.max(h - M.top - M.bottom, 0)}
          fill="transparent"
          onPointerDown={onSelect ? brush.start : undefined}
          onMouseMove={e => setHover(nearest(local(e, svg.current, width, h).x))}
        />
      </svg>
      <Tooltip tip={tip} width={width} />
    </div>
  );
}

export function ScatterChart({ data, width, height, selection, onSelect }: OutputRendererProps) {
  const { series, h, ts, y, xs, px, labels, label } = usePlot(data, width, height);
  const [hover, setHover] = useState<{ s: number; i: number } | null>(null);
  const svg = useRef<SVGSVGElement>(null);
  const r = xs.length > 200 ? 3 : 4;
  const points = series.flatMap((s, si) =>
    s.values.map((v, i) => {
      const n = num(v);
      return n === null || xs[i] === null ? null : { s: si, i, cx: px(xs[i]!), cy: y(n) };
    })
  );
  const nearest = (mx: number, my: number) => {
    let best: { s: number; i: number } | null = null;
    let dist = 14 * 14; // within 14px
    for (const p of points) {
      const d = p ? (p.cx - mx) ** 2 + (p.cy - my) ** 2 : Infinity;
      if (p && d < dist) {
        dist = d;
        best = { s: p.s, i: p.i };
      }
    }
    return best;
  };
  const hp = hover && points.find(p => p && p.s === hover.s && p.i === hover.i);
  const rows: number[] = data.rows ?? [];
  const chosen: number[] = selection?.rows ?? [];
  const picked = new Set(chosen);
  const brush = useBrush(
    svg,
    width,
    h,
    (b, add) => {
      const [x0, x1, y0, y1] = [Math.min(b.x0, b.x1), Math.max(b.x0, b.x1), Math.min(b.y0, b.y1), Math.max(b.y0, b.y1)];
      const inside = new Set(add ? chosen : []);
      for (const p of points) {
        if (p && p.cx >= x0 && p.cx <= x1 && p.cy >= y0 && p.cy <= y1) {
          inside.add(rows[p.i]);
        }
      }
      onSelect?.(inside.size ? { rows: [...inside].sort((a, z) => a - z) } : null);
    },
    (p, add) => {
      const near = nearest(p.x, p.y);
      if (near) {
        const next = toggle(chosen, rows[near.i], add);
        onSelect?.(next.length ? { rows: next } : null);
      } else if (!add && selection) {
        onSelect?.(null);
      }
    }
  );
  const tip: Tip | null = hp
    ? {
        x: hp.cx,
        y: Math.max(hp.cy - 30, 0),
        title: `${data.x}: ${label(hp.i)}`,
        lines: [{ name: series[hp.s].name, value: formatValue(series[hp.s].values[hp.i]), index: hp.s }]
      }
    : null;
  return (
    <div className="verknode-chart" onMouseLeave={() => setHover(null)}>
      <Legend series={series} />
      <svg
        ref={svg}
        width={width}
        height={h}
        className={onSelect ? 'verknode-chart-select verknode-chart-brushable nodrag nopan' : ''}
        onPointerDown={onSelect ? brush.start : undefined}
        onMouseMove={e => {
          const p = local(e, svg.current, width, h);
          setHover(nearest(p.x, p.y));
        }}
      >
        <YAxis ts={ts} y={y} w={width} />
        <XAxis labels={labels} h={h} w={width} />
        {points.map(p =>
          p ? (
            <circle
              key={`${p.s}-${p.i}`}
              cx={p.cx}
              cy={p.cy}
              r={r}
              fill={color(p.s)}
              className={`verknode-chart-dot${picked.size ? (picked.has(rows[p.i]) ? ' verknode-chart-picked' : ' verknode-chart-dim') : ''}`}
            />
          ) : null
        )}
        {brush.box && (
          <rect
            className="verknode-chart-brush"
            x={Math.min(brush.box.x0, brush.box.x1)}
            y={Math.min(brush.box.y0, brush.box.y1)}
            width={Math.abs(brush.box.x1 - brush.box.x0)}
            height={Math.abs(brush.box.y1 - brush.box.y0)}
          />
        )}
        {hp && <circle cx={hp.cx} cy={hp.cy} r={r + 2} fill={color(hp.s)} className="verknode-chart-ring" />}
      </svg>
      <Tooltip tip={tip} width={width} />
    </div>
  );
}
