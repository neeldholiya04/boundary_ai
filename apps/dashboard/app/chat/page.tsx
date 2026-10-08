"use client";

import { ArrowUp, CaretRight, HourglassMedium, NotePencil, ShieldWarning, Wrench } from "@phosphor-icons/react";
import Link from "next/link";
import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from "react";

import { apiGet, apiSend } from "@/lib/api";
import { timeAgo } from "@/lib/format";
import { ChatResponse, Conversation, Message } from "@/lib/types";
import { useLiveRefresh } from "@/lib/use-live-refresh";

const EXAMPLES = ["List the files in the workspace", "Write notes/demo.txt saying hello", "Search the files for “guarded”"];

const STATUS_LABEL: Record<string, string> = {
  waiting_approval: "Waiting for approval",
  completed: "Done",
  blocked: "Blocked",
  failed: "Failed",
  denied: "Denied",
  running: "Running",
  idle: "New"
};

function statusTone(status: string) {
  if (status === "waiting_approval") return "warn";
  if (status === "blocked" || status === "failed" || status === "denied") return "danger";
  return "";
}

function oneLine(value: string, fallback: string, max = 48) {
  const text = value.replace(/\s+/g, " ").trim();
  if (!text) return fallback;
  return text.length <= max ? text : `${text.slice(0, max).trimEnd()}…`;
}

function pretty(content: string) {
  try {
    return JSON.stringify(JSON.parse(content), null, 2);
  } catch {
    return content;
  }
}

function meta(message: Message): Record<string, unknown> {
  const m = message.metadata as unknown;
  if (!m) return {};
  if (typeof m === "string") {
    try {
      return JSON.parse(m) as Record<string, unknown>;
    } catch {
      return {};
    }
  }
  return m as Record<string, unknown>;
}

function ToolMessage({ message }: { message: Message }) {
  const [open, setOpen] = useState(false);
  const toolName = String(meta(message).tool_name ?? "a tool");
  return (
    <div className={`msg-tool${open ? " open" : ""}`}>
      <button type="button" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <Wrench size={14} aria-hidden />
        <span>
          Used <span className="mono">{toolName}</span>
        </span>
        <CaretRight size={12} className="caret" aria-hidden />
      </button>
      {open && <pre className="code">{pretty(message.content)}</pre>}
    </div>
  );
}

// Messages stored before notices were tagged: recognised by the fixed text the agent wrote.
const OLD_BLOCKED = [
  "Request blocked by the guard",
  "Tool call blocked",
  "The answer was withheld",
  "Tool output blocked",
  "I removed the secret",
  "Stopped before running",
  "Approved tool call was blocked",
];
const OLD_WAITING = ["Tool call requires approval", "Content is waiting for human review"];

function noticeKind(message: Message): string | null {
  if (message.role !== "assistant") return null;
  const tagged = meta(message).notice;
  if (typeof tagged === "string") return tagged;
  if (OLD_WAITING.some((p) => message.content.startsWith(p))) return "waiting";
  if (OLD_BLOCKED.some((p) => message.content.startsWith(p))) return "blocked";
  if (/\(Reference: run [0-9a-f]+; details are in the logs\.\)$/.test(message.content)) return "blocked";
  return null;
}

/** A block, a stop or an approval wait: shown as a notice, not as the agent's answer. */
function Notice({ kind, text }: { kind: string; text: string }) {
  const waiting = kind === "waiting";
  // "… (Reference: run 1a2b3c4d; details are in the logs.)" -> the sentence, then a quiet reference.
  const match = text.match(/^(.*?)\s*\(Reference: run ([0-9a-f]+); details are in the logs\.\)$/s);
  const body = match ? match[1] : text;
  return (
    <div className={`msg-notice ${waiting ? "waiting" : "blocked"}`} role="status">
      {waiting ? <HourglassMedium size={16} aria-hidden /> : <ShieldWarning size={16} aria-hidden />}
      <div>
        <p>{body}</p>
        {match && (
          <p className="msg-notice-ref">
            Reference {match[2]} ·{" "}
            <Link href={`/logs?q=${match[2]}`}>details in Logs</Link>
          </p>
        )}
        {waiting && (
          <p className="msg-notice-ref">
            <Link href="/approvals">Review on Approvals</Link>
          </p>
        )}
      </div>
    </div>
  );
}

