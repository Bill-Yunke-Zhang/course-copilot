"""LLM access layer: Google Cloud Vertex AI via the google-genai SDK.

Every call goes through `LlmClient.generate` (or `generate_with_tools`, for
a tool-calling turn), which records an LlmCall row
(token usage + computed cost) — this is the raw data behind the §13 cost
report. Nothing else in the codebase may call the SDK directly.

Tests replace the client with a fake by monkeypatching `LlmClient._call_api`
or passing a subclass; see tests/unit/conftest.py.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import MODEL_PRICES_PER_MTOK, get_settings
from app.models import LlmCall

logger = logging.getLogger(__name__)

# purposes: classification | generation | verification
VALID_PURPOSES = {"classification", "generation", "verification"}


@dataclass
class LlmResult:
    text: str
    parsed: Any  # pydantic instance when response_schema was given, else None
    input_tokens: int
    output_tokens: int
    model: str


@dataclass
class FunctionCall:
    """One tool call the model asked for. The model never runs it — your
    code decides whether to, and sends back the result."""

    name: str
    args: dict


@dataclass
class ToolTurn:
    """One model turn in a tool-calling conversation.

    `function_calls` is empty when the model answered in plain text, which
    is how a tool loop knows the model is done.
    """

    text: str
    function_calls: list[FunctionCall]
    input_tokens: int
    output_tokens: int
    model: str


# A tool-calling conversation is a list of plain dicts, oldest first, so
# harness code never touches SDK types:
#   {"role": "user", "text": "..."}
#   {"role": "model", "text": "...", "function_calls": [FunctionCall, ...]}
#   {"role": "tool", "name": "<tool name>", "response": {...}}
Message = dict


def _to_sdk_contents(messages: list[Message]) -> list:
    from google.genai import types

    contents = []
    for message in messages:
        role = message["role"]
        if role == "user":
            contents.append(
                types.Content(role="user", parts=[types.Part(text=message["text"])])
            )
        elif role == "model":
            parts = [types.Part(text=message["text"])] if message.get("text") else []
            parts += [
                types.Part(function_call=types.FunctionCall(name=c.name, args=c.args))
                for c in message.get("function_calls", [])
            ]
            contents.append(types.Content(role="model", parts=parts))
        elif role == "tool":
            contents.append(
                types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            function_response=types.FunctionResponse(
                                name=message["name"], response=message["response"]
                            )
                        )
                    ],
                )
            )
        else:
            raise ValueError(f"unknown message role {role!r}")
    return contents


def compute_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    prices = MODEL_PRICES_PER_MTOK.get(model)
    if prices is None:
        logger.warning("No price entry for model %r — logging cost as 0", model)
        return 0.0
    return (input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000


def usage_tokens(usage) -> tuple[int, int]:
    """(input_tokens, output_tokens) from a response's usage_metadata.

    Thinking models (gemini-3.x Flash and Flash-Lite included) report hidden
    reasoning in thoughts_token_count, separately from candidates_token_count,
    but bill it at the output rate — so both count as output. Kept SDK-free
    so it's testable without google-genai installed.
    """
    input_tokens = usage.prompt_token_count or 0
    output_tokens = (usage.candidates_token_count or 0) + (
        getattr(usage, "thoughts_token_count", None) or 0
    )
    return input_tokens, output_tokens


def client_kwargs(settings) -> dict:
    """Constructor arguments for genai.Client, honoring the env contract.

    LLM_BASE_URL redirects all LLM traffic (e.g. to the local mock service
    in mock_llm/ — keyless development, and how grading scripts the model's
    responses). Kept as a plain-dict helper so the routing logic is testable
    without the SDK installed.
    """
    if settings.llm_base_url:
        # The mock endpoint ignores auth; a placeholder key stops the SDK
        # from looking for a Google sign-in it doesn't need.
        return {
            "vertexai": True,
            "api_key": "mock",
            "http_options": {"base_url": settings.llm_base_url},
        }
    # Real Vertex AI, billed to GOOGLE_CLOUD_PROJECT. No API key (wisc.edu
    # blocks them): the SDK signs in with Application Default Credentials.
    return {
        "vertexai": True,
        "project": settings.google_cloud_project,
        "location": settings.google_cloud_location,
    }


class LlmClient:
    """Thin wrapper over google-genai with per-call usage/cost logging."""

    def __init__(self) -> None:
        self._client = None

    def _get_client(self):
        if self._client is None:
            # Imported lazily so tests never need the SDK configured.
            from google import genai

            self._client = genai.Client(**client_kwargs(get_settings()))
        return self._client

    def _call_api(
        self, model: str, prompt: str, response_schema: type[BaseModel] | None
    ) -> LlmResult:
        from google.genai import types

        config = types.GenerateContentConfig(temperature=0.0)
        if response_schema is not None:
            config.response_mime_type = "application/json"
            config.response_schema = response_schema

        response = self._get_client().models.generate_content(
            model=model, contents=prompt, config=config
        )
        input_tokens, output_tokens = usage_tokens(response.usage_metadata)
        return LlmResult(
            text=response.text or "",
            parsed=response.parsed if response_schema is not None else None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
        )

    def _call_api_tools(
        self, model: str, messages: list[Message], tools: list[dict]
    ) -> ToolTurn:
        from google.genai import types

        config = types.GenerateContentConfig(
            temperature=0.0,
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=tool["name"],
                            description=tool["description"],
                            parameters_json_schema=tool["parameters"],
                        )
                        for tool in tools
                    ]
                )
            ],
            # The harness runs tools itself; the SDK must never call them.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        )
        response = self._get_client().models.generate_content(
            model=model, contents=_to_sdk_contents(messages), config=config
        )
        content = response.candidates[0].content if response.candidates else None
        parts = (content.parts if content else None) or []
        input_tokens, output_tokens = usage_tokens(response.usage_metadata)
        return ToolTurn(
            text="".join(p.text for p in parts if p.text),
            function_calls=[
                FunctionCall(
                    name=p.function_call.name, args=dict(p.function_call.args or {})
                )
                for p in parts
                if p.function_call
            ],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
        )

    def generate(
        self,
        db: Session,
        purpose: str,
        prompt: str,
        response_schema: type[BaseModel] | None = None,
        agent_run_id: int | None = None,
        resource_id: int | None = None,
    ) -> LlmResult:
        if purpose not in VALID_PURPOSES:
            raise ValueError(f"Unknown LLM purpose: {purpose!r}")
        model = get_settings().model_for(purpose)
        result = self._call_api(model, prompt, response_schema)
        self._log_call(db, purpose, result, agent_run_id, resource_id)
        return result

    def generate_with_tools(
        self,
        db: Session,
        purpose: str,
        messages: list[Message],
        tools: list[dict],
        agent_run_id: int | None = None,
    ) -> ToolTurn:
        """One model turn with tools declared.

        `tools` are declarations only: {"name", "description", "parameters"}
        (a JSON Schema object). The model may answer with function_calls;
        executing them, and sending results back as {"role": "tool", ...}
        messages, is the caller's job (see app/core/tool_loop.py).
        """
        if purpose not in VALID_PURPOSES:
            raise ValueError(f"Unknown LLM purpose: {purpose!r}")
        model = get_settings().model_for(purpose)
        turn = self._call_api_tools(model, messages, tools)
        self._log_call(db, purpose, turn, agent_run_id, None)
        return turn

    def _log_call(
        self,
        db: Session,
        purpose: str,
        result: LlmResult | ToolTurn,
        agent_run_id: int | None,
        resource_id: int | None,
    ) -> None:
        db.add(
            LlmCall(
                purpose=purpose,
                model=result.model,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cost_usd=compute_cost_usd(
                    result.model, result.input_tokens, result.output_tokens
                ),
                agent_run_id=agent_run_id,
                resource_id=resource_id,
            )
        )
        db.commit()


@dataclass
class BoundLlm:
    """The LLM handle agents receive in their AgentContext.

    Binds the db session and agent_run_id so an agent's calls are
    automatically attributed to its run in the cost log. Agents only choose
    the prompt and (optionally) a response schema — or, for a tool-calling
    turn, the messages and tool declarations.
    """

    client: LlmClient
    db: Session
    agent_run_id: int

    def generate(
        self, prompt: str, response_schema: type[BaseModel] | None = None
    ) -> LlmResult:
        return self.client.generate(
            self.db,
            purpose="generation",
            prompt=prompt,
            response_schema=response_schema,
            agent_run_id=self.agent_run_id,
        )

    def generate_with_tools(
        self, messages: list[Message], tools: list[dict]
    ) -> ToolTurn:
        return self.client.generate_with_tools(
            self.db,
            purpose="generation",
            messages=messages,
            tools=tools,
            agent_run_id=self.agent_run_id,
        )


def parse_json_lenient(text: str) -> Any:
    """Parse model output as JSON, tolerating markdown code fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        cleaned = cleaned.rsplit("```", 1)[0]
    return json.loads(cleaned)
