"use client";

import { useEffect, useMemo, useState } from "react";

import { STAGE_NAMES } from "@/lib/format";

import { apiGet, apiSend } from "@/lib/api";
import {
  AttackResult,
  AttackRun,
  GuardStage,
  PlaygroundScenarios,
  ScanResult,
  ScanSpan
} from "@/lib/types";

const STAGES: { value: GuardStage; label: string; hint: string }[] = [
  { value: "user_input", label: "User input", hint: "what a user types into the agent" },
  { value: "tool_output", label: "Tool output", hint: "a web page or issue the agent reads" },
  { value: "tool_args", label: "Tool arguments", hint: "what the agent is about to send out" },
  { value: "final_output", label: "Final answer", hint: "what the agent is about to say" }
];

function actionClass(action: string): string {
  if (action === "block") return "danger";
  if (action === "escalate" || action === "redact" || action === "flag") return "warn";
  return "ok";
}

const ACTION_WORD: Record<string, string> = {
  allow: "Passes",
  flag: "Logged",
  redact: "Redacted",
  escalate: "Held for a person",
  block: "Blocked"
};

function errorMessage(error: unknown): string {
  const text = String(error instanceof Error ? error.message : error);
  try {
    const parsed = JSON.parse(text);
    return typeof parsed.detail === "string" ? parsed.detail : text;
  } catch {
    return text;
  }
}

/** Split `text` into plain and highlighted segments from (possibly overlapping) spans. */
function segments(text: string, spans: ScanSpan[]) {
  const sorted = [...spans].sort((a, b) => a.start - b.start);
  const out: { text: string; label?: string }[] = [];
  let at = 0;
  for (const span of sorted) {
    const start = Math.max(span.start, at);
    if (span.end <= start) continue;
    if (start > at) out.push({ text: text.slice(at, start) });
    out.push({ text: text.slice(start, span.end), label: span.label });
    at = span.end;
  }
  if (at < text.length) out.push({ text: text.slice(at) });
  return out;
}

export default function PlaygroundPage() {
  const [tab, setTab] = useState<"scan" | "attack">("scan");
  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Playground</h1>
          <p>
            Scan runs one guard stage over any text, with no agent involved. Attack runs a poisoned page through the
            real agent twice, without and with the guard, against fake tools.
          </p>
        </div>
        <div className="seg" role="tablist" aria-label="Playground mode">
          {(["scan", "attack"] as const).map((t) => (
            <button
              key={t}
              className="seg-option"
              role="tab"
              aria-selected={tab === t}
              onClick={() => setTab(t)}
            >
              {t === "scan" ? "Scan" : "Attack"}
            </button>
          ))}
        </div>
      </header>
      {tab === "scan" ? <ScanPanel /> : <AttackPanel />}
    </div>
  );
}

