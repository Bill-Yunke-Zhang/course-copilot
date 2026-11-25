"""The generic grounding verifier (spec §7).

One agent-agnostic check runs against every output item of every agent,
regardless of archetype: does the item's cited source_ref evidence genuinely
support its content? Items that fail are never silently included.

This works generically because every archetype is required to carry
source_refs — the verifier needs no understanding of what any agent does.

-------------------------------------------------------------------------
DEFAULT FAILURE POLICY — REPLACEABLE STUDENT DESIGN SURFACE
The starter ships the simplest defensible policy: retry the LLM judgment
once (judges are noisy), then drop the item and disclose the drop in the
run's verification_summary (surfaced in the UI). Your team is expected to
document and possibly replace this policy — e.g. escalating to the user,
or asking the agent to regenerate (the Quiz agent is required to
regenerate, per spec §9.1; that regeneration loop belongs in the agent /
your harness extension, on top of this generic check). Swap the policy by
replacing DEFAULT_POLICY.
-------------------------------------------------------------------------
"""

import json
import re
from dataclasses import dataclass

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.llm import LlmClient
from app.models import ResourceItem


class VerificationPolicy:
    """Retry once, then drop and disclose."""

    max_attempts: int = 2

    def should_retry(self, attempts_so_far: int) -> bool:
        return attempts_so_far < self.max_attempts


DEFAULT_POLICY = VerificationPolicy()


@dataclass
class ItemVerdict:
    status: str  # "verified" | "dropped"
    attempts: int
    note: str | None


class _JudgeResponse(BaseModel):
    supported: bool
    reason: str


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).lower().strip()


def _excerpt_found(excerpt: str, content: str) -> bool:
    return _normalize(excerpt) in _normalize(content)


def _claim_text(payload: dict) -> str:
    claim = {k: v for k, v in payload.items() if k != "source_refs"}
    return json.dumps(claim, ensure_ascii=False)


def _build_judge_prompt(payload: dict, evidence_blocks: list[str]) -> str:
    evidence = "\n\n".join(evidence_blocks)
    return f"""You are a strict grounding verifier for a course study assistant.
An AI agent produced the output item below and cited the evidence below to
support it. Decide whether the cited evidence genuinely supports the item's
content. The item is unsupported if it asserts anything material (a date, a
fact, a correct answer, a status) that the evidence does not establish.

OUTPUT ITEM (JSON):
{_claim_text(payload)}

CITED EVIDENCE:
{evidence}

Respond with whether the item is supported and a one-sentence reason."""


def verify_item(
    db: Session,
    llm: LlmClient,
    payload: dict,
    agent_run_id: int,
    policy: VerificationPolicy = DEFAULT_POLICY,
) -> ItemVerdict:
    """Verify a single output item payload (any archetype)."""
    evidence_blocks: list[str] = []
    for ref in payload.get("source_refs", []):
        item = db.get(ResourceItem, ref["resource_item_id"])
        if item is None:
            # Deterministic failure — a retry can't make a dangling citation
            # valid, so the policy is not consulted.
            return ItemVerdict(
                status="dropped",
                attempts=0,
                note=f"cites unknown resource item {ref['resource_item_id']}",
            )
        if ref.get("excerpt"):
            found = _excerpt_found(ref["excerpt"], item.content_text)
            evidence_blocks.append(
                f"[resource item {item.id}: {item.title or item.path}]\n"
                f"cited excerpt: {ref['excerpt']}\n"
                f"excerpt verbatim-present in source: {'yes' if found else 'NO'}"
            )
        if ref.get("evidence"):
            evidence_blocks.append(
                f"[resource item {item.id}: {item.title or item.path}]\n"
                f"structured evidence: {json.dumps(ref['evidence'])}"
            )

    prompt = _build_judge_prompt(payload, evidence_blocks)

    attempts = 0
    last_reason: str | None = None
    while True:
        attempts += 1
        result = llm.generate(
            db,
            purpose="verification",
            prompt=prompt,
            response_schema=_JudgeResponse,
            agent_run_id=agent_run_id,
        )
        verdict: _JudgeResponse = result.parsed
        if verdict.supported:
            return ItemVerdict(status="verified", attempts=attempts, note=None)
        last_reason = verdict.reason
        if not policy.should_retry(attempts):
            return ItemVerdict(status="dropped", attempts=attempts, note=last_reason)
