"""The generic grounding verifier and its default retry-once policy."""

from app.core.verifier import DEFAULT_POLICY, verify_item
from tests.conftest import FakeLlm, make_resource


def _payload(item_id: int, excerpt: str) -> dict:
    return {
        "date": "2026-11-17",
        "title": "P4 due",
        "description": "due at 11:59 PM",
        "source_refs": [{"resource_item_id": item_id, "excerpt": excerpt}],
    }


def _resource(db):
    return make_resource(
        db,
        locator="https://example.edu/schedule.html",
        items=[
            {
                "title": "Schedule",
                "content": "P4 due Tuesday, November 17 at 11:59 PM.",
                "tags": ["schedule"],
            }
        ],
    )


def test_supported_item_verifies_first_try(db):
    item = _resource(db).items[0]
    llm = FakeLlm({"_JudgeResponse": {"supported": True, "reason": "ok"}})
    verdict = verify_item(db, llm, _payload(item.id, "November 17"), agent_run_id=None)
    assert verdict.status == "verified"
    assert verdict.attempts == 1


def test_noisy_judge_gets_one_retry(db):
    item = _resource(db).items[0]
    llm = FakeLlm(
        {
            "_JudgeResponse": [
                {"supported": False, "reason": "flaky first judgment"},
                {"supported": True, "reason": "supported after all"},
            ]
        }
    )
    verdict = verify_item(db, llm, _payload(item.id, "November 17"), agent_run_id=None)
    assert verdict.status == "verified"
    assert verdict.attempts == 2


def test_unsupported_item_dropped_and_disclosed(db):
    item = _resource(db).items[0]
    llm = FakeLlm(
        {"_JudgeResponse": {"supported": False, "reason": "date not in source"}}
    )
    verdict = verify_item(db, llm, _payload(item.id, "December 25"), agent_run_id=None)
    assert verdict.status == "dropped"
    assert verdict.attempts == DEFAULT_POLICY.max_attempts
    assert "date not in source" in verdict.note


def test_dangling_citation_dropped_without_retry(db):
    _resource(db)
    llm = FakeLlm({"_JudgeResponse": {"supported": True, "reason": "unreachable"}})
    verdict = verify_item(db, llm, _payload(999_999, "x"), agent_run_id=None)
    assert verdict.status == "dropped"
    assert verdict.attempts == 0
    assert llm.calls == []  # deterministic failure — no LLM spend


def test_judge_prompt_flags_excerpt_not_found_verbatim(db):
    item = _resource(db).items[0]
    llm = FakeLlm({"_JudgeResponse": {"supported": True, "reason": "ok"}})
    verify_item(db, llm, _payload(item.id, "totally absent text"), agent_run_id=None)
    assert "excerpt verbatim-present in source: NO" in llm.calls[0]["prompt"]
