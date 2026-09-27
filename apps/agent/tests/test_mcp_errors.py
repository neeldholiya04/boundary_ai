import httpx

from boundary_agent.mcp_manager import describe_error


def test_describe_error_unwraps_nested_task_groups():
    request = httpx.Request("POST", "https://mcp.example.test/mcp")
    response = httpx.Response(429, request=request)
    cause = httpx.HTTPStatusError("Client error '429 Too Many Requests'", request=request, response=response)
    wrapped = ExceptionGroup("unhandled errors in a TaskGroup", [ExceptionGroup("inner", [cause])])
    assert describe_error(wrapped) == "HTTPStatusError: Client error '429 Too Many Requests'"


def test_describe_error_keeps_plain_errors_and_names_empty_ones():
    assert describe_error(ValueError("bad args")) == "ValueError: bad args"
    assert describe_error(TimeoutError()) == "TimeoutError"
