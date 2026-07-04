"""Application configuration loaded from environment variables or a .env file.

No secrets live in code. Every deployment-specific value arrives via the
environment (RELAY_ prefix) — see .env.example for the documented shape.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="RELAY_",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Environment ─────────────────────────────────────────────────────────
    env: str = "dev"

    # ── Database ────────────────────────────────────────────────────────────
    # Admin URL — migrations only (owns schema, creates roles).
    database_url: str = "postgresql+psycopg://relay:relay@localhost:5432/relay"
    # App URL — API/worker. relay_app is subject to FORCED row-level security.
    app_database_url: str = (
        "postgresql+psycopg://relay_app:relay_app@localhost:5432/relay"
    )
    app_db_password: SecretStr = SecretStr("relay_app")

    # ── API ─────────────────────────────────────────────────────────────────
    # Tenant-bootstrap endpoint token. None ⇒ bootstrap endpoint disabled.
    admin_token: SecretStr | None = None

    # ── Send safety ─────────────────────────────────────────────────────────
    # Phase 0: no real sender exists. This flag is one of several independent
    # layers (worker check, DB trigger, absent provider integration) that all
    # must agree before a real send could ever occur.
    real_send_enabled: bool = False

    # ── Guardrails (dumb limits — the harness, not the planner) ────────────
    max_iterations_default: int = Field(default=100, ge=1)
    budget_units_default: float = Field(default=50.0, gt=0)

    # ── Compute routing cost stubs (units per task) ─────────────────────────
    cost_local_units: float = Field(default=0.1, ge=0)
    cost_hosted_units: float = Field(default=1.0, ge=0)
    cost_hosted_extended_units: float = Field(default=3.0, ge=0)

    # ── Compute backends (Phase 1A) ─────────────────────────────────────────
    # 'offline' is hermetic and the default everywhere; real backends are an
    # explicit deployment decision. No silent fallback between them.
    compute_local_backend: Literal["offline", "openai"] = "offline"
    compute_hosted_backend: Literal["offline", "anthropic"] = "offline"
    # Local tier: any OpenAI-compatible endpoint (Ollama default port).
    local_openai_base_url: str = "http://localhost:11434/v1"
    local_openai_model: str = ""
    local_openai_api_key: SecretStr = SecretStr("local-no-auth")
    # Hosted tier: Claude API. Model ID is deployment config, never code.
    anthropic_api_key: SecretStr | None = None
    hosted_model: str = ""
    compute_timeout_seconds: float = Field(default=60.0, gt=0)
    compute_max_output_tokens: int = Field(default=1024, ge=64)

    # ── Tenancy primitives ──────────────────────────────────────────────────
    # Dev default only; production uses a KMS-managed key (Phase 3).
    master_key: SecretStr = SecretStr("dev-master-key-not-for-production")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings (cached; clearable in tests)."""
    return Settings()
