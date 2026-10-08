"use client";

import { FormEvent, useEffect, useState } from "react";

import { StartMode } from "@/components/rule-editor";
import { apiGet, apiSend } from "@/lib/api";
import { apiErrorText } from "@/lib/errors";
import { MCPTool, Policy } from "@/lib/types";
import { useEscape } from "@/lib/use-escape";

type Kind = "require_approval" | "block_tool" | "validate_args" | "guard_signal" | "token_budget" | "cost_budget";

export const TOOL_RULE_KINDS: { kind: Kind; label: string; hint: string; needsTool: boolean }[] = [
  { kind: "require_approval", label: "Ask before running", hint: "Every call waits for a person on Approvals.", needsTool: true },
  { kind: "block_tool", label: "Block the tool", hint: "Every call is refused.", needsTool: true },
  { kind: "validate_args", label: "Keep paths in a folder", hint: "Calls whose path leaves the folders you list are refused.", needsTool: true },
  { kind: "guard_signal", label: "React to flagged runs", hint: "Once a run has read content the guard flagged, this tool needs approval or is blocked.", needsTool: true },
  { kind: "token_budget", label: "Token budget", hint: "Tool calls stop once a conversation has used this many tokens.", needsTool: false },
  { kind: "cost_budget", label: "Cost budget", hint: "Tool calls stop once a conversation has cost this much.", needsTool: false }
];

/** One sentence for what a tool rule does, so the list reads without opening it. */
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
      return `Tool calls stop once a conversation has used ${Number(c.max_tokens ?? 0).toLocaleString()} tokens.`;
    case "cost_budget":
      return `Tool calls stop once a conversation has cost $${String(c.max_cost ?? "?")}.`;
    case "guard_signal":
      return a.verdict === "block"
        ? `After the guard flags content a run read, calls to ${tool} are blocked.`
        : `After the guard flags content a run read, calls to ${tool} need approval.`;
    default:
      return policy.rule_type;
  }
}

type Form = {
  kind: Kind;
  name: string;
  tool: string;
  reason: string;
  pathArg: string;
  folders: string;
  verdict: string;
  limit: string;
};

const EMPTY: Form = {
  kind: "require_approval",
  name: "",
  tool: "",
  reason: "",
  pathArg: "path",
  folders: "notes/",
  verdict: "require_approval",
  limit: ""
};

function formFromPolicy(p: Policy): Form {
  const c = (p.conditions ?? {}) as Record<string, unknown>;
  const a = (p.action ?? {}) as Record<string, unknown>;
  return {
    kind: p.rule_type as Kind,
    name: p.name,
    tool: p.target_tool ?? "",
    reason: typeof a.reason === "string" ? a.reason : "",
    pathArg: typeof c.path_arg === "string" ? c.path_arg : "path",
    folders: Array.isArray(c.allow_prefixes) ? (c.allow_prefixes as string[]).join(", ") : "notes/",
    verdict: a.verdict === "block" ? "block" : "require_approval",
    limit: String(c.max_tokens ?? c.max_cost ?? "")
  };
}

