"use client";

import { FormEvent, useEffect, useState } from "react";

import { apiGet, apiSend } from "@/lib/api";
import { Policy } from "@/lib/types";
import { useLiveRefresh } from "@/lib/use-live-refresh";

const DEFAULT_FORM = {
  name: "",
  ruleType: "block_tool",
  targetTool: "",
  priority: 100,
  conditions: "{}",
  action: "{\"reason\":\"Explain why this rule exists\"}"
};

const PRESETS = {
  blockDelete: {
    name: "Block deletes",
    ruleType: "block_tool",
    targetTool: "delete_file",
    priority: 200,
    conditions: "{}",
    action: "{\"reason\":\"Deletes are blocked in this demo\"}"
  },
  approveWrite: {
    name: "Approve writes",
    ruleType: "require_approval",
    targetTool: "write_file",
    priority: 150,
    conditions: "{}",
    action: "{\"reason\":\"Writes require human approval\"}"
  },
  restrictNotes: {
    name: "Notes only",
    ruleType: "validate_args",
    targetTool: "write_file",
    priority: 180,
    conditions: "{\"path_arg\":\"path\",\"allow_prefixes\":[\"notes/\"]}",
    action: "{}"
  },
  taintedWrite: {
    name: "Tainted run: approve writes",
    ruleType: "guard_signal",
    targetTool: "write_file",
    priority: 190,
    conditions: "{\"run_tainted\":true}",
    action: "{\"verdict\":\"require_approval\",\"reason\":\"This run read content flagged as possible prompt injection\"}"
  }
};

