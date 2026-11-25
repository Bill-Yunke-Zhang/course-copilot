"""Mock LLM service — a stand-in for Vertex AI's generateContent endpoint.

Point the app at it by setting LLM_BASE_URL=http://mock-llm:9000 in .env
(no Google sign-in needed), then start the stack with:

    docker compose --profile mock up

Why it exists:
- **Keyless development**: exercise the full running stack — ingestion,
  agent runs, verification, cost accounting — without a Google API key or
  spending credits.
- **Deterministic testing/grading**: unlike a real model, its output is
  controllable. Grading runs your containers against this contract with
  scripted responses, which is why honoring LLM_BASE_URL/LLM_MODEL from the
  environment is a graded requirement.

Behavior for a generateContent request, in order:
1. If a scripted response is queued (see /control below) whose `match`
   substring appears in the prompt (or has no `match`), return it.
2. Else, if the request declares a response schema, fabricate a minimal
   valid instance of it (strings "mock", numbers 0, booleans true, first
   value for enums; arrays empty, except arrays of enums which get one
   value — so tag classification visibly suggests something).
3. Else, return a plain text placeholder.

A scripted entry can also be a tool-calling turn, for requests that declare
tools — the model asks for calls instead of answering:
    {"match": "...", "function_calls": [{"name": "read_file",
                                         "args": {"path": "README.md"}}]}

Control API (used by tests and the grading harness):
    POST /control/enqueue  {"match": "optional substring",
                            "json": {...}}  or  {"text": "..."}
                            or  {"function_calls": [...]}
    POST /control/reset
    GET  /control/requests   → every generateContent request seen so far
"""

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Mock LLM")

_scripted: list[dict] = []
_requests: list[dict] = []


def fabricate(schema: Any) -> Any:
    """Build a minimal valid instance of a response schema.

    Accepts both shapes the SDK may send: Vertex-style ({"type": "OBJECT"})
    and standard JSON schema ({"type": "object"}).
    """
    if not isinstance(schema, dict):
        return None
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    for key in ("anyOf", "any_of", "oneOf"):
        options = schema.get(key)
        if options:
            non_null = [
                o
                for o in options
                if str(o.get("type", "")).lower() not in ("null", "")
                or "properties" in o
            ]
            return fabricate(non_null[0] if non_null else options[0])

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        schema_type = next((t for t in schema_type if t != "null"), "null")
    schema_type = str(schema_type or "").lower()

    if schema_type == "object" or "properties" in schema:
        return {k: fabricate(v) for k, v in schema.get("properties", {}).items()}
    if schema_type == "array":
        # Arrays default to empty EXCEPT arrays of enums, which return one
        # value — so e.g. the resource classifier's `tags` yields a visible
        # (if arbitrary) suggestion instead of nothing. Object arrays stay
        # empty because fabricated objects would carry dangling source_refs.
        items = schema.get("items")
        if isinstance(items, dict) and items.get("enum"):
            return [fabricate(items)]
        return []
    if schema_type == "string":
        return "mock"
    if schema_type == "integer":
        return 0
    if schema_type == "number":
        return 0.0
    if schema_type == "boolean":
        return True
    return None


def _prompt_text(body: dict) -> str:
    contents = body.get("contents", [])
    if isinstance(contents, str):
        return contents
    parts_text: list[str] = []
    for content in contents:
        for part in content.get("parts", []):
            if "text" in part:
                parts_text.append(part["text"])
    return "\n".join(parts_text)


def _declared_tools(body: dict) -> list[str]:
    names: list[str] = []
    for tool in body.get("tools") or []:
        for decl in (
            tool.get("functionDeclarations") or tool.get("function_declarations") or []
        ):
            names.append(decl.get("name", ""))
    return names


def _function_responses(body: dict) -> list[dict]:
    """Tool results the caller sent back, so tests can check what reached
    the model (e.g. that a refused call returned no file content)."""
    found: list[dict] = []
    contents = body.get("contents", [])
    if isinstance(contents, str):
        return found
    for content in contents:
        for part in content.get("parts", []):
            response = part.get("functionResponse") or part.get("function_response")
            if response:
                found.append(
                    {"name": response.get("name"), "response": response.get("response")}
                )
    return found


def _response_schema(body: dict) -> dict | None:
    config = body.get("generationConfig") or body.get("generation_config") or {}
    for key in (
        "responseSchema",
        "responseJsonSchema",
        "response_schema",
        "response_json_schema",
    ):
        if key in config:
            return config[key]
    return None


def _pop_scripted(prompt: str) -> dict | None:
    for index, entry in enumerate(_scripted):
        match = entry.get("match")
        if match is None or match in prompt:
            return _scripted.pop(index)
    return None


@app.post("/control/enqueue")
async def enqueue_response(request: Request) -> dict:
    entry = await request.json()
    if not any(key in entry for key in ("json", "text", "function_calls")):
        return JSONResponse(
            {"error": "provide 'json', 'text' or 'function_calls'"}, status_code=422
        )
    _scripted.append(entry)
    return {"queued": len(_scripted)}


@app.post("/control/reset")
def reset() -> dict:
    _scripted.clear()
    _requests.clear()
    return {"ok": True}


@app.get("/control/requests")
def list_requests() -> list[dict]:
    return _requests


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "mock-llm"}


@app.post("/{path:path}")
async def generate_content(path: str, request: Request):
    if not path.endswith("generateContent"):
        return JSONResponse({"error": f"unsupported path: {path}"}, status_code=404)

    body = await request.json()
    prompt = _prompt_text(body)
    schema = _response_schema(body)
    model = path.rsplit("/", 1)[-1].removesuffix(":generateContent")

    scripted = _pop_scripted(prompt)
    function_calls = (scripted or {}).get("function_calls") or []
    if function_calls:
        text = ""
        parts = [
            {"functionCall": {"name": c["name"], "args": c.get("args", {})}}
            for c in function_calls
        ]
    else:
        if scripted is not None:
            text = scripted.get("text") or json.dumps(scripted.get("json"))
        elif schema is not None:
            text = json.dumps(fabricate(schema))
        else:
            text = "This is a mock LLM response."
        parts = [{"text": text}]

    _requests.append(
        {
            "model": model,
            "prompt": prompt,
            "had_schema": schema is not None,
            "scripted": scripted is not None,
            "tools": _declared_tools(body),
            "function_responses": _function_responses(body),
        }
    )

    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": parts},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": max(1, len(prompt) // 4),
            "candidatesTokenCount": max(1, len(text) // 4),
            "totalTokenCount": max(2, len(prompt) // 4 + len(text) // 4),
        },
        "modelVersion": model,
    }
