"""on_upstream_update propagation and resource-change reruns through chains.

Test agents live in a throwaway package under tmp_path, never in
backend/app/agents/. The RQ queue is replaced by `jobs`, a FIFO recorder
that tests drain explicitly — so each test controls exactly when queued
work executes, and a scheduling loop would show up as a runaway queue.
"""

import sys
import textwrap
import uuid

import pytest
from sqlalchemy import func, select

import app.core.queue as queue_module
import app.core.registry as registry_module
from app.core import orchestrator
from app.core.ingestion.extract import content_hash
from app.core.registry import AgentRegistry
from app.models import AgentRun, LlmCall, ResourceItemTag
from tests.conftest import FakeLlm, make_resource

JUDGE_OK = {"_JudgeResponse": {"supported": True, "reason": "excerpt matches"}}
READING_TEXT = "Chapter 3 covers consistent hashing and virtual nodes."
NEW_READING = "Chapter 3 (revised) covers consistent hashing with bounded loads."
SLIDES_TEXT = "Lecture 9 slides: virtual nodes smooth out the key distribution."

# One grounded item plus one citing a missing resource item, which the
# verifier drops deterministically — so "verified output" is observable.
AGENT = """
from app.core.manifest import AgentManifest
from {pkg} import _trace

SEEN = []

MANIFEST = AgentManifest(
    agent_id="{agent_id}",
    display_name="{agent_id}",
    description="propagation test agent",
    required_resource_tags={tags!r},
    optional_upstream_agents={upstreams!r},
    output_archetype="text_block",
    trigger="{trigger}",
    cacheable=True,
    dashboard_widget=False,
)

def run(ctx):
    _trace.EVENTS.append("{agent_id}")
    SEEN.append(ctx.upstream)
    if {fail!r}:
        raise RuntimeError("{agent_id} exploded")
    items = [i for tag in {tags!r} for i in ctx.resources[tag]]
    if not items:
        return []
    item = items[0]
    return [
        {{
            "heading": "{agent_id} kept",
            "body": item.content,
            "source_refs": [{{"resource_item_id": item.id,
                              "excerpt": item.content[:20]}}],
        }},
        {{
            "heading": "{agent_id} dropped",
            "body": "made up",
            "source_refs": [{{"resource_item_id": 999999, "excerpt": "nope"}}],
        }},
    ]
"""


def agent(trigger="on_demand", upstreams=(), tags=("reading",), fail=False):
    return {
        "trigger": trigger,
        "upstreams": list(upstreams),
        "tags": list(tags),
        "fail": fail,
    }


def agent_installer(tmp_path, monkeypatch):
    """Temp agent package; returns (install(specs) -> (registry, events), cleanup).

    Shared with tests/integration/test_propagation_api.py.
    """
    name = f"propagation_agents_{uuid.uuid4().hex[:8]}"
    pkg_dir = tmp_path / name
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    (pkg_dir / "_trace.py").write_text("EVENTS = []\n")
    monkeypatch.syspath_prepend(str(tmp_path))

    def install(specs: dict[str, dict]):
        for agent_id, spec in specs.items():
            source = AGENT.format(pkg=name, agent_id=agent_id, **spec)
            (pkg_dir / f"{agent_id}.py").write_text(textwrap.dedent(source))
        reg = AgentRegistry(package=name)
        monkeypatch.setattr(registry_module, "_registry", reg)
        return reg, sys.modules[f"{name}._trace"].EVENTS

    def cleanup():
        for mod in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
            del sys.modules[mod]

    return install, cleanup


@pytest.fixture
def install(tmp_path, monkeypatch, registry):
    """install({agent_id: agent(...)}) -> (registry, trace events list)."""
    installer, cleanup = agent_installer(tmp_path, monkeypatch)
    yield installer
    cleanup()


