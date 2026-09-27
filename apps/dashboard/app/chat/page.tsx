"use client";

import { FormEvent, useEffect, useRef, useState } from "react";

import { apiGet, apiSend } from "@/lib/api";
import { useLiveRefresh } from "@/lib/use-live-refresh";
import { ChatResponse, Conversation, Message } from "@/lib/types";

function runStatusClass(status: string) {
  if (status === "waiting_approval") {
    return "warning";
  }
  if (status === "blocked" || status === "failed" || status === "denied") {
    return "danger";
  }
  if (status === "completed") {
    return "success";
  }
  return "";
}

function compactLine(value: string, fallback: string, maxLength = 44) {
  const normalized = value.replace(/\s+/g, " ").trim();
  if (!normalized) {
    return fallback;
  }
  if (normalized.length <= maxLength) {
    return normalized;
  }
  return `${normalized.slice(0, maxLength).trimEnd()}...`;
}

function formatToolContent(content: string) {
  try {
    return JSON.stringify(JSON.parse(content), null, 2);
  } catch {
    return content;
  }
}

function readToolMetadata(metadata: Message["metadata"]) {
  if (!metadata) {
    return {};
  }
  if (typeof metadata === "string") {
    try {
      return JSON.parse(metadata) as Record<string, unknown>;
    } catch {
      return {};
    }
  }
  return metadata;
}

function ToolMessage({ message }: { message: Message }) {
  const [expanded, setExpanded] = useState(false);
  const metadata = readToolMetadata(message.metadata);
  const toolName = typeof metadata.tool_name === "string" ? metadata.tool_name : "tool";
  const serverId = typeof metadata.server_id === "string" ? metadata.server_id : null;

  return (
    <article className="chat-bubble tool tool-message">
      <header>
        <span>Tool call</span>
      </header>
      <div className="tool-message-top">
        <div className="tool-message-copy">
          <strong>Used {toolName}</strong>
          {serverId && <span className="tool-message-server">{serverId.slice(0, 8)}</span>}
        </div>
        <button
          type="button"
          className="button secondary tool-message-toggle"
          aria-expanded={expanded}
          onClick={() => setExpanded((current) => !current)}
        >
          {expanded ? "Hide details" : "Show details"}
        </button>
      </div>
      {expanded && <pre className="tool-message-details">{formatToolContent(message.content)}</pre>}
    </article>
  );
}

