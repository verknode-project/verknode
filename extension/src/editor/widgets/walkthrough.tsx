import React, { useContext, useEffect, useMemo, useRef, useState } from 'react';

import { MermaidDiagram } from './diagrams';
import { NodeChip } from './chat';
import { CanvasAction, CanvasLink, CanvasLinkContext, OutputRendererProps } from './registry';

/**
 * Walkthrough: a document that explains the workflow, read a section at a time like a slide show
 * (nodes/walkthrough.py has the format). Its vk: links point at the parts of the workflow - a node, a group,
 * settings, a canvas widget, an output, a view - and run buttons run them; stepping to a section shows what
 * its "<!-- show: ... -->" line names. The same reader is the node's canvas widget and a page of its own
 * (dialog, browser window, JupyterLab tab); ✎ edits the document in place.
 */

export interface WalkSection {
  title: string;
  body: string;
  show: string[];
}

const SHOW = /<!--\s*show:\s*([\s\S]*?)\s*-->/gi;
/** The text without its code (a reference written as an example in `code` is not one). */
const prose = (text: string) => text.replace(/```[\s\S]*?```|`[^`\n]*`/g, m => ' '.repeat(m.length));
/** Comments (the show lines) out of a text, but not out of its code. */
const uncomment = (text: string) => text.replace(/(`[^`\n]*`)|<!--[\s\S]*?-->/g, (m, code) => code ?? '');

/** The title and the sections ("## " starts one; text before it is the opening section). */
export function parseWalkthrough(text: string): { title: string; sections: WalkSection[] } {
  let rest = text ?? '';
  let title = '';
  const m = /^\s*#\s+(.+)\n?/.exec(rest);
  if (m) {
    title = m[1].trim();
    rest = rest.slice(m[0].length);
  }
  const sections: WalkSection[] = [];
  rest.split(/^##\s+/m).forEach((part, i) => {
    let head = title;
    let body = part;
    if (i > 0) {
      const nl = part.indexOf('\n');
      head = (nl < 0 ? part : part.slice(0, nl)).trim();
      body = nl < 0 ? '' : part.slice(nl + 1);
    }
    const show = [...prose(body).matchAll(SHOW)].flatMap(x => x[1].split(',').map(a => a.trim()).filter(Boolean));
    if (i === 0 && !body.trim() && !show.length) {
      return;
    }
    sections.push({ title: head.trim(), body: body.replace(/^\n+|\n+$/g, ''), show });
  });
  return { title, sections };
}

/** A vk: address (with or without "vk:") as what it does on the canvas, or null. */
export function parseRef(address: string): CanvasAction | null {
  const parts = address.trim().replace(/^vk:/, '').split('/').filter(p => p !== '');
  const num = (s: string | undefined) => (s !== undefined && /^\d+$/.test(s) ? Number(s) : NaN);
  const [kind, a, b] = parts;
  if (kind === 'run') {
    if (a === 'all' && parts.length === 2) {
      return { kind: 'run', all: true };
    }
    if (a === 'group' && parts.length === 3 && !isNaN(num(b))) {
      return { kind: 'run', group: num(b) };
    }
    const ids = (a ?? '').split(',').filter(Boolean).map(Number);
    return parts.length === 2 && ids.length && ids.every(n => Number.isInteger(n)) ? { kind: 'run', nodes: ids } : null;
  }
  if (kind === 'group') {
    return parts.length === 2 && !isNaN(num(a)) ? { kind: 'group', group: num(a) } : null;
  }
  const node = num(a);
  if (isNaN(node)) {
    return null;
  }
  if (kind === 'settings' && parts.length <= 3) {
    return { kind: 'settings', node, ...(b ? { field: b } : {}) };
  }
  if (kind === 'output' && parts.length <= 3 && (b === undefined || !isNaN(num(b)))) {
    return { kind: 'output', node, ...(b ? { port: num(b) } : {}) };
  }
  if ((kind === 'node' || kind === 'widget' || kind === 'view') && parts.length === 2) {
    return { kind, node };
  }
  return null;
}

