"""Tracing (Langfuse) and agent-level metrics (Prometheus) for the agent runtime.

Langfuse: one trace per run. The trace id is derived from the run id, so a run that pauses for a
human approval and resumes later lands in the same trace. Inside it:

    agent run (as_type=agent)
      planner (generation: model, input messages, output, tokens, cost)       one per planner call
      tool.<name> (tool: arguments, the result the planner was given)         one per tool call
      guard.<stage> (guardrail: redacted excerpt, overall action)             one per guard check
        <policy_id> (guardrail: score, threshold, action, would-be action, latency, error)

Privacy rule: a trace never holds more than the LLM provider sees. Inputs and outputs are the
guard's output (redacted text, redacted excerpts, withheld stubs), never the raw user message or
raw tool output. Tracing is off unless LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set; every
call here is then a cheap no-op.

Prometheus (agent side; guard metrics come from boundary_guard.metrics.PrometheusSink):

    llm_requests_total{model, purpose, outcome}    llm_tokens_total{model, purpose, kind}
    llm_cost_usd_total{model, purpose}             agent_runs_total{status}
    agent_run_latency_seconds{status}              guard_async_queue_depth
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge, Histogram

logger = logging.getLogger("boundary.telemetry")

RUN_BUCKETS = (0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0)


@dataclass(slots=True)
class _AgentMetrics:
    llm_requests: Counter
    llm_tokens: Counter
    llm_cost: Counter
    runs: Counter
    run_latency: Histogram
    async_queue: Gauge


_metrics_by_registry: dict[int, _AgentMetrics] = {}


def _agent_metrics(registry: CollectorRegistry) -> _AgentMetrics:
    found = _metrics_by_registry.get(id(registry))
    if found is None:
        found = _AgentMetrics(
            llm_requests=Counter(
                "llm_requests_total", "LLM calls.", ["model", "purpose", "outcome"], registry=registry
            ),
            llm_tokens=Counter(
                "llm_tokens_total", "LLM tokens.", ["model", "purpose", "kind"], registry=registry
            ),
            llm_cost=Counter(
                "llm_cost_usd_total", "LLM spend in USD.", ["model", "purpose"], registry=registry
            ),
            runs=Counter(
                "agent_runs_total",
                "Agent runs (or resumed runs) by final status.",
                ["status"],
                registry=registry,
            ),
            run_latency=Histogram(
                "agent_run_latency_seconds",
                "Wall time of one agent run (or resume) by final status.",
                ["status"],
                buckets=RUN_BUCKETS,
                registry=registry,
            ),
            async_queue=Gauge(
                "guard_async_queue_depth", "Async guard checks scheduled but not finished.", registry=registry
            ),
        )
        _metrics_by_registry[id(registry)] = found
    return found


class _Noop:
    """Stands in for a Langfuse observation when tracing is off."""

    def update(self, **_: Any) -> _Noop:
        return self

    def start_observation(self, **_: Any) -> _Noop:
        return self

    def end(self, **_: Any) -> None:
        return None


NOOP = _Noop()


class RunHandle:
    """Yielded by `Telemetry.run`. Call `finish(status, output)` with the response before leaving."""

    def __init__(self, observation: Any) -> None:
        self.observation = observation
        self.status = "error"  # if the block raises before finish()
        self.output: Any = None

    def set_input(self, value: Any) -> None:
        self.observation.update(input=value)  # the root observation's input is the trace input

    def finish(self, status: str, output: Any) -> None:
        self.status, self.output = status, output


class GuardSpan:
    """The guardrail observation for one check, filled in once the check has returned."""

    def __init__(self, observation: Any) -> None:
        self.observation = observation

    def record(self, result: Any, excerpt: str) -> None:
        if self.observation is NOOP:
            return
        flagged = result.would_action.value != "allow"
        self.observation.update(
            input=excerpt,
            output={"action": result.action.value, "would_action": result.would_action.value},
            metadata={"config_hash": result.config_hash, "latency_ms": round(result.latency_ms, 2)},
            level="WARNING" if flagged else "DEFAULT",
        )
        for d in result.decisions:
            child = self.observation.start_observation(
                name=d.policy_id,
                as_type="guardrail",
                output={"action": d.action.value, "would_action": d.would_action.value},
                metadata={
                    "mode": d.mode.value,
                    "execution": d.execution.value,
                    "detector": d.detector,
                    "score": d.score,
                    "threshold": d.threshold,
                    "latency_ms": round(d.latency_ms, 2),
                    "span_labels": sorted({span.label for span in d.spans}),
                    "error": d.error,
                    "policy_version": d.policy_version,
                },
                level="ERROR" if d.error else ("WARNING" if d.would_action.value != "allow" else "DEFAULT"),
            )
            child.end()


class Telemetry:
    def __init__(
        self,
        client: Any | None = None,
        *,
        registry: CollectorRegistry = REGISTRY,
        environment: str = "development",
        metadata: dict[str, str] | None = None,
        tags: list[str] | None = None,
    ) -> None:
        self.client = client
        self.environment = environment
        self.metadata = dict(metadata or {})
        self.tags = list(tags or [])
        self.metrics = _agent_metrics(registry)
        self._trace_url_failed = False

    @classmethod
    def from_settings(cls, settings: Any, *, metadata: dict[str, str] | None = None) -> Telemetry:
        client = None
        if settings.langfuse_public_key and settings.langfuse_secret_key:
            try:
                from langfuse import Langfuse

                client = Langfuse(
                    public_key=settings.langfuse_public_key,
                    secret_key=settings.langfuse_secret_key,
                    host=settings.langfuse_host,
                    environment=settings.app_env,
                )
            except Exception:  # tracing must never take the agent down
                logger.exception("Langfuse init failed; tracing disabled")
        return cls(client, environment=settings.app_env, metadata=metadata)

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def for_demo(self, tag: str = "playground") -> Telemetry:
        """Same Langfuse project (traces tagged `tag`), but metrics on a private registry, so demo
        runs and replayed LLM calls never show up in the app's /metrics."""
        return Telemetry(
            self.client,
            registry=CollectorRegistry(),
            environment=self.environment,
            metadata=self.metadata,
            tags=[*self.tags, tag],
        )

    def watch_guard_queue(self, guard: Any) -> None:
        self.metrics.async_queue.set_function(lambda: guard.pending_async)

    # ---- tracing -------------------------------------------------------------------------------

    @contextmanager
    def run(self, run_id: str, *, name: str, conversation_id: str, input: Any = None) -> Iterator[RunHandle]:
        """Trace one agent run (or the resumed part of one) and time it."""
        started = time.perf_counter()
        if self.client is None:
            handle = RunHandle(NOOP)
            try:
                yield handle
            finally:
                self._observe_run(handle.status, started)
            return

        from langfuse import propagate_attributes

        trace_id = self.client.create_trace_id(seed=run_id)
        metadata = {**self.metadata, "run_id": run_id}
        with (
            propagate_attributes(
                session_id=conversation_id,
                trace_name=name,
                metadata=metadata,
                tags=["agent", name, *self.tags],
            ),
            self.client.start_as_current_observation(
                trace_context={"trace_id": trace_id},
                name=name,
                as_type="agent",
                input=input,
                metadata=metadata,
            ) as observation,
        ):
            handle = RunHandle(observation)
            try:
                yield handle
            finally:
                level = "ERROR" if handle.status in ("failed", "error") else "DEFAULT"
                observation.update(output=handle.output, metadata={"status": handle.status}, level=level)
                self._observe_run(handle.status, started)

    @contextmanager
    def observe(self, name: str, *, as_type: str = "span", **fields: Any) -> Iterator[Any]:
        """A child observation of whatever is current (a no-op when tracing is off)."""
        if self.client is None:
            yield NOOP
            return
        with self.client.start_as_current_observation(name=name, as_type=as_type, **fields) as observation:
            yield observation

    @contextmanager
    def guard_check(self, stage: str) -> Iterator[GuardSpan]:
        """Wrap one guard check; call `.record(result, excerpt)` on the yielded span once it returns."""
        with self.observe(f"guard.{stage}", as_type="guardrail") as observation:
            yield GuardSpan(observation)

    def trace_url(self, run_id: str | None) -> str | None:
        if self.client is None or not run_id or self._trace_url_failed:
            return None
        try:
            return self.client.get_trace_url(trace_id=self.client.create_trace_id(seed=run_id))
        except Exception:  # e.g. bad keys: stop asking, keep the agent working
            self._trace_url_failed = True
            logger.warning("could not build Langfuse trace URLs; check the Langfuse keys", exc_info=True)
            return None

    def flush(self) -> None:
        if self.client is not None:
            self.client.flush()

    # ---- metrics -------------------------------------------------------------------------------

    def record_llm(
        self,
        *,
        model: str,
        purpose: str,
        outcome: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        m = self.metrics
        m.llm_requests.labels(model, purpose, outcome).inc()
        if prompt_tokens:
            m.llm_tokens.labels(model, purpose, "prompt").inc(prompt_tokens)
        if completion_tokens:
            m.llm_tokens.labels(model, purpose, "completion").inc(completion_tokens)
        if cost_usd:
            m.llm_cost.labels(model, purpose).inc(cost_usd)

    def _observe_run(self, status: str, started: float) -> None:
        self.metrics.runs.labels(status).inc()
        self.metrics.run_latency.labels(status).observe(time.perf_counter() - started)


# Used wherever no Telemetry is passed in (tests, the eval harness): tracing off, metrics on a
# private registry so they never mix into the app's /metrics.
DISABLED = Telemetry(None, registry=CollectorRegistry())
