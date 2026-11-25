# Course Copilot

A local, single-user study assistant for one course. You point it at your
course's resources: notes, the public schedule and syllabus pages, and your
GitLab project repos. A set of LLM agents, coordinated by a harness, turns
them into views that stay current: what's due, which topics have been
covered, on-demand quizzes, a checklist of spec requirements, and whether
your project repo is on track.

Everything runs on your own machine with Docker Compose. Every item an agent
produces cites its source. A verifier drops any item whose citation does not
check out, and the UI says how many items were dropped.

## Features

- **Resources.** Add URLs, files, folders or GitLab repos. Each item is
  classified onto a fixed 11-tag vocabulary, and the tags stay editable. The
  schedule and syllabus pages are crawled for linked readings and specs.
- **Dashboard.** This week's deadlines and concept coverage, with a cost panel
  that shows token spend and cache savings.
- **Quiz.** Ask in plain language, for example "Quiz me on lecture 5 with 3
  multiple-choice questions". It generates questions from your notes and
  slides, grades them, and links each wrong answer to its source.
- **Assignment Tracker.** An agent explores your project repo through a
  sandboxed tool loop (`list_files`, `read_file`, `git_log`) and reports
  progress on each assignment, with file and commit evidence.
- **Spec Checklist.** Extracts every requirement from the project
  instructions as a checklist with verbatim excerpts. You tick items off as
  you finish them.
- **Settings.** Stores your GitLab personal access token encrypted with
  AES-256-GCM. The API never returns it.
- **MCP server.** Exposes deadlines, agent outputs and quiz requests to any
  Model Context Protocol client over stdio.

## Architecture

```
React + TS (5173) ──► FastAPI (8000) ──► Redis / RQ queue ──► worker
        ▲                  │                                    │
        └──── polls ───────┴──────────── MySQL ◄────────────────┘
```

The core is the **harness** in `backend/app/core/`:

- **Registry.** Agents are plug-ins. Dropping a module with an `AgentManifest`
  into `backend/app/agents/` registers it.
- **Orchestrator.** Resolves resources by tag. Triggers runs when a resource
  changes or on demand. Runs upstream agents first in topological order and
  rejects dependency cycles. Propagates staleness and caches runs by an input
  fingerprint.
- **Tool loop.** Allows only the tools listed in the manifest. Confines paths
  to the repo, so `..`, symlink escapes and `.git` are refused, and enforces a
  step budget. Every call is recorded.
- **Untrusted-content marking.** Wraps all document and tool output in markers
  before it reaches the model.
- **Verifier.** First checks deterministically that each cited excerpt exists
  verbatim, then asks an LLM judge.
- **Cost accounting.** Logs tokens and cost for every LLM call, per run.

See [`docs/harness.md`](docs/harness.md) for the full contract and
[`docs/custom-agent.md`](docs/custom-agent.md) for the Spec Checklist design.

## Team

Built by **Bill Yunke Zhang** and **Ian Guo**. The base framework was
provided as a course starter: the orchestrator core, the Deadline and Concept
Coverage agents, ingestion and the cost panel.

| Area | Owner |
|---|---|
| Path confinement, untrusted-content marking, encrypted SecretStore, Settings page | Bill |
| Sandboxed tool loop, Repo Progress agent, Assignment Tracker page | Bill |
| Spec Checklist agent and page | Bill (implementation), Ian (initial design) |
| Upstream dependency resolution (topological order, cycles, staleness) | Ian |
| Quiz agent and page | Ian |
| MCP server | Ian |

## Getting started

Requirements: Docker Desktop, plus a Google Cloud project with Vertex AI
enabled for real LLM calls. Mock mode, described below, needs neither.

```bash
cp .env.template .env
# edit .env: set the MySQL passwords, GOOGLE_CLOUD_PROJECT and GCLOUD_CONFIG_DIR
gcloud auth application-default login
docker compose up --build
```

- Frontend: http://localhost:5173
- API docs: http://localhost:8000/docs

The LLM is Vertex AI, called through the `google-genai` SDK. Authentication
uses Application Default Credentials, and Docker Compose mounts your gcloud
config folder read-only, so no API key is involved. The default model is
`gemini-3.5-flash-lite`. You can route classification, generation and
verification to different models with the `LLM_MODEL_*` overrides in `.env`.

### Mock mode (no cloud account needed)

```bash
# in .env:  LLM_BASE_URL=http://mock-llm:9000
docker compose --profile mock up --build
```

`mock_llm/` is a local stand-in for the Vertex `generateContent` API. It
returns minimal valid JSON by default. You can script exact responses through
its `/control` API, which is how the end-to-end tests drive the app.

### MCP server

```bash
docker compose exec -T backend python -m app.mcp_server
```

Any MCP client can launch this command. It provides these tools:

- `list_deadlines` (read-only)
- `get_agent_output` (read-only)
- `request_quiz`

It also provides the resource `copilot://agents/{agent_id}/output`.

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt

pytest tests/ -q                       # in-memory SQLite and a fake LLM; no .env needed
ruff check backend/ tests/ mock_llm/
cd frontend && npm install && npx tsc --noEmit
```

## Repository layout

```
backend/
  app/core/          harness: orchestrator, verifier, LLM layer, registry,
                     tool loop, path confinement, untrusted marking, SecretStore
  app/core/ingestion classifier, schedule/syllabus crawler, repo fetch
  app/agents/        agent plug-ins (deadline, concept coverage, quiz,
                     repo progress, spec checklist)
  app/routers/       HTTP API
  app/mcp_server.py  MCP server (stdio)
frontend/
  src/pages/         Dashboard, Resources, Quiz, Assignment Tracker,
                     Checklist, Settings
mock_llm/            controllable Vertex stand-in (compose profile "mock")
tests/               unit and integration tests
docs/                harness contract, custom agent design, validation notes
```