/** What a reference is about, as text (and whether it points at nothing). */
function describeRef(a: CanvasAction, link: CanvasLink | null): { what: string; missing: string | null } {
  const name = (id: number) => {
    const n = link?.node(id);
    return n ? `${n.label || n.name} (#${id})` : `#${id}`;
  };
  const missingNode = (id: number) => (link && !link.node(id) ? `There is no node #${id} in the workflow` : null);
  switch (a.kind) {
    case 'group': {
      const g = link?.groups().find(x => x.id === a.group);
      return { what: g ? `the group "${g.text || a.group}"` : `group ${a.group}`, missing: link && !g ? `There is no group ${a.group}` : null };
    }
    case 'run': {
      if (a.all) {
        return { what: 'run the whole workflow', missing: null };
      }
      if (a.group !== undefined) {
        const g = link?.groups().find(x => x.id === a.group);
        return { what: `run the group "${g?.text ?? a.group}"`, missing: link && !g ? `There is no group ${a.group}` : null };
      }
      const ids = a.nodes ?? [];
      return { what: `run ${ids.map(name).join(', ')}`, missing: ids.map(missingNode).find(Boolean) ?? null };
    }
    case 'settings':
      return { what: `the settings of ${name(a.node)}${a.field ? ` - "${a.field}"` : ''}`, missing: missingNode(a.node) };
    case 'output':
      return { what: `output ${a.port ?? 1} of ${name(a.node)}`, missing: missingNode(a.node) };
    case 'widget':
      return { what: `the canvas widget of ${name(a.node)}`, missing: missingNode(a.node) };
    case 'view':
      return { what: `the view of ${name(a.node)}`, missing: missingNode(a.node) };
    default:
      return { what: name(a.node), missing: missingNode(a.node) };
  }
}

const KIND_ICON: Record<string, string> = { node: '●', group: '▢', settings: '⚙', widget: '◫', output: '▤', view: '⧉' };

/** Where a look at a node's data goes: beside the walkthrough (its widget on the canvas), at the link's height. */
function beside(el: Element): DOMRect {
  const dock = (el.closest('.verknode-widget-dock') ?? el.closest('.verknode-walk') ?? el).getBoundingClientRect();
  const at = el.getBoundingClientRect();
  return new DOMRect(dock.left, at.top - 20, dock.width, at.height);
}

/** The nodes a reference shows (flashes on hover). */
function refNodes(a: CanvasAction, link: CanvasLink): number[] {
  if (a.kind === 'group' || (a.kind === 'run' && a.group !== undefined)) {
    return link.groups().find(g => g.id === a.group)?.nodes ?? [];
  }
  if (a.kind === 'run') {
    return a.nodes ?? [];
  }
  return [a.node];
}

/** The walkthrough on the canvas (null: a page of its own): what a reference shows, it keeps in view too. */
const KeepInView = React.createContext<React.RefObject<HTMLElement> | null>(null);

function RefLink({ action, children }: { action: CanvasAction; children: React.ReactNode }) {
  const link = useContext(CanvasLinkContext);
  const keep = useContext(KeepInView);
  const { what, missing } = describeRef(action, link);
  if (!link || missing) {
    return (
      <span className="verknode-walk-ref verknode-walk-ref-missing" title={missing ?? what}>
        {children}
      </span>
    );
  }
  if (action.kind === 'run') {
    return (
      <button
        className="verknode-walk-run"
        title={`${what[0].toUpperCase()}${what.slice(1)}`}
        disabled={link.running()}
        onMouseEnter={() => link.highlight(refNodes(action, link), false)}
        onClick={e => {
          e.stopPropagation();
          link.act(action);
        }}
      >
        ▶ {children}
      </button>
    );
  }
  const node = action.kind === 'node' ? action.node : null;
  return (
    <span
      className={`verknode-walk-ref verknode-walk-ref-${action.kind}`}
      role="button"
      tabIndex={0}
      title={`Show ${what}`}
      onMouseEnter={e => {
        link.highlight(refNodes(action, link), false);
        if (node !== null) {
          link.inspect(node, false, beside(e.currentTarget));
        }
      }}
      onMouseLeave={() => node !== null && link.inspect(null)}
      onClick={e => {
        e.stopPropagation();
        if (node !== null) {
          link.inspect(node, true, beside(e.currentTarget));
        }
        link.act(action, keep?.current ?? undefined);
      }}
      onKeyDown={e => {
        if (e.key === 'Enter') {
          e.stopPropagation();
          link.act(action, keep?.current ?? undefined);
        }
      }}
    >
      <span className="verknode-walk-ref-icon">{KIND_ICON[action.kind]}</span>
      {children}
    </span>
  );
}

