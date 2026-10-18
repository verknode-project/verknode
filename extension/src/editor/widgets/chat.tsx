import React, { useContext, useEffect, useRef, useState } from 'react';

import { CanvasLinkContext, CanvasNodeRef, OutputRendererProps } from './registry';

/**
 * The display of the Chat node: the conversation and a box to type in. The node runs in a loop and
 * waits (data.waiting) for the message typed here; sending hands it over (onSend), the model answers
 * further on, and the next time round the conversation shows it. End chat stops the loop; when it
 * does not run, Start chat runs it afresh.
 *
 * A Workflow Agent Chat (data.agent) keeps its conversation: Resume chat goes on with it, New chat
 * starts again. It has the Sandbox checkbox (data.sandbox), Show (its nodes, data.group_nodes) and,
 * under its replies, Show changes (message.nodes); Tab adds the nodes selected on the canvas to the
 * message (as @#id).
 */
interface Message {
  role: 'user' | 'assistant' | 'system';
  content: string;
  meta?: string;
  confirm?: boolean; // (the assistant waits for an OK: Yes / No buttons)
  nodes?: number[]; // (the nodes the reply changed)
}

export function ChatRenderer({ data, width, height, onSend, running, nodeId, nodeRunning }: OutputRendererProps) {
  const stored: Message[] = Array.isArray(data?.messages) ? data.messages : [];
  const agent = !!data?.agent;
  // (an agent chat: whether its own node runs - another part of the workflow may run meanwhile)
  const active = agent ? !!nodeRunning : !!running;
  const waiting = !!data?.waiting && active;
  const link = useContext(CanvasLinkContext);
  // (the message just sent shows at once, before the node shows it)
  const [sent, setSent] = useState<Message | null>(null);
  useEffect(() => setSent(null), [JSON.stringify(stored), waiting]);
  const messages = sent ? [...stored, sent] : stored;
  const [draft, setDraft] = useState('');
  const [offer, setOffer] = useState<CanvasNodeRef[]>([]);
  // (a selection added to a message, or dismissed, is not offered again)
  const settled = useRef('');
  const keyOf = (refs: CanvasNodeRef[]) => refs.map(r => r.id).join();
  const list = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    list.current?.scrollTo({ top: list.current.scrollHeight });
  }, [messages.length, active]);
  useEffect(() => {
    if (waiting) {
      input.current?.focus({ preventScroll: true });
    }
  }, [waiting]);
  const last = messages[messages.length - 1];
  const thinking = active && !waiting && last?.role === 'user';
  const sendText = (text: string) => {
    if (!text || !waiting || !onSend) {
      return;
    }
    setSent({ role: 'user', content: text });
    setDraft('');
    settled.current = offer.length ? keyOf(offer) : settled.current;
    setOffer([]);
    onSend({ text });
  };
  const send = () => sendText(draft.trim());
  const confirm = waiting && last?.role === 'assistant' && !!last.confirm;
  const sandbox = data?.sandbox !== false;
  // (the checkbox: while it runs, it goes to the chat - only while it waits; else it is the setting)
  const canToggle = !!onSend && (!active || waiting);
  const said = stored.some(m => m.role === 'user');
  const groupNodes: number[] = Array.isArray(data?.group_nodes) ? data.group_nodes : [];
  // (the workflow runs something else: the chat cannot start now)
  const blocked = !active && !!running;
  const start = (fresh: boolean) => onSend?.(fresh ? { start: true, new: true } : { start: true });
  // the nodes selected on the canvas, offered to the message (Tab)
  const mentioned = (refs: CanvasNodeRef[]) => refs.filter(r => !draft.includes(`@#${r.id}`));
  const offerSelection = () => {
    if (agent && link) {
      const refs = link.selection(nodeId);
      setOffer(keyOf(refs) === settled.current ? [] : mentioned(refs));
    }
  };
  const insertOffer = () => {
    const el = input.current;
    const text = offer.map(r => `@#${r.id}`).join(' ') + ' ';
    const at = el ? el.selectionStart : draft.length;
    const before = draft.slice(0, at);
    const next = before + (before && !before.endsWith(' ') ? ' ' : '') + text + draft.slice(at);
    setDraft(next);
    settled.current = keyOf(offer);
    setOffer([]);
    window.setTimeout(() => {
      const pos = (before && !before.endsWith(' ') ? before.length + 1 : before.length) + text.length;
      el?.setSelectionRange(pos, pos);
      el?.focus();
    });
  };
  const show = (ids: number[]) => link?.highlight(ids);
  return (
    <div className="verknode-chat nodrag" style={{ width, height }}>
      {agent && (
        <div className="verknode-chat-bar">
          <span className="verknode-chat-where">
            {data?.group ? `Builds in "${data.group}"` : ''}
            {data?.group && groupNodes.length ? ` · ${groupNodes.length} node${groupNodes.length > 1 ? 's' : ''}` : ''}
          </span>
          <button
            className="verknode-chat-link"
            disabled={!groupNodes.length || !link}
            title="Highlight the nodes the agent works on (its group) on the canvas"
            onClick={() => show(groupNodes)}
          >
            ◎ Show
          </button>
        </div>
      )}
      <div className="verknode-chat-messages nowheel" ref={list}>
        {messages
          .filter(m => m.role !== 'system')
          .map((m, i) => (
            <div key={i} className={`verknode-chat-message verknode-chat-${m.role}`}>
              <div className="verknode-chat-bubble">
                <Markdown text={m.content} />
              </div>
              {(m.meta || (agent && m.nodes?.length)) && (
                <div className="verknode-chat-meta">
                  {m.meta}
                  {agent && m.nodes?.length ? (
                    <>
                      {m.meta ? ' · ' : ''}
                      <button className="verknode-chat-link" disabled={!link} title="Highlight the nodes this reply changed" onClick={() => show(m.nodes!)}>
                        Show changes
                      </button>
                    </>
                  ) : null}
                </div>
              )}
            </div>
          ))}
        {(thinking || (sent && !waiting)) && (
          <div className="verknode-chat-message verknode-chat-assistant">
            <div className="verknode-chat-bubble verknode-chat-typing" title="Waiting for the answer">
              <span />
              <span />
              <span />
            </div>
            {active && !waiting && data?.status && <div className="verknode-chat-meta">{data.status}</div>}
          </div>
        )}
        {confirm && (
          <div className="verknode-chat-confirm">
            <button className="jp-mod-styled jp-mod-accept" onClick={() => sendText('Yes')} title="Make the changes">
              Yes, go ahead
            </button>
            <button className="jp-mod-styled jp-mod-reject" onClick={() => sendText('No')} title="Change nothing">
              No
            </button>
          </div>
        )}
        {!active && !agent && (
          <div className="verknode-chat-note">
            {data?.ended ? 'The chat has ended. ' : messages.length ? 'The chat is not running. ' : ''}
            <button className="verknode-chat-link" disabled={!onSend} onClick={() => onSend?.({ start: true })}>
              {messages.length > 1 || data?.ended ? 'New chat' : 'Start chat'}
            </button>
          </div>
        )}
        {!active && agent && (
          <div className="verknode-chat-note">
            {blocked
              ? 'The workflow is running something else: the chat can start when it is done.'
              : said
                ? data?.ended
                  ? 'The chat has ended: resume it to go on with this conversation.'
                  : 'The chat is not running: resume it to go on with this conversation.'
                : ''}
          </div>
        )}
      </div>
      {agent && !sandbox && (
        <div className="verknode-chat-warning" role="alert">
          ⚠ Sandbox is off: the agent works on this workflow's real files - its test runs can write, overwrite
          and delete them, and run scripts - and their results go straight into the current workflow.
        </div>
      )}
      <div className="verknode-chat-input">
        {waiting && offer.length > 0 && (
          <button className="verknode-chat-offer" onMouseDown={e => e.preventDefault()} onClick={insertOffer} title="Add the selected nodes to the message (Tab)">
            <kbd>Tab</kbd> add the selection:{' '}
            {offer
              .slice(0, 4)
              .map(r => `#${r.id} ${r.label || r.name}`)
              .join(', ')}
            {offer.length > 4 ? ` and ${offer.length - 4} more` : ''}
          </button>
        )}
        <textarea
          ref={input}
          className="verknode-widget-input"
          rows={2}
          value={draft}
          placeholder={
            waiting
              ? data?.placeholder || 'Type a message…'
              : thinking
                ? 'Wait for the answer…'
                : active
                  ? 'The workflow is running…'
                  : agent
                    ? said
                      ? 'Resume the chat to type'
                      : 'Begin the chat to type'
                    : 'Start the chat to type'
          }
          disabled={!waiting}
          onFocus={offerSelection}
          onBlur={() => setOffer([])}
          onChange={e => setDraft(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Tab' && !e.shiftKey && offer.length) {
              e.preventDefault();
              insertOffer();
            } else if (e.key === 'Escape' && offer.length) {
              e.stopPropagation();
              settled.current = keyOf(offer);
              setOffer([]);
            } else if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              send();
            }
          }}
        />
        <div className="verknode-chat-buttons">
          {agent && !active ? (
            said ? (
              <>
                <button className="jp-mod-styled jp-mod-accept" disabled={!onSend || blocked} onClick={() => start(false)} title="Go on with this conversation">
                  Resume chat
                </button>
                <button className="jp-mod-styled" disabled={!onSend || blocked} onClick={() => start(true)} title="Start again, without this conversation">
                  New chat
                </button>
              </>
            ) : (
              <button className="jp-mod-styled jp-mod-accept" disabled={!onSend || blocked} onClick={() => start(false)} title="Start the chat">
                Begin chat
              </button>
            )
          ) : (
            <>
              <button className="jp-mod-styled jp-mod-accept" disabled={!waiting || !draft.trim()} onClick={send} title="Send (Enter)">
                Send
              </button>
              <button className="jp-mod-styled" disabled={!waiting} onClick={() => onSend?.({ end: true })} title="End the chat (the loop stops)">
                End chat
              </button>
            </>
          )}
          {agent && (
            <label
              className="verknode-chat-sandbox"
              title={
                sandbox
                  ? 'The agent tries everything on a copy, in a file sandbox: nothing real is written'
                  : "Off: the agent's runs work on the real files and the current workflow"
              }
            >
              <input type="checkbox" checked={sandbox} disabled={!canToggle} onChange={e => onSend?.({ sandbox: e.target.checked, live: active })} />
              Sandbox
            </label>
          )}
        </div>
      </div>
    </div>
  );
}

