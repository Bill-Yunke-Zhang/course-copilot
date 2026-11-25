"""The generic orchestrator (spec §7) — the harness's execution engine.

Shipped working and proven against the two starter agents (Deadline, Concept
Coverage), which are deliberately leaf nodes: multi-tag resolution,
triggering, verification, caching/staleness, and assembly all work end to
end. What the starter does NOT implement — clearly marked below — is
everything to do with `optional_upstream_agents`:

  * resolving upstream agent outputs (running a stale/missing upstream first)
  * topological ordering and dependency-cycle rejection
  * the `on_upstream_update` trigger path (re-running dependents when an
    upstream's output changes)

Building those is required student work; staff-authored mystery agents with
real dependencies are dropped into `app/agents/` at grading time and must
work with ZERO changes required of them — only your harness generalizes.
"""

import contextvars
import hashlib
import json
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.archetypes import validate_payload
from app.core.llm import BoundLlm, LlmClient
from app.core.manifest import AgentContext, AgentManifest, ResourceItemView
from app.core.registry import get_registry
from app.core.verifier import DEFAULT_POLICY, verify_item
from app.models import AgentRun, OutputItem, ResourceItem, ResourceItemTag

logger = logging.getLogger(__name__)


# ── Resource resolution ──────────────────────────────────────────────────────


def resolve_resources(
    db: Session, manifest: AgentManifest
) -> dict[str, list[ResourceItem]]:
    """Map each required tag to the resource items CURRENTLY carrying it.

    Resolution always runs against live tag state — user tag edits and
    classifier output are indistinguishable here, by design. A tag nobody
    carries maps to an empty list; agents must degrade gracefully.
    """
    resolved: dict[str, list[ResourceItem]] = {}
    for tag in manifest.required_resource_tags:
        rows = db.scalars(
            select(ResourceItem)
            .join(ResourceItemTag)
            .where(ResourceItemTag.tag == tag)
            .order_by(ResourceItem.id)
        ).all()
        resolved[tag] = list(rows)
    return resolved


def _to_view(item: ResourceItem) -> ResourceItemView:
    return ResourceItemView(
        id=item.id,
        resource_id=item.resource_id,
        kind=item.resource.kind,
        locator=item.resource.locator,
        path=item.path,
        title=item.title,
        content=item.content_text,
        tags=item.tags,
        meta=item.meta or {},
    )


# ── Caching / staleness ──────────────────────────────────────────────────────