/* ------------------------------------------------------------------ Markdown */
/** Markdown of a walkthrough: headings, paragraphs, lists, quotes, tables, code (mermaid: diagrams),
 * images and links - vk: links as references, other links to the web or to files next to the workflow. */
export function DocMarkdown({ text }: { text: string }) {
  const parts = (text ?? '').split(/```([a-zA-Z0-9_+-]*)\n?([\s\S]*?)```/g);
  return (
    <>
      {parts.map((part, i) =>
        i % 3 === 1 ? null : i % 3 === 2 && parts[i - 1].toLowerCase() === 'mermaid' ? (
          <MermaidDiagram key={i} code={part} />
        ) : i % 3 === 2 ? (
          <pre key={i} className="verknode-walk-code">
            <code>{part.replace(/\n$/, '')}</code>
          </pre>
        ) : (
          <Blocks key={i} text={uncomment(part)} />
        )
      )}
    </>
  );
}

const ITEM = /^\s*([-*+•]|\d+[.)])\s+/;

function Blocks({ text }: { text: string }) {
  const out: React.ReactNode[] = [];
  const lines = text.split('\n');
  let i = 0;
  const para: string[] = [];
  const flush = () => {
    if (para.length) {
      out.push(
        <p key={out.length}>
          {para.map((l, j) => (
            <React.Fragment key={j}>
              {j > 0 && ' '}
              <Inline text={l.trim()} />
            </React.Fragment>
          ))}
        </p>
      );
      para.length = 0;
    }
  };
  while (i < lines.length) {
    const line = lines[i];
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (!line.trim()) {
      flush();
      i++;
    } else if (h) {
      flush();
      const Tag = `h${Math.min(6, h[1].length + 2)}` as 'h3';
      out.push(
        <Tag key={out.length}>
          <Inline text={h[2]} />
        </Tag>
      );
      i++;
    } else if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      flush();
      out.push(<hr key={out.length} />);
      i++;
    } else if (/^\s*>/.test(line)) {
      flush();
      const quote: string[] = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) {
        quote.push(lines[i].replace(/^\s*>\s?/, ''));
        i++;
      }
      out.push(
        <blockquote key={out.length}>
          <Blocks text={quote.join('\n')} />
        </blockquote>
      );
    } else if (/^\s*\|/.test(line)) {
      flush();
      const rows: string[] = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) {
        rows.push(lines[i]);
        i++;
      }
      const cells = (l: string) => l.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
      const body = rows.filter(l => !/^\s*\|?\s*:?-{2,}/.test(l)).map(cells);
      out.push(
        <div key={out.length} className="verknode-walk-table">
          <table>
            <thead>
              <tr>
                {body[0].map((c, j) => (
                  <th key={j}>
                    <Inline text={c} />
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {body.slice(1).map((r, k) => (
                <tr key={k}>
                  {r.map((c, j) => (
                    <td key={j}>
                      <Inline text={c} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    } else if (ITEM.test(line)) {
      flush();
      const ordered = /^\s*\d/.test(line);
      const items: string[] = [];
      while (i < lines.length && lines[i].trim() && (ITEM.test(lines[i]) || /^\s{2,}\S/.test(lines[i]))) {
        if (ITEM.test(lines[i])) {
          items.push(lines[i].replace(ITEM, ''));
        } else {
          items[items.length - 1] += ' ' + lines[i].trim();
        }
        i++;
      }
      const Tag = ordered ? 'ol' : 'ul';
      out.push(
        <Tag key={out.length}>
          {items.map((t, j) => (
            <li key={j}>
              <Inline text={t} />
            </li>
          ))}
        </Tag>
      );
    } else {
      para.push(line);
      i++;
    }
  }
  flush();
  return <>{out}</>;
}

const INLINE = /(!\[[^\]]*\]\([^)\s]+\)|\[[^\]]+\]\([^)\s]+\)|`[^`]+`|\*\*[^*]+\*\*|__[^_]+__|(?<![\w*])\*[^*\s][^*]*\*(?!\w)|(?<![\w_])_[^_\s][^_]*_(?![\w_])|(?<![\w#&])#\d+(?!\w))/g;

function FileLink({ path, children }: { path: string; children: React.ReactNode }) {
  const link = useContext(CanvasLinkContext);
  return (
    <a
      href={link?.fileUrl(path) ?? undefined}
      title={`Open ${path} in JupyterLab`}
      onClick={e => {
        if (link) {
          e.preventDefault();
          link.openFile(path);
        }
      }}
    >
      {children}
    </a>
  );
}

function DocImage({ src, alt }: { src: string; alt: string }) {
  const link = useContext(CanvasLinkContext);
  const url = /^(https?:|data:)/.test(src) ? src : link?.fileUrl(src) ?? null;
  return url ? <img className="verknode-walk-img" src={url} alt={alt} title={alt} /> : <span>{alt}</span>;
}

function Inline({ text }: { text: string }) {
  const parts = text.split(INLINE);
  return (
    <>
      {parts.map((p, i) => {
        if (i % 2 === 0) {
          return <React.Fragment key={i}>{p}</React.Fragment>;
        }
        let m = /^!\[([^\]]*)\]\(([^)\s]+)\)$/.exec(p);
        if (m) {
          return <DocImage key={i} src={m[2]} alt={m[1]} />;
        }
        m = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(p);
        if (m) {
          const [, label, href] = m;
          if (href.startsWith('vk:')) {
            const action = parseRef(href);
            return action ? (
              <RefLink key={i} action={action}>
                <Inline text={label} />
              </RefLink>
            ) : (
              <span key={i} className="verknode-walk-ref verknode-walk-ref-missing" title={`${href} is not a reference`}>
                {label}
              </span>
            );
          }
          if (/^(https?:|mailto:)/.test(href)) {
            return (
              <a key={i} href={href} target="_blank" rel="noopener noreferrer">
                <Inline text={label} />
              </a>
            );
          }
          return (
            <FileLink key={i} path={href}>
              <Inline text={label} />
            </FileLink>
          );
        }
        if (/^#\d+$/.test(p)) {
          return <NodeChip key={i} text={p} id={Number(p.slice(1))} />;
        }
        if (p.startsWith('`')) {
          return <code key={i}>{p.slice(1, -1)}</code>;
        }
        if (p.startsWith('**') || p.startsWith('__')) {
          return (
            <b key={i}>
              <Inline text={p.slice(2, -2)} />
            </b>
          );
        }
        return (
          <i key={i}>
            <Inline text={p.slice(1, -1)} />
          </i>
        );
      })}
    </>
  );
}