function ScanPanel() {
  const [stage, setStage] = useState<GuardStage>("tool_output");
  const [text, setText] = useState("");
  const [submitted, setSubmitted] = useState("");
  const [result, setResult] = useState<ScanResult | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function scan() {
    setBusy(true);
    setNote(null);
    try {
      const r = await apiSend<ScanResult>("/api/guard/scan", {
        method: "POST",
        body: JSON.stringify({ stage, text })
      });
      setSubmitted(text);
      setResult(r);
    } catch (e) {
      setNote(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  const allSpans = useMemo(() => (result ? result.policies.flatMap((p) => p.spans) : []), [result]);
  // Policies that would act come first; the rest passed.
  const ordered = useMemo(
    () =>
      result
        ? [...result.policies].sort((a, b) => Number(b.would_action !== "allow") - Number(a.would_action !== "allow"))
        : [],
    [result]
  );
  const hint = STAGES.find((s) => s.value === stage)?.hint;

  return (
    <div className="split">
      <section className="surface surface-pad stack-md" aria-labelledby="scan-input-title">
        <h2 id="scan-input-title" className="h2">
          Text to check
        </h2>
        <div className="field">
          <span className="label" id="stage-label">
            Stage
          </span>
          <div className="seg wrap" role="radiogroup" aria-labelledby="stage-label">
            {STAGES.map((s) => (
              <button
                key={s.value}
                type="button"
                role="radio"
                aria-checked={stage === s.value}
                className="seg-option"
                onClick={() => setStage(s.value)}
              >
                {s.label}
              </button>
            ))}
          </div>
          <span className="help">Checks {hint}.</span>
        </div>
        <label className="field">
          <span className="sr-only">Text</span>
          <textarea
            className="textarea"
            value={text}
            maxLength={8000}
            rows={12}
            placeholder="Paste a web page, an issue comment, a prompt…"
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && text.trim() && !busy) scan();
            }}
          />
        </label>
        <div className="bar">
          <span className="help">{text.length.toLocaleString()} / 8,000 · not stored</span>
          <button className="btn btn-primary" disabled={busy || !text.trim()} onClick={() => scan()}>
            {busy ? "Scanning…" : "Scan"}
          </button>
        </div>
        {note && (
          <p className="notice error" role="alert">
            {note}
          </p>
        )}
      </section>

      <section className="surface surface-pad stack-md" aria-labelledby="verdict-title" aria-live="polite">
        <h2 id="verdict-title" className="h2">
          Verdict
        </h2>
        {!result ? (
          <div className="empty compact">
            <p>Every policy for the chosen stage runs and reports here. Nothing is sent to a model.</p>
          </div>
        ) : (
          <>
            <div className="verdict">
              <span className={`verdict-word text-${actionClass(result.action) === "ok" ? "success" : actionClass(result.action) === "warn" ? "warn" : "danger"}`}>
                {ACTION_WORD[result.action] ?? result.action}
              </span>
              {result.would_action !== result.action && (
                <span className={`badge ${actionClass(result.would_action)}`}>
                  If enforced: {ACTION_WORD[result.would_action] ?? result.would_action}
                </span>
              )}
              <span className="meta">
                {STAGE_NAMES[result.stage] ?? result.stage} · {result.latency_ms.toFixed(0)} ms · config{" "}
                <span className="mono">{result.config_hash.slice(0, 10)}</span>
              </span>
            </div>
            {allSpans.length > 0 && (
              <div className="field">
                <span className="label">Matches</span>
                <pre className="code wrap">
                  {segments(submitted, allSpans).map((seg, i) =>
                    seg.label ? (
                      <mark key={i} className="hit" title={seg.label}>
                        {seg.text}
                      </mark>
                    ) : (
                      <span key={i}>{seg.text}</span>
                    )
                  )}
                </pre>
              </div>
            )}
            {result.text !== submitted && (
              <div className="field">
                <span className="label">What the agent would see</span>
                <pre className="code wrap">{result.text}</pre>
              </div>
            )}
            <div className="rows">
              {ordered.map((p) => (
                <div className="row" key={p.policy_id}>
                  <div className="row-main">
                    <div className="row-title">
                      <span className="mono">{p.policy_id}</span>
                      {p.mode !== "enforce" && <span className="badge">{p.mode}</span>}
                    </div>
                    <p className="meta">
                      <span className="mono">{p.detector}</span>
                      {p.score !== null && ` · score ${p.score.toFixed(2)}`}
                      {p.threshold !== null && ` of ${p.threshold}`}
                      {` · ${p.latency_ms.toFixed(0)} ms`}
                    </p>
                    {p.error && <p className="text-danger small">Error: {p.error}</p>}
                    {p.reasons.length > 0 && <p className="row-desc">{p.reasons.join("; ")}</p>}
                  </div>
                  <div className="row-side">
                    <span className={`badge ${actionClass(p.would_action)}`}>
                      {p.mode === "shadow" && p.would_action !== "allow"
                        ? `Would ${p.would_action}`
                        : ACTION_WORD[p.action] ?? p.action}
                    </span>
                  </div>
                </div>
              ))}
            </div>
            {result.pending_async.length > 0 && (
              <p className="help">Run in the background, not shown: {result.pending_async.join(", ")}</p>
            )}
          </>
        )}
      </section>
    </div>
  );
}

