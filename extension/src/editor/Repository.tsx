import React, { useMemo, useState } from 'react';

import { NodeDescription } from '../types';
import { NodeIcon } from './NodeView';
import { matchingNodes } from './hinting';

export const NODE_DRAG_MIME = 'application/x-verknode-node';

const PORT_TYPE_NAMES: Record<string, string> = {
  table: 'Data table',
  flowvariable: 'Flow variables',
  model: 'Model',
  image: 'Image',
  dbsession: 'DB session',
  dbdata: 'DB data',
  llm: 'LLM connection',
  agent: 'Agent'
};

const TOP_ORDER = ['IO', 'Manipulation', 'Views', 'Analytics', 'DB', 'Structured Data', 'Flow Control', 'Chemistry', 'Scripting'];

interface Folder {
  name: string;
  path: string;
  folders: Map<string, Folder>;
  nodes: NodeDescription[];
}

function buildTree(nodes: NodeDescription[]): Folder {
  const root: Folder = { name: '', path: '', folders: new Map(), nodes: [] };
  for (const n of nodes) {
    let f = root;
    for (const part of n.category.split('/').filter(Boolean)) {
      if (!f.folders.has(part)) {
        const path = f.path ? `${f.path}/${part}` : part;
        f.folders.set(part, { name: part, path, folders: new Map(), nodes: [] });
      }
      f = f.folders.get(part)!;
    }
    f.nodes.push(n);
  }
  return root;
}

function sortedFolders(f: Folder): Folder[] {
  const rank = (name: string) => (TOP_ORDER.includes(name) ? TOP_ORDER.indexOf(name) : TOP_ORDER.length);
  return [...f.folders.values()].sort((a, b) => rank(a.name) - rank(b.name) || a.name.localeCompare(b.name));
}

/**
 * Node repository: category tree with search, drag nodes onto
 * the canvas or double-click to add them.
 */
export function Repository(props: {
  nodes: NodeDescription[];
  onAdd: (factory: string) => void;
  onSelect: (node: NodeDescription) => void;
  /** controlled search (the Tab quick-add mirrors its typing here) */
  query?: string;
  onQuery?: (q: string) => void;
  /** factory to highlight (quick-add selection) */
  highlight?: string | null;
}) {
  const [ownQuery, setOwnQuery] = useState('');
  const query = props.query ?? ownQuery;
  const setQuery = props.onQuery ?? setOwnQuery;
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const filtered = useMemo(() => matchingNodes(props.nodes, query), [props.nodes, query]);
  const tree = useMemo(() => buildTree(filtered), [filtered]);

  const toggle = (path: string) => {
    const next = new Set(collapsed);
    if (next.has(path)) {
      next.delete(path);
    } else {
      next.add(path);
    }
    setCollapsed(next);
  };

  const renderFolder = (f: Folder, depth: number): React.ReactNode => {
    const open = query.trim() !== '' || !collapsed.has(f.path);
    return (
      <div key={f.path}>
        <div className="verknode-repo-folder" style={{ paddingLeft: depth * 12 + 4 }} onClick={() => toggle(f.path)}>
          <span className="verknode-repo-caret">{open ? '▾' : '▸'}</span>
          <span className="verknode-repo-foldericon">🗀</span> {f.name}
        </div>
        {open && (
          <>
            {sortedFolders(f).map(c => renderFolder(c, depth + 1))}
            {[...f.nodes]
              .sort((a, b) => a.name.localeCompare(b.name))
              .map(n => (
                <div
                  key={n.factory}
                  className={`verknode-repo-node ${props.highlight === n.factory ? 'verknode-repo-highlight' : ''} ${missing(n).length ? 'verknode-repo-missing' : ''}`}
                  ref={el => {
                    if (el && props.highlight === n.factory) {
                      el.scrollIntoView({ block: 'nearest' });
                    }
                  }}
                  style={{ paddingLeft: (depth + 1) * 12 + 10 }}
                  draggable
                  title={
                    n.shortDescription +
                    (missing(n).length ? `\nNeeds ${missing(n).join(', ')} - not installed (pip install ${missing(n).join(' ')})` : '')
                  }
                  onDragStart={e => {
                    e.dataTransfer.setData(NODE_DRAG_MIME, n.factory);
                    e.dataTransfer.effectAllowed = 'copy';
                  }}
                  onClick={() => props.onSelect(n)}
                  onDoubleClick={() => props.onAdd(n.factory)}
                >
                  <NodeIcon nodeType={n.nodeType} icon={n.icon} size={16} category={n.category} />
                  <span>{n.name}</span>
                  {n.custom && (
                    <span className="verknode-custom-mark" title={`Custom node: ${n.source}`}>
                      ✎
                    </span>
                  )}
                </div>
              ))}
          </>
        )}
      </div>
    );
  };

  return (
    <div className="verknode-repo">
      <div className="verknode-panel-title">Node Repository</div>
      <input
        className="verknode-repo-search jp-mod-styled"
        placeholder="Search nodes…"
        value={query}
        onChange={e => setQuery(e.target.value)}
      />
      <div className="verknode-repo-tree">{sortedFolders(tree).map(f => renderFolder(f, 0))}</div>
    </div>
  );
}