/* ------------------------------------------------------------------ the place in the document */
/** The section each walkthrough is at, shared by its widget and its pages (they step together). */
const positions = new Map<number, number>();
const listeners = new Set<() => void>();

function usePosition(nodeId: number): [number, (i: number) => void] {
  const [, setTick] = useState(0);
  useEffect(() => {
    const on = () => setTick(t => t + 1);
    listeners.add(on);
    return () => {
      listeners.delete(on);
    };
  }, []);
  return [
    positions.get(nodeId) ?? 0,
    i => {
      positions.set(nodeId, i);
      listeners.forEach(l => l());
    }
  ];
}

/** Show what a section is about (its show line): its widgets opened, its nodes, groups and widgets brought
 * into view together (with the walkthrough itself when it is on the canvas: keep), then settings, outputs. */
function showSection(section: WalkSection | undefined, link: CanvasLink | null, keep?: Element) {
  if (!section || !link) {
    return;
  }
  const actions = section.show.map(parseRef).filter((a): a is CanvasAction => !!a && a.kind !== 'run');
  actions.forEach(a => a.kind === 'widget' && link.act({ ...a, quiet: true }));
  const ids = actions.flatMap(a => (a.kind === 'node' || a.kind === 'widget' ? [a.node] : a.kind === 'group' ? refNodes(a, link) : []));
  if (ids.length) {
    link.highlight([...new Set(ids)], true, keep);
  }
  actions.filter(a => a.kind === 'settings' || a.kind === 'output' || a.kind === 'view').forEach(a => link.act(a));
}

