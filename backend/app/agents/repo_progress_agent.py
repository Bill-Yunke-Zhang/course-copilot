"""Repo Progress Agent (T5).

Inspects the team's own project repo through the tool loop and gives each
tracked assignment one status (on track / behind / done / not started /
unknown), grounded in what the tools actually returned.

Two phases per repo:

1. Explore — `run_tool_loop` with the read-only repo tools. The model decides
   what to look at (commits, files, tests); the loop enforces the allowlist,
   path confinement and step budget, and records the trace.
2. Judge — one structured `generate()` call over the tool results gathered
   in phase 1 (wrapped as untrusted), returning a status per assignment.

Grounding is deterministic: a status may only cite files and commits the
tools returned in this run. Anything else the model names is dropped from
the evidence before the item reaches the verifier.

Assignments come from the optional `deadline_agent` upstream (title + due
date, with its citations copied in so the date is grounded too). Without
it, the whole repo is tracked as one "Project" item.
"""

import json
from datetime import date
from typing import Literal

from pydantic import BaseModel

from app.core.ingestion import repo_fetch
from app.core.manifest import AgentContext, AgentManifest, ResourceItemView
from app.core.tool_loop import ToolCallRecord, run_tool_loop
from app.core.tools import repo_tools
from app.core.untrusted import UNTRUSTED_NOTICE, wrap_untrusted

MANIFEST = AgentManifest(
    agent_id="repo_progress_agent",
    display_name="Repo Progress",
    description=(
        "Inspects the team's project repo with read-only tools and reports, "
        "per assignment, whether the work is on track or behind, citing the "
        "commits and files it looked at."
    ),
    required_resource_tags=["project-code"],
    optional_upstream_agents=["deadline_agent"],
    output_archetype="status_item",
    trigger="on_demand",
    cacheable=True,
    # Shown on its own Assignment Tracker page, not as a dashboard card.
    dashboard_widget=False,
    tools=["list_files", "read_file", "git_log"],
)

MAX_STEPS = 8
# Per tool result, in the judge prompt; the tools already cap their output.
MAX_OBSERVATION_CHARS = 4000

State = Literal["on_track", "behind", "done", "not_started", "unknown"]
STATE_LABELS = {
    "on_track": "On track",
    "behind": "Behind",
    "done": "Done",
    "not_started": "Not started",
    "unknown": "Unknown",
}


class _Status(BaseModel):
    assignment: int  # 1-based index into the numbered assignment list
    state: State
    detail: str
    files: list[str] = []
    commits: list[str] = []


class _Report(BaseModel):
    statuses: list[_Status]


_EXPLORE_PROMPT = """{notice}

You are checking a student team's project repository to see how far along
each assignment below is. Use the tools to look at the evidence yourself:
recent commits (git_log), which files exist (list_files), and the contents of
the files that matter for each assignment, such as source code, tests and
the README (read_file). You have at most {max_steps} tool calls, so choose
them well.

When you are done, answer in plain text with a short note per assignment,
naming the exact file paths and commit hashes your judgment rests on.

ASSIGNMENTS:
{assignments}
"""

_JUDGE_PROMPT = """{notice}

Today is {today}. Judge the progress of a student team's project repository
on each assignment below, using ONLY the tool results below (from read-only
tools run on the repo) and the investigator's notes.

For each assignment, return:
- assignment: its number from the list
- state: one of on_track, behind, done, not_started, unknown. Use "behind"
  when the due date is near or past and the work the assignment needs is
  missing; "unknown" when the tool results don't show enough.
- detail: one or two sentences saying what the evidence shows.
- files: exact paths of files from the tool results that support it.
- commits: short hashes of commits from the tool results that support it.
Never cite a file or commit that does not appear in the tool results.

ASSIGNMENTS:
{assignments}

TOOL RESULTS:
{observations}

INVESTIGATOR NOTES:
{notes}
"""


def run(ctx: AgentContext) -> list[dict]:
    items = ctx.resources.get("project-code", [])
    if not items:
        return []
    assignments = _assignments(ctx.upstream.get("deadline_agent", []))

    output: list[dict] = []
    for locator, repo_items in _by_repo(items).items():
        root = repo_fetch.local_clone_path(locator)
        if not root.is_dir():
            # Not cloned (yet): there is nothing the tools could inspect.
            continue
        listing = wrap_untrusted(_numbered(assignments), source="deadline_agent")
        loop = run_tool_loop(
            ctx,
            _EXPLORE_PROMPT.format(
                notice=UNTRUSTED_NOTICE, max_steps=MAX_STEPS, assignments=listing
            ),
            repo_tools(root),
            max_steps=MAX_STEPS,
        )
        observed = _Observed(loop.calls)
        report = ctx.llm.generate(
            _JUDGE_PROMPT.format(
                notice=UNTRUSTED_NOTICE,
                today=date.today().isoformat(),
                assignments=listing,
                observations=observed.prompt_block(),
                notes=wrap_untrusted(loop.text or "(none)", source="investigator"),
            ),
            response_schema=_Report,
        ).parsed
        output += _items_for_repo(report, assignments, locator, repo_items, observed)
    return output