/**
 * "Node Description" view.
 */
/** The packages a node needs that are not installed (not the optional ones). */
function missing(n: NodeDescription): string[] {
  return (n.requires ?? []).filter(r => !r.installed && !r.optional).map(r => r.package);
}

export function Description({ node }: { node: NodeDescription | null }) {
  if (!node) {
    return (
      <div className="verknode-desc">
        <div className="verknode-panel-title">Node Description</div>
        <div className="verknode-desc-empty">Select a node to see its description.</div>
      </div>
    );
  }
  const ports = (title: string, list: typeof node.inPorts) =>
    list.length > 0 && (
      <>
        <h4>{title}</h4>
        <dl>
          {list.map(p => (
            <React.Fragment key={p.index}>
              <dt>
                <span className={`verknode-desc-port verknode-desc-port-${p.type}`} /> {p.index}: {p.name}
                {p.optional ? ' (optional)' : ''}
              </dt>
              <dd>{p.description || PORT_TYPE_NAMES[p.type]}</dd>
            </React.Fragment>
          ))}
        </dl>
      </>
    );
  return (
    <div className="verknode-desc">
      <div className="verknode-panel-title">Node Description</div>
      <div className="verknode-desc-body">
        <div className="verknode-desc-head">
          <NodeIcon nodeType={node.nodeType} icon={node.icon} size={28} category={node.category} />
          <div>
            <h3>{node.name}</h3>
            <div className="verknode-desc-cat">{node.category.replace(/\//g, ' › ')}</div>
            {node.custom && <div className="verknode-desc-cat verknode-mono">custom: {node.source}</div>}
          </div>
        </div>
        <p dangerouslySetInnerHTML={{ __html: node.description || node.shortDescription }} />
        {node.requires && node.requires.length > 0 && (
          <div className="verknode-desc-needs">
            <b>Needs</b>{' '}
            {node.requires.map((r, i) => (
              <span
                key={r.package}
                className={r.installed ? 'verknode-needs-ok' : r.optional ? 'verknode-needs-optional' : 'verknode-needs-missing'}
                title={r.installed ? 'installed' : `not installed: pip install ${r.package}`}
              >
                {i > 0 ? ', ' : ''}
                {r.package}
                {r.optional ? ' (optional)' : ''} {r.installed ? '✓' : '✗'}
              </span>
            ))}
            {node.requires.some(r => !r.installed && !r.optional) && (
              <div className="verknode-needs-hint">
                Not installed: <code>pip install {node.requires.filter(r => !r.installed && !r.optional).map(r => r.package).join(' ')}</code>
              </div>
            )}
          </div>
        )}
        {node.fields.length > 0 && (
          <>
            <h4>Dialog Options</h4>
            <dl>
              {node.fields.map(f => (
                <React.Fragment key={f.key}>
                  <dt>{f.label}</dt>
                  {f.description && <dd>{f.description}</dd>}
                </React.Fragment>
              ))}
            </dl>
          </>
        )}
        {ports('Input Ports', node.inPorts)}
        {ports('Output Ports', node.outPorts)}
      </div>
    </div>
  );
}
