# Project Schedule

## Team

| Name | NetID |
|---|---|
| Ian Guo | yguo388 |
| Bill Zhang | yyzhang |

## Timeline

| Milestone | Tasks | Owner | Target date |
|---|---|---|---|
| `docs/schedule.md` merged to `main` through an approved MR | T1 | all | 2026-11-04 |
| `docs/custom-agent.md` (Spec Checklist) committed and signed off by a TA | T8 | all | 2026-11-04 |
| `wrap_untrusted` (exactly one marker pair, early-close attempts neutralized) and `resolve_inside` (absolute, `..`, symlink-out and `.git/` refused) merged with unit tests | T4 | yyzhang | 2026-11-04 |
| Basic upstream resolution merged: missing/stale upstreams run first, verified payloads and run ids returned, uninstalled upstream resolves to no output | T3 | yguo388 | 2026-11-05 |
| Due 1: Dependency cycles rejected with a clear error; topological order across deep chains | T3 | yguo388 | 2026-11-07 |
| Due 1: `on_upstream_update` propagation; resource edit/re-tag/removal re-runs exactly the dependent agents through a chain | T3 | yguo388 | 2026-11-07 |
| Due 1: Dependency tests: ordering, deep chain, cycle rejection, multi-upstream assembly, staleness through a chain, cache hit when nothing changed | T3 | yguo388 | 2026-11-08 |
| Due 1: Quiz agent request parsing (`{scope, count, format}` from free text) and scope resolution from upstream output and current resource metadata | T6 | yguo388 | 2026-11-08 |
| Due 1: `SecretStore` encrypts the PAT at rest in the `credentials` table (key outside the repo and DB), round-trip tests, no plaintext in DB/files/logs/clones | T4 | yyzhang | 2026-11-06 |
| Due 1: Private repo with no stored token fails with a clear, actionable error; PAT entry/replace UI and API | T4 | yyzhang | 2026-11-07 |
| Due 1: `run_tool_loop` (allowlist, refusals/errors returned to the model, step budget, `ToolCall` row per call, clean ending) with hijacked-model tests via `FakeLlm(tool_turns=[...])` | T4, T5 | yyzhang | 2026-11-08 |
| Due 2: Quiz generation through `wrap_untrusted`; failed questions regenerated or honestly disclosed, never padded | T6 | yguo388 | 2026-11-10 |
| Due 2: Quiz page: free-text box only, take quiz, score, review wrong answers with link to cited source excerpt | T6 | yguo388 | 2026-11-12 |
| Due 2: Quiz attempts persisted to `quiz_attempts` / `quiz_attempt_questions` with per-question correctness and topic, with tests | T6 | yguo388 | 2026-11-12 |
| Due 2: MCP server over stdio: `list_deadlines`, `get_agent_output` (unknown id is a tool error), `request_quiz`, `copilot://agents/{agent_id}/output`; read-only except `request_quiz`, no credentials or raw repo files | T7 | yguo388 | 2026-11-14 |
| Due 2: MCP in-process tests and manual MCP Inspector check of every tool and the resource | T7 | yguo388 | 2026-11-15 |
| Due 2: Repo Progress agent: `project-code` + `deadline_agent` upstream, inspects repo only through the tool loop, per-assignment status with `evidence` source_refs | T5 | yyzhang | 2026-11-10 |
| Due 2: Assignment Tracker page: card per assignment, expandable tool-call trace, working check-now | T5 | yyzhang | 2026-11-12 |
| Due 2: Spec Checklist agent in `backend/app/agents/` (standard manifest, fixed tags, `source_ref` per item, generic verifier) with tests | T8 | yyzhang | 2026-11-13 |
| Due 2: Spec Checklist page: grouped by task/due date, tick-off, expandable source excerpt | T8 | yyzhang | 2026-11-15 |
| Due 2: End-to-end check in mock mode: drop-in test agent with upstreams runs unchanged, full CI green, no secrets in repo | T3, T4, T5, T6, T7, T8 | all | 2026-11-15 |
| Individual document `docs/yyzhang.md` (all five sections) | T9 | yyzhang | 2026-11-15 |
| Everything merged to `main` via approved MRs; TA code review scheduled | T10 | all | 2026-11-17 |

## Check-ins

| Date | What we will review |
|---|---|
| 2026-11-05 | Shared interfaces merged (`wrap_untrusted`, `resolve_inside`, basic upstream resolution) so both members can build agents on `main` |
| 2026-11-08 | Due 1: harness (T3), security (T4) and tool loop done; move dates that have slipped |
| 2026-11-12 | Progress on agents and pages; open MRs reviewed within 48 hours |
| 2026-11-15 | Due 2: all features merged; what is left before the 2026-11-17 deadline |