def _assignments(deadlines: list[dict]) -> list[dict]:
    """Tracked assignments, from the Deadline agent's timeline if present."""
    tracked = [
        {
            "title": d.get("title", "").strip(),
            "due_date": d.get("date"),
            "source_refs": list(d.get("source_refs", [])),
        }
        for d in deadlines
        if d.get("title", "").strip()
    ]
    return tracked or [{"title": "Project", "due_date": None, "source_refs": []}]


def _numbered(assignments: list[dict]) -> str:
    lines = []
    for n, a in enumerate(assignments, start=1):
        due = f" (due {a['due_date']})" if a["due_date"] else ""
        lines.append(f"{n}. {a['title']}{due}")
    return "\n".join(lines)


def _by_repo(items: list[ResourceItemView]) -> dict[str, list[ResourceItemView]]:
    repos: dict[str, list[ResourceItemView]] = {}
    for item in items:
        if item.kind == "repo":
            repos.setdefault(item.locator, []).append(item)
    return repos


class _Observed:
    """What the tools actually returned in this run: the only things a status
    may cite."""

    def __init__(self, calls: list[ToolCallRecord]) -> None:
        self.ok_calls = [c for c in calls if c.status == "ok" and c.result]
        self.files: dict[str, int] = {}  # path -> step that returned it
        self.commits: dict[str, tuple[str, int]] = {}  # hash -> (log line, step)
        for call in self.ok_calls:
            result = call.result
            if call.name == "read_file" and result.get("path"):
                self.files[result["path"]] = call.step
            for path in result.get("files", []):
                self.files.setdefault(path, call.step)
            for line in result.get("commits", []):
                sha = line.split(" ", 1)[0]
                if sha:
                    self.commits.setdefault(sha, (line, call.step))

    def prompt_block(self) -> str:
        if not self.ok_calls:
            return "(no tool returned a result)"
        blocks = []
        for call in self.ok_calls:
            body = json.dumps(call.result, ensure_ascii=False, default=str)
            blocks.append(
                wrap_untrusted(
                    body[:MAX_OBSERVATION_CHARS],
                    source=f"step {call.step}: {call.name} {json.dumps(call.args)}",
                )
            )
        return "\n\n".join(blocks)

    def commit(self, cited: str) -> tuple[str, int] | None:
        """Match a cited hash (short or long, at least 7 chars) to a seen one."""
        cited = cited.strip().lower()
        if len(cited) < 7:
            return None
        for sha, seen in self.commits.items():
            if sha.lower().startswith(cited) or cited.startswith(sha.lower()):
                return seen
        return None


def _items_for_repo(
    report: _Report,
    assignments: list[dict],
    locator: str,
    repo_items: list[ResourceItemView],
    observed: _Observed,
) -> list[dict]:
    by_path = {i.path: i for i in repo_items}
    items: list[dict] = []
    done: set[int] = set()
    for status in report.statuses:
        index = status.assignment - 1
        if not 0 <= index < len(assignments) or index in done:
            continue  # unknown or repeated assignment number
        done.add(index)
        assignment = assignments[index]

        files = [f for f in status.files if f in observed.files]
        commits = [observed.commit(c) for c in status.commits]
        commits = [c for c in commits if c is not None]
        anchor = next((by_path[f] for f in files if f in by_path), repo_items[0])
        evidence = {
            "repo": locator,
            "head_sha": anchor.meta.get("git", {}).get("head_sha"),
            "files": files,
            "commits": [line for line, _ in commits],
            "tool_steps": sorted(
                {observed.files[f] for f in files} | {step for _, step in commits}
            ),
        }
        items.append(
            {
                "label": f"{assignment['title']}: {STATE_LABELS[status.state]}",
                "detail": status.detail.strip(),
                "state": status.state,
                "assignment": assignment["title"],
                "due_date": assignment["due_date"],
                "source_refs": [{"resource_item_id": anchor.id, "evidence": evidence}]
                + assignment["source_refs"],
            }
        )
    return items
