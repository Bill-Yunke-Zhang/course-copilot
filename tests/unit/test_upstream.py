"""Upstream resolution: running upstreams first, verified payloads, caching.

Test agents live in a throwaway package under tmp_path (loaded with
AgentRegistry(package=...)), never in backend/app/agents/.
"""

import sys
import textwrap
import uuid

import pytest
from sqlalchemy import func, select

import app.core.registry as registry_module
from app.core import orchestrator
from app.core.registry import AgentRegistry
from app.models import AgentRun, LlmCall
from tests.conftest import FakeLlm, make_resource

READING_TEXT = "Chapter 3 covers consistent hashing and virtual nodes."

JUDGE_OK = {"_JudgeResponse": {"supported": True, "reason": "excerpt matches"}}

# Upstream: one grounded item, plus one citing a resource item that does not
# exist (the verifier drops that deterministically, with no LLM call).
UPSTREAM = """
from app.core.manifest import AgentManifest

SEEN = []

MANIFEST = AgentManifest(
    agent_id="{agent_id}",
    display_name="{agent_id}",
    description="test upstream",
    required_resource_tags=["{tag}"],
    optional_upstream_agents={upstreams!r},
    output_archetype="text_block",
    trigger="on_demand",
    cacheable=True,
    dashboard_widget=False,
)

def run(ctx):
    SEEN.append(ctx.upstream)
    {body}
    item = ctx.resources["{tag}"][0]
    return [
        {{
            "heading": "{agent_id} kept",
            "body": item.content,
            "source_refs": [{{"resource_item_id": item.id,
                              "excerpt": "consistent hashing"}}],
        }},
        {{
            "heading": "{agent_id} dropped",
            "body": "made up",
            "source_refs": [{{"resource_item_id": 999999, "excerpt": "nope"}}],
        }},
    ]
"""

# Downstream: records the ctx.upstream it was given, emits one grounded item.
DOWNSTREAM = """
from app.core.manifest import AgentManifest

SEEN = []

MANIFEST = AgentManifest(
    agent_id="{agent_id}",
    display_name="{agent_id}",
    description="test downstream",
    required_resource_tags=["{tag}"],
    optional_upstream_agents={upstreams!r},
    output_archetype="text_block",
    trigger="on_demand",
    cacheable=True,
    dashboard_widget=False,
)

def run(ctx):
    SEEN.append(ctx.upstream)
    item = ctx.resources["{tag}"][0]
    return [
        {{
            "heading": "summary",
            "body": "virtual nodes",
            "source_refs": [{{"resource_item_id": item.id,
                              "excerpt": "virtual nodes"}}],
        }}
    ]
"""


@pytest.fixture
def agent_pkg(tmp_path, monkeypatch, registry):
    """Build a temp agent package; returns install(modules) -> registry."""
    name = f"upstream_test_agents_{uuid.uuid4().hex[:8]}"
    pkg_dir = tmp_path / name
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))

    def install(modules: dict[str, str]) -> AgentRegistry:
        for mod_name, source in modules.items():
            (pkg_dir / f"{mod_name}.py").write_text(textwrap.dedent(source))
        reg = AgentRegistry(package=name)
        monkeypatch.setattr(registry_module, "_registry", reg)
        return reg

    yield install
    for mod in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
        del sys.modules[mod]


def upstream_src(agent_id, upstreams=(), fail=False, tag="reading"):
    body = "raise RuntimeError('upstream exploded')" if fail else "pass"
    return UPSTREAM.format(
        agent_id=agent_id, upstreams=list(upstreams), body=body, tag=tag
    )


def downstream_src(agent_id, upstreams, tag="reading"):
    return DOWNSTREAM.format(agent_id=agent_id, upstreams=list(upstreams), tag=tag)


@pytest.fixture
def reading(db):
    return make_resource(
        db,
        locator="https://example.edu/ch3.html",
        items=[{"title": "Ch3", "content": READING_TEXT, "tags": ["reading"]}],
    )