/* ------------------------------------------------------------------ the reader */
export interface WalkthroughData {
  title?: string;
  document?: string;
  draft?: boolean;
  problems?: string[];
}

export function Walkthrough(props: {
  nodeId: number;
  data: WalkthroughData;
  /** a page of its own (dialog, window, tab) rather than the canvas widget */
  page?: boolean;
  /** the node is running (e.g. drafting) */
  busy?: boolean;
  width?: number;
  height?: number;
}) {
  const { nodeId, data, page } = props;
  const link = useContext(CanvasLinkContext);
  const doc = data.document ?? '';
  const { title: docTitle, sections } = useMemo(() => parseWalkthrough(doc), [doc]);
  const [at, setAt] = usePosition(nodeId);
  const index = Math.min(at, Math.max(0, sections.length - 1));
  const [editing, setEditing] = useState<string | null>(null);
  const [contents, setContents] = useState(false);
  const body = useRef<HTMLDivElement>(null);
  const root = useRef<HTMLDivElement>(null);
  const title = data.title || docTitle || 'Walkthrough';

  const go = (i: number) => {
    const to = Math.max(0, Math.min(sections.length - 1, i));
    setContents(false);
    if (to !== index || i === index) {
      setAt(to);
      showSection(sections[to], link, page ? undefined : root.current ?? undefined);
    }
  };
  useEffect(() => {
    body.current?.scrollTo({ top: 0 });
  }, [index, doc]);
  useEffect(() => {
    if (page) {
      root.current?.focus({ preventScroll: true });
    }
  }, [page]);

  if (editing !== null) {
    return (
      <WalkthroughEditor
        nodeId={nodeId}
        text={editing}
        page={page}
        width={props.width}
        height={props.height}
        busy={props.busy}
        onChange={setEditing}
        onDone={() => setEditing(null)}
      />
    );
  }
  const keys = (e: React.KeyboardEvent) => {
    if ((e.target as HTMLElement).closest('input, textarea, select')) {
      return;
    }
    const to =
      e.key === 'ArrowRight' || e.key === 'PageDown' || (e.key === ' ' && !e.shiftKey) ? index + 1
      : e.key === 'ArrowLeft' || e.key === 'PageUp' || (e.key === ' ' && e.shiftKey) ? index - 1
      : e.key === 'Home' ? 0
      : e.key === 'End' ? sections.length - 1
      : null;
    if (to !== null) {
      e.preventDefault();
      e.stopPropagation();
      go(to);
    }
  };
  const section = sections[index];
  const style = page ? undefined : { width: props.width, height: props.height };
  return (
    <KeepInView.Provider value={page ? null : root}>
      <div
        ref={root}
        className={`verknode-walk nodrag ${page ? 'verknode-walk-page' : ''}`}
        style={style}
        tabIndex={0}
        onKeyDown={keys}
      >
        <div className="verknode-walk-head">
          <span className="verknode-walk-title" title={title}>
            {title}
          </span>
          {sections.length > 1 && (
            <button className="verknode-tb" title="Contents" aria-pressed={contents} onClick={() => setContents(c => !c)}>
              ☰
            </button>
          )}
          <button className="verknode-tb" title="Edit the walkthrough" onClick={() => setEditing(doc)}>
            ✎
          </button>
          {!page && link && (
            <button className="verknode-tb" title="Open it as a page of its own (a JupyterLab tab or a window: Settings › Views)" onClick={() => link.openWalkthrough(nodeId)}>
              ⇱
            </button>
          )}
        </div>
        {!!data.problems?.length && (
          <div className="verknode-walk-problems" title={data.problems.join('\n')}>
            ⚠ {data.problems.length} reference{data.problems.length > 1 ? 's point' : ' points'} at nothing
          </div>
        )}
        {contents ? (
          <ol className="verknode-walk-contents nowheel">
            {sections.map((s, i) => (
              <li key={i} className={i === index ? 'verknode-walk-here' : ''}>
                <button onClick={() => go(i)}>{s.title || (i === 0 ? 'Start' : `Section ${i + 1}`)}</button>
              </li>
            ))}
          </ol>
        ) : (
          <div className="verknode-walk-body nowheel" ref={body}>
            {props.busy && !doc.trim() ? (
              <div className="verknode-walk-empty">Drafting the walkthrough…</div>
            ) : !section ? (
              <div className="verknode-walk-empty">
                <p>No walkthrough yet.</p>
                <button className="verknode-tb" onClick={() => setEditing(starter(link))}>
                  ✎ Write one
                </button>{' '}
                <DraftButton nodeId={nodeId} current="" disabled={!!props.busy} />
              </div>
            ) : (
              <>
                {section.title && <h2 className="verknode-walk-section">{section.title}</h2>}
                <DocMarkdown text={section.body} />
              </>
            )}
          </div>
        )}
        {sections.length > 0 && (
          <div className="verknode-walk-foot">
            <button className="verknode-tb" disabled={index === 0} title="Back (←)" onClick={() => go(index - 1)}>
              ◀
            </button>
            <span className="verknode-walk-dots" aria-label={`Section ${index + 1} of ${sections.length}`}>
              {sections.map((s, i) => (
                <button
                  key={i}
                  className={i === index ? 'verknode-walk-dot verknode-walk-here' : 'verknode-walk-dot'}
                  title={s.title || `Section ${i + 1}`}
                  onClick={() => go(i)}
                />
              ))}
            </span>
            <span className="verknode-walk-count">
              {index + 1} / {sections.length}
            </span>
            <button className="verknode-tb" disabled={index >= sections.length - 1} title="Next (→, Space)" onClick={() => go(index + 1)}>
              ▶
            </button>
          </div>
        )}
      </div>
    </KeepInView.Provider>
  );
}

