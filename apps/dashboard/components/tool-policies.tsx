"use client";

import { Plus, Trash } from "@phosphor-icons/react";
import { FormEvent, useEffect, useState } from "react";

import { apiGet, apiSend } from "@/lib/api";
import { MCPTool, Policy } from "@/lib/types";

type Kind = "block_tool" | "require_approval" | "validate_args" | "guard_signal" | "token_budget" | "cost_budget";

const KINDS: { kind: Kind; label: string; hint: string; needsTool: boolean }[] = [
  { kind: "require_approval", label: "Ask before running", hint: "Every call waits for a person on Approvals.", needsTool: true },
  { kind: "block_tool", label: "Block the tool", hint: "Every call is refused.", needsTool: true },
  { kind: "validate_args", label: "Keep paths in a folder", hint: "Calls whose path leaves the folders you list are refused.", needsTool: true },
  { kind: "guard_signal", label: "React to flagged runs", hint: "Once the guard flags content a run read, this tool needs approval or is blocked.", needsTool: true },
  { kind: "token_budget", label: "Token budget", hint: "Tool calls stop once a conversation has used this many tokens.", needsTool: false },
  { kind: "cost_budget", label: "Cost budget", hint: "Tool calls stop once a conversation has cost this much.", needsTool: false }
];

/** One sentence for what a tool policy does, so the list reads without opening the JSON. */
export function describePolicy(policy: Policy): string {
  const tool = policy.target_tool ?? "any tool";
  const c = (policy.conditions ?? {}) as Record<string, unknown>;
  const a = (policy.action ?? {}) as Record<string, unknown>;
  switch (policy.rule_type) {
    case "block_tool":
      return `Blocks every call to ${tool}.`;
    case "require_approval":
      return `Every call to ${tool} waits for a person to approve it.`;
    case "validate_args": {
      const prefixes = Array.isArray(c.allow_prefixes) ? (c.allow_prefixes as string[]).join(", ") : "";
      return c.path_arg
        ? `Calls to ${tool} are refused unless "${String(c.path_arg)}" is under ${prefixes || "an allowed folder"}.`
        : `Calls to ${tool} are refused when an argument has a disallowed value.`;
    }
    case "token_budget":
      return `Tool calls stop once a conversation has used ${String(c.max_tokens ?? "?")} tokens.`;
    case "cost_budget":
      return `Tool calls stop once a conversation has cost $${String(c.max_cost ?? "?")}.`;
    case "guard_signal":
      return a.verdict === "block"
        ? `After the guard flags content in a run, calls to ${tool} are blocked.`
        : `After the guard flags content in a run, calls to ${tool} need approval.`;
    default:
      return policy.rule_type;
  }
}

const EMPTY = {
  kind: "require_approval" as Kind,
  name: "",
  tool: "",
  reason: "",
  pathArg: "path",
  folders: "notes/",
  verdict: "require_approval",
  limit: ""
};