def compute_fingerprint(
    manifest: AgentManifest,
    resolved: dict[str, list[ResourceItem]],
    upstream_run_ids: dict[str, int],
    request: dict | None,
) -> str:
    """Hash of everything a run's output depends on.

    Two runs with equal fingerprints saw identical inputs, so a `cacheable`
    agent may serve the earlier verified result. Membership changes (a
    resource added, removed, or re-tagged) and content changes both alter
    the fingerprint; so does a different on_demand request payload. Each
    item's user-editable `meta` is part of the state too: agents see it in
    ctx (ResourceItemView.meta) and may select by it, so a metadata-only
    edit must not be served a result computed from the old metadata.
    """
    state = {
        "agent_id": manifest.agent_id,
        "items": sorted(
            (item.id, item.content_hash, _meta_hash(item.meta))
            for items in resolved.values()
            for item in items
        ),
        "upstream": sorted(upstream_run_ids.items()),
        "request": request,
    }
    encoded = json.dumps(state, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _meta_hash(meta: dict | None) -> str:
    encoded = json.dumps(meta or {}, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _latest_materialized_run(db: Session, agent_id: str) -> AgentRun | None:
    """Latest succeeded run that actually holds output items (not a cache hit)."""
    return db.scalars(
        select(AgentRun)
        .where(
            AgentRun.agent_id == agent_id,
            AgentRun.status == "succeeded",
            AgentRun.from_cache.is_(False),
        )
        .order_by(AgentRun.id.desc())
        .limit(1)
    ).first()


def latest_output_run(db: Session, agent_id: str) -> AgentRun | None:
    """The run whose output the UI should display (resolving cache hits)."""
    latest = db.scalars(
        select(AgentRun)
        .where(AgentRun.agent_id == agent_id, AgentRun.status == "succeeded")
        .order_by(AgentRun.id.desc())
        .limit(1)
    ).first()
    if latest is None:
        return None
    if latest.from_cache and latest.reused_run_id is not None:
        return db.get(AgentRun, latest.reused_run_id)
    return latest


def is_stale(db: Session, agent_id: str) -> bool:
    """True when this agent's latest materialized output is out of date.

    That is the case when:
    - it has no materialized output;
    - its own fingerprint (resources, request, and the run ids of its
      upstreams' latest outputs) differs from that run's; or
    - any installed upstream is itself stale, checked recursively. An
      upstream whose inputs changed but that has not re-run yet still has
      the same latest run id, so the fingerprint alone cannot see that its
      output is out of date — and neither can anything consuming it.

    Read-only: runs nothing and makes no LLM calls. An agent on a
    dependency cycle is never fresh, so it always goes to execution, which
    rejects the cycle with a clear error.
    """
    return _is_stale(db, agent_id, (), {})


def _is_stale(
    db: Session, agent_id: str, path: tuple[str, ...], memo: dict[str, bool]
) -> bool:
    if agent_id in path:  # cycle: stop walking, never treat it as fresh
        return True
    if agent_id in memo:  # shared upstream in a diamond: check it once
        return memo[agent_id]
    manifest = get_registry().get(agent_id).manifest
    last = _latest_materialized_run(db, agent_id)
    if last is None:
        stale = True
    else:
        resolved = resolve_resources(db, manifest)
        upstream_run_ids = current_upstream_run_ids(db, manifest)
        fingerprint = compute_fingerprint(
            manifest, resolved, upstream_run_ids, last.request_payload
        )
        stale = fingerprint != last.input_fingerprint or _any_upstream_stale(
            db, manifest, path, memo
        )
    memo[agent_id] = stale
    return stale


def _any_upstream_stale(
    db: Session,
    manifest: AgentManifest,
    path: tuple[str, ...] = (),
    memo: dict[str, bool] | None = None,
) -> bool:
    """True if any installed upstream of `manifest` is stale (read-only)."""
    memo = {} if memo is None else memo
    path = path + (manifest.agent_id,)
    return any(
        _is_stale(db, upstream_id, path, memo)
        for upstream_id in _installed_upstreams(manifest)
    )


# ── Upstream dependencies — STUDENT WORK ─────────────────────────────────────


class UpstreamCycleError(RuntimeError):
    """A manifest's optional_upstream_agents form a dependency cycle."""


class UpstreamRunError(RuntimeError):
    """An upstream agent had to run first, and that run failed."""


# Agent ids whose upstreams are currently being resolved, outermost first.
# A ContextVar (not a parameter) because the recursion goes through
# execute_run, whose signature the worker and API depend on.
_resolving: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "upstream_resolving", default=()
)


def _installed_upstreams(manifest: AgentManifest) -> list[str]:
    """Declared upstreams that exist in this installation, in manifest order.

    "Optional" means a declared upstream that is not installed is simply
    absent — it never appears in ctx.upstream or the fingerprint.
    """
    registry = get_registry()
    installed: list[str] = []
    for upstream_id in manifest.optional_upstream_agents:
        try:
            registry.get(upstream_id)
        except KeyError:
            continue
        installed.append(upstream_id)
    return installed


def current_upstream_run_ids(db: Session, manifest: AgentManifest) -> dict[str, int]:
    """{upstream agent_id: latest materialized run id}, without running anything.

    This is exactly the `upstream_run_ids` that resolve_upstream_outputs
    would hand to compute_fingerprint if no upstream needed to (re)run, so
    is_stale and request_run's cache check fingerprint the same state that
    execute_run does. Upstreams that are not installed, or have never
    produced output, are left out.
    """
    run_ids: dict[str, int] = {}
    for upstream_id in _installed_upstreams(manifest):
        last = _latest_materialized_run(db, upstream_id)
        if last is not None:
            run_ids[upstream_id] = last.id
    return run_ids


def _verified_payloads(db: Session, run_id: int) -> list[dict]:
    rows = db.scalars(
        select(OutputItem)
        .where(
            OutputItem.agent_run_id == run_id,
            OutputItem.verification_status == "verified",
        )
        .order_by(OutputItem.position)
    ).all()
    return [row.payload for row in rows]


def resolve_upstream_outputs(
    db: Session, manifest: AgentManifest, llm: LlmClient | None = None
) -> tuple[dict[str, list[dict]], dict[str, int]]:
    """Resolve the outputs this agent's `optional_upstream_agents` provide.

    Returns ({agent_id: [verified payloads]}, {agent_id: run_id}).

    For each declared upstream, in manifest order:

    - Not installed (not in the registry): skipped. It appears in neither
      dict and is not an error.
    - No output yet, or `is_stale()`: it is run FIRST, synchronously in this
      process (a new AgentRun passed straight to execute_run, not the RQ
      queue), so this downstream run sees the result. execute_run resolves
      that upstream's own upstreams the same way, so chains recurse
      depth-first. Doing that needs `llm`; without one a missing/stale
      upstream raises ValueError.
    - Its output is read from its latest materialized run (cache-hit runs
      only point at one through reused_run_id and hold no items), keeping
      only items that passed verification, ordered by position. That run's
      id goes into the second dict, and from there into the fingerprint.

    Failure policy: if an upstream that had to run fails, this raises
    UpstreamRunError, so the downstream run fails too (execute_run records
    the message in run.error). Handing the downstream empty output instead
    would let it publish — and cache — a result built on missing data that
    looks complete. An upstream that succeeds with zero items (nothing to
    report) is not a failure: the downstream gets [] for it.

    Cycles: agents whose upstreams are being resolved are tracked through
    the recursion; an upstream already on that path raises
    UpstreamCycleError naming the cycle, so resolution can never recurse
    forever. is_stale treats any agent on a cycle as stale, so a cycle is
    always walked into and rejected here, never served as fresh output.
    Ordering: resolution is depth-first post-order — every upstream
    finishes before the agent consuming it starts — which is a topological
    order of the dependency graph however deep the chain, so no separate
    sort is needed here.
    """
    path = _resolving.get() + (manifest.agent_id,)
    token = _resolving.set(path)
    try:
        outputs: dict[str, list[dict]] = {}
        run_ids: dict[str, int] = {}
        for upstream_id in _installed_upstreams(manifest):
            if upstream_id in path:
                cycle = path[path.index(upstream_id) :] + (upstream_id,)
                raise UpstreamCycleError(
                    "dependency cycle in optional_upstream_agents: "
                    + " -> ".join(cycle)
                )
            if is_stale(db, upstream_id):
                _run_upstream_now(db, llm, upstream_id, manifest.agent_id)
            last = _latest_materialized_run(db, upstream_id)
            if last is None:  # unreachable unless the run above lied
                raise UpstreamRunError(f"upstream {upstream_id!r} has no output")
            outputs[upstream_id] = _verified_payloads(db, last.id)
            run_ids[upstream_id] = last.id
        return outputs, run_ids
    finally:
        _resolving.reset(token)


def _run_upstream_now(
    db: Session, llm: LlmClient | None, upstream_id: str, downstream_id: str
) -> AgentRun:
    if llm is None:
        raise ValueError(
            f"upstream {upstream_id!r} of {downstream_id!r} is missing or stale "
            "and must run first, but no LlmClient was given"
        )
    run = AgentRun(agent_id=upstream_id, status="queued", trigger="upstream_dependency")
    db.add(run)
    db.commit()
    run = execute_run(db, llm, run.id)
    if run.status != "succeeded":
        raise UpstreamRunError(
            f"upstream {upstream_id!r} (run {run.id}) failed: {run.error}"
        )
    return run


# Agents that materialized new output during the current outermost
# execute_run (the run itself plus any upstream it ran synchronously).
# Propagation is deferred until that outermost run finishes, so an
# on_upstream_update dependent that the same resolution is about to run
# synchronously is fresh by then and is not queued a second time.
_pending_updates: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "pending_upstream_updates", default=None
)