/** A little Markdown for answers: paragraphs, lists, ```code blocks```, `code` and **bold**. */
function Markdown({ text }: { text: string }) {
  const parts = text.split(/```[a-zA-Z0-9_+-]*\n?([\s\S]*?)```/g);
  return (
    <>
      {parts.map((part, i) =>
        i % 2 === 1 ? (
          <pre key={i} className="verknode-chat-code">
            <code>{part.replace(/\n$/, '')}</code>
          </pre>
        ) : (
          <Blocks key={i} text={part} />
        )
      )}
    </>
  );
}

function Blocks({ text }: { text: string }) {
  const blocks = text.split(/\n{2,}/).filter(b => b.trim());
  return (
    <>
      {blocks.map((block, i) => {
        const lines = block.split('\n').filter(l => l.trim());
        if (lines.length > 1 && lines.every(l => l.trim().startsWith('|'))) {
          const cells = (l: string) => l.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
          const rows = lines.filter(l => !/^\s*\|?\s*:?-{2,}/.test(l)).map(cells);
          return (
            <div key={i} className="verknode-chat-table">
              <table>
                <thead>
                  <tr>
                    {rows[0].map((c, j) => (
                      <th key={j}>{c}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.slice(1).map((r, k) => (
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
        }
        const items = lines.every(l => /^\s*([-*•]|\d+[.)])\s+/.test(l));
        if (items && lines.length) {
          const ordered = /^\s*\d/.test(lines[0]);
          const Tag = ordered ? 'ol' : 'ul';
          return (
            <Tag key={i}>
              {lines.map((l, j) => (
                <li key={j}>
                  <Inline text={l.replace(/^\s*([-*•]|\d+[.)])\s+/, '')} />
                </li>
              ))}
            </Tag>
          );
        }
        return (
          <p key={i}>
            {lines.map((l, j) => (
              <React.Fragment key={j}>
                {j > 0 && <br />}
                <Inline text={l.replace(/^#{1,6}\s+/, '')} />
              </React.Fragment>
            ))}
          </p>
        );
      })}
    </>
  );
}

function Inline({ text }: { text: string }) {
  const parts = text.split(/(`[^`]+`|\*\*[^*]+\*\*)/g);
  return (
    <>
      {parts.map((p, i) =>
        p.startsWith('`') && p.endsWith('`') && p.length > 1 ? (
          <code key={i}>{p.slice(1, -1)}</code>
        ) : p.startsWith('**') && p.endsWith('**') && p.length > 4 ? (
          <b key={i}>{p.slice(2, -2)}</b>
        ) : (
          <React.Fragment key={i}>{p}</React.Fragment>
        )
      )}
    </>
  );
}
