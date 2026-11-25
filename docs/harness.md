# The Harness: Contracts and Extension Points

This document is the reference for how agents plug into Course Copilot's
orchestration engine, and for the exact places the starter expects you to
extend it. Read it before writing any agent.

## 1. The agent manifest contract

An agent is one Python module in `backend/app/agents/` defining two things:

```python
from app.core.manifest import AgentContext, AgentManifest

MANIFEST = AgentManifest(
    agent_id="syllabus_policy_qa",        # unique, stable
    display_name="Syllabus Policy Q&A",
    description="Answers policy questions from the syllabus, with citations.",
    required_resource_tags=["syllabus"],  # fixed vocabulary only (app/config.py)
    optional_upstream_agents=[],          # agent_ids whose output this agent consumes
    output_archetype="text_block",        # one of the registered archetypes
    trigger="on_demand",                  # or on_resource_change / on_upstream_update
    cacheable=True,
    dashboard_widget=False,
)

def run(ctx: AgentContext) -> list[dict]:
    ...
```

Dropping a conforming module into `agents/` is the **only** registration
step. If you find yourself editing `app/core/` to make a new agent work,
you are breaking the property the project is graded on: at grading time a
staff-authored mystery agent is copied into `agents/` and must run with
zero changes to core harness code.

`run(ctx)` returns a list of plain dicts, each conforming to the manifest's
`output_archetype` (see `app/core/archetypes.py`) and each carrying at
least one `source_ref` (`{resource_item_id, excerpt}` — or structured
`evidence` for `project-code`-derived claims). The harness validates,
verifies, and stores them; agents never touch the database or the UI.

What `ctx` provides:

- `ctx.resources` — `{tag: [ResourceItemView, …]}` for each required tag,
  resolved against **current** tag state (user edits included). Missing
  tags resolve to `[]`; degrade gracefully, never crash.
- `ctx.upstream` — `{agent_id: [verified payload dicts]}` for available
  upstream agents (empty until you implement upstream resolution — see §4).
- `ctx.request` — the on-demand request payload (e.g. the quiz free-text).
- `ctx.llm.generate(prompt, response_schema=…)` — the only LLM access an
  agent gets; every call is token/cost-logged against the run automatically.
- `ctx.allowed_tools` — the manifest's optional `tools` list; the only tools
  the tool loop will run for this agent (§5).

## 2. Output archetypes

Five fixed shapes, rendered generically by the frontend and verified
generically by the grounding verifier:

| archetype | payload fields (plus `source_refs`) |
|---|---|
| `timeline_item` | `date`, `title`, `description` |
| `status_item` | `label`, `detail` (optional extra `state` badge) |
| `qa_item` | `question`, `choices[]`, `correct_index` |
| `card_item` | `front`, `back` |
| `text_block` | `heading`, `body` |

A team may add **one new archetype** for its student-designed agent:
register the schema with `register_archetype()` in
`backend/app/core/archetypes.py` and the renderer with
`registerArchetype()` in `frontend/src/archetypes/registry.tsx`. The five
shipped archetypes must keep rendering and verifying exactly as before —
mystery agents use them.

## 3. The run lifecycle (what the starter already does)

`request_run → [cache check] → queue → execute_run → validate → verify → store`

- **Triggers.** `on_resource_change` fires from resource add/edit/re-tag
  events via deterministic tag matching (no LLM — it must stay cheap).
  `on_demand` fires from the API (`POST /api/agents/{id}/runs`).
  `on_upstream_update` is student work (§4).