def _upstream_dependents(agent_id: str) -> list[str]:
    """Installed agents with trigger="on_upstream_update" that consume `agent_id`."""
    return [
        a.manifest.agent_id
        for a in get_registry().all()
        if a.manifest.trigger == "on_upstream_update"
        and agent_id in _installed_upstreams(a.manifest)
    ]


def _dependency_order(agent_ids: list[str]) -> list[str]:
    """`agent_ids` reordered so each comes after its installed upstreams.

    Requests are queued in this order, so a single FIFO worker runs an
    upstream before anything consuming it, and the dependent then finds it
    fresh instead of re-running it synchronously. Cycle-safe: an agent
    already on the walk is not revisited (execution rejects the cycle).
    """
    wanted = set(agent_ids)
    order: list[str] = []
    done: set[str] = set()

    def visit(agent_id: str, path: tuple[str, ...]) -> None:
        if agent_id in done or agent_id in path:
            return
        manifest = get_registry().get(agent_id).manifest
        for upstream_id in _installed_upstreams(manifest):
            visit(upstream_id, path + (agent_id,))
        done.add(agent_id)
        if agent_id in wanted:
            order.append(agent_id)

    for agent_id in agent_ids:
        visit(agent_id, ())
    return order


def _has_queued_run(db: Session, agent_id: str) -> bool:
    """A run of this agent is queued and not yet started.

    When that run executes it resolves every upstream against its latest
    output (refreshing stale ones first), so it already covers any update
    published after it was queued — scheduling another would be duplicate
    work (e.g. both sides of a diamond finishing before the join runs).
    """
    return (
        db.scalars(
            select(AgentRun.id)
            .where(AgentRun.agent_id == agent_id, AgentRun.status == "queued")
            .limit(1)
        ).first()
        is not None
    )