class JobQueue:
    """FIFO stand-in for the RQ "agents" queue."""

    def __init__(self):
        self.pending: list[int] = []
        self.history: list[int] = []
        self.fail_with: Exception | None = None

    def enqueue(self, queue_name, func, *args, **kwargs):
        if self.fail_with is not None:
            raise self.fail_with
        assert queue_name == "agents" and func.__name__ == "run_agent_job"
        self.pending.append(args[0])
        self.history.append(args[0])

    def drain(self, db, llm, limit=50) -> list[AgentRun]:
        """Run queued jobs in order (as one worker would) until empty."""
        executed = []
        while self.pending:
            assert len(executed) < limit, "queue storm: propagation never settled"
            executed.append(orchestrator.execute_run(db, llm, self.pending.pop(0)))
        return executed


def patch_queue(monkeypatch) -> JobQueue:
    queue = JobQueue()
    monkeypatch.setattr(queue_module, "enqueue", queue.enqueue)
    return queue


@pytest.fixture
def jobs(monkeypatch):
    return patch_queue(monkeypatch)


@pytest.fixture
def sources(db):
    reading = make_resource(
        db,
        locator="https://example.edu/ch3.html",
        items=[{"title": "Ch3", "content": READING_TEXT, "tags": ["reading"]}],
    )
    slides = make_resource(
        db,
        locator="https://example.edu/lec9.pdf",
        items=[{"title": "Lec9", "content": SLIDES_TEXT, "tags": ["slides"]}],
    )
    return reading, slides


def _run_now(db, llm, agent_id, trigger="on_demand"):
    run = AgentRun(agent_id=agent_id, status="queued", trigger=trigger)
    db.add(run)
    db.commit()
    return orchestrator.execute_run(db, llm, run.id)


def _runs(db, agent_id):
    return db.scalars(
        select(AgentRun).where(AgentRun.agent_id == agent_id).order_by(AgentRun.id)
    ).all()


def _materialized(db, agent_id):
    return [
        r for r in _runs(db, agent_id) if r.status == "succeeded" and not r.from_cache
    ]


def _llm_calls(db):
    return db.scalar(select(func.count()).select_from(LlmCall))


def _headings(payloads):
    return [p["heading"] for p in payloads]


# ── Scheduling after a successful upstream run ──────────────────────────────


def test_successful_upstream_schedules_on_upstream_update_dependent(
    db, sources, install, jobs
):
    reg, _ = install(
        {
            "a": agent(),
            "b": agent("on_upstream_update", ["a"]),
            "od": agent("on_demand", ["a"]),  # depends on a, wrong trigger
            "other": agent("on_upstream_update", ["z"]),  # unrelated upstream
        }
    )
    llm = FakeLlm(JUDGE_OK)

    a_run = _run_now(db, llm, "a")

    assert a_run.status == "succeeded", a_run.error
    queued = [db.get(AgentRun, run_id) for run_id in jobs.pending]
    assert [(r.agent_id, r.trigger, r.status) for r in queued] == [
        ("b", "upstream_update", "queued")
    ]
    assert _runs(db, "od") == [] and _runs(db, "other") == []

    [b_run] = jobs.drain(db, llm)
    assert b_run.status == "succeeded", b_run.error
    # b consumed a's new run, verified items only
    assert _headings(reg.get("b").module.SEEN[-1]["a"]) == ["a kept"]
    assert [r.id for r in _runs(db, "a")] == [a_run.id]  # a not re-run
    assert not orchestrator.is_stale(db, "b")


def test_chain_propagates_hop_by_hop_in_dependency_order(db, sources, install, jobs):
    reg, events = install(
        {
            "a": agent(),
            "b": agent("on_upstream_update", ["a"]),
            "c": agent("on_upstream_update", ["b"]),
        }
    )
    llm = FakeLlm(JUDGE_OK)

    _run_now(db, llm, "a")
    executed = jobs.drain(db, llm)

    assert [(r.agent_id, r.status) for r in executed] == [
        ("b", "succeeded"),
        ("c", "succeeded"),
    ]
    assert events == ["a", "b", "c"]
    assert [len(_runs(db, x)) for x in "abc"] == [1, 1, 1]
    assert _headings(reg.get("c").module.SEEN[-1]["b"]) == ["b kept"]
    b_run, c_run = executed
    assert c_run.input_fingerprint == orchestrator.compute_fingerprint(
        reg.get("c").manifest,
        orchestrator.resolve_resources(db, reg.get("c").manifest),
        {"b": b_run.id},
        None,
    )
    assert not any(orchestrator.is_stale(db, x) for x in "abc")

    # a second upstream update flows through the whole chain again, once
    _run_now(db, llm, "a")
    jobs.drain(db, llm)
    assert [len(_materialized(db, x)) for x in "abc"] == [2, 2, 2]