export default function ChatPage() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [selectedConversationId, setSelectedConversationId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [conversationLabels, setConversationLabels] = useState<Record<string, string>>({});
  const [draft, setDraft] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const transcriptRef = useRef<HTMLDivElement | null>(null);

  const selectedConversation =
    conversations.find((conversation) => conversation.id === selectedConversationId) ?? null;
  const selectedConversationLabel = selectedConversationId
    ? conversationLabels[selectedConversationId] ??
      compactLine(selectedConversation?.title ?? "", "New chat")
    : "New chat";

  async function warmConversationLabels(items: Conversation[]) {
    const unresolved = items.filter((conversation) => !conversationLabels[conversation.id]);
    if (unresolved.length === 0) {
      return;
    }

    const resolved = await Promise.all(
      unresolved.map(async (conversation) => {
        const data = await apiGet<Message[]>(`/api/conversations/${conversation.id}/messages`);
        const firstUserMessage = data.find((message) => message.role === "user")?.content ?? "";
        return [
          conversation.id,
          compactLine(firstUserMessage, conversation.title === "New conversation" ? "New chat" : conversation.title)
        ] as const;
      })
    );

    setConversationLabels((current) => ({
      ...current,
      ...Object.fromEntries(resolved)
    }));
  }

  async function loadConversations() {
    const data = await apiGet<Conversation[]>("/api/conversations");
    setConversations(data);
    warmConversationLabels(data).catch(() => undefined);
    if (!selectedConversationId && data[0]) {
      setSelectedConversationId(data[0].id);
    }
  }

  async function loadMessages(conversationId: string) {
    const data = await apiGet<Message[]>(`/api/conversations/${conversationId}/messages`);
    setMessages(data);
    const firstUserMessage = data.find((message) => message.role === "user")?.content ?? "";
    setConversationLabels((current) => ({
      ...current,
      [conversationId]: compactLine(
        firstUserMessage,
        conversations.find((conversation) => conversation.id === conversationId)?.title === "New conversation"
          ? "New chat"
          : conversations.find((conversation) => conversation.id === conversationId)?.title ?? "New chat"
      )
    }));
  }

  async function createConversation() {
    const response = await apiSend<{ id: string }>("/api/conversations", {
      method: "POST",
      body: JSON.stringify({ title: "New conversation" })
    });
    await loadConversations();
    setSelectedConversationId(response.id);
    setMessages([]);
    setDraft("");
  }

  useEffect(() => {
    loadConversations().catch((error) => setStatus(String(error)));
  }, []);

  useEffect(() => {
    if (selectedConversationId) {
      loadMessages(selectedConversationId).catch((error) => setStatus(String(error)));
    } else {
      setMessages([]);
    }
  }, [selectedConversationId]);

  useEffect(() => {
    if (transcriptRef.current) {
      transcriptRef.current.scrollTop = transcriptRef.current.scrollHeight;
    }
  }, [messages, selectedConversationId]);

  useLiveRefresh(() => {
    loadConversations().catch(() => undefined);
    if (selectedConversationId) {
      loadMessages(selectedConversationId).catch(() => undefined);
    }
  });

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!draft.trim() || busy || selectedConversation?.pending_approval) {
      return;
    }

    setBusy(true);
    setStatus("Running agent...");
    try {
      const response = await apiSend<ChatResponse>("/api/chat", {
        method: "POST",
        body: JSON.stringify({
          conversation_id: selectedConversationId,
          message: draft
        })
      });
      setDraft("");
      setStatus(`${response.status}: ${response.assistant_message}`);
      await loadConversations();
      setSelectedConversationId(response.conversation_id);
      await loadMessages(response.conversation_id);
    } catch (error) {
      setStatus(String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page page-fixed">
      <header className="page-header">
        <div>
          <h2>Chat</h2>
          {status && <p className="page-status">{status}</p>}
        </div>
      </header>

      <div className="chat-shell">
        <aside className="chat-sidebar panel">
          <div className="chat-sidebar-header">
            <div>
              <h3>Conversations</h3>
            </div>
            <button className="button" onClick={() => createConversation()}>
              New chat
            </button>
          </div>

          <div className="chat-conversation-list">
            {conversations.map((conversation) => (
              <button
                key={conversation.id}
                className={`conversation-item${
                  selectedConversationId === conversation.id ? " active" : ""
                }`}
                onClick={() => setSelectedConversationId(conversation.id)}
              >
                <div className="conversation-item-top">
                  <strong className="conversation-title">
                    {conversationLabels[conversation.id] ??
                      compactLine(
                        conversation.title,
                        conversation.title === "New conversation" ? "New chat" : "Untitled"
                      )}
                  </strong>
                  <span
                    className={`badge ${runStatusClass(conversation.latest_run_status)}`}
                  >
                    {conversation.pending_approval ? "approval" : conversation.latest_run_status}
                  </span>
                </div>
              </button>
            ))}
          </div>
        </aside>

        <section className="panel chat-main">
          <div className="chat-main-header">
            <div>
              <h3>{selectedConversationLabel}</h3>
              <p className="muted">
                {selectedConversation
                  ? `${selectedConversation.spent_tokens} tokens / $${selectedConversation.spent_cost.toFixed(2)}`
                  : "New conversation"}
              </p>
              {selectedConversation?.pending_approval && (
                <p className="chat-inline-status warning">
                  Waiting for approval: {selectedConversation.pending_approval_reason}
                </p>
              )}
            </div>
            <span className={`badge ${runStatusClass(selectedConversation?.latest_run_status ?? "idle")}`}>
              {selectedConversation?.latest_run_status ?? "idle"}
            </span>
          </div>

          <div className="chat-transcript" ref={transcriptRef}>
            {messages.length === 0 && (
              <div className="empty-chat">
                <p>Try a simple safe action first.</p>
                <pre>{`list files\nwrite file notes/demo.txt: hello\nsearch files for guarded`}</pre>
              </div>
            )}

            {messages.map((message) => (
              message.role === "tool" ? (
                <ToolMessage key={message.id} message={message} />
              ) : (
                <article key={message.id} className={`chat-bubble ${message.role}`}>
                  <header>
                    <span>{message.role}</span>
                  </header>
                  <div>
                    <p>{message.content}</p>
                  </div>
                </article>
              )
            ))}
          </div>

          <form className="chat-composer" onSubmit={handleSubmit}>
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder={
                selectedConversation?.pending_approval
                  ? "This conversation is waiting for approval."
                  : "Ask the agent to use MCP tools..."
              }
              disabled={busy || selectedConversation?.pending_approval}
            />
            <div className="chat-composer-footer">
              <span className="chat-composer-status">
                {selectedConversation?.pending_approval
                  ? "Approve or deny the pending tool call to continue this thread."
                  : "Local and remote MCP tools are available in this chat."}
              </span>
              <button
                className="button"
                disabled={busy || selectedConversation?.pending_approval || !draft.trim()}
              >
                {busy ? "Running..." : "Send"}
              </button>
            </div>
          </form>
        </section>
      </div>
    </div>
  );
}
