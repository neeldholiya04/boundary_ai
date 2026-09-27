import pytest

from boundary_agent.config import Settings
from boundary_agent.llm import LiteLLMPlanner, MissingPlanner, MockPlanner, get_planner
from boundary_agent.types import ExecutedToolStep, PlannerMessage, ToolCall, ToolDescriptor


def mock_tools() -> list[ToolDescriptor]:
    return [
        ToolDescriptor("server-1", "local-sandbox", "stdio", "list_files", None, None),
        ToolDescriptor("server-1", "local-sandbox", "stdio", "read_file", None, None),
        ToolDescriptor("server-1", "local-sandbox", "stdio", "write_file", None, None),
        ToolDescriptor("server-1", "local-sandbox", "stdio", "delete_file", None, None),
        ToolDescriptor("server-1", "local-sandbox", "stdio", "search_files", None, None),
    ]


@pytest.mark.anyio
async def test_mock_planner_understands_natural_delete_phrase() -> None:
    planner = MockPlanner()
    decision = await planner.plan("now delete the file notes/demo.txt", mock_tools(), [], [])
    assert decision.tool_call is not None
    assert decision.tool_call.tool_name == "delete_file"
    assert decision.tool_call.arguments == {"path": "notes/demo.txt"}


@pytest.mark.anyio
async def test_mock_planner_advances_through_multiple_actions() -> None:
    planner = MockPlanner()
    prompt = "list files\nwrite file notes/demo.txt: hello\nread file notes/demo.txt"

    first = await planner.plan(prompt, mock_tools(), [], [])
    assert first.tool_call is not None
    assert first.tool_call.tool_name == "list_files"

    second = await planner.plan(
        prompt,
        mock_tools(),
        [ExecutedToolStep(ToolCall("server-1", "list_files", {"path": "."}), {"result": []})],
        [],
    )
    assert second.tool_call is not None
    assert second.tool_call.tool_name == "write_file"

    third = await planner.plan(
        prompt,
        mock_tools(),
        [
            ExecutedToolStep(ToolCall("server-1", "list_files", {"path": "."}), {"result": []}),
            ExecutedToolStep(
                ToolCall("server-1", "write_file", {"path": "notes/demo.txt", "content": "hello"}),
                {"path": "notes/demo.txt", "bytes_written": 5},
            ),
        ],
        [],
    )
    assert third.tool_call is not None
    assert third.tool_call.tool_name == "read_file"


@pytest.mark.anyio
async def test_mock_planner_can_select_exa_web_search() -> None:
    tools = [
        *mock_tools(),
        ToolDescriptor("server-2", "exa", "streamable_http", "web_search_exa", None, None),
    ]
    planner = MockPlanner()
    decision = await planner.plan("search the web for boundary-ai", tools, [], [])
    assert decision.tool_call is not None
    assert decision.tool_call.tool_name == "web_search_exa"
    assert decision.tool_call.arguments["query"] == "boundary-ai"


@pytest.mark.anyio
async def test_mock_planner_explains_previous_block_reason_from_history() -> None:
    planner = MockPlanner()
    history = [
        PlannerMessage(role="user", content="list files"),
        PlannerMessage(role="assistant", content="Tool call blocked: privacy"),
        PlannerMessage(role="user", content="why was it blocked?"),
    ]

    decision = await planner.plan("why was it blocked?", mock_tools(), [], history)

    assert decision.tool_call is None
    assert decision.assistant_message == "Your last tool call was blocked because: privacy"


def test_litellm_planner_builds_safe_unique_tool_aliases() -> None:
    planner = LiteLLMPlanner(Settings(llm_model="openai/gpt-4.1-mini"))
    tools = [
        ToolDescriptor("server:1", "local/sandbox", "stdio", "write:file", None, None),
        ToolDescriptor("server:2", "local/sandbox", "stdio", "write:file", None, None),
    ]

    aliases = planner._build_tool_aliases(tools)

    assert len(aliases) == 2
    assert all(":" not in alias for alias in aliases)
    assert all("/" not in alias for alias in aliases)
    assert list(aliases.values()) == tools


def test_get_planner_requires_explicit_mock_opt_in() -> None:
    planner = get_planner(Settings(llm_provider="mock", allow_demo_mock_planner=False))

    assert isinstance(planner, MissingPlanner)