def test_diamond_join_runs_once_with_both_current_outputs(db, sources, install, jobs):
    reg, events = install(
        {
            "a": agent(),
            "b": agent("on_upstream_update", ["a"]),
            "c": agent("on_upstream_update", ["a"], tags=["slides"]),
            "d": agent("on_upstream_update", ["b", "c"]),
        }
    )
    llm = FakeLlm(JUDGE_OK)

    _run_now(db, llm, "a")
    executed = jobs.drain(db, llm)

    assert [r.agent_id for r in executed] == ["b", "c", "d"]
    assert all(r.status == "succeeded" for r in executed)
    assert events == ["a", "b", "c", "d"]  # d ran once, after both inputs
    seen = reg.get("d").module.SEEN[-1]
    assert _headings(seen["b"]) == ["b kept"]
    assert _headings(seen["c"]) == ["c kept"]
    b_run, c_run, d_run = executed
    assert d_run.input_fingerprint == orchestrator.compute_fingerprint(
        reg.get("d").manifest,
        orchestrator.resolve_resources(db, reg.get("d").manifest),
        {"b": b_run.id, "c": c_run.id},
        None,
    )


def test_synchronous_resolution_does_not_also_queue_the_same_dependent(
    db, sources, install, jobs
):
    # x needs a and b; b (on_upstream_update) also depends on a. Running x
    # runs a then b synchronously — a's update must not queue b again.
    install(
        {
            "a": agent(),
            "b": agent("on_upstream_update", ["a"]),
            "x": agent("on_demand", ["a", "b"]),
        }
    )
    llm = FakeLlm(JUDGE_OK)

    x_run = _run_now(db, llm, "x")

    assert x_run.status == "succeeded", x_run.error
    assert [r.trigger for r in _runs(db, "b")] == ["upstream_dependency"]
    assert jobs.history == []


def test_already_queued_dependent_is_not_scheduled_twice(db, sources, install, jobs):
    install({"a": agent(), "b": agent("on_upstream_update", ["a"])})
    llm = FakeLlm(JUDGE_OK)

    _run_now(db, llm, "a")
    _run_now(db, llm, "a")  # second update before the worker got to b

    assert len(jobs.pending) == 1
    jobs.drain(db, llm)
    assert len(_runs(db, "b")) == 1
    assert _headings(  # it consumed the newest a
        orchestrator._verified_payloads(db, _materialized(db, "a")[-1].id)
    ) == ["a kept"]
    assert not orchestrator.is_stale(db, "b")


# ── Failures ────────────────────────────────────────────────────────────────


def test_failed_upstream_run_publishes_no_update(db, sources, install, jobs):
    install({"a": agent(fail=True), "b": agent("on_upstream_update", ["a"])})

    a_run = _run_now(db, FakeLlm(JUDGE_OK), "a")

    assert a_run.status == "failed" and "a exploded" in a_run.error
    assert jobs.history == []
    assert _runs(db, "b") == []


def test_downstream_failure_keeps_upstream_success(db, sources, install, jobs):
    install({"a": agent(), "b": agent("on_upstream_update", ["a"], fail=True)})
    llm = FakeLlm(JUDGE_OK)

    a_run = _run_now(db, llm, "a")
    [b_run] = jobs.drain(db, llm)

    assert b_run.status == "failed" and "b exploded" in b_run.error
    db.refresh(a_run)
    assert a_run.status == "succeeded" and a_run.error is None
    assert [r.id for r in _runs(db, "a")] == [a_run.id]
    assert orchestrator.latest_output_run(db, "a").id == a_run.id