def _run_now(db, llm, agent_id):
    run = AgentRun(agent_id=agent_id, status="queued", trigger="on_demand")
    db.add(run)
    db.commit()
    return orchestrator.execute_run(db, llm, run.id)


def _runs(db, agent_id):
    return db.scalars(
        select(AgentRun).where(AgentRun.agent_id == agent_id).order_by(AgentRun.id)
    ).all()


def test_downstream_gets_only_verified_upstream_payloads(db, reading, agent_pkg):
    reg = agent_pkg({"up": upstream_src("up"), "down": downstream_src("down", ["up"])})
    run = _run_now(db, FakeLlm(JUDGE_OK), "down")

    assert run.status == "succeeded", run.error
    seen = reg.get("down").module.SEEN[-1]
    assert [p["heading"] for p in seen["up"]] == ["up kept"]  # dropped item absent


def test_missing_upstream_runs_first_then_downstream(db, reading, agent_pkg):
    reg = agent_pkg({"up": upstream_src("up"), "down": downstream_src("down", ["up"])})
    assert _runs(db, "up") == []

    down = _run_now(db, FakeLlm(JUDGE_OK), "down")

    up_runs = _runs(db, "up")
    assert down.status == "succeeded", down.error
    assert [r.status for r in up_runs] == ["succeeded"]
    assert up_runs[0].trigger == "upstream_dependency"
    assert up_runs[0].finished_at <= down.finished_at
    # the downstream agent already saw the output of that just-finished run
    assert [p["heading"] for p in reg.get("down").module.SEEN[-1]["up"]] == ["up kept"]


def test_fresh_upstream_is_not_rerun(db, reading, agent_pkg):
    agent_pkg({"up": upstream_src("up"), "down": downstream_src("down", ["up"])})
    llm = FakeLlm(JUDGE_OK)
    up = _run_now(db, llm, "up")
    assert not orchestrator.is_stale(db, "up")

    down = _run_now(db, llm, "down")

    assert down.status == "succeeded", down.error
    assert [r.id for r in _runs(db, "up")] == [up.id]


def test_chain_resolves_recursively(db, reading, agent_pkg):
    reg = agent_pkg(
        {
            "a": upstream_src("a"),
            "b": upstream_src("b", upstreams=["a"]),
            "c": downstream_src("c", ["b"]),
        }
    )
    run = _run_now(db, FakeLlm(JUDGE_OK), "c")

    assert run.status == "succeeded", run.error
    assert len(_runs(db, "a")) == 1 and len(_runs(db, "b")) == 1
    # b's run row is created first, but a has to finish before b's agent runs
    assert _runs(db, "a")[0].finished_at <= _runs(db, "b")[0].finished_at
    assert [x["heading"] for x in reg.get("b").module.SEEN[-1]["a"]] == ["a kept"]
    assert set(reg.get("c").module.SEEN[-1]) == {"b"}


def test_uninstalled_upstream_is_absent_not_an_error(db, reading, agent_pkg):
    reg = agent_pkg(
        {"up": upstream_src("up"), "down": downstream_src("down", ["up", "ghost"])}
    )
    run = _run_now(db, FakeLlm(JUDGE_OK), "down")

    assert run.status == "succeeded", run.error
    assert set(reg.get("down").module.SEEN[-1]) == {"up"}
    manifest = reg.get("down").manifest
    _, run_ids = orchestrator.resolve_upstream_outputs(db, manifest)
    assert "ghost" not in run_ids


def test_failed_upstream_fails_the_downstream_run(db, reading, agent_pkg):
    reg = agent_pkg(
        {"up": upstream_src("up", fail=True), "down": downstream_src("down", ["up"])}
    )
    run = _run_now(db, FakeLlm(JUDGE_OK), "down")

    assert run.status == "failed"
    assert "upstream 'up'" in run.error and "upstream exploded" in run.error
    assert reg.get("down").module.SEEN == []  # downstream agent never ran


