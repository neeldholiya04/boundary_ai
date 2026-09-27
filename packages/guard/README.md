# boundary-guard

Composable input/output filtering for tool-calling LLM agents. Policies are defined in
versioned YAML (see [`policies/guard.yaml`](../../policies/guard.yaml)) and run at four stages:
`user_input`, `tool_args`, `tool_output`, `final_output`.

```python
from boundary_guard import Guard, Stage, CheckContext

guard = Guard.from_yaml("policies/guard.yaml")
result = await guard.check(Stage.TOOL_OUTPUT, page_text, CheckContext(run_id="r1", tool_name="fetch_url"))
if result.blocked:
    ...  # withhold the content from the model
text_for_model = result.text  # redacted / repaired text
```

Each policy has a `mode` (`enforce` / `shadow` / `off`) and an `execution` (`blocking` /
`async`). Every decision records its score, latency, cost, and the config hash.