export default function PoliciesPage() {
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [status, setStatus] = useState<string | null>(null);
  const [form, setForm] = useState(DEFAULT_FORM);
  const [createOpen, setCreateOpen] = useState(false);

  async function loadPolicies() {
    const data = await apiGet<Policy[]>("/api/policies");
    setPolicies(data);
  }

  useEffect(() => {
    loadPolicies().catch((error) => setStatus(String(error)));
  }, []);

  useLiveRefresh(() => {
    loadPolicies().catch(() => undefined);
  });

  async function createPolicy(event: FormEvent) {
    event.preventDefault();
    try {
      if (!form.name.trim()) {
        setStatus("Policy name is required.");
        return;
      }

      const conditions = JSON.parse(form.conditions);
      const action = JSON.parse(form.action);
      await apiSend("/api/policies", {
        method: "POST",
        body: JSON.stringify({
          name: form.name.trim(),
          rule_type: form.ruleType,
          target_tool: form.targetTool || null,
          priority: Number(form.priority),
          conditions,
          action
        })
      });
      setForm(DEFAULT_FORM);
      setStatus("Policy created.");
      await loadPolicies();
      setCreateOpen(false);
    } catch (error) {
      setStatus(`Policy JSON is invalid or request failed: ${String(error)}`);
    }
  }

  async function togglePolicy(policy: Policy) {
    try {
      await apiSend(`/api/policies/${policy.id}`, {
        method: "PATCH",
        body: JSON.stringify({ enabled: !policy.enabled })
      });
      await loadPolicies();
    } catch (error) {
      setStatus(String(error));
    }
  }

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h2>Policies</h2>
          {status && <p className="page-status">{status}</p>}
        </div>
        <div className="row wrap">
          <button className="button secondary" onClick={() => loadPolicies()}>
            Refresh
          </button>
          <button className="button" onClick={() => setCreateOpen(true)}>
            Create rule
          </button>
        </div>
      </header>

      <section className="panel stack">
        <div className="row wrap" style={{ justifyContent: "space-between" }}>
          <div>
            <h3>Active Rules</h3>
          </div>
          <span className="badge">{policies.length} rules</span>
        </div>
        <div className="list policy-list">
          {policies.length === 0 && (
            <div className="empty-state">
              <p>No policies yet.</p>
              <button className="button" onClick={() => setCreateOpen(true)}>
                Create the first rule
              </button>
            </div>
          )}
          {policies.map((policy) => (
            <div className="card" key={policy.id}>
              <div className="row wrap" style={{ justifyContent: "space-between" }}>
                <strong>{policy.name}</strong>
                <span className={`badge ${policy.enabled ? "success" : "danger"}`}>
                  {policy.enabled ? "enabled" : "disabled"}
                </span>
              </div>
              <p className="muted">
                {policy.rule_type} / target `{policy.target_tool ?? "*"}` / priority{" "}
                {policy.priority}
              </p>
              <pre>{JSON.stringify({ conditions: policy.conditions, action: policy.action }, null, 2)}</pre>
              <div className="row wrap" style={{ marginTop: 12 }}>
                <button className="button secondary" onClick={() => togglePolicy(policy)}>
                  {policy.enabled ? "Disable" : "Enable"}
                </button>
              </div>
            </div>
          ))}
        </div>
      </section>

      {createOpen && (
        <div className="modal-backdrop" onClick={() => setCreateOpen(false)}>
          <section
            className="modal panel"
            role="dialog"
            aria-modal="true"
            aria-labelledby="create-rule-title"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="modal-header">
              <div>
                <h3 id="create-rule-title">Create Rule</h3>
              </div>
              <button className="button secondary" onClick={() => setCreateOpen(false)}>
                Close
              </button>
            </div>

            <div className="row wrap">
              <button className="button secondary" onClick={() => setForm(PRESETS.blockDelete)}>
                Block deletes
              </button>
              <button className="button secondary" onClick={() => setForm(PRESETS.approveWrite)}>
                Approval for writes
              </button>
              <button className="button secondary" onClick={() => setForm(PRESETS.restrictNotes)}>
                Notes path only
              </button>
              <button className="button secondary" onClick={() => setForm(PRESETS.taintedWrite)}>
                Approve writes after flagged content
              </button>
            </div>

            <form className="stack" onSubmit={createPolicy}>
              <div className="field">
                <label>Name</label>
                <input
                  required
                  value={form.name}
                  onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))}
                  placeholder="Block deletes"
                />
              </div>
              <div className="field">
                <label>Rule type</label>
                <select
                  value={form.ruleType}
                  onChange={(event) => setForm((current) => ({ ...current, ruleType: event.target.value }))}
                >
                  <option value="block_tool">block_tool</option>
                  <option value="require_approval">require_approval</option>
                  <option value="validate_args">validate_args</option>
                  <option value="token_budget">token_budget</option>
                  <option value="cost_budget">cost_budget</option>
                  <option value="guard_signal">guard_signal</option>
                </select>
              </div>
              <div className="field">
                <label>Target tool</label>
                <input
                  value={form.targetTool}
                  onChange={(event) =>
                    setForm((current) => ({ ...current, targetTool: event.target.value }))
                  }
                  placeholder="delete_file"
                />
              </div>
              <div className="field">
                <label>Priority</label>
                <input
                  type="number"
                  value={form.priority}
                  onChange={(event) =>
                    setForm((current) => ({ ...current, priority: Number(event.target.value) }))
                  }
                />
              </div>
              <div className="field">
                <label>Conditions JSON</label>
                <textarea
                  value={form.conditions}
                  onChange={(event) =>
                    setForm((current) => ({ ...current, conditions: event.target.value }))
                  }
                />
              </div>
              <div className="field">
                <label>Action JSON</label>
                <textarea
                  value={form.action}
                  onChange={(event) =>
                    setForm((current) => ({ ...current, action: event.target.value }))
                  }
                />
              </div>
              <div className="row wrap" style={{ justifyContent: "flex-end" }}>
                <button
                  type="button"
                  className="button secondary"
                  onClick={() => {
                    setForm(DEFAULT_FORM);
                    setCreateOpen(false);
                  }}
                >
                  Cancel
                </button>
                <button className="button">Create Policy</button>
              </div>
            </form>
          </section>
        </div>
      )}
    </div>
  );
}