/** Create or edit a rule that decides tool calls (the policy engine). */
export function ToolRuleEditor({
  policy,
  onClose,
  onSaved,
  onBack
}: {
  policy: Policy | null;
  onClose: () => void;
  onSaved: (message: string) => void;
  onBack?: () => void;
}) {
  const [form, setForm] = useState<Form>(policy ? formFromPolicy(policy) : EMPTY);
  const [startMode, setStartMode] = useState<"shadow" | "enforce">("shadow");
  const [tools, setTools] = useState<MCPTool[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const kind = TOOL_RULE_KINDS.find((k) => k.kind === form.kind) ?? TOOL_RULE_KINDS[0];
  // A validate_args rule made through the API can refuse specific argument values instead of checking a
  // path. The form can't show those, so it keeps them as they are rather than replacing them.
  const valueRule = policy?.rule_type === "validate_args" && !(policy.conditions ?? {}).path_arg;
  const set = (patch: Partial<Form>) => setForm((f) => ({ ...f, ...patch }));

  useEffect(() => {
    apiGet<MCPTool[]>("/api/mcp/tools").then(setTools).catch(() => undefined);
  }, []);
  useEscape(onClose, !saving);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (kind.needsTool && !form.tool.trim()) return setError("Pick the tool this rule applies to.");
    const limit = Number(form.limit);
    if (!kind.needsTool && !(limit > 0)) return setError("Enter a limit above zero.");

    const previous = (policy?.action ?? {}) as Record<string, unknown>;
    let conditions: Record<string, unknown> = {};
    // Keep markers the form doesn't show (e.g. `default` on the shipped taint rules).
    // (only while it is still the kind of rule that shipped).
    let action: Record<string, unknown> =
      previous.default && form.kind === policy?.rule_type ? { default: true } : {};
    if (form.reason.trim()) action.reason = form.reason.trim();
    if (form.kind === "validate_args" && valueRule) conditions = { ...(policy?.conditions ?? {}) };
    else if (form.kind === "validate_args")
      conditions = {
        path_arg: form.pathArg.trim() || "path",
        allow_prefixes: form.folders.split(",").map((s) => s.trim()).filter(Boolean)
      };
    if (form.kind === "guard_signal") {
      conditions = { run_tainted: true };
      action = { ...action, verdict: form.verdict };
    }
    if (form.kind === "token_budget") conditions = { max_tokens: Math.round(limit) };
    if (form.kind === "cost_budget") conditions = { max_cost: limit };

    const name = form.name.trim() || `${kind.label}${kind.needsTool ? `: ${form.tool.trim()}` : ""}`;
    const body = {
      name,
      rule_type: form.kind,
      target_tool: kind.needsTool ? form.tool.trim() : null,
      conditions,
      action
    };
    setSaving(true);
    try {
      if (policy) {
        await apiSend(`/api/policies/${policy.id}`, { method: "PATCH", body: JSON.stringify(body) });
        onSaved(`Saved "${name}".`);
      } else {
        await apiSend("/api/policies", { method: "POST", body: JSON.stringify({ ...body, mode: startMode }) });
        onSaved(
          startMode === "shadow"
            ? `Created "${name}" in shadow: it logs what it would decide until you enforce it.`
            : `Created "${name}". It applies from the next tool call.`
        );
      }
    } catch (e) {
      setError(apiErrorText(e));
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
        aria-labelledby="tool-rule-title"
        onMouseDown={(e) => e.stopPropagation()}
        onSubmit={submit}
      >
        <div className="modal-head">
          <div>
            <h2 id="tool-rule-title">{policy ? "Edit tool rule" : "New tool rule"}</h2>
            <p className="help">Decides a tool call by the tool and its arguments, before it runs.</p>
          </div>
          <div className="actions">
            {onBack && (
              <button type="button" className="btn btn-ghost btn-sm" onClick={onBack}>
                Back
              </button>
            )}
            <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>
              Close
            </button>
          </div>
        </div>
        <div className="modal-body">
          <div className="field">
            <span className="label" id="kind-label">
              What should happen
            </span>
            <div className="choice-list" role="radiogroup" aria-labelledby="kind-label">
              {TOOL_RULE_KINDS.map((k) => (
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

          {form.kind === "validate_args" && valueRule && (
            <p className="notice">
              This rule refuses specific argument values, set through the API. Saving keeps them unchanged.
            </p>
          )}
          {form.kind === "validate_args" && !valueRule && (
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
                step="any"
                value={form.limit}
                onChange={(e) => set({ limit: e.target.value })}
                placeholder={form.kind === "cost_budget" ? "0.50" : "50000"}
              />
            </label>
          )}

          <div className="form-grid">
            <label className="field">
              <span className="label">
                Name <span className="subtle">(optional)</span>
              </span>
              <input className="input" value={form.name} onChange={(e) => set({ name: e.target.value })} placeholder={kind.label} />
            </label>
            <label className="field">
              <span className="label">
                Reason shown <span className="subtle">(optional)</span>
              </span>
              <input
                className="input"
                value={form.reason}
                onChange={(e) => set({ reason: e.target.value })}
                placeholder="Writes need a review"
              />
            </label>
          </div>

          {error && (
            <p className="notice error" role="alert">
              {error}
            </p>
          )}
        </div>
        <div className="modal-foot">
          {policy ? (
            <span className="help">Keeps its mode ({policy.mode})</span>
          ) : (
            <StartMode value={startMode} onChange={setStartMode} />
          )}
          <div className="actions">
            <button type="button" className="btn btn-ghost" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="btn btn-primary" disabled={saving}>
              {saving ? "Saving…" : policy ? "Save" : "Create"}
            </button>
          </div>
        </div>
      </form>
    </div>
  );
}
