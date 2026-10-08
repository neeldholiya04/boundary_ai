"use client";

import { ArrowClockwise, Plus } from "@phosphor-icons/react";
import { FormEvent, useEffect, useState } from "react";

import { apiGet, apiSend } from "@/lib/api";
import { timeAgo } from "@/lib/format";
import { MCPServer, MCPTool } from "@/lib/types";
import { useLiveRefresh } from "@/lib/use-live-refresh";

const STATUS: Record<string, { label: string; tone: string }> = {
  connected: { label: "Connected", tone: "ok" },
  pending: { label: "Not checked yet", tone: "" },
  auth_error: { label: "Auth failed", tone: "danger" },
  discovery_failed: { label: "Unreachable", tone: "danger" },
  execution_failed: { label: "Tool failed", tone: "danger" }
};

/** Where a server lives: its URL or its command. Configs come masked from the API. */
function where(server: MCPServer): string {
  const c = server.config ?? {};
  if (typeof c.url === "string") return c.url;
  if (typeof c.command === "string") {
    const args = Array.isArray(c.args) ? (c.args as string[]).join(" ") : "";
    return `${c.command.split("/").pop()} ${args}`.trim();
  }
  return server.transport;
}

function params(tool: MCPTool): { name: string; required: boolean }[] {
  const schema = (tool.input_schema ?? {}) as { properties?: Record<string, unknown>; required?: string[] };
  const required = new Set(schema.required ?? []);
  return Object.keys(schema.properties ?? {}).map((name) => ({ name, required: required.has(name) }));
}