function NewToolPolicy({ onClose, onSaved }: { onClose: () => void; onSaved: (message: string) => void }) {
  const [form, setForm] = useState(EMPTY);
  const [tools, setTools] = useState<MCPTool[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const kind = KINDS.find((k) => k.kind === form.kind)!;
  const set = (patch: Partial<typeof EMPTY>) => setForm((f) => ({ ...f, ...patch }));

  useEffect(() => {
    apiGet<MCPTool[]>("/api/mcp/tools").then(setTools).catch(() => undefined);
  }, []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (kind.needsTool && !form.tool.trim()) return setError("Pick the tool this policy applies to.");
    const limit = Number(form.limit);
    if (!kind.needsTool && !(limit > 0)) return setError("Enter a limit above zero.");

    let conditions: Record<string, unknown> = {};
    let action: Record<string, unknown> = form.reason.trim() ? { reason: form.reason.trim() } : {};
    if (form.kind === "validate_args")
      conditions = {
        path_arg: form.pathArg.trim() || "path",
        allow_prefixes: form.folders.split(",").map((s) => s.trim()).filter(Boolean)
      };
    if (form.kind === "guard_signal") {
      conditions = { run_tainted: true };
      action = { ...action, verdict: form.verdict };
    }
    if (form.kind === "token_budget") conditions = { max_tokens: limit };
    if (form.kind === "cost_budget") conditions = { max_cost: limit };

    const name = form.name.trim() || `${kind.label}${kind.needsTool ? `: ${form.tool.trim()}` : ""}`;
    setSaving(true);
    try {
      await apiSend("/api/policies", {
        method: "POST",
        body: JSON.stringify({
          name,
          rule_type: form.kind,
          target_tool: kind.needsTool ? form.tool.trim() : null,
          priority: 100,
          conditions,
          action
        })
      });
      onSaved(`Created "${name}". It applies from the next tool call.`);
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
        aria-labelledby="tool-policy-title"
        onMouseDown={(e) => e.stopPropagation()}
        onSubmit={submit}
      >
        <div className="modal-head">
          <div>
            <h2 id="tool-policy-title">New tool policy</h2>
            <p className="help">Decides a tool call by the tool and its arguments, before it runs.</p>
          </div>
        </div>
        <div className="modal-body">
          <div className="field">
            <span className="label" id="kind-label">What should happen</span>
            <div className="choice-list" role="radiogroup" aria-labelledby="kind-label">
              {KINDS.map((k) => (
                <label className="choice" key={k.kind}>
                  <input type="radio" name="kind" checked={form.kind === k.kind} onChange={() => set({ kind: k.kind })} />
                  <span>
                    <strong>{k.label}</strong>
                    <span className="help">{k.hint}</span>
                  </span>
                </label>
              ))}
            </div>
          </div>

          {kind.needsTool && (
            <label className="field">
              <span className="label">Tool</span>
              <input
                className="input mono"
                list="known-tools"
                value={form.tool}
                onChange={(e) => set({ tool: e.target.value })}
                placeholder="write_file"
                autoFocus
              />
              <datalist id="known-tools">
                {tools.map((t) => (
                  <option key={`${t.server_id}:${t.name}`} value={t.name}>
                    {t.server_name}
                  </option>
                ))}
              </datalist>
            </label>
          )}

          {form.kind === "validate_args" && (
            <div className="form-grid">
              <label className="field">
                <span className="label">Path argument</span>
                <input className="input mono" value={form.pathArg} onChange={(e) => set({ pathArg: e.target.value })} />
              </label>
              <label className="field">
                <span className="label">Allowed folders</span>
                <input className="input mono" value={form.folders} onChange={(e) => set({ folders: e.target.value })} />
                <span className="help">Comma separated, relative to the workspace.</span>
              </label>
            </div>
          )}

          {form.kind === "guard_signal" && (
            <label className="field">
              <span className="label">Then</span>
              <select className="select" value={form.verdict} onChange={(e) => set({ verdict: e.target.value })}>
                <option value="require_approval">Ask a person first</option>
                <option value="block">Block the call</option>
              </select>
            </label>
          )}

          {!kind.needsTool && (
            <label className="field">
              <span className="label">{form.kind === "cost_budget" ? "Limit in US dollars" : "Limit in tokens"}</span>
              <input
                className="input"
                type="number"
                min={0}
                step={form.kind === "cost_budget" ? "0.01" : "1000"}
                value={form.limit}
                onChange={(e) => set({ limit: e.target.value })}
                placeholder={form.kind === "cost_budget" ? "0.50" : "50000"}
                autoFocus
              />
            </label>
          )}

          <div className="form-grid">
            <label className="field">
              <span className="label">Name <span className="subtle">(optional)</span></span>
              <input className="input" value={form.name} onChange={(e) => set({ name: e.target.value })} placeholder={kind.label} />
            </label>
            <label className="field">
              <span className="label">Reason shown <span className="subtle">(optional)</span></span>
              <input className="input" value={form.reason} onChange={(e) => set({ reason: e.target.value })} placeholder="Writes need a review" />
            </label>
          </div>

          {error && (
            <p className="notice error" role="alert">
              {error}
            </p>
          )}
        </div>
        <div className="modal-foot">
          <span className="help">Applies from the next tool call.</span>
          <div className="actions">
            <button type="button" className="btn btn-ghost" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="btn btn-primary" disabled={saving}>
              {saving ? "Creating…" : "Create policy"}
            </button>
          </div>
        </div>
      </form>
    </div>
  );
}

/** The policy engine's tool policies, as a section of the Guardrails page. */
export function ToolPolicies({ onNote }: { onNote: (note: { text: string; error?: boolean }) => void }) {
  const [policies, setPolicies] = useState<Policy[] | null>(null);
  const [creating, setCreating] = useState(false);

  async function load() {
    setPolicies(await apiGet<Policy[]>("/api/policies"));
  }
  useEffect(() => {
    load().catch((e) => onNote({ text: String(e), error: true }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function run(work: () => Promise<unknown>, message: string) {
    try {
      await work();
      onNote({ text: message });
      await load();
    } catch (e) {
      onNote({ text: String(e instanceof Error ? e.message : e), error: true });
    }
  }

  const toggle = (p: Policy) =>
    run(
      () => apiSend(`/api/policies/${p.id}`, { method: "PATCH", body: JSON.stringify({ enabled: !p.enabled }) }),
      `${p.name} is ${p.enabled ? "off" : "on"}.`
    );
  const remove = (p: Policy) => {
    if (!window.confirm(`Delete the tool policy "${p.name}"?`)) return;
    return run(() => apiSend(`/api/policies/${p.id}`, { method: "DELETE" }), `Deleted ${p.name}.`);
  };

  return (
    <section className="section" aria-labelledby="tool-policies-title">
      <div className="section-head">
        <div>
          <h2 id="tool-policies-title">Tool policies</h2>
          <p>Decide a tool call by the tool and its arguments: approval, blocking, allowed folders, budgets.</p>
        </div>
        <button className="btn btn-sm" onClick={() => setCreating(true)}>
          <Plus size={14} weight="bold" aria-hidden /> New tool policy
        </button>
      </div>

      {policies === null ? (
        <div className="surface surface-pad">
          <div className="skeleton" style={{ width: "50%" }} />
        </div>
      ) : policies.length === 0 ? (
        <div className="surface empty">
          <strong>No tool policies</strong>
          <p>Every tool call runs unless the guard stops it.</p>
        </div>
      ) : (
        <div className="rows">
          {policies.map((p) => (
            <div className={`row${p.enabled ? "" : " dim"}`} key={p.id}>
              <div className="row-main">
                <div className="row-title">
                  <span>{p.name}</span>
                  {p.target_tool && <span className="chip mono">{p.target_tool}</span>}
                </div>
                <p className="row-desc">{describePolicy(p)}</p>
              </div>
              <div className="row-side">
                <label className="switch" title={p.enabled ? "On" : "Off"}>
                  <input
                    type="checkbox"
                    role="switch"
                    checked={p.enabled}
                    onChange={() => toggle(p)}
                    aria-label={`${p.name} on`}
                  />
                  <span aria-hidden />
                </label>
                <button className="btn btn-ghost btn-sm icon-only" onClick={() => remove(p)} aria-label={`Delete ${p.name}`}>
                  <Trash size={16} aria-hidden />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {creating && (
        <NewToolPolicy
          onClose={() => setCreating(false)}
          onSaved={(message) => {
            setCreating(false);
            onNote({ text: message });
            load().catch(() => undefined);
          }}
        />
      )}
    </section>
  );
}
