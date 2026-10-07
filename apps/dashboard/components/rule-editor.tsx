"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";

import { apiGet, apiSend } from "@/lib/api";
import {
  CheckTypes,
  GuardRule,
  GuardStage,
  MCPTool,
  RuleAction,
  RuleCheckType,
  RuleDryRun,
  RuleSpec
} from "@/lib/types";

// Words people use for the four places a rule can sit in the agent loop.
export const STAGE_LABELS: Record<GuardStage, string> = {
  user_input: "User request",
  tool_args: "Tool call (outgoing)",
  tool_output: "Tool result (incoming)",
  final_output: "Answer"
};

const ACTION_LABELS: Record<RuleAction, string> = {
  flag: "Log only",
  redact: "Redact the match",
  escalate: "Ask a human",
  block: "Block"
};

type Draft = {
  name: string;
  description: string;
  stages: GuardStage[];
  tools: string[];
  checkType: RuleCheckType;
  lines: string; // keywords / patterns / topic examples, one per line
  policy: string;
  model: string;
  label: string;
  action: RuleAction;
  taintsRun: boolean;
  shouldFire: string;
  shouldPass: string;
};

const EMPTY: Draft = {
  name: "",
  description: "",
  stages: ["user_input"],
  tools: [],
  checkType: "keywords",
  lines: "",
  policy: "",
  model: "",
  label: "",
  action: "block",
  taintsRun: false,
  shouldFire: "",
  shouldPass: ""
};

// Starting points for the common cases: the operator edits them rather than starting blank.
const PRESETS: { label: string; draft: Partial<Draft> }[] = [
  {
    label: "Off-limits topic",
    draft: {
      name: "No competitor pricing",
      stages: ["user_input"],
      checkType: "topic",
      lines: "What do our competitors charge per seat?\nCompare Acme's pricing with ours",
      action: "block",
      shouldFire: "How much does Acme charge for enterprise?",
      shouldPass: "Summarise the release notes for tinycache"
    }
  },
  {
    label: "Redact a codename",
    draft: {
      name: "Hide project codenames",
      stages: ["tool_output", "final_output"],
      checkType: "keywords",
      lines: "Project Falcon",
      label: "CODENAME",
      action: "redact",
      shouldFire: "Project Falcon ships in May",
      shouldPass: "The falcon is a bird of prey"
    }
  },
  {
    label: "Approve a tool",
    draft: {
      name: "Approve every email",
      stages: ["tool_args"],
      tools: ["send_email"],
      checkType: "always",
      action: "escalate"
    }
  },
  {
    label: "Plain-language policy",
    draft: {
      name: "No legal advice",
      stages: ["final_output"],
      checkType: "llm_judge",
      policy: "The answer must not give legal advice about the user's own situation.",
      action: "escalate"
    }
  }
];

