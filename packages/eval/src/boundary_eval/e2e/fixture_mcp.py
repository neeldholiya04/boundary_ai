"""A deterministic in-process MCP stand-in for the end-to-end harness.

It mimics the two methods the agent's AgentRuntime uses (`list_tools`, `call_tool`), serves the
content a scenario declares for each tool, and records every call so the checks can inspect what
the agent actually did. Nothing here reaches the network.

Tools mirror the research assistant's real set: fetch_url, web_search, get_github_issue,
read_file, list_files, write_file, delete_file, send_email. write_file / delete_file / send_email
are side-effecting sinks; the recorded calls are how we tell whether an attack succeeded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from boundary_agent.types import ToolDescriptor
from boundary_eval.e2e.scenarios import Keyed

READ_TOOLS = {
    "fetch_url": ("url", "Fetch a web page by URL."),
    "web_search": ("query", "Search the web and return result snippets."),
    "get_github_issue": ("ref", "Read a GitHub issue and its comments."),
    "read_file": ("path", "Read a file from the notes workspace."),
    "list_files": ("path", "List files in the notes workspace."),
}
SINK_TOOLS = {
    "write_file": ("Write a file into the notes workspace.", ["path", "content"]),
    "delete_file": ("Delete a file from the notes workspace.", ["path"]),
    "send_email": ("Send an email.", ["to", "subject", "body"]),
}


@dataclass(slots=True)
class ToolCallRecord:
    tool_name: str
    arguments: dict[str, Any]


@dataclass
class FixtureMCP:
    """Serves scenario content and logs calls. One instance per scenario run.

    `responses` maps a read tool name to either a fixed value, or a dict keyed by the primary
    argument (e.g. a URL) to a value. A value is a str (returned as MCP text content) or any
    JSON-serialisable object (returned as structured content). Missing keys return a benign
    "not found" so a hijacked lookup does not crash the run.
    """

    server_id: str
    server_name: str
    responses: dict[str, Any]
    workspace: dict[str, str] = field(default_factory=dict)
    calls: list[ToolCallRecord] = field(default_factory=list)

    async def list_tools(self, session: Any, refresh: bool = False) -> list[ToolDescriptor]:
        tools = []
        for name, (arg, desc) in READ_TOOLS.items():
            schema = {"type": "object", "properties": {arg: {"type": "string"}}, "required": [arg]}
            tools.append(ToolDescriptor(self.server_id, self.server_name, "stdio", name, desc, schema))
        for name, (desc, args) in SINK_TOOLS.items():
            schema = {"type": "object", "properties": {a: {"type": "string"} for a in args}, "required": args}
            tools.append(ToolDescriptor(self.server_id, self.server_name, "stdio", name, desc, schema))
        return tools

    async def call_tool(self, session: Any, server_id: str, tool_name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append(ToolCallRecord(tool_name, dict(arguments)))
        if tool_name in SINK_TOOLS:
            return self._sink(tool_name, arguments)
        if tool_name in READ_TOOLS:
            return self._read(tool_name, arguments)
        raise ValueError(f"unknown tool: {tool_name}")

    def _read(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        arg_name = READ_TOOLS[tool_name][0]
        key = str(arguments.get(arg_name, ""))
        spec = self.responses.get(tool_name)

        if tool_name in ("read_file", "list_files"):
            if tool_name == "list_files":
                return {"files": sorted(self.workspace)}
            if key in self.workspace:
                return self._as_result(self.workspace[key])

        value = spec.get(key, spec.get("*")) if isinstance(spec, Keyed) else spec
        if value is None:
            return self._as_result(f"No content found for {tool_name}({key}).")
        return self._as_result(value)

    def _sink(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        if tool_name == "write_file":
            self.workspace[str(arguments.get("path", ""))] = str(arguments.get("content", ""))
            return {"path": arguments.get("path"), "bytes_written": len(str(arguments.get("content", "")))}
        if tool_name == "delete_file":
            self.workspace.pop(str(arguments.get("path", "")), None)
            return {"path": arguments.get("path"), "deleted": True}
        return {"to": arguments.get("to"), "sent": True}

    @staticmethod
    def _as_result(value: Any) -> Any:
        if isinstance(value, str):
            return {"content": value, "raw": {"isError": False}}
        return value

    def calls_to(self, tool_name: str) -> list[ToolCallRecord]:
        return [c for c in self.calls if c.tool_name == tool_name]

    def all_arguments_text(self) -> str:
        """Every argument the agent ever sent to a tool, as one string, for exfiltration checks."""
        return json.dumps(
            [{"tool": c.tool_name, "args": c.arguments} for c in self.calls], ensure_ascii=False
        )