def test_downstream_scheduling_failure_keeps_upstream_success(
    db, sources, install, jobs
):
    install({"a": agent(), "b": agent("on_upstream_update", ["a"])})
    jobs.fail_with = ConnectionError("redis is down")

    a_run = _run_now(db, FakeLlm(JUDGE_OK), "a")

    db.refresh(a_run)
    assert a_run.status == "succeeded" and a_run.error is None
    [b_run] = _runs(db, "b")
    assert b_run.status == "failed"  # honest, not a forever-"queued" row
    assert "could not enqueue" in b_run.error and "redis is down" in b_run.error
    # and it doesn't block the next update from scheduling b
    jobs.fail_with = None
    _run_now(db, FakeLlm(JUDGE_OK), "a")
    assert [db.get(AgentRun, i).agent_id for i in jobs.pending] == ["b"]


def test_cycle_behind_an_update_fails_clearly_and_settles(db, sources, install, jobs):
    install(
        {
            "x": agent(),
            "a": agent("on_upstream_update", ["x", "b"]),
            "b": agent("on_upstream_update", ["a"]),
        }
    )
    llm = FakeLlm(JUDGE_OK)

    x_run = _run_now(db, llm, "x")
    executed = jobs.drain(db, llm, limit=5)

    assert x_run.status == "succeeded"
    assert executed and all(r.status == "failed" for r in executed)
    assert all("dependency cycle" in r.error for r in executed)
    assert "a -> b -> a" in executed[0].error
    assert jobs.pending == []
    assert len(jobs.history) <= 2  # no storm
    db.refresh(x_run)
    assert x_run.status == "succeeded"


# ── Caching ─────────────────────────────────────────────────────────────────


def test_unchanged_request_after_propagation_is_a_cache_hit(db, sources, install, jobs):
    install(
        {
            "a": agent(),
            "b": agent("on_upstream_update", ["a"]),
            "c": agent("on_upstream_update", ["b"]),
        }
    )
    llm = FakeLlm(JUDGE_OK)
    _run_now(db, llm, "a")
    jobs.drain(db, llm)
    db_calls, fake_calls = _llm_calls(db), len(llm.calls)

    hit = orchestrator.request_run(db, "c", trigger="on_demand")
    assert hit.from_cache is True
    assert hit.reused_run_id == _materialized(db, "c")[-1].id
    # re-announcing a's (unchanged) output schedules nothing either
    assert orchestrator.propagate_upstream_update(db, "a") == []
    assert jobs.pending == []
    assert (_llm_calls(db), len(llm.calls)) == (db_calls, fake_calls)


# ── Resource changes through dependency chains ──────────────────────────────
#
#   reading branch:  r (on_resource_change, reading) -> rd (on_upstream_update)
#                    t (on_resource_change, slides, upstream r)
#   slides branch:   s (on_resource_change, slides)  -> sd (on_upstream_update)
#   o (on_demand, reading) -> od (on_upstream_update): manual root, never auto


def _branches(install):
    return install(
        {
            "r": agent("on_resource_change", tags=["reading"]),
            "rd": agent("on_upstream_update", ["r"], tags=["reading"]),
            "t": agent("on_resource_change", ["r"], tags=["slides"]),
            "s": agent("on_resource_change", tags=["slides"]),
            "sd": agent("on_upstream_update", ["s"], tags=["slides"]),
            "o": agent("on_demand", tags=["reading"]),
            "od": agent("on_upstream_update", ["o"], tags=["reading"]),
        }
    )


@pytest.fixture
def settled(db, sources, install, jobs):
    """Every agent has run once and nothing is pending."""
    reg, events = _branches(install)
    llm = FakeLlm(JUDGE_OK)
    for agent_id in ("r", "s", "t", "o"):
        _run_now(db, llm, agent_id)
    jobs.drain(db, llm)
    assert not any(orchestrator.is_stale(db, a.manifest.agent_id) for a in reg.all())
    events.clear()
    counts = {
        a.manifest.agent_id: len(_runs(db, a.manifest.agent_id)) for a in reg.all()
    }
    return reg, events, llm, counts


