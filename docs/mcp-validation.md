# MCP server (T7) — manual validation against the Docker stack

Date: 2026-11-15 (US Central; the containers' clock was already
2026-11-16 UTC, which is the date the server uses for "today").
Branch: `feature/mcp-server`. Client: MCP Inspector CLI
`@modelcontextprotocol/inspector@2.12.0`. LLM: the mock service
(`mock_llm/`), no Vertex credentials.

**Result: every check passed. Nothing is blocked.** No production code
changed during validation.

## 1. Environment

| Item | Value |
|---|---|
| Host Node (global, unchanged) | v20.20.2 (below the 22.19 the Inspector needs) |
| Node used for the Inspector | v22.20.0, installed with `npm install --prefix <scratch>/node22 node@22.20.0` and put first on `PATH` for these commands only |
| Docker | 29.8.2 (Docker Desktop started with `open -a Docker`) |
| `.env` | created from `.env.template`, with `LLM_BASE_URL=http://mock-llm:9000` and the placeholder `GOOGLE_CLOUD_PROJECT=mock-project-not-used`; `GCLOUD_CONFIG_DIR` left empty. It is git-ignored (`.gitignore:1`) and not committed. |
| `mcp` in the backend container | 2.3.0 |

```bash
docker compose --profile mock up -d --build mysql redis mock-llm backend worker
```

At start there were no project volumes or containers, so no existing data
was touched. mysql and redis reported `healthy`, `GET /api/health`
returned `{"status":"ok"}`, mock-llm answered `GET /control/requests`, and
the worker was `Listening on agents, ingestion`. The frontend was not
started because it isn't needed here.

Every Inspector call below is:

```bash
npx -y @modelcontextprotocol/inspector@2.12.0 --cli \
  docker compose exec -T backend python -m app.mcp_server <method args>
```

## 2. Seeding a grounded example (normal APIs + scripted mock)

1. I uploaded a schedule file with `POST /api/resources/upload` (resource 1,
   item 1, which the mock classifier tagged `schedule`):
   ```
   - P1 was due on 2026-11-02 at 11:59 PM.
   - P3 is due on 2026-11-20 at 11:59 PM.
   - The final exam is on 2026-12-04 in room 1240.
   ```
2. I scripted the mock with `POST /control/enqueue`:
   - `_Deadlines` returns four items: P1 (2026-11-02), Final exam
     (2026-12-04), P3 (2026-11-20), and a made-up "Bonus quiz"
     (2026-12-10) whose excerpt is not in the source.
   - The judge says *supported* for the first three. For Bonus quiz it
     says *unsupported* twice (the first judgment and its one retry).
3. `POST /api/agents/deadline_agent/runs {"force":true}` produced a
   succeeded run with `items_total 4, items_dropped 1`. Sending the same
   request again without `force` produced a **cache hit** pointing at that
   run.
   - The last such pair is runs **16** (real) and **17** (cache hit, `reused_run_id=16`).
   - Retagging in step 4 started automatic deadline runs that got the
     mock's empty default response, so I re-seeded the same way. An
     earlier pair, runs 3 and 4, gave the identical results recorded below.
