"""The mock LLM service (mock_llm/) and the LLM_BASE_URL routing contract."""

import json

import pytest
from fastapi.testclient import TestClient

from mock_llm.main import app, fabricate

GENERATE_PATH = (
    "/v1beta1/publishers/google/models/gemini-3.5-flash-lite:generateContent"
)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        test_client.post("/control/reset")
        yield test_client


def _request_body(prompt: str, schema: dict | None = None) -> dict:
    body: dict = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    if schema is not None:
        body["generationConfig"] = {
            "responseMimeType": "application/json",
            "responseSchema": schema,
        }
    return body


def _text_of(response) -> str:
    return response.json()["candidates"][0]["content"]["parts"][0]["text"]


# ── Schema fabrication ───────────────────────────────────────────────────────


def test_fabricate_handles_vertex_uppercase_types():
    schema = {
        "type": "OBJECT",
        "properties": {
            "supported": {"type": "BOOLEAN"},
            "reason": {"type": "STRING"},
        },
    }
    assert fabricate(schema) == {"supported": True, "reason": "mock"}


def test_fabricate_handles_standard_json_schema():
    schema = {
        "type": "object",
        "properties": {
            "deadlines": {"type": "array", "items": {"type": "object"}},
            "count": {"type": "integer"},
        },
    }
    assert fabricate(schema) == {"deadlines": [], "count": 0}


def test_fabricate_picks_first_enum_value_and_non_null_anyof():
    assert fabricate({"enum": ["reading", "skip"]}) == "reading"
    assert fabricate({"anyOf": [{"type": "null"}, {"type": "integer"}]}) == 0


def test_fabricate_enum_arrays_suggest_one_value():
    # The classifier's `tags` field: an empty suggestion would make mock-mode
    # resources invisible to every agent, so enum arrays yield one value.
    schema = {
        "type": "ARRAY",
        "items": {"type": "STRING", "enum": ["schedule", "syllabus", "reading"]},
    }
    assert fabricate(schema) == ["schedule"]
    # object arrays still fabricate empty (no dangling source_refs)
    assert fabricate({"type": "ARRAY", "items": {"type": "OBJECT"}}) == []


# ── The generateContent endpoint ─────────────────────────────────────────────


def test_schema_request_gets_valid_json_and_usage(client):
    schema = {
        "type": "OBJECT",
        "properties": {"supported": {"type": "BOOLEAN"}, "reason": {"type": "STRING"}},
    }
    response = client.post(GENERATE_PATH, json=_request_body("judge this", schema))
    assert response.status_code == 200
    assert json.loads(_text_of(response)) == {"supported": True, "reason": "mock"}
    usage = response.json()["usageMetadata"]
    assert usage["promptTokenCount"] >= 1
    assert usage["candidatesTokenCount"] >= 1


def test_scripted_response_matches_prompt_substring(client):
    client.post(
        "/control/enqueue",
        json={"match": "deadline", "json": {"deadlines": []}},
    )
    unrelated = client.post(GENERATE_PATH, json=_request_body("classify a resource"))
    assert "mock LLM response" in _text_of(unrelated)

    matched = client.post(
        GENERATE_PATH, json=_request_body("extract every deadline from this")
    )
    assert json.loads(_text_of(matched)) == {"deadlines": []}
    # consumed — a second matching request falls through to defaults
    again = client.post(GENERATE_PATH, json=_request_body("deadline again"))
    assert "mock LLM response" in _text_of(again)


def test_requests_are_recorded_for_assertions(client):
    client.post(GENERATE_PATH, json=_request_body("hello there"))
    recorded = client.get("/control/requests").json()
    assert recorded[-1]["prompt"] == "hello there"
    assert recorded[-1]["model"] == "gemini-3.5-flash-lite"


def test_non_generate_paths_404(client):
    assert client.post("/v1beta1/models/x:embedContent", json={}).status_code == 404


# ── LLM_BASE_URL plumbing in the app ─────────────────────────────────────────


def test_client_kwargs_honor_llm_base_url():
    from app.config import Settings
    from app.core.llm import client_kwargs

    # _env_file=None keeps this hermetic: a developer's real .env (e.g. with
    # LLM_BASE_URL set for mock mode) must not leak into the test.
    plain = client_kwargs(
        Settings(_env_file=None, google_cloud_project="my-project-123", llm_base_url="")
    )
    assert "http_options" not in plain
    assert plain["project"] == "my-project-123"
    assert plain["location"] == "global"
    assert "api_key" not in plain  # wisc.edu blocks API keys; ADC signs in

    mocked = client_kwargs(
        Settings(
            _env_file=None, google_cloud_project="", llm_base_url="http://mock-llm:9000"
        )
    )
    assert mocked["http_options"] == {"base_url": "http://mock-llm:9000"}
    assert mocked["api_key"]  # placeholder auth so the SDK doesn't balk


def test_scripted_function_calls_come_back_as_a_tool_turn(client):
    client.post(
        "/control/enqueue",
        json={
            "match": "repo please",
            "function_calls": [{"name": "read_file", "args": {"path": "README.md"}}],
        },
    )
    body = _request_body("repo please")
    body["tools"] = [{"functionDeclarations": [{"name": "read_file"}]}]
    data = client.post(GENERATE_PATH, json=body).json()
    part = data["candidates"][0]["content"]["parts"][0]
    assert part["functionCall"] == {"name": "read_file", "args": {"path": "README.md"}}
    recorded = client.get("/control/requests").json()[-1]
    assert recorded["tools"] == ["read_file"]
