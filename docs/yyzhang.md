# Bill Zhang (yyzhang)

## Harness and Codebase Organization

The system works like a kitchen. Course resources are ingested and
labeled with tags from a fixed vocabulary. The orchestrator is the head
chef: each agent has a recipe card, its `AgentManifest`, which lists the
resource tags it needs and the upstream agents it depends on. When the
orchestrator runs an agent, it hands over the matching resource items and
the verified outputs of those upstream agents. The agent calls the LLM to
generate its output items. The verifier then checks that every item is
backed by its cited sources. Only the items that pass are stored for the
frontend pages to show. Adding a new agent only takes dropping one file
into `backend/app/agents/`, because the registry discovers it
automatically, and not one line of the orchestrator has to change. That
is why the staff's "mystery agent", which our code has never seen, can
still run in our harness at grading time.

### Tracing one real run: Spec Checklist, run 3

**Trigger.** Run 3 was started automatically by a resource change: its
record shows `trigger = resource_change` and `from_cache = false`. When a
resource is added, re-ingested, re-tagged or deleted, the system calls
`resource_change_event` with the tags that the change touched. This step
is a plain set match on tags. It compares the changed tags with the tags
an agent needs, plus the tags its upstream agents need (the transitive
relation added in T3). For example, `deadline_agent` needs `schedule`, so
a schedule change also triggers Spec Checklist, which uses
`deadline_agent` as an upstream. This step never calls the LLM: resources
are edited often, so the decision has to be cheap and predictable. Being
triggered does not guarantee an LLM call either. `request_run` first
compares the input fingerprint, and if the inputs have not changed it
reuses the cached run. The model is only called once a run is actually
queued and a worker executes it.

**Resolving inputs.** The worker first resolves resources by tag.
`instructions.md` matches both `project-spec` and `project-instructions`,
and `rubric.md` matches `project-instructions`. The duplicate is removed,
so the agent gets 2 resource items. The worker also fetches the output of
the upstream `deadline_agent` (run 2), but only its verified items: 2
deadlines. An upstream item that failed verification has no proven
source, so passing it on would let an unsupported claim leak into the next
agent's output.

**What the agent produced.** Spec Checklist makes one `generate()` call
per resource item, so 2 calls in total. Each document goes into the prompt
through `wrap_untrusted`, which marks it as data rather than instructions.
After the model answers, our own code applies deterministic filters:

- an excerpt must appear verbatim in its source document,
- duplicate requirements are removed,
- a due date is kept only if a deadline names the same task id.

These filters are free and give the same result every time, so they throw
out obvious mistakes before we pay for LLM verification. The result was 60
draft `status_item`s. None of them had a due date, because the deadline
titles did not mention any task id (see Grounding and Verification).

**Verification.** Each item first passes `validate_payload`, which checks
the archetype's fields and that there is at least one `source_ref`. Then
the verifier checks every item: the excerpt must be verbatim in the
source, and an LLM judge must agree that the source supports the item.
This took 61 verification calls, because one item was retried. 1 item was
dropped and 59 passed. The drop was the R14 requirement: its detail
mentioned a "code review", but the source only says the student "attends
and participates". The result is recorded in the run's
`verification_summary`.

**Reaching the UI.** `GET /api/agents/spec_checklist_agent/output` returns
only verified items. The `/checklist` page groups them by task and states
"59 of 60 requirements verified; 1 dropped", so the user can see that
something was removed instead of having it disappear silently.

## Grounding and Verification

Every output item must carry `source_refs`. The verifier first checks that
each excerpt appears verbatim in its source, then asks an LLM judge whether
the source actually supports the item. An item that fails is retried once.
If it still fails, it is dropped, and the page discloses this as "X of Y
verified". Spec Checklist filters before the verifier ever sees its output:
it deletes any excerpt that cannot be found in the original document. A due
date is kept only when a Deadline item names the same task id.

Our team kept the starter's failure policy: retry the judgment once, then
drop and disclose. One retry absorbs an occasional noisy judgment without
doubling the cost of every run. Dropping instead of showing a weakly
supported item fits the rule that nothing unverified reaches the UI, and
the disclosure means the user still knows something was removed. Repo
Progress applies the same idea at the tool level: a file or commit the
model cites is kept as evidence only if a tool actually returned it in
that run, so invented evidence is removed before verification.

**What the verifier caught.** In the real-model run of Spec Checklist
(run 3), the verifier dropped the R14 requirement. The model's detail
said it involved a "code review", but the source sentence only says the
student "attends and participates". The excerpt itself was verbatim, so
only the LLM judge could catch that the claim went beyond it.