def _new_runs(db, reg, counts):
    return {
        a.manifest.agent_id: len(_runs(db, a.manifest.agent_id))
        - counts[a.manifest.agent_id]
        for a in reg.all()
        if len(_runs(db, a.manifest.agent_id)) != counts[a.manifest.agent_id]
    }


def test_content_change_reruns_only_the_affected_branch(db, sources, settled, jobs):
    reg, events, llm, counts = settled
    item = sources[0].items[0]
    item.content_text = NEW_READING
    item.content_hash = content_hash(NEW_READING)
    db.commit()

    requested = orchestrator.resource_change_event(db, {"reading"})
    assert [r.agent_id for r in requested] == ["r", "t"]  # upstream first
    jobs.drain(db, llm)

    assert events == ["r", "t", "rd"]  # FIFO: rd is queued once r finishes
    assert _new_runs(db, reg, counts) == {"r": 1, "rd": 1, "t": 1}
    assert reg.get("rd").module.SEEN[-1]["r"][0]["body"] == NEW_READING
    assert reg.get("t").module.SEEN[-1]["r"][0]["body"] == NEW_READING
    # on_demand o is invalidated (and so is its dependent), but not run
    assert orchestrator.is_stale(db, "o") and orchestrator.is_stale(db, "od")
    assert not any(orchestrator.is_stale(db, x) for x in ("r", "rd", "t", "s", "sd"))


def test_retag_accounts_for_removed_and_added_tags(db, sources, settled, jobs):
    reg, events, llm, counts = settled
    item = sources[0].items[0]
    for row in list(item.tag_rows):
        db.delete(row)
    db.add(ResourceItemTag(resource_item_id=item.id, tag="slides"))
    db.commit()

    orchestrator.resource_change_event(db, {"reading", "slides"})  # old | new
    jobs.drain(db, llm)

    # reading lost its only item; slides gained one: both branches re-run
    assert _new_runs(db, reg, counts) == {"r": 1, "rd": 1, "t": 1, "s": 1, "sd": 1}
    assert reg.get("rd").module.SEEN[-1]["r"] == []  # r now has nothing to say
    assert sorted(events) == sorted(["r", "rd", "t", "s", "sd"])
    assert events.index("r") < events.index("rd")
    assert events.index("r") < events.index("t")
    assert events.index("s") < events.index("sd")


def test_removal_reruns_dependents_and_leaves_other_branch(db, sources, settled, jobs):
    reg, events, llm, counts = settled
    removed = {t for item in sources[1].items for t in item.tags}
    db.delete(sources[1])
    db.commit()

    orchestrator.resource_change_event(db, removed)
    jobs.drain(db, llm)

    # slides removed: s, sd, and t (which reads slides) re-run; r/rd don't
    assert _new_runs(db, reg, counts) == {"s": 1, "sd": 1, "t": 1}
    assert events.index("s") < events.index("sd")


def test_change_that_leaves_inputs_equal_is_served_from_cache(
    db, sources, settled, jobs
):
    reg, events, llm, counts = settled
    db_calls = _llm_calls(db)

    requested = orchestrator.resource_change_event(db, {"reading"})

    assert {r.agent_id for r in requested} == {"r", "t"}
    assert all(r.from_cache for r in requested)
    assert jobs.pending == [] and events == []
    assert _llm_calls(db) == db_calls
    # cache hits publish no update, so rd is untouched
    assert "rd" not in _new_runs(db, reg, counts)


def test_reingest_reports_tags_of_items_that_disappeared(db, monkeypatch, tmp_path):
    from app.core.ingestion import service

    folder = tmp_path / "notes"
    folder.mkdir()
    resource = make_resource(
        db,
        kind="file",
        locator=str(folder),
        items=[{"path": "gone.md", "content": READING_TEXT, "tags": ["reading"]}],
    )
    monkeypatch.setattr(service, "_ingest_directory", _drop_all_items)
    seen: list[set[str]] = []
    monkeypatch.setattr(service, "resource_change_event", lambda _db, t: seen.append(t))

    service.ingest_resource(db, FakeLlm(), resource.id)

    assert resource.ingest_status == "ready", resource.ingest_error
    assert seen == [{"reading"}]