function AttackPanel() {
  const [catalog, setCatalog] = useState<PlaygroundScenarios | null>(null);
  const [scenarioId, setScenarioId] = useState<string>("");
  const [mode, setMode] = useState<"builtin" | "custom">("builtin");
  const [page, setPage] = useState("");
  const [result, setResult] = useState<AttackResult | null>(null);
  const [note, setNote] = useState<{ text: string; error?: boolean } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    apiGet<PlaygroundScenarios>("/api/playground/scenarios")
      .then((c) => {
        setCatalog(c);
        setScenarioId(c.scenarios[0]?.id ?? "");
      })
      .catch((e) => setNote({ text: errorMessage(e), error: true }));
  }, []);

  const selected = catalog?.scenarios.find((s) => s.id === scenarioId) ?? null;

  async function run() {
    setBusy(true);
    setNote({ text: mode === "builtin" ? "Replaying the recorded runs…" : "Running the agent twice with the live model…" });
    setResult(null);
    try {
      const body = mode === "builtin" ? { scenario_id: scenarioId } : { page };
      setResult(await apiSend<AttackResult>("/api/playground/attack", { method: "POST", body: JSON.stringify(body) }));
      setNote(null);
    } catch (e) {
      setNote({ text: errorMessage(e), error: true });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack-lg">
      <section className="surface surface-pad stack-md" aria-labelledby="attack-title">
        <div className="bar">
          <h2 id="attack-title" className="h2">
            Poisoned content
          </h2>
          <div className="seg" role="radiogroup" aria-label="Content source">
            <button type="button" role="radio" aria-checked={mode === "builtin"} className="seg-option" onClick={() => setMode("builtin")}>
              Built-in scenario
            </button>
            <button
              type="button"
              role="radio"
              aria-checked={mode === "custom"}
              className="seg-option"
              disabled={catalog !== null && !catalog.live_runs}
              title={catalog && !catalog.live_runs ? "Live runs are turned off on this server" : undefined}
              onClick={() => setMode("custom")}
            >
              Your own page
            </button>
          </div>
        </div>

        {mode === "builtin" ? (
          <>
            <label className="field">
              <span className="label">Scenario</span>
              <select className="select" value={scenarioId} onChange={(e) => setScenarioId(e.target.value)}>
                {catalog?.scenarios.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.notes ? s.notes : s.id}
                  </option>
                ))}
              </select>
            </label>
            {selected && (
              <>
                <dl className="kv">
                  <div>
                    <dt>The user asks</dt>
                    <dd>{selected.user_task}</dd>
                  </div>
                </dl>
                <details className="disclosure">
                  <summary>Show the page the agent will read</summary>
                  <pre className="code wrap scroll">{selected.page}</pre>
                </details>
              </>
            )}
          </>
        ) : (
          <>
            <dl className="kv">
              <div>
                <dt>The user asks</dt>
                <dd>{catalog?.custom_task}</dd>
              </div>
            </dl>
            <label className="field">
              <span className="label">The page at that URL</span>
              <textarea
                className="textarea"
                value={page}
                rows={10}
                maxLength={catalog?.max_input_chars ?? 8000}
                placeholder="Write a page that tries to make the agent do something the user didn't ask for…"
                onChange={(e) => setPage(e.target.value)}
              />
            </label>
          </>
        )}

        <div className="bar">
          <span className="help">
            {mode === "builtin"
              ? "Replays recorded model responses: free and identical every time."
              : "Uses the live model and the daily demo budget. The tools are fake; nothing is sent or written."}
          </span>
          <button
            className="btn btn-primary"
            disabled={busy || (mode === "builtin" ? !scenarioId : !page.trim())}
            onClick={() => run()}
          >
            {busy ? "Running…" : "Run both"}
          </button>
        </div>
        {note && (
          <p className={`notice${note.error ? " error" : ""}`} role={note.error ? "alert" : "status"}>
            {note.text}
          </p>
        )}
      </section>

      {result && (
        <section className="stack-md" aria-label="Results">
          <p className="help">{result.source === "replay" ? "Replayed from the recorded cassette." : "Live model run."}</p>
          <div className="split">
            {result.runs.map((r) => (
              <RunCard key={r.config} run={r} />
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

function verdict(run: AttackRun): { text: string; cls: string } {
  if (run.attack_success === true) return { text: "Hijacked", cls: "danger" };
  if (run.attack_success === false) return { text: "Attack stopped", cls: "ok" };
  if (run.unexpected_actions.length > 0) return { text: "Did something unasked", cls: "danger" };
  return { text: "No unexpected actions", cls: "ok" };
}

function RunCard({ run }: { run: AttackRun }) {
  const v = verdict(run);
  return (
    <article className={`surface surface-pad stack-md run-card ${v.cls}`}>
      <div className="bar">
        <h3 className="h2">{run.label}</h3>
        <span className={`badge ${v.cls}`}>{v.text}</span>
      </div>

      <div className="field">
        <span className="label">Tool calls</span>
        {run.tool_calls.length === 0 ? (
          <p className="help">None</p>
        ) : (
          <ol className="call-list">
            {run.tool_calls.map((c, i) => {
              const unexpected = run.unexpected_actions.some(
                (u) => u.tool === c.tool && JSON.stringify(u.arguments) === JSON.stringify(c.arguments)
              );
              return (
                <li key={i} className={unexpected ? "unexpected" : undefined}>
                  <span className="mono">{c.tool}</span>
                  {unexpected && <span className="badge danger">Not asked for</span>}
                  <span className="meta mono">{JSON.stringify(c.arguments)}</span>
                </li>
              );
            })}
          </ol>
        )}
      </div>

      {run.guard_flags.length > 0 && (
        <div className="field">
          <span className="label">Guard flags</span>
          <ul className="call-list">
            {run.guard_flags.map((f, i) => (
              <li key={i}>
                <span className="mono">{f.policy}</span>
                <span className={`badge ${actionClass(f.would_action)}`}>
                  {f.mode === "shadow" ? `would ${f.would_action}` : f.action}
                </span>
                <span className="meta">
                  {STAGE_NAMES[f.stage] ?? f.stage}
                  {f.tool ? ` (${f.tool})` : ""}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="field">
        <span className="label">Final answer</span>
        <p className="answer">{run.final_message}</p>
      </div>
      <p className="meta">
        {run.status} · {run.steps} steps · ${run.cost_usd.toFixed(4)}
        {run.trace_url && (
          <>
            {" · "}
            <a href={run.trace_url} target="_blank" rel="noreferrer">
              Trace
            </a>
          </>
        )}
      </p>
    </article>
  );
}