def propagate_upstream_update(db: Session, agent_id: str) -> list[AgentRun]:
    """Schedule re-runs of dependents after this agent produced new output.

    Called once `agent_id` has materialized a new verified run. Every
    installed agent with trigger="on_upstream_update" that declares it as an
    upstream is requested through the normal request_run path (cache check,
    then the RQ "agents" queue) — unless:

    - it is not stale (it already consumed this output, e.g. it ran this
      upstream synchronously itself); or
    - it already has a queued, not-yet-started run, which will pick up the
      new output when it executes (no duplicate fanout).

    Chains propagate hop by hop: when a scheduled dependent's own run
    succeeds, it propagates to its dependents in turn. This only ever
    follows edges out of a run that just succeeded, and an agent on a
    dependency cycle can never succeed (execution rejects the cycle with
    UpstreamCycleError), so propagation cannot loop. Returns the runs it
    requested (queued runs or cache hits).
    """
    requested: list[AgentRun] = []
    for dependent_id in _dependency_order(_upstream_dependents(agent_id)):
        if _has_queued_run(db, dependent_id) or not is_stale(db, dependent_id):
            continue
        requested.append(request_run(db, dependent_id, trigger="upstream_update"))
    return requested


def _flush_upstream_updates(db: Session, agent_ids: list[str]) -> None:
    """Propagate each agent's new output; never fails the run that produced it.

    A scheduling failure is logged here and recorded on the dependent's run
    row by request_run; the upstream run that already succeeded keeps its
    status.
    """
    for agent_id in dict.fromkeys(agent_ids):
        try:
            propagate_upstream_update(db, agent_id)
        except Exception:
            db.rollback()
            logger.exception("Propagating new output of %s failed", agent_id)


# ── Triggering ───────────────────────────────────────────────────────────────