def _drop_all_items(db, llm, resource):
    from app.core.ingestion.service import _replace_items

    _replace_items(db, resource, [])
    db.commit()


# ── Mixed triggers: queued job vs. synchronous resolution ───────────────────
#
#   a (on_resource_change, reading) -> b (on_upstream_update, slides)
#                                       -> c (on_resource_change, slides)
#
# A reading change queues a and c (c reads reading through b). a's success
# queues b behind c; c then runs b synchronously, so b's queued job finds
# its work already done.


def _mixed(install):
    return install(
        {
            "a": agent("on_resource_change", tags=["reading"]),
            "b": agent("on_upstream_update", ["a"], tags=["slides"]),
            "c": agent("on_resource_change", ["b"], tags=["slides"]),
        }
    )


def _assert_settled(db, reg, jobs):
    assert jobs.pending == []
    assert db.scalars(select(AgentRun).where(AgentRun.status == "queued")).all() == []
    for agent_id in "abc":
        assert not orchestrator.is_stale(db, agent_id), agent_id
        manifest = reg.get(agent_id).manifest
        last = _materialized(db, agent_id)[-1]
        assert last.input_fingerprint == orchestrator.compute_fingerprint(
            manifest,
            orchestrator.resolve_resources(db, manifest),
            orchestrator.current_upstream_run_ids(db, manifest),
            None,
        )


def test_queued_dependency_already_run_synchronously_is_not_rerun(
    db, sources, install, jobs
):
    reg, events = _mixed(install)
    llm = FakeLlm(JUDGE_OK)

    requested = orchestrator.resource_change_event(db, {"reading"})
    assert [r.agent_id for r in requested] == ["a", "c"]
    executed = jobs.drain(db, llm)

    assert events == ["a", "b", "c"]
    assert [len(_materialized(db, x)) for x in "abc"] == [1, 1, 1]
    # b's queued job was already satisfied: settled as a cache hit, no LLM
    b_job = executed[-1]
    assert (b_job.agent_id, b_job.status, b_job.from_cache) == ("b", "succeeded", True)
    assert b_job.reused_run_id == _materialized(db, "b")[-1].id
    _assert_settled(db, reg, jobs)
    assert reg.get("c").module.SEEN[-1]["b"][0]["heading"] == "b kept"

    # a later edit of the upstream resource settles the same way
    item = sources[0].items[0]
    item.content_text = NEW_READING
    item.content_hash = content_hash(NEW_READING)
    db.commit()
    events.clear()
    db_calls = _llm_calls(db)

    orchestrator.resource_change_event(db, {"reading"})
    jobs.drain(db, llm)

    assert events == ["a", "b", "c"]
    assert [len(_materialized(db, x)) for x in "abc"] == [2, 2, 2]
    assert reg.get("b").module.SEEN[-1]["a"][0]["body"] == NEW_READING
    _assert_settled(db, reg, jobs)
    # one generate-free settle for b: LLM calls are only the 3 real runs'
    # verifications (one judged item each; dropped items cost nothing)
    assert _llm_calls(db) - db_calls == 3


def test_satisfied_check_spares_forced_and_distinct_request_runs(
    db, sources, install, jobs
):
    reg, events = _mixed(install)
    llm = FakeLlm(JUDGE_OK)
    orchestrator.resource_change_event(db, {"reading"})
    jobs.drain(db, llm)
    events.clear()

    # an explicit force=True on a fresh agent still regenerates
    forced = orchestrator.request_run(db, "b", trigger="on_demand", force=True)
    # a distinct request payload is new input, not a satisfied duplicate
    other = orchestrator.request_run(
        db, "c", trigger="on_demand", request={"focus": "ch3"}
    )
    assert forced.status == other.status == "queued"
    executed = jobs.drain(db, llm)

    assert [(r.agent_id, r.from_cache) for r in executed[:2]] == [
        ("b", False),
        ("c", False),
    ]
    assert all(r.status == "succeeded" for r in executed)
    assert events[:2] == ["b", "c"]
    assert jobs.pending == []