export default function ChatPage() {
  const [conversations, setConversations] = useState<Conversation[] | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [labels, setLabels] = useState<Record<string, string>>({});
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [traceUrl, setTraceUrl] = useState<string | null>(null);
  const [guardNotices, setGuardNotices] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const pickedFirst = useRef(false);
  const threadRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  const selected = conversations?.find((c) => c.id === selectedId) ?? null;
  const waiting = Boolean(selected?.pending_approval);

  function labelFor(c: Conversation) {
    return labels[c.id] ?? oneLine(c.title === "New conversation" ? "" : c.title, "New chat");
  }

  /** Conversations are named after their first message; fetch it once for the ones we haven't seen. */
  async function warmLabels(items: Conversation[]) {
    const missing = items.filter((c) => !labels[c.id]).slice(0, 30);
    const found = await Promise.all(
      missing.map(async (c) => {
        const data = await apiGet<Message[]>(`/api/conversations/${c.id}/messages`);
        return [c.id, oneLine(data.find((m) => m.role === "user")?.content ?? "", "New chat")] as const;
      })
    );
    if (found.length) setLabels((current) => ({ ...current, ...Object.fromEntries(found) }));
  }

  async function loadConversations() {
    const data = await apiGet<Conversation[]>("/api/conversations");
    setConversations(data);
    warmLabels(data).catch(() => undefined);
    if (!pickedFirst.current) {
      pickedFirst.current = true;
      // ?c=<id> opens a conversation (Approvals links here); otherwise the latest one.
      const wanted = new URLSearchParams(window.location.search).get("c");
      setSelectedId(data.find((c) => c.id === wanted)?.id ?? data[0]?.id ?? null);
    }
  }

  async function loadMessages(id: string) {
    const data = await apiGet<Message[]>(`/api/conversations/${id}/messages`);
    setMessages(data);
  }

  useEffect(() => {
    loadConversations().catch((e) => setError(String(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (selectedId) loadMessages(selectedId).catch((e) => setError(String(e)));
    else setMessages([]);
  }, [selectedId]);

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight });
  }, [messages, busy]);

  useLiveRefresh(() => {
    loadConversations().catch(() => undefined);
    if (selectedId) loadMessages(selectedId).catch(() => undefined);
  });

  function select(id: string | null) {
    setSelectedId(id);
    setGuardNotices([]); // notices belong to the last reply in the chat they came from
    setTraceUrl(null);
    setError(null);
    window.history.replaceState(null, "", id ? `/chat?c=${id}` : "/chat");
    if (!id) inputRef.current?.focus();
  }

  async function send(event?: FormEvent) {
    event?.preventDefault();
    const text = draft.trim();
    if (!text || busy || waiting) return;
    setBusy(true);
    setError(null);
    setGuardNotices([]);
    // Show the message straight away; the server's copy replaces it on reload.
    setMessages((m) => [
      ...m,
      { id: "pending", role: "user", content: text, metadata: null, created_at: new Date().toISOString() }
    ]);
    setDraft("");
    try {
      const response = await apiSend<ChatResponse>("/api/chat", {
        method: "POST",
        body: JSON.stringify({ conversation_id: selectedId, message: text })
      });
      setTraceUrl(response.trace_url ?? null);
      setGuardNotices(response.guard_notices ?? []);
      setSelectedId(response.conversation_id);
      window.history.replaceState(null, "", `/chat?c=${response.conversation_id}`);
      await Promise.all([loadConversations(), loadMessages(response.conversation_id)]);
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
      setDraft(text);
      setMessages((m) => m.filter((x) => x.id !== "pending"));
    } finally {
      setBusy(false);
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      send();
    }
  }

  const status = selected?.latest_run_status ?? "idle";

  return (
    <div className="page fill chat">
      <aside className="chat-list" aria-label="Conversations">
        <button className="btn chat-new" onClick={() => select(null)}>
          <NotePencil size={16} aria-hidden /> New chat
        </button>
        <nav className="chat-items">
          {conversations === null ? (
            <div className="skeleton" style={{ width: "80%", margin: 8 }} />
          ) : conversations.length === 0 ? (
            <p className="help" style={{ padding: 8 }}>
              No conversations yet.
            </p>
          ) : (
            conversations.map((c) => (
              <button
                key={c.id}
                className="chat-item"
                aria-current={c.id === selectedId ? "page" : undefined}
                onClick={() => select(c.id)}
              >
                <span className="chat-item-title">{labelFor(c)}</span>
                <span className="chat-item-meta">
                  {c.pending_approval ? (
                    <span className="text-warn">Needs approval</span>
                  ) : (
                    timeAgo(c.updated_at)
                  )}
                </span>
              </button>
            ))
          )}
        </nav>
      </aside>

      <section className="chat-main" aria-label="Conversation">
        <header className="chat-head">
          <div className="row-main">
            <h1 className="chat-title">{selected ? labelFor(selected) : "New chat"}</h1>
            <p className="meta">
              {selected
                ? `${selected.spent_tokens.toLocaleString()} tokens · $${selected.spent_cost.toFixed(4)}`
                : "The agent can read and write files through its tools. Guardrails check every step."}
            </p>
          </div>
          <div className="actions">
            {traceUrl && (
              <a className="btn btn-ghost btn-sm" href={traceUrl} target="_blank" rel="noreferrer">
                Trace
              </a>
            )}
            {selected && <span className={`badge ${statusTone(status)}`}>{STATUS_LABEL[status] ?? status}</span>}
          </div>
        </header>

        <div className="chat-thread" ref={threadRef}>
          <div className="chat-thread-inner">
            {messages.length === 0 && !busy ? (
              <div className="chat-empty">
                <h2>What should the agent do?</h2>
                <div className="chat-examples">
                  {EXAMPLES.map((example) => (
                    <button
                      key={example}
                      className="btn"
                      onClick={() => {
                        setDraft(example);
                        inputRef.current?.focus();
                      }}
                    >
                      {example}
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              messages.map((message) =>
                message.role === "tool" ? (
                  <ToolMessage key={message.id} message={message} />
                ) : noticeKind(message) ? (
                  <Notice key={message.id} kind={noticeKind(message)!} text={message.content} />
                ) : (
                  <div key={message.id} className={`msg ${message.role === "user" ? "msg-user" : "msg-agent"}`}>
                    {message.content}
                  </div>
                )
              )
            )}
            {busy && (
              <div className="msg msg-agent msg-working" role="status">
                Working
                <span className="dots" aria-hidden>
                  <i />
                  <i />
                  <i />
                </span>
              </div>
            )}
            {guardNotices.map((notice) => (
              <p key={notice} className="notice warn" role="status">
                <ShieldWarning size={16} aria-hidden /> {notice}
              </p>
            ))}
            {waiting && (
              <div className="notice warn chat-waiting">
                <span>Waiting for approval: {selected?.pending_approval_reason}</span>
                <Link className="btn btn-sm" href="/approvals">
                  Review
                </Link>
              </div>
            )}
            {error && (
              <p className="notice error" role="alert">
                {error}
              </p>
            )}
          </div>
        </div>

        <form className="composer" onSubmit={send}>
          <textarea
            ref={inputRef}
            className="composer-input"
            rows={1}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder={waiting ? "Waiting for a decision on Approvals" : "Message the agent"}
            aria-label="Message"
            disabled={waiting}
          />
          <button
            className="btn btn-accent composer-send"
            aria-label="Send"
            disabled={busy || waiting || !draft.trim()}
          >
            <ArrowUp size={16} weight="bold" aria-hidden />
          </button>
        </form>
        <p className="composer-hint">Enter to send, Shift+Enter for a new line.</p>
      </section>
    </div>
  );
}