def resource_change_event(db: Session, changed_tags: set[str]) -> list[AgentRun]:
    """A resource was added, edited, re-tagged, or removed.

    Deterministic tag matching only — NO LLM call. This fires on every
    resource edit, so it has to stay cheap; the fingerprint check inside
    request_run() then decides whether anything actually needs to run.
    """
    matched = [
        agent.manifest.agent_id
        for agent in get_registry().all()
        if agent.manifest.trigger == "on_resource_change"
        and changed_tags & _input_tags(agent.manifest)
    ]
    return [
        request_run(db, agent_id, trigger="resource_change")
        for agent_id in _dependency_order(matched)
    ]


def _input_tags(manifest: AgentManifest) -> set[str]:
    """Tags this agent's output depends on: its own plus every installed
    upstream's, transitively (cycle-safe).

    An on_resource_change agent consuming an upstream that reads a changed
    tag is affected by that change even if it doesn't require the tag
    itself. on_upstream_update agents are reached instead through
    propagate_upstream_update once their upstream re-materializes.
    """
    registry = get_registry()
    tags: set[str] = set()
    seen: set[str] = set()
    stack = [manifest]
    while stack:
        current = stack.pop()
        if current.agent_id in seen:
            continue
        seen.add(current.agent_id)
        tags |= set(current.required_resource_tags)
        stack.extend(
            registry.get(upstream_id).manifest
            for upstream_id in _installed_upstreams(current)
        )
    return tags


# ── Run lifecycle ────────────────────────────────────────────────────────────


def _cached_result(
    db: Session, manifest: AgentManifest, request: dict | None
) -> tuple[AgentRun, str] | None:
    """(last materialized run, fingerprint) if a cacheable agent's current
    inputs match that run exactly, else None. Read-only; no LLM calls."""
    if not manifest.cacheable:
        return None
    last = _latest_materialized_run(db, manifest.agent_id)
    if last is None:
        return None
    resolved = resolve_resources(db, manifest)
    upstream_run_ids = current_upstream_run_ids(db, manifest)
    fingerprint = compute_fingerprint(manifest, resolved, upstream_run_ids, request)
    # Equal fingerprints only prove the same upstream run ids were consumed;
    # a stale upstream must still refresh before a hit.
    if fingerprint != last.input_fingerprint or _any_upstream_stale(db, manifest):
        return None
    return last, fingerprint


# Triggers whose runs are only ever requested without force=True and without
# a request payload: their job is "bring this agent up to date", nothing more.
_AUTOMATIC_TRIGGERS = frozenset({"resource_change", "upstream_update"})


def request_run(
    db: Session,
    agent_id: str,
    trigger: str,
    request: dict | None = None,
    force: bool = False,
) -> AgentRun:
    """Entry point for all run requests (any trigger path).

    Applies the caching policy synchronously: a `cacheable` agent whose
    current input fingerprint matches its last verified run is served from
    cache (recorded as an AgentRun with from_cache=True — the §13 report
    counts these). Otherwise the run is enqueued for the worker.

    `force=True` skips the cache check and always regenerates — the
    semantics of an explicit user "run/check now" button. Automatic
    triggers (resource-change events) never force, so caching still does
    its §13 job there.
    """
    from app import worker  # local import: worker imports this module too

    manifest = get_registry().get(agent_id).manifest

    if not force:
        cached = _cached_result(db, manifest, request)
        if cached is not None:
            last, fingerprint = cached
            hit = AgentRun(
                agent_id=agent_id,
                status="succeeded",
                trigger=trigger,
                input_fingerprint=fingerprint,
                from_cache=True,
                reused_run_id=last.id,
                request_payload=request,
                verification_summary=last.verification_summary,
                finished_at=datetime.utcnow(),
            )
            db.add(hit)
            db.commit()
            return hit

    run = AgentRun(
        agent_id=agent_id,
        status="queued",
        trigger=trigger,
        request_payload=request,
    )
    db.add(run)
    db.commit()

    from app.core.queue import enqueue

    try:
        enqueue("agents", worker.run_agent_job, run.id)
    except Exception as exc:
        # Don't leave a "queued" row that no job will ever execute.
        run.status = "failed"
        run.error = f"could not enqueue run: {exc}"
        run.finished_at = datetime.utcnow()
        db.commit()
        raise
    return run


