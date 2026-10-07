"use client";

import { useEffect, useMemo, useState } from "react";

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
  if (action === "escalate" || action === "redact" || action === "flag") return "warning";
  return "success";
}

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
      <header className="page-header">
        <div>
          <h2>Playground</h2>
          <p className="muted">
            Try the guardrails yourself. <strong>Scan</strong> runs one guard stage over any text, with no
            model involved. <strong>Attack</strong> runs a poisoned page through the real agent twice, with no
            defence and with the guard on, against fake read-only tools.
          </p>
        </div>
        <div className="row" role="tablist">
          <button
            className={`button${tab === "scan" ? "" : " secondary"}`}
            role="tab"
            aria-selected={tab === "scan"}
            onClick={() => setTab("scan")}
          >
            Scan
          </button>
          <button
            className={`button${tab === "attack" ? "" : " secondary"}`}
            role="tab"
            aria-selected={tab === "attack"}
            onClick={() => setTab("attack")}
          >
            Attack
          </button>
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

  return (
    <div className="grid two">
      <section className="panel stack">
        <h3>Text to check</h3>
        <label className="field">
          <span>Stage</span>
          <select value={stage} onChange={(e) => setStage(e.target.value as GuardStage)}>
            {STAGES.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}: {s.hint}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Text</span>
          <textarea
            value={text}
            maxLength={8000}
            rows={12}
            placeholder="Paste a web page, an issue comment, a prompt…"
            onChange={(e) => setText(e.target.value)}
          />
        </label>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <span className="muted">{text.length} / 8000 characters · not stored or traced</span>
          <button className="button" disabled={busy || !text.trim()} onClick={() => scan()}>
            {busy ? "Scanning…" : "Scan"}
          </button>
        </div>
        {note && <p className="page-status">{note}</p>}
      </section>

      <section className="panel stack">
        <h3>Verdict</h3>
        {!result ? (
          <p className="muted">Each policy for the chosen stage runs and reports its verdict here.</p>
        ) : (
          <>
            <div className="row wrap">
              <span className={`badge ${actionClass(result.action)}`}>applied: {result.action}</span>
              {result.would_action !== result.action && (
                <span className={`badge ${actionClass(result.would_action)}`}>
                  if enforced: {result.would_action}
                </span>
              )}
              <span className="badge mono">{result.latency_ms.toFixed(0)} ms</span>
              <span className="badge mono">config {result.config_hash}</span>
            </div>
            {allSpans.length > 0 && (
              <div className="card">
                <p className="muted">Matched spans</p>
                <pre className="scan-text">
                  {segments(submitted, allSpans).map((seg, i) =>
                    seg.label ? (
                      <mark key={i} className="span-hit" title={seg.label}>
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
              <div className="card">
                <p className="muted">What the agent would see (redacted)</p>
                <pre className="scan-text">{result.text}</pre>
              </div>
            )}
            <div className="list">
              {result.policies.map((p) => (
                <article className="card" key={p.policy_id}>
                  <div className="row wrap" style={{ justifyContent: "space-between" }}>
                    <strong className="mono">{p.policy_id}</strong>
                    <div className="row">
                      <span className="badge">{p.mode}</span>
                      <span className={`badge ${actionClass(p.would_action)}`}>
                        {p.mode === "shadow" && p.would_action !== "allow" ? `would ${p.would_action}` : p.action}
                      </span>
                    </div>
                  </div>
                  <p className="muted mono">
                    {p.detector}
                    {p.score !== null && ` · score ${p.score.toFixed(3)}`}
                    {p.threshold !== null && ` / threshold ${p.threshold}`}
                    {` · ${p.latency_ms.toFixed(0)} ms`}
                  </p>
                  {p.error && <p className="chat-inline-status warning">error: {p.error}</p>}
                  {p.reasons.length > 0 && <p className="muted">{p.reasons.join("; ")}</p>}
                </article>
              ))}
            </div>
            {result.pending_async.length > 0 && (
              <p className="muted">
                Runs in the background, not shown here: {result.pending_async.join(", ")}
              </p>
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
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    apiGet<PlaygroundScenarios>("/api/playground/scenarios")
      .then((c) => {
        setCatalog(c);
        setScenarioId(c.scenarios[0]?.id ?? "");
      })
      .catch((e) => setNote(errorMessage(e)));
  }, []);

  const selected = catalog?.scenarios.find((s) => s.id === scenarioId) ?? null;

  async function run() {
    setBusy(true);
    setNote(mode === "builtin" ? "Replaying the recorded runs…" : "Running the agent twice with the live model…");
    setResult(null);
    try {
      const body = mode === "builtin" ? { scenario_id: scenarioId } : { page };
      const r = await apiSend<AttackResult>("/api/playground/attack", {
        method: "POST",
        body: JSON.stringify(body)
      });
      setResult(r);
      setNote(null);
    } catch (e) {
      setNote(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <section className="panel stack">
        <div className="row wrap" style={{ justifyContent: "space-between" }}>
          <h3>Poisoned content</h3>
          <div className="row">
            <button
              className={`button${mode === "builtin" ? "" : " secondary"}`}
              onClick={() => setMode("builtin")}
            >
              Built-in scenario
            </button>
            <button
              className={`button${mode === "custom" ? "" : " secondary"}`}
              disabled={catalog !== null && !catalog.live_runs}
              title={catalog && !catalog.live_runs ? "Live runs are turned off on this server" : undefined}
              onClick={() => setMode("custom")}
            >
              Paste your own page
            </button>
          </div>
        </div>

        {mode === "builtin" ? (
          <>
            <label className="field">
              <span>Scenario</span>
              <select value={scenarioId} onChange={(e) => setScenarioId(e.target.value)}>
                {catalog?.scenarios.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.id}
                    {s.notes ? `: ${s.notes}` : ""}
                  </option>
                ))}
              </select>
            </label>
            {selected && (
              <>
                <p>
                  <span className="muted">The user asks: </span>
                  {selected.user_task}
                </p>
                <details>
                  <summary className="muted">Show the content the agent will read</summary>
                  <pre className="scan-text scroll">{selected.page}</pre>
                </details>
                <p className="muted">Replays recorded model responses: free and identical every time.</p>
              </>
            )}
          </>
        ) : (
          <>
            <p>
              <span className="muted">The user asks: </span>
              {catalog?.custom_task}
            </p>
            <label className="field">
              <span>The page at that URL</span>
              <textarea
                value={page}
                rows={10}
                maxLength={catalog?.max_input_chars ?? 8000}
                placeholder="Write a page that tries to make the agent do something the user didn't ask for…"
                onChange={(e) => setPage(e.target.value)}
              />
            </label>
            <p className="muted">
              Uses the live model (counts against the daily demo budget). The tools are fake: nothing is really
              sent or written.
            </p>
          </>
        )}

        <div className="row" style={{ justifyContent: "flex-end" }}>
          <button
            className="button"
            disabled={busy || (mode === "builtin" ? !scenarioId : !page.trim())}
            onClick={() => run()}
          >
            {busy ? "Running…" : "Run with and without the guard"}
          </button>
        </div>
        {note && <p className="page-status">{note}</p>}
      </section>

      {result && (
        <>
          <p className="muted">
            {result.source === "replay" ? "Replayed from the recorded cassette." : "Live model run."}
          </p>
          <div className="grid two">
            {result.runs.map((r) => (
              <RunCard key={r.config} run={r} />
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function verdict(run: AttackRun): { text: string; cls: string } {
  if (run.attack_success === true) return { text: "Hijacked", cls: "danger" };
  if (run.attack_success === false) return { text: "Attack stopped", cls: "success" };
  if (run.unexpected_actions.length > 0) return { text: "Did something unasked", cls: "danger" };
  return { text: "No unexpected actions", cls: "success" };
}

function RunCard({ run }: { run: AttackRun }) {
  const v = verdict(run);
  return (
    <section className="panel stack">
      <div className="row wrap" style={{ justifyContent: "space-between" }}>
        <h3>{run.label}</h3>
        <div className="row">
          <span className={`badge ${v.cls}`}>{v.text}</span>
          <span className="badge">{run.status}</span>
        </div>
      </div>

      <div className="stack">
        <p className="muted">Tool calls</p>
        {run.tool_calls.length === 0 ? (
          <p className="muted">none</p>
        ) : (
          <ol className="tool-calls">
            {run.tool_calls.map((c, i) => {
              const unexpected = run.unexpected_actions.some(
                (u) => u.tool === c.tool && JSON.stringify(u.arguments) === JSON.stringify(c.arguments)
              );
              return (
                <li key={i} className={unexpected ? "unexpected" : undefined}>
                  <span className="mono">{c.tool}</span>{" "}
                  <span className="muted mono">{JSON.stringify(c.arguments)}</span>
                </li>
              );
            })}
          </ol>
        )}
      </div>

      {run.guard_flags.length > 0 && (
        <div className="stack">
          <p className="muted">Guard flags</p>
          <ul className="tool-calls">
            {run.guard_flags.map((f, i) => (
              <li key={i}>
                <span className="mono">{f.policy}</span>{" "}
                <span className={`badge ${actionClass(f.would_action)}`}>
                  {f.mode === "shadow" ? `would ${f.would_action}` : f.action}
                </span>{" "}
                <span className="muted">
                  on {f.stage}
                  {f.tool ? ` (${f.tool})` : ""}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="card">
        <p className="muted">Final answer</p>
        <p>{run.final_message}</p>
      </div>
      {run.trace_url && (
        <a className="muted" href={run.trace_url} target="_blank" rel="noreferrer">
          View trace in Langfuse ↗
        </a>
      )}
    </section>
  );
}
