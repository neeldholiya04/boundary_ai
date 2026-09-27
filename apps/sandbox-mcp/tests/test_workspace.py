from pathlib import Path

from boundary_mcp.workspace import SandboxWorkspace, WorkspaceError


def test_write_read_and_search(tmp_path: Path) -> None:
    workspace = SandboxWorkspace(tmp_path)

    write_result = workspace.write_file("notes/demo.txt", "hello boundary\nsecure tool call")
    assert write_result["path"] == "notes/demo.txt"

    read_result = workspace.read_file("notes/demo.txt")
    assert "secure tool call" in read_result["content"]

    search_result = workspace.search_files("boundary")
    assert search_result["matches"][0]["path"] == "notes/demo.txt"


def test_list_files_returns_serializable_entries(tmp_path: Path) -> None:
    workspace = SandboxWorkspace(tmp_path)
    workspace.write_file("notes/demo.txt", "hello")

    result = workspace.list_files("notes")
    assert result == [{"path": "notes/demo.txt", "is_dir": False, "size": 5}]


def test_rejects_escape_outside_sandbox(tmp_path: Path) -> None:
    workspace = SandboxWorkspace(tmp_path)

    try:
        workspace.write_file("../escape.txt", "nope")
    except WorkspaceError as exc:
        assert "sandbox root" in str(exc)
    else:
        raise AssertionError("Expected WorkspaceError for sandbox escape")