def execute_run(db: Session, llm: LlmClient, run_id: int) -> AgentRun:
    """Execute one queued agent run: resolve → run → validate → verify → store.

    After the outermost run finishes (succeeded or failed), every agent that
    materialized new output along the way — the run itself and any upstream
    it ran synchronously — propagates to its on_upstream_update dependents.
    That happens outside the run's own try block: a downstream scheduling
    failure never turns an already-succeeded run into a failed one.
    """
    outermost = _pending_updates.get() is None
    if not outermost:
        return _execute_run(db, llm, run_id)
    token = _pending_updates.set([])
    try:
        return _execute_run(db, llm, run_id)
    finally:
        updated = _pending_updates.get() or []
        _pending_updates.reset(token)
        _flush_upstream_updates(db, updated)


def _execute_run(db: Session, llm: LlmClient, run_id: int) -> AgentRun:
    run = db.get(AgentRun, run_id)
    if run is None:
        raise KeyError(f"no AgentRun with id {run_id}")
    agent = get_registry().get(run.agent_id)
    manifest = agent.manifest

    # An automatic run can be satisfied while it waits in the queue: another
    # run resolved this agent synchronously as its upstream (e.g. a
    # resource-change job queued ahead of this upstream-update job). Settle
    # it exactly as request_run would now — a cache hit, zero LLM calls, no
    # new output to propagate — instead of materializing the same output
    # twice. Forced and on-demand runs (which may carry a request payload)
    # are never short-circuited here.
    if run.trigger in _AUTOMATIC_TRIGGERS and run.request_payload is None:
        cached = _cached_result(db, manifest, None)
        if cached is not None:
            last, fingerprint = cached
            run.status = "succeeded"
            run.input_fingerprint = fingerprint
            run.from_cache = True
            run.reused_run_id = last.id
            run.verification_summary = last.verification_summary
            run.started_at = run.finished_at = datetime.utcnow()
            db.commit()
            return run

    run.status = "running"
    run.started_at = datetime.utcnow()
    db.commit()

    try:
        resolved = resolve_resources(db, manifest)
        upstream_outputs, upstream_run_ids = resolve_upstream_outputs(db, manifest, llm)
        run.input_fingerprint = compute_fingerprint(
            manifest, resolved, upstream_run_ids, run.request_payload
        )

        ctx = AgentContext(
            resources={
                tag: [_to_view(i) for i in items] for tag, items in resolved.items()
            },
            upstream=upstream_outputs,
            request=run.request_payload,
            llm=BoundLlm(client=llm, db=db, agent_run_id=run.id),
            allowed_tools=list(manifest.tools),
        )
        drafts = agent.run(ctx)

        # Structural validation: archetype conformance + source_refs present.
        # A malformed item is an agent bug, not a grounding failure — fail
        # the run loudly rather than dropping quietly.
        payloads = [validate_payload(manifest.output_archetype, d) for d in drafts]

        run.status = "verifying"
        db.commit()

        dropped_notes: list[str] = []
        for position, payload in enumerate(payloads):
            verdict = verify_item(db, llm, payload, run.id, DEFAULT_POLICY)
            db.add(
                OutputItem(
                    agent_run_id=run.id,
                    archetype=manifest.output_archetype,
                    position=position,
                    payload=payload,
                    verification_status=verdict.status,
                    verification_attempts=verdict.attempts,
                    verifier_note=verdict.note,
                )
            )
            if verdict.status == "dropped":
                dropped_notes.append(verdict.note or "unsupported")

        run.verification_summary = {
            "items_total": len(payloads),
            "items_dropped": len(dropped_notes),
            "notes": dropped_notes,
        }
        run.status = "succeeded"
        run.finished_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        db.rollback()
        run.status = "failed"
        run.error = str(exc)
        run.finished_at = datetime.utcnow()
        db.commit()
        logger.exception("Agent run %s (%s) failed", run.id, run.agent_id)
        return run

    # Only a run that materialized verified output publishes an update.
    _pending_updates.get().append(run.agent_id)
    return run
