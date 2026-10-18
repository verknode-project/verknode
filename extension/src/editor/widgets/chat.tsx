import React, { useEffect, useRef, useState } from 'react';

import { OutputRendererProps } from './registry';

/**
 * The display of the Chat node: the conversation and a box to type in. The node runs in a loop and
 * waits (data.waiting) for the message typed here; sending hands it over (onSend), the model answers
 * further on, and the next time round the conversation shows it. End chat stops the loop; when it
 * does not run, Start chat runs it afresh.
 */
interface Message {
  role: 'user' | 'assistant' | 'system';
  content: string;
  meta?: string;
}

export function ChatRenderer({ data, width, height, onSend, running }: OutputRendererProps) {
  const stored: Message[] = Array.isArray(data?.messages) ? data.messages : [];
  const waiting = !!data?.waiting && !!running;
  // (the message just sent shows at once, before the node shows it)
  const [sent, setSent] = useState<Message | null>(null);
  useEffect(() => setSent(null), [JSON.stringify(stored), waiting]);
  const messages = sent ? [...stored, sent] : stored;
  const [draft, setDraft] = useState('');
  const list = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    list.current?.scrollTo({ top: list.current.scrollHeight });
  }, [messages.length, running]);
  useEffect(() => {
    if (waiting) {
      input.current?.focus({ preventScroll: true });
    }
  }, [waiting]);
  const last = messages[messages.length - 1];
  const thinking = !!running && !waiting && last?.role === 'user';
  const send = () => {
    const text = draft.trim();
    if (!text || !waiting || !onSend) {
      return;
    }
    setSent({ role: 'user', content: text });
    setDraft('');
    onSend({ text });
  };
  return (
    <div className="verknode-chat nodrag" style={{ width, height }}>
      <div className="verknode-chat-messages nowheel" ref={list}>
        {messages
          .filter(m => m.role !== 'system')
          .map((m, i) => (
            <div key={i} className={`verknode-chat-message verknode-chat-${m.role}`}>
              <div className="verknode-chat-bubble">
                <Markdown text={m.content} />
              </div>
              {m.meta && <div className="verknode-chat-meta">{m.meta}</div>}
            </div>
          ))}
        {(thinking || (sent && !waiting)) && (
          <div className="verknode-chat-message verknode-chat-assistant">
            <div className="verknode-chat-bubble verknode-chat-typing" title="Waiting for the answer">
              <span />
              <span />
              <span />
            </div>
          </div>
        )}
        {!running && (
          <div className="verknode-chat-note">
            {data?.ended ? 'The chat has ended. ' : messages.length ? 'The chat is not running. ' : ''}
            <button className="verknode-chat-link" disabled={!onSend} onClick={() => onSend?.({ start: true })}>
              {messages.length > 1 || data?.ended ? 'New chat' : 'Start chat'}
            </button>
          </div>
        )}
      </div>
      <div className="verknode-chat-input">
        <textarea
          ref={input}
          className="verknode-widget-input"
          rows={2}
          value={draft}
          placeholder={
            waiting ? data?.placeholder || 'Type a message…' : thinking ? 'Wait for the answer…' : running ? 'The workflow is running…' : 'Start the chat to type'
          }
          disabled={!waiting}
          onChange={e => setDraft(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              send();
            }
          }}
        />
        <div className="verknode-chat-buttons">
          <button className="jp-mod-styled jp-mod-accept" disabled={!waiting || !draft.trim()} onClick={send} title="Send (Enter)">
            Send
          </button>
          <button className="jp-mod-styled" disabled={!waiting} onClick={() => onSend?.({ end: true })} title="End the chat (the loop stops)">
            End chat
          </button>
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
