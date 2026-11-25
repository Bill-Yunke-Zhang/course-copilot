"""Tools an agent's tool loop can call, and the repo tools the starter ships.

A tool is a declaration the model sees (name, description, JSON Schema for
its arguments) plus a handler the harness runs. The model only ever ASKS for
a call; app/core/tool_loop.py decides whether to run it.

An agent names the tools it may use in its manifest (`tools=[...]`), and the
loop refuses anything else. That allowlist is the first containment layer.
The second is inside each tool: a tool must refuse arguments that would
reach outside what it is meant to touch, however the model was talked into
asking for them.

============================= STUDENT WORK (T4) ==============================
`resolve_inside` below is yours. Every repo tool that takes a path calls it.
===============================================================================
"""

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# Caps keep one tool result from flooding the model's context (and the bill).
MAX_LISTED_FILES = 200
MAX_READ_CHARS = 20_000
MAX_LOG_ENTRIES = 20

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}


class ToolRefused(Exception):
    """A tool call that must not run. The message is sent back to the model,
    so make it say what was refused and why — never echo a secret."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    # JSON Schema object for the arguments.
    parameters: dict
    # handler(args) -> JSON-serializable dict; raises ToolRefused to refuse.
    handler: Callable[[dict], dict]
    # True for tools that change something outside the run. None of the
    # starter's tools do; a tool that does needs a human approval step.
    changes_state: bool = False

    def declaration(self) -> dict:
        """What the model sees — never the handler."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


def resolve_inside(root: Path, relative: str) -> Path:
    """Resolve `relative` under `root`, refusing anything that escapes it.

    STUDENT WORK (T4). Return the absolute, resolved path of `relative`
    inside `root`. Raise ToolRefused, without touching the file system
    beyond resolving the path, when:

    - `relative` is absolute (`/etc/passwd`)
    - it climbs out of `root` (`../../etc/passwd`, `src/../../x`)
    - it resolves outside `root` through a symlink
    - it points into `.git/` (the clone's metadata is not course content)

    An empty string means `root` itself.

    Refusal messages state the reason only and never repeat the requested
    path: they go back to the model, and a path aimed outside the repo can
    carry text that must not reach it. The trace still records the args.
    """
    base = Path(root).resolve()
    if relative == "":
        return base
    if "\x00" in relative:
        raise ToolRefused("path contains a NUL byte")
    if Path(relative).is_absolute():
        raise ToolRefused("absolute paths are not allowed")
    # resolve() collapses `..` and follows symlinks, so one containment check
    # covers both climbing out and a link that points outside the root.
    target = (base / relative).resolve()
    if not target.is_relative_to(base):
        raise ToolRefused("path escapes the repository root")
    # Any `.git` component (case-insensitive: macOS file systems are) is clone
    # metadata, including a submodule's, or a symlink that resolves into it.
    if any(part.lower() == ".git" for part in target.relative_to(base).parts):
        raise ToolRefused("paths inside .git/ are not allowed")
    return target


def _list_files(root: Path, args: dict) -> dict:
    base = resolve_inside(root, str(args.get("path", "")))
    if not base.is_dir():
        raise ToolRefused("not a directory")
    files = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or any(p in SKIP_DIRS for p in path.parts):
            continue
        files.append(str(path.relative_to(root)))
        if len(files) >= MAX_LISTED_FILES:
            break
    return {"files": files, "truncated": len(files) >= MAX_LISTED_FILES}


def _read_file(root: Path, args: dict) -> dict:
    path = resolve_inside(root, str(args.get("path", "")))
    if not path.is_file():
        raise ToolRefused("not a file")
    text = path.read_text(errors="replace")
    return {
        "path": str(path.relative_to(root)),
        "content": text[:MAX_READ_CHARS],
        "truncated": len(text) > MAX_READ_CHARS,
    }


def _git_log(root: Path, args: dict) -> dict:
    try:
        count = max(1, min(int(args.get("n", 10)), MAX_LOG_ENTRIES))
    except (TypeError, ValueError):
        raise ToolRefused("n must be an integer") from None
    out = subprocess.run(
        ["git", "-C", str(root), "log", f"-{count}", "--format=%h %cI %an: %s"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise ToolRefused("this resource is not a git repository")
    return {"commits": out.stdout.strip().splitlines()}


REPO_TOOL_NAMES = ("list_files", "read_file", "git_log")
# Every tool name a manifest may list.
KNOWN_TOOLS = set(REPO_TOOL_NAMES)


def repo_tools(root: Path) -> list[ToolSpec]:
    """The three read-only tools over one local repo or notes directory."""
    root = Path(root)
    return [
        ToolSpec(
            name="list_files",
            description="List files under a folder of the repository, as "
            "paths relative to the repository root.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Folder relative to the repo root; "
                        "empty for the whole repo.",
                    }
                },
            },
            handler=lambda args: _list_files(root, args),
        ),
        ToolSpec(
            name="read_file",
            description="Read one text file from the repository.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to the repo root.",
                    }
                },
                "required": ["path"],
            },
            handler=lambda args: _read_file(root, args),
        ),
        ToolSpec(
            name="git_log",
            description="The repository's most recent commits: short hash, "
            "commit date, author and subject.",
            parameters={
                "type": "object",
                "properties": {
                    "n": {
                        "type": "integer",
                        "description": f"How many commits (1-{MAX_LOG_ENTRIES}).",
                    }
                },
            },
            handler=lambda args: _git_log(root, args),
        ),
    ]
