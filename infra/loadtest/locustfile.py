"""Locust user for the boundary-ai load test (driven by `boundary-eval loadtest`).

Each simulated user sends research requests to the agent in a closed loop: the next request starts
as soon as the previous answer arrives. The agent runs with the stub planner (fixed simulated LLM
latency), so throughput and latency reflect the guard and the agent loop, not a provider.

A response that isn't a completed run (e.g. the guard blocked it because a check timed out) is
counted as a failure, so the failure rate is the share of benign requests that didn't go through.
"""

import itertools

from locust import HttpUser, constant, task

PROMPTS = [
    "Read the vectorlite page and summarise the known issues.",
    "Summarise how vectorlite handles crashes, from its README.",
    "What does the vectorlite README say about upgrading to 0.7?",
    "Give me a short summary of the vectorlite page.",
]
_prompts = itertools.cycle(PROMPTS)


class ResearchUser(HttpUser):
    wait_time = constant(0)

    @task
    def chat(self) -> None:
        with self.client.post(
            "/api/chat", json={"message": next(_prompts)}, name="chat", catch_response=True, timeout=120
        ) as response:
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}")
                return
            status = response.json().get("status")
            if status != "completed":
                response.failure(f"run {status}")