/** A first document: the outline of a walkthrough, with the workflow's groups as sections. */
function starter(link: CanvasLink | null): string {
  const groups = link?.groups() ?? [];
  const parts = ['# Walkthrough', '', 'What this workflow is for, and what it gives.', ''];
  for (const g of groups.length ? groups : [{ id: 0, text: 'The first step', nodes: [] }]) {
    parts.push(`## ${g.text || `Group ${g.id}`}`, g.id ? `<!-- show: group/${g.id} -->` : '', 'What these nodes do, and why.', '');
  }
  parts.push('## Try it', '[Run the workflow](vk:run/all)', '');
  return parts.join('\n');
}

/** ✨ Draft: the node writes the document with its LLM (the current one is replaced: asked first). */
function DraftButton({ nodeId, current, disabled, onStarted }: { nodeId: number; current: string; disabled?: boolean; onStarted?: () => void }) {
  const link = useContext(CanvasLinkContext);
  const [asking, setAsking] = useState(false);
  if (!link) {
    return null;
  }
  const start = () => {
    setAsking(false);
    onStarted?.();
    void link.saveSettings(nodeId, { document: '', draft: true }).then(() => link.act({ kind: 'run', nodes: [nodeId] }));
  };
  if (asking) {
    return (
      <span className="verknode-walk-ask">
        Replace the document with a draft?{' '}
        <button className="verknode-tb" onClick={start}>
          Replace
        </button>
        <button className="verknode-tb" onClick={() => setAsking(false)}>
          Keep
        </button>
      </span>
    );
  }
  return (
    <button
      className="verknode-tb"
      disabled={disabled || link.running()}
      title="The node's LLM (its Model settings or LLM port) writes a walkthrough of the workflow"
      onClick={() => (current.trim() ? setAsking(true) : start())}
    >
      ✨ Draft with the LLM
    </button>
  );
}

