from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class WorkspaceError(Exception):
    """Raised when a sandbox operation is invalid."""


@dataclass(slots=True)
class WorkspaceEntry:
    path: str
    is_dir: bool
    size: int


class SandboxWorkspace:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative_path: str) -> Path:
        candidate = self.root if not relative_path else (self.root / relative_path).resolve()

        if candidate != self.root and self.root not in candidate.parents:
            raise WorkspaceError("Path must stay under the sandbox root.")

        return candidate

    def list_files(self, relative_path: str = ".") -> list[dict[str, Any]]:
        target = self.resolve(relative_path)
        if not target.exists():
            raise WorkspaceError(f"Path not found: {relative_path}")
        if not target.is_dir():
            raise WorkspaceError(f"Path is not a directory: {relative_path}")

        items = []
        for child in sorted(target.iterdir(), key=lambda entry: (not entry.is_dir(), entry.name.lower())):
            rel_path = child.relative_to(self.root).as_posix()
            items.append(
                asdict(
                    WorkspaceEntry(
                        path=rel_path,
                        is_dir=child.is_dir(),
                        size=0 if child.is_dir() else child.stat().st_size,
                    )
                )
            )
        return items

    def read_file(self, relative_path: str) -> dict[str, Any]:
        target = self.resolve(relative_path)
        if not target.exists():
            raise WorkspaceError(f"File not found: {relative_path}")
        if not target.is_file():
            raise WorkspaceError(f"Path is not a file: {relative_path}")

        return {
            "path": target.relative_to(self.root).as_posix(),
            "content": target.read_text(encoding="utf-8"),
        }

    def write_file(self, relative_path: str, content: str, create_dirs: bool = True) -> dict[str, Any]:
        target = self.resolve(relative_path)
        if target.exists() and target.is_dir():
            raise WorkspaceError(f"Cannot write into directory: {relative_path}")
        if create_dirs:
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {
            "path": target.relative_to(self.root).as_posix(),
            "bytes_written": len(content.encode("utf-8")),
        }

    def delete_file(self, relative_path: str) -> dict[str, Any]:
        target = self.resolve(relative_path)
        if not target.exists():
            raise WorkspaceError(f"File not found: {relative_path}")
        if not target.is_file():
            raise WorkspaceError(f"Path is not a file: {relative_path}")
        target.unlink()
        return {
            "path": relative_path,
            "deleted": True,
        }

    def search_files(self, query: str, relative_path: str = ".") -> dict[str, Any]:
        if not query.strip():
            raise WorkspaceError("Query must not be empty.")
        target = self.resolve(relative_path)
        if not target.exists():
            raise WorkspaceError(f"Path not found: {relative_path}")
        if not target.is_dir():
            raise WorkspaceError(f"Path is not a directory: {relative_path}")

        matches: list[dict[str, Any]] = []
        for file_path in sorted(target.rglob("*")):
            if not file_path.is_file():
                continue
            try:
                lines = file_path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for index, line in enumerate(lines, start=1):
                if query.lower() in line.lower():
                    matches.append(
                        {
                            "path": file_path.relative_to(self.root).as_posix(),
                            "line": index,
                            "snippet": line.strip(),
                        }
                    )
        return {
            "query": query,
            "matches": matches,
        }