- **Caching (§13's required cost optimization).** For a `cacheable` agent,
  `request_run` fingerprints the currently-resolved inputs (item ids +
  content hashes + request payload); a match with the last verified run is
  recorded as a `from_cache` AgentRun and costs zero LLM calls. The
  Dashboard cost panel reports "N of M requested runs served from cache".
- **Verification.** Every output item passes the generic grounding check in
  `app/core/verifier.py`: an LLM-as-judge decides whether the cited
  evidence supports the item. The DEFAULT failure policy is *retry the
  judgment once, then drop the item and disclose the drop* in
  `verification_summary` (shown in the UI). This policy is deliberately
  replaceable — document whatever your team ships.

## 4. STUDENT WORK: the marked extension points

Search the backend for `STUDENT WORK` — every stub is marked. In summary:

1. **Upstream dependency resolution** —
   `orchestrator.resolve_upstream_outputs`. Topological execution order,
   run-missing-or-stale-upstreams-first, cycle detection and rejection,
   and returning upstream payloads + run ids for the fingerprint.
2. **Upstream update propagation** —
   `orchestrator.propagate_upstream_update` for `on_upstream_update`
   agents.
3. **SecretStore** — `app/core/secret_store.py`: encrypted-at-rest PAT
   storage. The private-repo fetch path is already wired to it; public
   repos clone anonymously and never touch it. The raw token must never
   exist in plaintext in the DB, config, or logs.
4. **Path confinement and untrusted content** — `tools.resolve_inside`
   and `untrusted.wrap_untrusted` (§5, §6).
5. **The tool loop** — `tool_loop.run_tool_loop` (§5).
6. **The MCP server** — `app/mcp_server.py` (§7).
7. **Your agents** — Quiz (on_demand, parses free text itself, persists
   attempts to the shipped `quiz_attempts` tables), Repo Progress (uses
   the tool loop), and your own student-designed agent + UI.

Things that are grading infrastructure and must NOT change: the 11-tag
vocabulary, the manifest contract's shape (including the `tools` field),
the five shipped archetypes' behavior, the plug-in discovery mechanism,
the signatures of `run_tool_loop`, `resolve_inside`, `repo_tools` and
`wrap_untrusted`, the `ToolCall` table and its endpoint, and the LLM
environment contract (`LLM_BASE_URL` / `LLM_MODEL` honored by all LLM traffic —
grading runs your containers against the mock service in `mock_llm/` with
scripted responses).

## 5. Tools and the tool loop

Most agents here are one structured `generate()` call inside a fixed graph:
a *workflow*. An agent that lists `tools` in its manifest gets a
model-driven loop instead: the model decides which tool to call next, and
the harness decides whether to run it.

```python
MANIFEST = AgentManifest(..., tools=["list_files", "read_file", "git_log"])

def run(ctx: AgentContext) -> list[dict]:
    root = repo_fetch.local_clone_path(item.locator)
    result = run_tool_loop(ctx, prompt, repo_tools(root), max_steps=6)
    ...
```

- `app/core/tools.py` — `ToolSpec` (what the model sees, plus a handler
  it never sees), the three read-only repo tools, and `resolve_inside`,
  which every path-taking tool calls (student work).
- `app/core/llm.py` — `generate_with_tools(messages, tools)` makes one
  model turn and returns its `function_calls` (empty when the model
  answers). It is cost-logged like every other call. The message format
  is documented there; no SDK types reach harness code.
- `app/core/tool_loop.py` — `run_tool_loop` (student work). Its docstring
  is the contract: allowlist from `ctx.allowed_tools`, refusals and errors
  returned to the model, a step budget, and a `ToolCall` row per requested
  call.
- `GET /api/runs/{run_id}/tool-calls` — the trace, refused calls included.

Design for a compromised model. A crawled page or a repo file can talk the
model into asking for anything; the loop is safe only if the allowlist,
the argument checks and the budget hold no matter what it asks for.
Grading tests exactly that, with a scripted model.

## 6. Untrusted content

Resource text goes into a prompt only through
`wrap_untrusted(content, source)` (`app/core/untrusted.py`, student work),
with `UNTRUSTED_NOTICE` in the prompt. The markers tell the model which
text is data. They lower the odds an injection works; they don't replace
§5's containment.

## 7. The MCP server

`app/mcp_server.py` (student work) exposes Course Copilot over the Model
Context Protocol, stdio transport, from inside the backend container:

```bash
docker compose exec -T backend python -m app.mcp_server
```

Any MCP client can launch that command; instructions.md (T7) shows how
to check it with the MCP Inspector. The server reads through the same
models and helpers as the API. It is read-only except `request_quiz`, and
never returns credentials. stdout belongs to the protocol: log to stderr.