/* ------------------------------------------------------------------ the editor */
function WalkthroughEditor(props: {
  nodeId: number;
  text: string;
  page?: boolean;
  width?: number;
  height?: number;
  busy?: boolean;
  onChange: (text: string) => void;
  onDone: () => void;
}) {
  const { nodeId, text, page } = props;
  const link = useContext(CanvasLinkContext);
  const area = useRef<HTMLTextAreaElement>(null);
  const [caret, setCaret] = useState(0);
  const [menu, setMenu] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const insert = (snippet: string) => {
    const ta = area.current;
    const start = ta?.selectionStart ?? text.length;
    const end = ta?.selectionEnd ?? text.length;
    props.onChange(text.slice(0, start) + snippet + text.slice(end));
    setMenu(false);
    window.requestAnimationFrame(() => {
      ta?.focus();
      ta?.setSelectionRange(start + snippet.length, start + snippet.length);
      setCaret(start + snippet.length);
    });
  };
  const save = async (close: boolean) => {
    if (!link) {
      return;
    }
    try {
      setError(null);
      await link.saveSettings(nodeId, { document: text });
      if (close) {
        props.onDone();
      }
    } catch (e) {
      setError((e as Error).message);
    }
  };
  // the section the caret is in, previewed (page) - its "## " heading is the last one before the caret
  const all = parseWalkthrough(text).sections;
  const headings = (text.slice(0, caret).match(/^##\s+/gm) ?? []).length;
  const opening = !!text.split(/^##\s+/m)[0].replace(/^\s*#\s+.+\n?/, '').trim();
  const preview = all[Math.max(0, Math.min(all.length - 1, opening ? headings : headings - 1))];
  const style = page ? undefined : { width: props.width, height: props.height };
  return (
    <div className={`verknode-walk verknode-walk-editing nodrag ${page ? 'verknode-walk-page' : ''}`} style={style}>
      <div className="verknode-walk-head">
        <span className="verknode-walk-title">Editing</span>
        <div className="verknode-walk-menu-wrap">
          <button className="verknode-tb" aria-expanded={menu} onClick={() => setMenu(m => !m)} title="Insert a reference to a part of the workflow">
            Insert ▾
          </button>
          {menu && link && <InsertMenu link={link} nodeId={nodeId} onInsert={insert} />}
        </div>
        <button className="verknode-tb" title="A new section" onClick={() => insert('\n## New section\n<!-- show: node/1 -->\n\n')}>
          + Section
        </button>
        <DraftButton nodeId={nodeId} current={text} disabled={props.busy} onStarted={props.onDone} />
        <span className="verknode-walk-spacer" />
        <button className="verknode-tb" onClick={props.onDone} title="Close without saving (Esc)">
          Cancel
        </button>
        <button className="verknode-tb verknode-tb-primary" onClick={() => void save(true)} title="Save (Ctrl+S; Ctrl+Enter: save and close)">
          Save
        </button>
      </div>
      {error && <div className="verknode-walk-problems">{error}</div>}
      <div className="verknode-walk-editor-body">
        <textarea
          ref={area}
          className="verknode-walk-source nowheel"
          spellCheck
          value={text}
          onChange={e => {
            props.onChange(e.target.value);
            setCaret(e.target.selectionStart);
          }}
          onSelect={e => setCaret((e.target as HTMLTextAreaElement).selectionStart)}
          onKeyDown={e => {
            e.stopPropagation();
            if ((e.ctrlKey || e.metaKey) && e.key === 's') {
              e.preventDefault();
              void save(false);
            } else if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
              e.preventDefault();
              void save(true);
            } else if (e.key === 'Escape') {
              props.onDone();
            }
          }}
        />
        {page && (
          <div className="verknode-walk-preview nowheel">
            {preview ? (
              <>
                {preview.title && <h2 className="verknode-walk-section">{preview.title}</h2>}
                <DocMarkdown text={preview.body} />
              </>
            ) : (
              <div className="verknode-walk-empty">The section at the cursor shows here.</div>
            )}
          </div>
        )}
      </div>
      <div className="verknode-walk-hint">
        ## starts a section · [text](vk:node/3) refers to node #3 · Insert ▾ adds references to the nodes selected on the canvas
      </div>
    </div>
  );
}

/** References to insert: the nodes selected on the canvas (each way to point at them), all nodes, the groups. */
function InsertMenu({ link, nodeId, onInsert }: { link: CanvasLink; nodeId: number; onInsert: (s: string) => void }) {
  const selected = link.selection(nodeId);
  const all = link.nodes().filter(n => n.id !== nodeId);
  const byId = new Map(all.map(n => [n.id, n]));
  const name = (id: number) => byId.get(id)?.label || byId.get(id)?.name || `#${id}`;
  const ways = (id: number) => {
    const n = byId.get(id);
    return [
      { label: 'the node', md: `[${name(id)}](vk:node/${id})` },
      ...(n?.dialog ? [{ label: 'its settings', md: `[the settings of ${name(id)}](vk:settings/${id})` }] : []),
      ...(n?.widget ? [{ label: 'its canvas widget', md: `[${name(id)}](vk:widget/${id})` }] : []),
      ...(n?.outputs ? [{ label: 'its output', md: `[the output of ${name(id)}](vk:output/${id})` }] : []),
      ...(n?.view ? [{ label: 'its view', md: `[the view of ${name(id)}](vk:view/${id})` }] : []),
      { label: 'a run button', md: `[Run ${name(id)}](vk:run/${id})` },
      { label: 'show it when the section opens', md: `<!-- show: node/${id} -->\n` }
    ];
  };
  return (
    <div className="verknode-walk-menu nowheel" role="menu">
      {selected.length > 0 && <div className="verknode-walk-menu-head">Selected on the canvas</div>}
      {selected.length > 1 && (
        <button role="menuitem" onClick={() => onInsert(`[Run these steps](vk:run/${selected.map(n => n.id).join(',')})`)}>
          ▶ a run button for all {selected.length}
        </button>
      )}
      {selected.map(n => (
        <div key={n.id} className="verknode-walk-menu-node">
          <div className="verknode-walk-menu-name">
            {name(n.id)} <span className="verknode-walk-menu-id">#{n.id}</span>
          </div>
          {ways(n.id).map(w => (
            <button key={w.label} role="menuitem" onClick={() => onInsert(w.md)}>
              {w.label}
            </button>
          ))}
        </div>
      ))}
      {!selected.length && <div className="verknode-walk-menu-note">Select nodes on the canvas for more ways to point at them.</div>}
      {link.groups().length > 0 && <div className="verknode-walk-menu-head">Groups</div>}
      {link.groups().map(g => (
        <div key={g.id} className="verknode-walk-menu-row">
          <button role="menuitem" onClick={() => onInsert(`[${g.text || `group ${g.id}`}](vk:group/${g.id})`)}>
            {g.text || `Group ${g.id}`}
          </button>
          <button role="menuitem" title="Show this group when the section opens" onClick={() => onInsert(`<!-- show: group/${g.id} -->\n`)}>
            show
          </button>
          <button role="menuitem" title="A button that runs this group" onClick={() => onInsert(`[Run ${g.text || `group ${g.id}`}](vk:run/group/${g.id})`)}>
            ▶
          </button>
        </div>
      ))}
      <div className="verknode-walk-menu-head">Nodes</div>
      {all.map(n => (
        <button key={n.id} role="menuitem" onClick={() => onInsert(`[${name(n.id)}](vk:node/${n.id})`)}>
          {name(n.id)} <span className="verknode-walk-menu-id">#{n.id}</span>
        </button>
      ))}
      <div className="verknode-walk-menu-head">Workflow</div>
      <button role="menuitem" onClick={() => onInsert('[Run the whole workflow](vk:run/all)')}>
        ▶ a button that runs everything
      </button>
    </div>
  );
}

/** The node's canvas widget (renderer "walkthrough"). */
export function WalkthroughRenderer({ data, width, height, nodeId, nodeRunning }: OutputRendererProps) {
  return <Walkthrough nodeId={nodeId ?? 0} data={data ?? {}} width={width} height={height} busy={nodeRunning} />;
}