function toLines(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

function draftFromRule(rule: GuardRule): Draft {
  const { spec } = rule;
  const lines = spec.check.keywords ?? spec.check.patterns ?? spec.check.examples ?? [];
  return {
    name: spec.name,
    description: spec.description ?? "",
    stages: spec.stages,
    tools: spec.tools ?? [],
    checkType: spec.check.type,
    lines: lines.join("\n"),
    policy: spec.check.policy ?? "",
    model: spec.check.model ?? "",
    label: spec.check.label ?? "",
    action: spec.action,
    taintsRun: spec.taints_run ?? false,
    shouldFire: spec.tests.should_fire.join("\n"),
    shouldPass: spec.tests.should_pass.join("\n")
  };
}

function specFromDraft(draft: Draft, mode: RuleSpec["mode"]): RuleSpec {
  const check: RuleSpec["check"] = { type: draft.checkType };
  if (draft.checkType === "keywords") check.keywords = toLines(draft.lines);
  if (draft.checkType === "pattern") check.patterns = toLines(draft.lines);
  if (draft.checkType === "topic") check.examples = toLines(draft.lines);
  if (draft.checkType === "llm_judge") {
    check.policy = draft.policy.trim();
    if (draft.model.trim()) check.model = draft.model.trim();
  }
  if (draft.label.trim()) check.label = draft.label.trim().toUpperCase();
  const toolStagesOnly = draft.stages.every((s) => s === "tool_args" || s === "tool_output");
  return {
    name: draft.name.trim(),
    description: draft.description.trim() || null,
    stages: draft.stages,
    tools: toolStagesOnly && draft.tools.length ? draft.tools : null,
    check,
    action: draft.action,
    mode,
    taints_run: draft.taintsRun,
    tests: { should_fire: toLines(draft.shouldFire), should_pass: toLines(draft.shouldPass) }
  };
}

/** The API answers 422 with pydantic's message; show the human part of it. */
function errorText(e: unknown): string {
  const raw = String(e instanceof Error ? e.message : e);
  try {
    const detail = JSON.parse(raw).detail;
    return typeof detail === "string" ? detail : JSON.stringify(detail);
  } catch {
    return raw;
  }
}

export function RuleEditor({
  rule,
  onClose,
  onSaved
}: {
  rule: GuardRule | null;
  onClose: () => void;
  onSaved: (message: string) => void;
}) {
  const [draft, setDraft] = useState<Draft>(rule ? draftFromRule(rule) : EMPTY);
  const [types, setTypes] = useState<CheckTypes | null>(null);
  const [tools, setTools] = useState<string[]>([]);
  const [report, setReport] = useState<RuleDryRun | null>(null);
  const [busy, setBusy] = useState<"test" | "save" | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiGet<CheckTypes>("/api/guard/check-types").then(setTypes).catch(() => undefined);
    apiGet<MCPTool[]>("/api/mcp/tools")
      .then((list) => setTools(Array.from(new Set(list.map((t) => t.name))).sort()))
      .catch(() => undefined);
  }, []);

  const checkInfo = types?.checks.find((c) => c.type === draft.checkType);
  const toolStagesOnly = draft.stages.every((s) => s === "tool_args" || s === "tool_output");
  const canRedact = checkInfo?.can_redact ?? (draft.checkType === "keywords" || draft.checkType === "pattern");
  const linesLabel = useMemo(
    () =>
      ({ keywords: "Keywords (one per line)", pattern: "Patterns (one per line)", topic: "Example requests (one per line)" })[
        draft.checkType as "keywords" | "pattern" | "topic"
      ],
    [draft.checkType]
  );

  function update<K extends keyof Draft>(key: K, value: Draft[K]) {
    setDraft((d) => ({ ...d, [key]: value }));
    setReport(null);
  }

  function toggleStage(stage: GuardStage) {
    const next = draft.stages.includes(stage) ? draft.stages.filter((s) => s !== stage) : [...draft.stages, stage];
    update("stages", next.length ? next : draft.stages);
  }

  async function runTest() {
    setBusy("test");
    setError(null);
    try {
      const spec = specFromDraft(draft, "enforce");
      setReport(
        await apiSend<RuleDryRun>("/api/guard/rules/test", {
          method: "POST",
          body: JSON.stringify({ spec, sample_benign: true })
        })
      );
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    setBusy("save");
    setError(null);
    try {
      // New rules start in shadow; an edit keeps the rule's current mode.
      const spec = specFromDraft(draft, rule?.spec.mode ?? "shadow");
      const saved = await apiSend<GuardRule>(rule ? `/api/guard/rules/${rule.id}` : "/api/guard/rules", {
        method: rule ? "PUT" : "POST",
        body: JSON.stringify(spec)
      });
      onSaved(
        rule
          ? `Saved ${saved.policy_id} (v${saved.version})`
          : `Created ${saved.policy_id} in shadow: it logs what it would do until you enforce it`
      );
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="modal-backdrop" role="presentation">
      <section className="modal panel stack" role="dialog" aria-modal="true" aria-labelledby="rule-editor-title">
        <div className="modal-header">
          <h3 id="rule-editor-title">{rule ? `Edit ${rule.policy_id}` : "New guard rule"}</h3>
          <button className="button secondary" onClick={onClose}>
            Close
          </button>
        </div>

        {!rule && (
          <div className="row wrap" aria-label="Start from">
            {PRESETS.map((p) => (
              <button
                key={p.label}
                type="button"
                className="button secondary"
                onClick={() => {
                  setDraft({ ...EMPTY, ...p.draft });
                  setReport(null);
                }}
              >
                {p.label}
              </button>
            ))}
          </div>
        )}

        <form className="stack" onSubmit={save}>
          <div className="field">
            <label htmlFor="rule-name">Name</label>
            <input id="rule-name" required value={draft.name} onChange={(e) => update("name", e.target.value)} />
          </div>

          <fieldset className="field">
            <legend>Where it checks</legend>
            <div className="row wrap">
              {(Object.keys(STAGE_LABELS) as GuardStage[]).map((stage) => (
                <label key={stage} className="check">
                  <input type="checkbox" checked={draft.stages.includes(stage)} onChange={() => toggleStage(stage)} />
                  {STAGE_LABELS[stage]}
                </label>
              ))}
            </div>
          </fieldset>

          {toolStagesOnly && (
            <fieldset className="field">
              <legend>Only these tools (none selected = every tool)</legend>
              <div className="row wrap">
                {tools.map((tool) => (
                  <label key={tool} className="check mono">
                    <input
                      type="checkbox"
                      checked={draft.tools.includes(tool)}
                      onChange={() =>
                        update(
                          "tools",
                          draft.tools.includes(tool) ? draft.tools.filter((t) => t !== tool) : [...draft.tools, tool]
                        )
                      }
                    />
                    {tool}
                  </label>
                ))}
                {tools.length === 0 && <span className="muted">No tools discovered yet.</span>}
              </div>
            </fieldset>
          )}

          <div className="field">
            <label htmlFor="rule-check">What it looks for</label>
            <select
              id="rule-check"
              value={draft.checkType}
              onChange={(e) => {
                const next = e.target.value as RuleCheckType;
                const redacts = types?.checks.find((c) => c.type === next)?.can_redact;
                setDraft((d) => ({ ...d, checkType: next, action: d.action === "redact" && !redacts ? "block" : d.action }));
                setReport(null);
              }}
            >
              {(types?.checks ?? []).map((c) => (
                <option key={c.type} value={c.type}>
                  {c.label}
                </option>
              ))}
            </select>
            {checkInfo && <p className="muted">{checkInfo.hint}</p>}
          </div>

          {linesLabel && (
            <div className="field">
              <label htmlFor="rule-lines">{linesLabel}</label>
              <textarea id="rule-lines" required value={draft.lines} onChange={(e) => update("lines", e.target.value)} />
            </div>
          )}

          {draft.checkType === "llm_judge" && (
            <>
              <div className="field">
                <label htmlFor="rule-policy">Policy, in plain words</label>
                <textarea
                  id="rule-policy"
                  required
                  value={draft.policy}
                  onChange={(e) => update("policy", e.target.value)}
                  placeholder="The answer must not give legal advice about the user's own situation."
                />
              </div>
              <div className="field">
                <label htmlFor="rule-model">Judge model</label>
                <input
                  id="rule-model"
                  className="mono"
                  value={draft.model}
                  onChange={(e) => update("model", e.target.value)}
                  placeholder={types?.judge_model ?? "default"}
                />
              </div>
            </>
          )}

          <div className="row wrap">
            <div className="field">
              <label htmlFor="rule-action">When it matches</label>
              <select id="rule-action" value={draft.action} onChange={(e) => update("action", e.target.value as RuleAction)}>
                {(Object.keys(ACTION_LABELS) as RuleAction[]).map((a) => (
                  <option key={a} value={a} disabled={a === "redact" && !canRedact}>
                    {ACTION_LABELS[a]}
                  </option>
                ))}
              </select>
            </div>
            {draft.action === "redact" && (
              <div className="field">
                <label htmlFor="rule-label">Placeholder label</label>
                <input
                  id="rule-label"
                  className="mono"
                  value={draft.label}
                  onChange={(e) => update("label", e.target.value)}
                  placeholder="REDACTED"
                />
              </div>
            )}
          </div>

          {draft.stages.includes("tool_output") && (
            <label className="check">
              <input type="checkbox" checked={draft.taintsRun} onChange={(e) => update("taintsRun", e.target.checked)} />
              Taint the run when it matches (later writes and deletes need approval)
            </label>
          )}

          <div className="row wrap">
            <div className="field" style={{ flex: 1 }}>
              <label htmlFor="rule-fire">Should match (one per line)</label>
              <textarea id="rule-fire" value={draft.shouldFire} onChange={(e) => update("shouldFire", e.target.value)} />
            </div>
            <div className="field" style={{ flex: 1 }}>
              <label htmlFor="rule-pass">Should not match (one per line)</label>
              <textarea id="rule-pass" value={draft.shouldPass} onChange={(e) => update("shouldPass", e.target.value)} />
            </div>
          </div>

          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}

          {report && <DryRunReport report={report} />}

          <div className="row wrap" style={{ justifyContent: "flex-end" }}>
            <button type="button" className="button secondary" onClick={runTest} disabled={busy !== null}>
              {busy === "test" ? "Testing…" : "Test"}
            </button>
            <button className="button" disabled={busy !== null}>
              {busy === "save" ? "Saving…" : rule ? "Save" : "Create in shadow"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}

function DryRunReport({ report }: { report: RuleDryRun }) {
  return (
    <div className="card stack" aria-live="polite">
      <strong>{report.passed ? "Examples pass" : "Some examples don't match what you expected"}</strong>
      {report.examples.map((e, i) => (
        <p key={i} className="mono">
          {e.ok ? "✓" : "✗"} {e.expected === "fire" ? "should match" : "should not match"}: {e.text}
          {e.fired ? ` → ${e.action}` : " → no match"}
          {e.redacted ? ` → "${e.redacted}"` : ""}
          {e.error ? ` (error: ${e.error})` : ""}
        </p>
      ))}
      {report.benign && (
        <p className="muted">
          Fired on {report.benign.fired} of {report.benign.checked} benign eval records at these stages (
          {(report.benign.rate * 100).toFixed(1)}%).
          {report.benign.samples.map((s) => ` ${s.record_id}`).join(",")}
        </p>
      )}
    </div>
  );
}
