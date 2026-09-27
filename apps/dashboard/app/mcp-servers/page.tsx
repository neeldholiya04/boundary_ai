"use client";

import { FormEvent, useEffect, useState } from "react";

import { apiGet, apiSend } from "@/lib/api";
import { MCPServer, MCPTool } from "@/lib/types";
import { useLiveRefresh } from "@/lib/use-live-refresh";

const DEFAULT_CONFIG = "{\n  \"url\": \"https://example.com/mcp\"\n}";

function statusClass(status: string) {
  if (status === "connected") {
    return "success";
  }
  if (status === "auth_error" || status === "discovery_failed" || status === "execution_failed") {
    return "danger";
  }
  if (status === "pending") {
    return "warning";
  }
  return "";
}

export default function MCPServersPage() {
  const [servers, setServers] = useState<MCPServer[]>([]);
  const [tools, setTools] = useState<MCPTool[]>([]);
  const [status, setStatus] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [transport, setTransport] = useState("streamable_http");
  const [config, setConfig] = useState(DEFAULT_CONFIG);

  async function loadAll() {
    const [serverData, toolData] = await Promise.all([
      apiGet<MCPServer[]>("/api/mcp/servers"),
      apiGet<MCPTool[]>("/api/mcp/tools")
    ]);
    setServers(serverData);
    setTools(toolData);
  }

  useEffect(() => {
    loadAll().catch((error) => setStatus(String(error)));
  }, []);

  useLiveRefresh(() => {
    loadAll().catch(() => undefined);
  });

  async function createServer(event: FormEvent) {
    event.preventDefault();
    try {
      await apiSend("/api/mcp/servers", {
        method: "POST",
        body: JSON.stringify({
          name,
          transport,
          enabled: true,
          config: JSON.parse(config)
        })
      });
      setName("");
      setTransport("streamable_http");
      setConfig(DEFAULT_CONFIG);
      setStatus("Server registered.");
      await loadAll();
    } catch (error) {
      setStatus(`Server config is invalid or request failed: ${String(error)}`);
    }
  }

  async function refreshServer(serverId: string) {
    try {
      await apiSend(`/api/mcp/servers/${serverId}/refresh`, { method: "POST", body: "{}" });
      await loadAll();
      setStatus("Server refreshed.");
    } catch (error) {
      setStatus(String(error));
    }
  }

  async function toggleServer(server: MCPServer) {
    try {
      await apiSend(`/api/mcp/servers/${server.id}`, {
        method: "PATCH",
        body: JSON.stringify({ enabled: !server.enabled })
      });
      await loadAll();
    } catch (error) {
      setStatus(String(error));
    }
  }

  return (
    <div className="page page-fixed">
      <header className="page-header">
        <div>
          <h2>MCP</h2>
          {status && <p className="page-status">{status}</p>}
        </div>
        <span className="badge">{tools.length} tools</span>
      </header>

      <div className="page-body-scroll">
        <div className="grid two">
          <section className="panel">
            <h3>Add Server</h3>
            <form className="stack" onSubmit={createServer}>
              <div className="field">
                <label>Name</label>
                <input value={name} onChange={(event) => setName(event.target.value)} />
              </div>
              <div className="field">
                <label>Transport</label>
                <select value={transport} onChange={(event) => setTransport(event.target.value)}>
                  <option value="streamable_http">streamable_http</option>
                  <option value="sse">sse</option>
                  <option value="stdio">stdio</option>
                </select>
              </div>
              <div className="field">
                <label>Config JSON</label>
                <textarea value={config} onChange={(event) => setConfig(event.target.value)} />
              </div>
              <button className="button">Register</button>
            </form>
          </section>

          <section className="panel stack">
            <div className="row wrap" style={{ justifyContent: "space-between" }}>
              <h3>Servers</h3>
              <button className="button secondary" onClick={() => loadAll()}>
                Refresh
              </button>
            </div>
            <div className="list list-scroll mcp-server-list">
              {servers.map((server) => (
                <div className="card" key={server.id}>
                  <div className="row wrap" style={{ justifyContent: "space-between" }}>
                    <strong>{server.name}</strong>
                    <span className={`badge ${statusClass(server.status)}`}>{server.status}</span>
                  </div>
                  <p className="muted">
                    {server.transport} / {server.tool_count} tools / {server.enabled ? "enabled" : "disabled"}
                  </p>
                  {server.last_error && <p style={{ color: "var(--danger)" }}>{server.last_error}</p>}
                  <pre>{JSON.stringify(server.config, null, 2)}</pre>
                  <div className="row wrap" style={{ marginTop: 12 }}>
                    <button className="button secondary" onClick={() => refreshServer(server.id)}>
                      Refresh tools
                    </button>
                    <button className="button secondary" onClick={() => toggleServer(server)}>
                      {server.enabled ? "Disable" : "Enable"}
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </section>
        </div>

        <section className="panel stack">
          <h3>Tools</h3>
          <div className="list list-scroll mcp-tools-list">
            {tools.length === 0 && (
              <div className="empty-state">
                <p>No tools discovered.</p>
              </div>
            )}
            {tools.map((tool) => (
              <div className="card" key={`${tool.server_id}-${tool.name}`}>
                <div className="row wrap" style={{ justifyContent: "space-between" }}>
                  <strong>{tool.name}</strong>
                  <span className="badge">{tool.server_name}</span>
                </div>
                <p className="muted">{tool.description ?? "No description provided."}</p>
                <pre>{JSON.stringify(tool.input_schema, null, 2)}</pre>
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}