def test_cycle_is_rejected_not_recursed(db, reading, agent_pkg):
    agent_pkg({"a": upstream_src("a", upstreams=["b"]), "b": upstream_src("b", ["a"])})
    run = _run_now(db, FakeLlm(JUDGE_OK), "a")

    assert run.status == "failed"
    assert "dependency cycle" in run.error and "a -> b -> a" in run.error


def test_fingerprint_includes_upstream_run_and_cache_hits(
    db, reading, agent_pkg, enqueued
):
    reg = agent_pkg({"up": upstream_src("up"), "down": downstream_src("down", ["up"])})
    llm = FakeLlm(JUDGE_OK)
    down = _run_now(db, llm, "down")
    up_run = _runs(db, "up")[-1]
    manifest = reg.get("down").manifest

    resolved = orchestrator.resolve_resources(db, manifest)
    assert down.input_fingerprint == orchestrator.compute_fingerprint(
        manifest, resolved, {"up": up_run.id}, None
    )
    assert down.input_fingerprint != orchestrator.compute_fingerprint(
        manifest, resolved, {}, None
    )
    assert not orchestrator.is_stale(db, "down")

    hit = orchestrator.request_run(db, "down", trigger="on_demand")
    assert hit.from_cache is True
    assert hit.reused_run_id == down.id
    assert enqueued == []


def test_new_upstream_output_makes_downstream_stale(db, reading, agent_pkg, enqueued):
    agent_pkg({"up": upstream_src("up"), "down": downstream_src("down", ["up"])})
    llm = FakeLlm(JUDGE_OK)
    _run_now(db, llm, "down")
    assert not orchestrator.is_stale(db, "down")

    _run_now(db, llm, "up")  # upstream produces a new materialized run

    assert orchestrator.is_stale(db, "down")
    miss = orchestrator.request_run(db, "down", trigger="on_demand")
    assert miss.from_cache is False
    assert len(enqueued) == 1


# ── Stale upstream behind an unchanged run id (single edge, distinct tags) ──
#
# up reads only `reading`, down reads only `slides`. Editing the reading
# leaves down's own resources and up's latest run id unchanged, so down's
# fingerprint still matches — yet up's output is stale, so down is too.

SLIDES_TEXT = "Lecture 9 slides: virtual nodes smooth out the key distribution."
NEW_READING = "Chapter 3 (revised) covers consistent hashing with bounded loads."


@pytest.fixture
def split_sources(db):
    reading = make_resource(
        db,
        locator="https://example.edu/ch3.html",
        items=[{"title": "Ch3", "content": READING_TEXT, "tags": ["reading"]}],
    )
    make_resource(
        db,
        locator="https://example.edu/lec9.pdf",
        items=[{"title": "Lec9", "content": SLIDES_TEXT, "tags": ["slides"]}],
    )
    return reading.items[0]


def _edit_content(db, item, text):
    from app.core.ingestion.extract import content_hash

    item.content_text = text
    item.content_hash = content_hash(text)
    db.commit()


def _llm_call_count(db):
    return db.scalar(select(func.count()).select_from(LlmCall))


def _single_edge(agent_pkg):
    return agent_pkg(
        {
            "up": upstream_src("up", tag="reading"),
            "down": downstream_src("down", ["up"], tag="slides"),
        }
    )


def test_stale_upstream_makes_downstream_stale(db, split_sources, agent_pkg):
    _single_edge(agent_pkg)
    assert _run_now(db, FakeLlm(JUDGE_OK), "down").status == "succeeded"
    assert not orchestrator.is_stale(db, "up")
    assert not orchestrator.is_stale(db, "down")

    _edit_content(db, split_sources, NEW_READING)  # up is NOT re-run

    assert orchestrator.is_stale(db, "up")
    assert orchestrator.is_stale(db, "down")