function AddServer({ onClose, onSaved }: { onClose: () => void; onSaved: (message: string) => void }) {
  const [name, setName] = useState("");
  const [transport, setTransport] = useState<"streamable_http" | "sse" | "stdio">("streamable_http");
  const [url, setUrl] = useState("");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [headers, setHeaders] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    let config: Record<string, unknown>;
    if (transport === "stdio") {
      if (!command.trim()) return setError("Enter the command that starts the server.");
      config = { command: command.trim(), args: args.split(" ").filter(Boolean) };
    } else {
      if (!/^https?:\/\//.test(url.trim())) return setError("Enter the server's http(s) URL.");
      config = { url: url.trim() };
      if (headers.trim()) {
        try {
          config.headers = JSON.parse(headers);
        } catch {
          return setError("Headers must be a JSON object, for example {\"Authorization\": \"Bearer …\"}.");
        }
      }
    }
    setSaving(true);
    try {
      await apiSend("/api/mcp/servers", {
        method: "POST",
        body: JSON.stringify({ name: name.trim(), transport, enabled: true, config })
      });
      onSaved(`Added ${name.trim()}. Refresh it to discover its tools.`);
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <form
        className="modal narrow"
        role="dialog"
        aria-modal="true"
        aria-labelledby="add-server-title"
        onMouseDown={(e) => e.stopPropagation()}
        onSubmit={submit}
      >
        <div className="modal-head">
          <div>
            <h2 id="add-server-title">Add an MCP server</h2>
            <p className="help">Its tools become available to the agent, behind the same guardrails.</p>
          </div>
        </div>
        <div className="modal-body">
          <label className="field">
            <span className="label">Name</span>
            <input className="input" required value={name} onChange={(e) => setName(e.target.value)} placeholder="docs" autoFocus />
          </label>
          <div className="field">
            <span className="label" id="transport-label">Transport</span>
            <div className="seg" role="radiogroup" aria-labelledby="transport-label">
              {(
                [
                  ["streamable_http", "HTTP"],
                  ["sse", "SSE"],
                  ["stdio", "Local command"]
                ] as const
              ).map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  role="radio"
                  aria-checked={transport === value}
                  className="seg-option"
                  onClick={() => setTransport(value)}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          {transport === "stdio" ? (
            <div className="form-grid">
              <label className="field">
                <span className="label">Command</span>
                <input className="input mono" value={command} onChange={(e) => setCommand(e.target.value)} placeholder="npx" />
              </label>
              <label className="field">
                <span className="label">Arguments</span>
                <input className="input mono" value={args} onChange={(e) => setArgs(e.target.value)} placeholder="-y @acme/mcp" />
              </label>
            </div>
          ) : (
            <>
              <label className="field">
                <span className="label">URL</span>
                <input className="input mono" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://example.com/mcp" />
              </label>
              <label className="field">
                <span className="label">
                  Headers <span className="subtle">(optional, JSON)</span>
                </span>
                <textarea
                  className="textarea mono"
                  rows={3}
                  value={headers}
                  onChange={(e) => setHeaders(e.target.value)}
                  placeholder='{"Authorization": "Bearer …"}'
                />
                <span className="help">Stored on the server and never shown again in full.</span>
              </label>
            </>
          )}
          {error && (
            <p className="notice error" role="alert">
              {error}
            </p>
          )}
        </div>
        <div className="modal-foot">
          <span />
          <div className="actions">
            <button type="button" className="btn btn-ghost" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="btn btn-primary" disabled={saving}>
              {saving ? "Adding…" : "Add server"}
            </button>
          </div>
        </div>
      </form>
    </div>
  );
}

export default function ToolsPage() {
  const [servers, setServers] = useState<MCPServer[] | null>(null);
  const [tools, setTools] = useState<MCPTool[]>([]);
  const [note, setNote] = useState<{ text: string; error?: boolean } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  async function load() {
    const [s, t] = await Promise.all([apiGet<MCPServer[]>("/api/mcp/servers"), apiGet<MCPTool[]>("/api/mcp/tools")]);
    setServers(s);
    setTools(t);
  }

  useEffect(() => {
    load().catch((e) => setNote({ text: String(e), error: true }));
  }, []);
  useLiveRefresh(() => {
    load().catch(() => undefined);
  });

  async function act(key: string, work: () => Promise<unknown>, message: string) {
    setBusy(key);
    try {
      await work();
      setNote({ text: message });
      await load();
    } catch (e) {
      setNote({ text: String(e instanceof Error ? e.message : e), error: true });
    } finally {
      setBusy(null);
    }
  }

  const refresh = (s: MCPServer) =>
    act(s.id, () => apiSend(`/api/mcp/servers/${s.id}/refresh`, { method: "POST", body: "{}" }), `Refreshed ${s.name}.`);
  const toggle = (s: MCPServer) =>
    act(
      s.id,
      () => apiSend(`/api/mcp/servers/${s.id}`, { method: "PATCH", body: JSON.stringify({ enabled: !s.enabled }) }),
      `${s.name} is ${s.enabled ? "off" : "on"}.`
    );

  const byServer = new Map<string, MCPTool[]>();
  for (const t of tools) byServer.set(t.server_name, [...(byServer.get(t.server_name) ?? []), t]);

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Tools</h1>
          <p>The MCP servers the agent can call. Every call still passes the guardrails and tool policies.</p>
        </div>
        <div className="actions">
          <button className="btn btn-primary" onClick={() => setAdding(true)}>
            <Plus size={14} weight="bold" aria-hidden /> Add server
          </button>
        </div>
      </header>

      {note && (
        <p className={`notice${note.error ? " error" : ""}`} role={note.error ? "alert" : "status"}>
          {note.text}
        </p>
      )}

      <section className="section" aria-labelledby="servers-title">
        <div className="section-head">
          <h2 id="servers-title">Servers</h2>
        </div>
        {servers === null ? (
          <div className="surface surface-pad">
            <div className="skeleton" style={{ width: "40%" }} />
          </div>
        ) : servers.length === 0 ? (
          <div className="surface empty">
            <strong>No servers</strong>
            <p>Add an MCP server to give the agent tools.</p>
          </div>
        ) : (
          <div className="rows">
            {servers.map((s) => {
              const st = STATUS[s.status] ?? { label: s.status, tone: "" };
              return (
                <div className={`row${s.enabled ? "" : " dim"}`} key={s.id}>
                  <div className="row-main">
                    <div className="row-title">
                      <span>{s.name}</span>
                      <span className={`badge ${s.enabled ? st.tone : ""}`}>{s.enabled ? st.label : "Off"}</span>
                    </div>
                    <p className="meta">
                      <span className="mono">{where(s)}</span>
                      <span aria-hidden> · </span>
                      {s.tool_count} {s.tool_count === 1 ? "tool" : "tools"}
                      {s.last_discovered_at && (
                        <>
                          <span aria-hidden> · </span>checked {timeAgo(s.last_discovered_at)}
                        </>
                      )}
                    </p>
                    {s.last_error && <p className="text-danger small">{s.last_error}</p>}
                  </div>
                  <div className="row-side">
                    <button
                      className="btn btn-ghost btn-sm"
                      onClick={() => refresh(s)}
                      disabled={busy === s.id || !s.enabled}
                    >
                      <ArrowClockwise size={14} aria-hidden /> {busy === s.id ? "Refreshing…" : "Refresh"}
                    </button>
                    <label className="switch" title={s.enabled ? "On" : "Off"}>
                      <input
                        type="checkbox"
                        role="switch"
                        checked={s.enabled}
                        onChange={() => toggle(s)}
                        disabled={busy === s.id}
                        aria-label={`${s.name} on`}
                      />
                      <span aria-hidden />
                    </label>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>

      <section className="section" aria-labelledby="tools-title">
        <div className="section-head">
          <div>
            <h2 id="tools-title">Available tools</h2>
            <p>{tools.length} tools the agent can choose from. Required arguments are marked.</p>
          </div>
        </div>
        {tools.length === 0 ? (
          <div className="surface empty">
            <p>No tools discovered. Refresh a connected server.</p>
          </div>
        ) : (
          <div className="rows">
            {[...byServer.entries()].map(([server, list]) => [
              <div className="group-label" key={`${server}-label`}>
                {server}
              </div>,
              ...list.map((t) => (
                <div className="row" key={`${t.server_id}:${t.name}`}>
                  <div className="row-main">
                    <div className="row-title">
                      <span className="mono">{t.name}</span>
                    </div>
                    {t.description && <p className="row-desc">{t.description}</p>}
                    {params(t).length > 0 && (
                      <p className="meta mono">
                        {params(t)
                          .map((p) => (p.required ? `${p.name}*` : p.name))
                          .join("  ")}
                      </p>
                    )}
                  </div>
                </div>
              ))
            ])}
          </div>
        )}
      </section>

      {adding && (
        <AddServer
          onClose={() => setAdding(false)}
          onSaved={(message) => {
            setAdding(false);
            setNote({ text: message });
            load().catch(() => undefined);
          }}
        />
      )}
    </div>
  );
}