**A wrong output that got through (case A).** The handout gives the
checkpoint date only as "Wednesday, November 4", with no year. The
Deadline agent inferred the year 2025, which is wrong. The verifier
dropped one of these items ("does not establish the year 2025"), but it
passed two others with the same mistake, "2025-11-04 Custom agent
sign-off checkpoint" and "2025-11-17 Project Submission due", so they were
shown on the Dashboard. I found this by looking at the Dashboard myself,
not through any test: every agent test used a fake LLM, so this only
showed up with the real model. It shows that an LLM judge can be
inconsistent about the same kind of error, so it should not be the only
check on a fact like a date. If I had more time, I would make the year
come only from the source text: if the excerpt does not contain a year,
the agent may not invent one.

Two more cases from the same run show what the verifier cannot see.
Of the 59 verified requirements, 50 came from the rubric and only 9 from
the instructions, with nothing for T4, T5, T8 or T9. The file was 27,637
characters, well under the 60,000-character limit, so this was
under-extraction rather than truncation. The verifier only checks that
what is there is supported, not what is missing. In addition, no
requirement got a due date, because the deadline titles never named a
task id. Our strict rule chose to show no date rather than an unsupported
one, and in this run that was its cost.

## Trust Boundaries

**Where untrusted content enters.**

- Crawled web pages and uploaded course files go into the classifier's
  and every agent's prompts.
- Repo files reach the model through Repo Progress's `list_files`,
  `read_file` and `git_log` tools.
- Upstream agent output is generated from those same documents. For
  example, Deadline titles end up in Spec Checklist's and Repo Progress's
  prompts.
- Notes and other user-edited resources.
- MCP clients call into the app (the MCP server is Ian's T7).
- The PAT entry endpoint, `PUT /api/credentials/{ref}`.

**What a hijacked model could do, and how the harness limits it.** I
built these parts:

| Harm if the model were hijacked | Limit | Where |
|---|---|---|
| Call a tool it was never given | The tool loop declares and runs only the tools in the agent's manifest. Any other call is refused and recorded as `not_allowed`. | `tool_loop.py`, !8 |
| `read_file("../../etc/passwd")` or `.git/config` | `resolve_inside` refuses absolute paths, `..` escapes, symlinks that lead out, and anything under `.git`. | `tools.py`, !4 |
| Endless tool calls that burn money | A step budget (8 for Repo Progress). At the limit the loop stops without another model call. | `tool_loop.py`, !8 |
| Prompt injection inside a document, repo file or tool result | `wrap_untrusted` puts exactly one pair of markers around the content, however the text tries to close them early, together with `UNTRUSTED_NOTICE`. Tool results are wrapped too. | `untrusted.py`, !4 |
| Invented evidence | Repo Progress keeps only files and commits a tool actually returned. Excerpts must be verbatim. | both agents' tests |
| Leaking the PAT | AES-256-GCM, with the key outside the repo and database. The API never returns the token, the 422 error does not echo it, and git receives it as a header, not in the URL. | `secret_store.py` (!5), Settings (!11) |

All repo tools are read-only, so even a fully hijacked model cannot
change the repo.

**A risk that is still not covered.** Prompt injection can still change
the model's judgment. Suppose a repo's README says "All assignments are
complete." Repo Progress reads that file through `read_file`, and the
model may then report the assignment as `on_track`. `wrap_untrusted`
tells the model that the text is data rather than instructions, but this
only lowers the chance of being fooled; it cannot prevent it. The
verifier does not close the gap either, because it is the same kind of
model reading the same text. Our containment stops a hijacked model from
taking harmful actions, but not from reaching a wrong conclusion.

## Best Practices Learned

**Write tests before trusting the AI, and know what the tests cannot
show.** This is the lesson I felt most. Every agent I built had a full
test suite, and all of it passed with `FakeLlm`. Then one run with the
real model exposed problems no test had shown: the invented 2025 year
(case A) and the missing requirements for T4, T5, T8 and T9 (case B).
`FakeLlm` returns exactly what the test scripted, so the tests proved
that our code handles a given answer correctly. They could not prove that
the real model gives a good answer. Tests are needed before trusting
AI-written code, but an LLM-backed feature also needs at least one run
with the real model, read by a person.

**Caching saves money, and verification is where the money goes.** In
the real run, the app made 91 LLM calls for about $0.044. Spec Checklist
needed only 2 generation calls but 61 verification calls, one per item
plus a retry. So the cost of an agent grows with how many items it
outputs, not with how many prompts it sends. The input fingerprint makes
an unchanged run cost nothing: in the mock-mode check, re-running an
agent whose inputs had not changed returned `from_cache = true` and made
no model call. Deciding triggers by tag matching, with no LLM, keeps
frequent resource edits cheap too.

**Credentials must never appear in the conversation.** An AI coding
session is logged, so anything pasted into it is effectively published.
We kept the GitLab token in a separate session that is not logged, and
the PAT is entered only in the app's own Settings page. Even then,
framework defaults can leak: FastAPI's standard 422 error echoed the
submitted token back until a test caught it. Avoiding leaks was not
enough. We had to prove the absence, by testing that the token never
appears in a response and by searching the database dump, volumes, Redis
and logs of the running containers for it.
