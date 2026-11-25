"""Pydantic schemas for the HTTP API."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ResourceCreate(BaseModel):
    kind: str  # file | url | repo
    locator: str
    title: str = ""
    credential_ref: str | None = None


class ResourceUpdate(BaseModel):
    title: str | None = None
    locator: str | None = None


class ItemTagsUpdate(BaseModel):
    tags: list[str]
    meta: dict | None = None


class BulkTagsUpdate(BaseModel):
    # Empty prefix means every item of the resource; otherwise all items at
    # or under this sub-path (e.g. a repo's "slides" folder).
    path_prefix: str = ""
    tags: list[str]


class ResourceItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    path: str
    title: str
    tags: list[str]
    meta: dict
    content_preview: str = ""


class ResourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    locator: str
    title: str
    discovered_from_id: int | None
    credential_ref: str | None
    ingest_status: str
    ingest_error: str | None
    created_at: datetime
    items: list[ResourceItemOut] = []


class RunRequest(BaseModel):
    payload: dict | None = None
    # Skip the cache and regenerate even if inputs are unchanged (the
    # explicit "run/check now" button semantics).
    force: bool = False


class OutputItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    archetype: str
    position: int
    payload: dict
    verification_status: str
    verifier_note: str | None


class AgentRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    agent_id: str
    status: str
    trigger: str
    from_cache: bool
    reused_run_id: int | None
    verification_summary: dict | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None


class ManifestOut(BaseModel):
    agent_id: str
    display_name: str
    description: str
    required_resource_tags: list[str]
    optional_upstream_agents: list[str]
    output_archetype: str
    trigger: str
    cacheable: bool
    dashboard_widget: bool
    tools: list[str] = []


class ToolCallOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    step: int
    name: str
    args: dict
    status: str
    result_preview: str
    created_at: datetime


class AgentOut(BaseModel):
    manifest: ManifestOut
    latest_run: AgentRunOut | None
    stale: bool


class DashboardCard(BaseModel):
    manifest: ManifestOut
    run: AgentRunOut | None
    items: list[OutputItemOut] = []
    stale: bool


class SourceRefResolved(BaseModel):
    resource_item_id: int
    resource_id: int | None
    title: str | None
    locator: str | None


class CostByAgent(BaseModel):
    agent_id: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


class CostSummary(BaseModel):
    total_cost_usd: float
    total_calls: int
    by_purpose: dict[str, float]
    by_agent: list[CostByAgent]
    runs_requested: int
    runs_from_cache: int