def test_stale_upstream_blocks_downstream_cache_hit(
    db, split_sources, agent_pkg, enqueued
):
    reg = _single_edge(agent_pkg)
    llm = FakeLlm(JUDGE_OK)
    assert _run_now(db, llm, "down").status == "succeeded"
    first_up = _runs(db, "up")[-1]
    _edit_content(db, split_sources, NEW_READING)

    # freshness/cache checks themselves run nothing and call no LLM
    calls_before = _llm_call_count(db)
    run = orchestrator.request_run(db, "down", trigger="on_demand")
    assert run.from_cache is False
    assert run.status == "queued"
    assert enqueued == [("agents", "run_agent_job", (run.id,))]
    assert _llm_call_count(db) == calls_before
    assert [r.id for r in _runs(db, "up")] == [first_up.id]

    # the queued job (what the worker does) refreshes up, then runs down
    run = orchestrator.execute_run(db, llm, run.id)
    assert run.status == "succeeded", run.error
    up_runs = _runs(db, "up")
    assert len(up_runs) == 2 and up_runs[-1].status == "succeeded"
    assert up_runs[-1].trigger == "upstream_dependency"
    seen = reg.get("down").module.SEEN[-1]["up"]
    assert [p["body"] for p in seen] == [NEW_READING]
    assert not orchestrator.is_stale(db, "up")
    assert not orchestrator.is_stale(db, "down")

    # nothing changed since: cache hit, zero additional LLM calls, no enqueue
    calls_before = _llm_call_count(db)
    fake_calls_before = len(llm.calls)
    hit = orchestrator.request_run(db, "down", trigger="on_demand")
    assert hit.from_cache is True
    assert hit.reused_run_id == run.id
    assert len(enqueued) == 1
    assert _llm_call_count(db) == calls_before
    assert len(llm.calls) == fake_calls_before


def test_stale_upstream_propagates_through_a_chain(
    db, split_sources, agent_pkg, enqueued
):
    # a (reading) -> b (slides) -> c (slides): only a's input changes
    agent_pkg(
        {
            "a": upstream_src("a", tag="reading"),
            "b": upstream_src("b", upstreams=["a"], tag="slides"),
            "c": downstream_src("c", ["b"], tag="slides"),
        }
    )
    llm = FakeLlm(JUDGE_OK)
    assert _run_now(db, llm, "c").status == "succeeded"
    assert not orchestrator.is_stale(db, "c")

    _edit_content(db, split_sources, NEW_READING)

    assert orchestrator.is_stale(db, "a")
    assert orchestrator.is_stale(db, "b")
    assert orchestrator.is_stale(db, "c")
    run = orchestrator.request_run(db, "c", trigger="on_demand")
    assert run.from_cache is False

    run = orchestrator.execute_run(db, llm, run.id)
    assert run.status == "succeeded", run.error
    assert [len(_runs(db, x)) for x in ("a", "b", "c")] == [2, 2, 2]
    assert not orchestrator.is_stale(db, "c")


def test_freshness_check_terminates_on_a_cycle(db, split_sources, agent_pkg, enqueued):
    # A cycle can't execute, so fake "fresh" output rows for both agents and
    # make sure the freshness walk doesn't recurse forever.
    reg = agent_pkg(
        {
            "a": upstream_src("a", upstreams=["b"]),
            "b": upstream_src("b", upstreams=["a"]),
        }
    )
    for agent_id in ("a", "b"):
        db.add(AgentRun(agent_id=agent_id, status="succeeded", trigger="manual"))
    db.commit()
    for agent_id in ("a", "b"):
        manifest = reg.get(agent_id).manifest
        last = _runs(db, agent_id)[-1]
        last.input_fingerprint = orchestrator.compute_fingerprint(
            manifest,
            orchestrator.resolve_resources(db, manifest),
            orchestrator.current_upstream_run_ids(db, manifest),
            None,
        )
    db.commit()

    assert orchestrator.is_stale(db, "a")  # a cycle is never considered fresh
    run = orchestrator.request_run(db, "a", trigger="on_demand")
    assert run.from_cache is False  # goes to execution, which rejects the cycle