4. For the quiz, I uploaded a notes file (item 2: "The learning rate
   controls the step size." / "…stepping against the gradient.") and
   retagged it `personal-notes` with `PUT /api/resource-items/2/tags`.
   Then I scripted `_ParsedRequest`, `_ScopeSelection`, `_Questions` (two
   questions citing item 2) and two *supported* judgments.

## 3. Inspector results

| # | Check | Command (method args) | Observed | Result |
|---|---|---|---|---|
| 1 | tools/list | `--method tools/list` | Exactly `list_deadlines`, `get_agent_output`, `request_quiz`. The first two have `readOnlyHint: true`; `request_quiz` has `readOnlyHint: false, destructiveHint: false` | pass |
| 2 | Resource-template discovery | `--method resources/templates/list` | One template: `copilot://agents/{agent_id}/output`, `mimeType: application/json` | pass |
| 3 | No static resources | `--method resources/list` | `{"resources": []}` | pass |
| 4 | list_deadlines | `--method tools/call --tool-name list_deadlines` | `today: 2026-11-16`, `includes_today: true`. Deadlines are `2026-11-20 P3 due` and `2026-12-04 Final exam`, each with a citation `{resource_item_id: 1, excerpt: "<verbatim line>", source_title: "MCP validation schedule"}`. `excluded_past: 1` (P1). The dropped Bonus quiz is absent. `run: {run_id: 16, requested_run_id: 17, from_cache: true, items_total: 4, items_dropped: 1}`. `isError: false` | pass |
| 5 | get_agent_output | `--method tools/call --tool-name get_agent_output --tool-arg agent_id=deadline_agent` | The three verified items in position order (P1 due, Final exam, P3 due), each with `source_refs`. The dropped item is absent. It was resolved through cache hit 17 → 16 | pass |
| 6 | Resource agrees with tool | `--method resources/read --uri copilot://agents/deadline_agent/output` | The parsed JSON equals the tool's `structuredContent` (`True`). The quiz resource also equals the `get_agent_output quiz_agent` result (`True`) | pass |
| 7 | Unknown agent (tool) | `--method tools/call --tool-name get_agent_output --tool-arg agent_id=nope` | `isError: true`, text `Unknown agent_id 'nope'. Registered agents: concept_coverage_agent, deadline_agent, quiz_agent, repo_progress_agent, spec_checklist_agent.` (Inspector exit code 5 = tool returned isError) | pass |
| 8 | Unknown agent (resource) | `--method resources/read --uri copilot://agents/nope/output` | Protocol error with the same message (Inspector exit 1) | pass |
| 9 | Known agent with no output | `get_agent_output agent_id=quiz_agent` (before any quiz succeeded) | `{"has_output": false, "run": null, "items": []}` | pass |
| 10 | Reads have no side effects | 5 read calls (rows 5, 6, 8, 9 and list_deadlines), counted before and after | `agent_runs 4→4`, `output_items 4→4`, `llm_calls 8→8`, `quiz_attempts 0→0`, mock requests `8→8`, `rq:queue:agents` length `0→0` | pass |
| 11 | request_quiz, real queue path | `--tool-arg "text=Quiz me on   the schedule"` (before notes existed) | `{run_id: 5, status: "queued", from_cache: false, request: "Quiz me on the schedule"}` (whitespace collapsed by the quiz service). The worker ran `run_agent_job(5)`, which failed honestly with "There is no study material to quiz on yet…" | pass |
| 12 | request_quiz, success then cache | `--tool-arg "text=Quiz me on lecture 2 with 2 multiple choice questions"` | `{run_id: 12, status: "queued"}`. The worker ran it: `GET /api/quiz/runs/12` showed `succeeded`, 2 verified, 0 dropped. The same text with extra whitespace gave `{run_id: 13, status: "succeeded", from_cache: true, reused_run_id: 12}`. `get_agent_output quiz_agent` then returned both questions with citations to item 2 (`run_id 12, requested_run_id 13`). `quiz_attempts` stayed 0 | pass |
| 13 | Invalid quiz text | `--tool-arg "text=   "` | `isError: true`, `Type what you want to be quizzed on.` | pass |
| 14 | No secrets in responses | grep of every saved Inspector JSON for `change_me`, `glpat`, `MYSQL`, `GOOGLE_CLOUD`, `password` | No matches | pass |
| 15 | stdout is protocol only | Raw JSON-RPC piped through the same `docker compose exec -T backend python -m app.mcp_server`: initialize, initialized, tools/list, tools/call (`nope`), resources/read | 4 stdout lines, all valid JSON-RPC 2.0 (ids 1–4; id 3 has `isError: true`). The single diagnostic line went to stderr | pass |

Note on row 15: when stdin closes right after the last request, the
server exits before writing that request's reply (a first attempt got
only ids 1–2). Real clients such as the Inspector keep stdin open, and
holding it open for a few seconds returned every reply. This is stdio
shutdown behaviour, not a defect in the server.

## 4. What is still outstanding

Nothing for T7. The Inspector's web UI (run without `--cli`) was not
used; the CLI covers the same methods.

The stack was left running with the seeded data in its volumes. Stop it
without deleting data:

```bash
docker compose --profile mock stop
```
