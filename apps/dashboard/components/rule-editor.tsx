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

/**
 * The spec to send. `base` is the rule being edited: fields the form doesn't show (execution, judge
 * threshold, pattern flags, keyword options, timeouts) are carried over rather than reset, since PUT
 * replaces the whole spec. Check options only carry over while the check type is unchanged.
 */
function specFromDraft(draft: Draft, mode: RuleSpec["mode"], base?: RuleSpec): RuleSpec {
  const sameType = base?.check.type === draft.checkType;
  const check: RuleSpec["check"] = sameType ? { ...base!.check } : { type: draft.checkType };
  delete check.keywords;
  delete check.patterns;
  delete check.examples;
  delete check.policy;
  delete check.model;
  delete check.label;
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
    ...base,
    name: draft.name.trim(),
    description: draft.description.trim() || null,
    stages: draft.stages,
    tools: toolStagesOnly && draft.tools.length ? draft.tools : null,
    check,
    action: draft.action,
    mode,
    // Only meaningful (and only accepted) for rules that check tool output; the checkbox is hidden
    // otherwise, so a value left over from before the stage was unticked must not be sent.
    taints_run: draft.stages.includes("tool_output") && draft.taintsRun,
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
  onSaved,
  onBack
}: {
  rule: GuardRule | null;
  onClose: () => void;
  onSaved: (message: string) => void;
  onBack?: () => void;
}) {
  const [draft, setDraft] = useState<Draft>(rule ? draftFromRule(rule) : EMPTY);
  const [startMode, setStartMode] = useState<"shadow" | "enforce">("shadow");
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
      const spec = specFromDraft(draft, "enforce", rule?.spec);
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
      // New rules start in the mode picked (shadow unless asked); an edit keeps the current mode.
      const spec = specFromDraft(draft, rule?.spec.mode ?? startMode, rule?.spec);
      const saved = await apiSend<GuardRule>(rule ? `/api/guard/rules/${rule.id}` : "/api/guard/rules", {
        method: rule ? "PUT" : "POST",
        body: JSON.stringify(spec)
      });
      onSaved(
        rule
          ? `Saved ${saved.policy_id} (v${saved.version})`
          : startMode === "shadow"
            ? `Created ${saved.spec.name} in shadow: it logs what it would do until you enforce it`
            : `Created ${saved.spec.name}. It applies from the next message.`
      );
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  }

  // Escape closes; the name field gets focus when the editor opens.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && busy === null) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  return (
    <div className="modal-backdrop" role="presentation">
      <section className="modal" role="dialog" aria-modal="true" aria-labelledby="rule-editor-title">
        <header className="modal-head">
          <div>
            <h2 id="rule-editor-title">{rule ? "Edit text rule" : "New text rule"}</h2>
            <p className="help">
              {rule ? <span className="mono">{rule.policy_id}</span> : "Checks what a message, tool result or answer says."}
            </p>
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
        </header>

        <form id="rule-form" className="modal-body" onSubmit={save}>
          {!rule && (
            <div className="field">
              <span className="label">Start from</span>
              <div className="chips">
                {PRESETS.map((p) => (
                  <button
                    key={p.label}
                    type="button"
                    className="chip-toggle"
                    onClick={() => {
                      setDraft({ ...EMPTY, ...p.draft });
                      setReport(null);
                    }}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
            </div>
          )}

          <div className="field">
            <label className="label" htmlFor="rule-name">Name</label>
            <input
              className="input"
              id="rule-name"
              required
              autoFocus
              value={draft.name}
              onChange={(e) => update("name", e.target.value)}
            />
          </div>

          <fieldset className="form-section">
            <legend>Where it checks</legend>
            <div className="chips" role="group" aria-label="Stages">
              {(Object.keys(STAGE_LABELS) as GuardStage[]).map((stage) => (
                <button
                  key={stage}
                  type="button"
                  className="chip-toggle"
                  aria-pressed={draft.stages.includes(stage)}
                  onClick={() => toggleStage(stage)}
                >
                  {STAGE_LABELS[stage]}
                </button>
              ))}
            </div>
            {toolStagesOnly && (
              <div className="field">
                <span className="label">Only these tools</span>
                <div className="chips" role="group" aria-label="Tools">
                  {Array.from(new Set([...tools, ...draft.tools])).sort().map((tool) => (
                    <button
                      key={tool}
                      type="button"
                      className="chip-toggle mono"
                      aria-pressed={draft.tools.includes(tool)}
                      onClick={() =>
                        update(
                          "tools",
                          draft.tools.includes(tool) ? draft.tools.filter((t) => t !== tool) : [...draft.tools, tool]
                        )
                      }
                    >
                      {tool}
                    </button>
                  ))}
                  {tools.length === 0 && draft.tools.length === 0 && (
                    <span className="help">No tools discovered yet.</span>
                  )}
                </div>
                <p className="help">
                  None selected: every tool.
                  {draft.tools.some((t) => !tools.includes(t)) &&
                    " Some selected tools aren't connected right now; the rule applies once they are."}
                </p>
              </div>
            )}
          </fieldset>

          <fieldset className="form-section">
            <legend>What it looks for</legend>
            <div className="field">
              <label className="label" htmlFor="rule-check">Check</label>
              <select
                className="select"
                id="rule-check"
                value={draft.checkType}
                onChange={(e) => {
                  const next = e.target.value as RuleCheckType;
                  const redacts = types?.checks.find((c) => c.type === next)?.can_redact;
                  setDraft((d) => ({
                    ...d,
                    checkType: next,
                    action: d.action === "redact" && !redacts ? "block" : d.action
                  }));
                  setReport(null);
                }}
              >
                {(types?.checks ?? [])
                  // "Every call" rules are tool rules now; only an existing one still shows it.
                  .filter((c) => c.type !== "always" || rule?.spec.check.type === "always")
                  .map((c) => (
                  <option key={c.type} value={c.type}>
                    {c.label}
                  </option>
                ))}
              </select>
              {checkInfo && <p className="help">{checkInfo.hint}</p>}
            </div>

            {linesLabel && (
              <div className="field">
                <label className="label" htmlFor="rule-lines">{linesLabel}</label>
                <textarea className="textarea mono" id="rule-lines" required value={draft.lines} onChange={(e) => update("lines", e.target.value)} />
              </div>
            )}

            {draft.checkType === "llm_judge" && (
              <>
                <div className="field">
                  <label className="label" htmlFor="rule-policy">Policy, in plain words</label>
                  <textarea
                    className="textarea"
                    id="rule-policy"
                    required
                    value={draft.policy}
                    onChange={(e) => update("policy", e.target.value)}
                    placeholder="The answer must not give legal advice about the user's own situation."
                  />
                </div>
                <div className="field">
                  <label className="label" htmlFor="rule-model">Judge model</label>
                  <input
                    id="rule-model"
                    className="input mono"
                    value={draft.model}
                    onChange={(e) => update("model", e.target.value)}
                    placeholder={types?.judge_model ?? "default"}
                  />
                  <p className="help">Leave empty to use the default model.</p>
                </div>
              </>
            )}
          </fieldset>

          <fieldset className="form-section">
            <legend>What happens</legend>
            <div className="form-grid">
              <div className="field">
                <label className="label" htmlFor="rule-action">When it matches</label>
                <select
                  className="select"
                  id="rule-action"
                  value={draft.action}
                  onChange={(e) => update("action", e.target.value as RuleAction)}
                >
                  {(Object.keys(ACTION_LABELS) as RuleAction[]).map((a) => (
                    <option key={a} value={a} disabled={a === "redact" && !canRedact}>
                      {ACTION_LABELS[a]}
                    </option>
                  ))}
                </select>
                {!canRedact && <p className="help">Redact needs a keywords or pattern check.</p>}
              </div>
              {draft.action === "redact" && (
                <div className="field">
                  <label className="label" htmlFor="rule-label">Placeholder label</label>
                  <input
                    id="rule-label"
                    className="input mono"
                    value={draft.label}
                    onChange={(e) => update("label", e.target.value)}
                    placeholder="REDACTED"
                  />
                  <p className="help">Matches become &lt;{(draft.label || "REDACTED").toUpperCase()}_1&gt;.</p>
                </div>
              )}
            </div>
            {draft.stages.includes("tool_output") && (
              <label className="check">
                <input
                  type="checkbox"
                  checked={draft.taintsRun}
                  onChange={(e) => update("taintsRun", e.target.checked)}
                />
                Taint the run when it matches: later writes and deletes need approval, even in shadow
              </label>
            )}
          </fieldset>

          <fieldset className="form-section">
            <legend>Examples</legend>
            <p className="help">Test runs the rule on these and on the eval set&apos;s clean records.</p>
            <div className="form-grid">
              <div className="field">
                <label className="label" htmlFor="rule-fire">Should match (one per line)</label>
                <textarea className="textarea" id="rule-fire" value={draft.shouldFire} onChange={(e) => update("shouldFire", e.target.value)} />
              </div>
              <div className="field">
                <label className="label" htmlFor="rule-pass">Should not match (one per line)</label>
                <textarea className="textarea" id="rule-pass" value={draft.shouldPass} onChange={(e) => update("shouldPass", e.target.value)} />
              </div>
            </div>
          </fieldset>

          {error && (
            <p className="notice error" role="alert">
              {error}
            </p>
          )}

          {report && <DryRunReport report={report} />}
        </form>

        <footer className="modal-foot">
          {rule ? (
            <span className="help">Keeps its mode ({rule.spec.mode})</span>
          ) : (
            <StartMode value={startMode} onChange={setStartMode} />
          )}
          <div className="actions">
            <button type="button" className="btn" onClick={runTest} disabled={busy !== null}>
              {busy === "test" ? "Testing…" : "Test"}
            </button>
            <button type="submit" form="rule-form" className="btn btn-primary" disabled={busy !== null}>
              {busy === "save" ? "Saving…" : rule ? "Save" : "Create"}
            </button>
          </div>
        </footer>
      </section>
    </div>
  );
}

/** Shadow first (log what it would do) or enforce straight away. Shared by both rule editors. */
export function StartMode({
  value,
  onChange
}: {
  value: "shadow" | "enforce";
  onChange: (mode: "shadow" | "enforce") => void;
}) {
  return (
    <div className="seg" role="radiogroup" aria-label="Start in">
      {(
        [
          ["shadow", "Start in shadow"],
          ["enforce", "Enforce now"]
        ] as const
      ).map(([mode, label]) => (
        <button
          key={mode}
          type="button"
          role="radio"
          aria-checked={value === mode}
          className={`seg-option ${mode}`}
          onClick={() => onChange(mode)}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

function DryRunReport({ report }: { report: RuleDryRun }) {
  return (
    <section className="dry-run surface surface-pad" aria-live="polite">
      <strong className={report.passed ? "text-success" : "text-danger"}>
        {report.passed ? "All examples behave as expected" : "Some examples don't match what you expected"}
      </strong>
      <ul>
        {report.examples.map((e, i) => (
          <li key={i} className={e.ok ? "" : "text-danger"}>
            <span className="mono">{e.ok ? "pass" : "fail"}</span>{" "}
            {e.expected === "fire" ? "Should match" : "Should not match"}: <q>{e.text}</q>
            {e.fired ? ` gives ${e.action}` : " gives no match"}
            {e.redacted ? (
              <>
                , shown as <q className="mono">{e.redacted}</q>
              </>
            ) : null}
            {e.error ? ` (error: ${e.error})` : ""}
          </li>
        ))}
      </ul>
      {report.benign && (
        <p className="help">
          On clean eval records at these stages it fired {report.benign.fired} of {report.benign.checked} times (
          {(report.benign.rate * 100).toFixed(1)}%)
          {report.benign.samples.length > 0 ? `, e.g. ${report.benign.samples.map((s) => s.record_id).join(", ")}` : ""}.
        </p>
      )}
    </section>
  );
}
