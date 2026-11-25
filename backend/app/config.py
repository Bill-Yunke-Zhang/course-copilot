"""Application configuration, loaded from environment variables (.env)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# The fixed resource-tag vocabulary. This is NOT student-extensible — every
# team's implementation must produce exactly this tag space, because grading
# drops staff-authored agents into your harness and those agents declare
# `required_resource_tags` drawn from this list.
TAG_VOCABULARY: list[str] = [
    "schedule",
    "syllabus",
    "project-spec",
    "reading",
    "video",
    "reference",
    "slides",
    "activity",
    "personal-notes",
    "project-instructions",
    "project-code",
]

# The fixed output archetypes the frontend can render generically.
# Teams MAY add a new archetype for their student-designed agent (see
# docs/harness.md), but these five must keep rendering and verifying
# exactly as shipped — staff grading agents depend on them.
OUTPUT_ARCHETYPES: list[str] = [
    "timeline_item",
    "status_item",
    "qa_item",
    "card_item",
    "text_block",
]

# USD per 1M tokens (input, output) by model. Prices drift — check
# https://cloud.google.com/vertex-ai/generative-ai/pricing when adding a
# model. Unknown models log usage with cost 0 and a warning.
MODEL_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    # Introductory price through 2026-12-31; rises to (1.50, 7.50) on
    # 2027-01-01 per the pricing page — update before the next semester.
    "gemini-3.8-flash": (0.75, 3.75),
}


class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str = "mysql+pymysql://app_user:change_me_app@mysql:3306/copilot"
    redis_url: str = "redis://redis:6379/0"

    # Your Google Cloud project ID (not its display name) -- the project
    # billed to your education credit. There is no API key: wisc.edu's
    # Google Cloud policy blocks them. You sign in once with
    # `gcloud auth application-default login`, and the SDK reads that
    # sign-in (ADC) from the gcloud folder mounted into the container.
    google_cloud_project: str = ""
    google_cloud_location: str = "global"
    # When set, ALL LLM traffic is sent to this base URL instead of Google —
    # used to run against the local mock LLM service (mock_llm/) for
    # keyless development and grading. GRADED CONTRACT: the app must honor
    # LLM_BASE_URL and LLM_MODEL from the environment; grading runs your
    # containers against a mock endpoint with scripted responses.
    llm_base_url: str = ""
    llm_model: str = "gemini-3.5-flash-lite"
    # Optional per-purpose overrides; blank means "use llm_model".
    llm_model_classification: str = ""
    llm_model_generation: str = ""
    llm_model_verification: str = ""

    repo_clone_dir: str = "/data/repos"
    # SecretStore's AES key (app/core/secret_store.py): SECRET_STORE_KEY
    # (urlsafe base64 of 32 bytes) if set, else a key file generated on first
    # use on its own volume — never in the repo or the database.
    secret_store_key: str = ""
    secret_key_file: str = "/data/secrets/secret_store.key"
    # Where files picked in the browser land (a shared backend/worker volume).
    upload_dir: str = "/data/uploads"
    cors_origins: str = "http://localhost:5173"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def model_for(self, purpose: str) -> str:
        override = getattr(self, f"llm_model_{purpose}", "")
        return override or self.llm_model


@lru_cache
def get_settings() -> Settings:
    return Settings()
