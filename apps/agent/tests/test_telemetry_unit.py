from prometheus_client import CollectorRegistry

from boundary_agent.telemetry import Telemetry


def test_demo_telemetry_keeps_its_metrics_out_of_the_app_registry():
    app_registry = CollectorRegistry()
    app = Telemetry(None, registry=app_registry, metadata={"config_hash": "h"})
    demo = app.for_demo()
    demo.record_llm(model="m", purpose="planner", outcome="ok", prompt_tokens=10, cost_usd=0.01)
    with demo.run("run-1", name="chat", conversation_id="c") as handle:
        handle.finish("completed", "ok")
    labels = {"model": "m", "purpose": "planner", "outcome": "ok"}
    assert app_registry.get_sample_value("llm_requests_total", labels) is None
    assert app_registry.get_sample_value("agent_runs_total", {"status": "completed"}) is None
    assert demo.tags == ["playground"] and demo.metadata == {"config_hash": "h"}
