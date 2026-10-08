# boundary-guard

Composable input/output checks for tool-calling LLM agents. Policies are defined in versioned YAML
(the production set is [`policies/guard.yaml`](../../policies/guard.yaml)) and run at four stages:
`user_input`, `tool_args`, `tool_output`, `final_output`. The library does not import the agent, so it
can wrap any LLM app.

```python
from boundary_guard import CheckContext, Guard, Stage

guard = Guard.from_yaml("policies/guard.yaml")
result = await guard.check(Stage.TOOL_OUTPUT, page_text, CheckContext(run_id="r1", tool_name="fetch_url"))
if result.blocked:
    ...  # withhold the content from the model
text_for_model = result.text  # redacted / repaired text
await guard.drain()  # wait for async policies (tests, shutdown)
```

Each policy has a detector, an action (`allow`, `flag`, `redact`, `escalate`, `block`), a mode
(`enforce` / `shadow` / `off`), an execution (`blocking` / `async`), a timeout and an `on_error`
(fail open or closed). The strongest action wins; `would_action` records what a shadow policy would
have done. Every decision records its score, latency, cost and the config hash.

## Detectors

| Type | What | Extra |
|---|---|---|
| `regex_rules` | rulesets in YAML (secrets incl. the imported gitleaks rules on RE2, jailbreak phrasing); can `redact_first` | – |
| `keywords`, `pattern`, `always` | dashboard rules: word lists (optional fuzzy match), bounded regexes, unconditional | – |
| `json_schema` | output format, with repair | – |
| `hf_classifier` | any Hugging Face sequence classifier, chunked (Prompt Guard 2, ProtectAI, toxic-bert, our detector) | `ml` |
| `all_of` | several detectors must agree, with an optional decide-alone score for the first | `ml` |
| `embeddings_topic` | nearest exemplar, allow vs deny lists, with margin, floor and word minimum | `ml` |
| `presidio` | personal data (Presidio) | `ml` |
| `nli_groundedness` | answer claims vs sources (NLI) | `ml` |
| `llm_judge` | a plain-language policy judged by any LiteLLM model | `judge` |

Install extras with `boundary-guard[ml]`, `[judge]`, `[metrics]` (Prometheus sink). The workspace
installs `ml` by default. Model revisions are pinned to Hugging Face commit SHAs in the policy file.

## Test

```bash
uv run pytest packages/guard/tests
```

The ML detector tests download pinned models the first time; Prompt Guard is gated (request access,
then `uv run hf auth login` or `HF_TOKEN`).

## Key files (`src/boundary_guard/`)

- `core/config.py`: policy YAML schema and `load_config`
- `core/pipeline.py`: `Guard`: runs a stage's policies, combines verdicts, applies redactions, async
  policies, decision sinks
- `core/redact.py`: span merging and placeholders (`<EMAIL_1>`, `<OPENAI_KEY_1>`)
- `core/types.py`: `Stage`, `Action`, `Mode`, `CheckContext`, `GuardResult`
- `detectors/`: one module per detector type (above)
- `metrics.py`: Prometheus metrics

How each detector and model is used in production: [docs/MODELS.md](../../docs/MODELS.md).
How they are measured: [docs/EVAL.md](../../docs/EVAL.md).
