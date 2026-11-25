"""The agent manifest contract (spec §5) and the context agents run with.

An agent is a Python module in `app/agents/` that defines:

    MANIFEST: AgentManifest           # module-level constant
    def run(ctx: AgentContext) -> list[dict]   # output item payloads

The harness discovers agents by scanning that directory (app/core/registry.py)
— registering a new agent must NEVER require editing core harness code. Each
returned dict must conform to the manifest's declared `output_archetype`
(app/core/archetypes.py) and carry at least one source_ref; the harness
validates and then verifies every item before anything reaches the UI.
"""

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from app.config import TAG_VOCABULARY
from app.core.archetypes import known_archetypes
from app.core.llm import BoundLlm
from app.core.tools import KNOWN_TOOLS


class AgentManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: str  # unique, stable
    display_name: str
    # Natural-language; human-readable and potentially usable by an LLM
    # router in future extensions.
    description: str
    # Drawn only from the fixed vocabulary in app/config.py.
    required_resource_tags: list[str]
    # agent_ids this agent may consume output from, if available at run time.
    optional_upstream_agents: list[str] = []
    output_archetype: str
    trigger: Literal["on_resource_change", "on_demand", "on_upstream_update"]
    # True: reuse the last verified run if nothing relevant changed.
    # False: always regenerate on request.
    cacheable: bool
    # True: contributes a card to the "This Week" dashboard.
    dashboard_widget: bool
    # Tools this agent's tool loop may run (app/core/tools.py). Empty: the
    # agent calls no tools. The loop refuses any tool not listed here.
    tools: list[str] = []

    @field_validator("required_resource_tags")
    @classmethod
    def _tags_in_vocabulary(cls, tags: list[str]) -> list[str]:
        unknown = [t for t in tags if t not in TAG_VOCABULARY]
        if unknown:
            raise ValueError(
                f"tags {unknown} are not in the fixed vocabulary {TAG_VOCABULARY}"
            )
        return tags

    @field_validator("tools")
    @classmethod
    def _tools_known(cls, tools: list[str]) -> list[str]:
        unknown = [t for t in tools if t not in KNOWN_TOOLS]
        if unknown:
            raise ValueError(f"unknown tools {unknown}; known: {sorted(KNOWN_TOOLS)}")
        return tools

    @field_validator("output_archetype")
    @classmethod
    def _archetype_known(cls, archetype: str) -> str:
        if archetype not in known_archetypes():
            raise ValueError(
                f"unknown output_archetype {archetype!r}; known: {known_archetypes()}"
            )
        return archetype


@dataclass
class ResourceItemView:
    """Read-only view of a resource item, as agents see it."""

    id: int
    resource_id: int
    kind: str  # file | url | repo
    locator: str
    path: str
    title: str
    content: str
    tags: list[str]
    meta: dict


@dataclass
class AgentContext:
    """Everything an agent gets to work with — nothing else.

    resources: the CURRENT resource items carrying each of the agent's
        required tags (a tag maps to [] if nothing carries it — agents must
        degrade gracefully, not crash).
    upstream: verified output payloads of each available upstream agent,
        keyed by agent_id.
    request: the on_demand request payload (e.g. free text), if any.
    llm: LLM handle; calls are logged against this run automatically.
    allowed_tools: the manifest's `tools` — the only tools the tool loop
        (app/core/tool_loop.py) will run for this agent.
    """

    resources: dict[str, list[ResourceItemView]]
    upstream: dict[str, list[dict]] = field(default_factory=dict)
    request: dict | None = None
    llm: BoundLlm | None = None
    allowed_tools: list[str] = field(default_factory=list)

    def all_items(self) -> list[ResourceItemView]:
        """All resolved items, deduplicated, in stable id order."""
        seen: dict[int, ResourceItemView] = {}
        for items in self.resources.values():
            for item in items:
                seen[item.id] = item
        return [seen[k] for k in sorted(seen)]
