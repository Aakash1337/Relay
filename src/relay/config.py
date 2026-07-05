"""Application configuration loaded from environment variables or a .env file.

No secrets live in code. Every deployment-specific value arrives via the
environment (RELAY_ prefix) — see .env.example for the documented shape.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
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
    # One of several independent layers (worker check, DB trigger, provider
    # registry, eligibility attests) that ALL must agree before a real send
    # can occur. Default false; nothing else matters while it is false.
    real_send_enabled: bool = False

    # ── Real sender (Phase 1C — §6 decision record) ────────────────────────
    # 'none' keeps real sending structurally absent (the Phase 0 posture).
    # 'ses' is the pilot sender: SES SANDBOX, self-to-self only. The
    # Smartlead enrollment adapter is deliberately deferred (see
    # docs/decisions/sending-provider.md).
    sender_provider: Literal["none", "ses"] = "none"
    # Region: read the STANDARD name AWS_REGION (which boto3 also auto-loads)
    # so there is one source of truth; RELAY_AWS_REGION still accepted.
    aws_region: str = Field(
        default="",
        validation_alias=AliasChoices("AWS_REGION", "RELAY_AWS_REGION"),
    )
    # From address: RELAY_SES_FROM is the canonical name; the older
    # RELAY_SES_FROM_ADDRESS is still accepted.
    ses_from_address: str = Field(
        default="",
        validation_alias=AliasChoices("RELAY_SES_FROM", "RELAY_SES_FROM_ADDRESS"),
    )
    ses_configuration_set: str = ""
    # Pilot recipient allowlist (RELAY_PILOT_RECIPIENTS): comma-separated
    # addresses that a REAL send may target during the pilot. This is a
    # structural gate on TOP of test_consent + SES sandbox — real sends go
    # ONLY to these inboxes (checked in eligibility AND at the last hop).
    # Empty ⇒ no real send is possible (fail-closed).
    pilot_recipients: str = ""
    #: List-Unsubscribe mailto target; required for real-mode eligibility.
    unsubscribe_mailto: str = ""
    #: Optional https one-click unsubscribe endpoint (RFC 8058). Only when
    #: this is set does the sender advertise List-Unsubscribe-Post:
    #: One-Click — mailto alone cannot honor a one-click POST.
    unsubscribe_url: str = ""
    # Operator attestations for the real-mode eligibility checks. Each one
    # is a recorded human claim ("I verified this"), not a guess by code.
    sender_identity_approved: bool = False
    sender_domain_authenticated: bool = False
    #: Path/anchor of the §6 decision record authorizing the provider.
    provider_terms_record: str = ""
    # Volume + reputation caps for the pilot.
    real_send_daily_cap: int = Field(default=5, ge=0)
    bounce_complaint_window_days: int = Field(default=7, ge=1)
    max_bounces_complaints_in_window: int = Field(default=2, ge=0)
    # ── Deliverability pacing (Phase 3) — each is OFF (0) by default ───────
    # Pacing failures DEFER a queued job to a later worker tick; they never
    # block it terminally (unlike the caps above, which are §6 hard stops).
    #: Rolling-hour cap on real sends per (tenant, mailbox); 0 disables.
    real_send_hourly_cap: int = Field(default=0, ge=0)
    #: Minimum seconds between two real sends from one mailbox; 0 disables.
    real_send_min_spacing_seconds: int = Field(default=0, ge=0)
    #: Warmup ramp: on day N since the tenant's first real send the
    #: effective daily cap is min(real_send_daily_cap, start + increment*N).
    #: start=0 disables the ramp entirely.
    warmup_daily_start: int = Field(default=0, ge=0)
    warmup_daily_increment: int = Field(default=0, ge=0)
    # SNS event ingestion (webhook token and/or SQS polling).
    ses_webhook_token: SecretStr | None = None
    sqs_queue_url: str = ""

    # ── Guardrails (dumb limits — the harness, not the planner) ────────────
    max_iterations_default: int = Field(default=100, ge=1)
    budget_units_default: float = Field(default=50.0, gt=0)

    # ── Compute routing cost stubs (units per task) ─────────────────────────
    cost_local_units: float = Field(default=0.1, ge=0)
    cost_hosted_units: float = Field(default=1.0, ge=0)
    cost_hosted_extended_units: float = Field(default=3.0, ge=0)

    # ── Compute backends (Phase 1A) ─────────────────────────────────────────
    # Each tier picks a provider + a model, independently — swapping the
    # orchestrator from Gemini to Claude (or the workhorse from Gemma to a
    # local Ollama model) is a two-line .env change, never a code change.
    # 'offline' is hermetic and the default everywhere; real backends are an
    # explicit deployment decision. No silent fallback between them.
    compute_local_backend: Literal["offline", "openai", "google", "anthropic"] = (
        "offline"
    )
    compute_hosted_backend: Literal["offline", "openai", "google", "anthropic"] = (
        "offline"
    )
    # Per-tier model IDs. Deployment config, never code.
    local_model: str = ""
    hosted_model: str = ""
    # Provider: any OpenAI-compatible endpoint (Ollama default port).
    openai_compat_base_url: str = "http://localhost:11434/v1"
    openai_compat_api_key: SecretStr = SecretStr("local-no-auth")
    # Provider: Google Gemini API (Gemini + Gemma models).
    google_api_key: SecretStr | None = None
    google_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    # Provider: Claude API.
    anthropic_api_key: SecretStr | None = None
    compute_timeout_seconds: float = Field(default=60.0, gt=0)
    compute_max_output_tokens: int = Field(default=1024, ge=64)

    # USD per guardrail cost unit, for the economics projection. 0 (default)
    # means "not calibrated" and the USD figure is omitted, not guessed.
    cost_unit_usd: float = Field(default=0.0, ge=0)

    # ── Rate limiting & bounded retries (Phase 2) ──────────────────────────
    # Requests/second per external target; 0 disables that bucket. Waits
    # beyond max_wait raise Backpressure (work parks instead of queueing).
    rate_limit_local_rps: float = Field(default=0.0, ge=0)
    rate_limit_hosted_rps: float = Field(default=0.0, ge=0)
    rate_limit_crm_rps: float = Field(default=0.0, ge=0)
    rate_limit_max_wait_seconds: float = Field(default=30.0, gt=0)
    # Bounded retry for TRANSIENT compute failures only (never refusals,
    # never invalid output, never a different provider).
    compute_retry_attempts: int = Field(default=2, ge=0)
    compute_retry_base_seconds: float = Field(default=0.5, ge=0)

    # ── Alerting thresholds (Phase 2) ───────────────────────────────────────
    alert_spend_units_per_hour: float = Field(default=100.0, gt=0)
    alert_failure_streak: int = Field(default=3, ge=2)
    alert_queue_stale_seconds: float = Field(default=600.0, gt=0)
    #: Optional webhook (Slack/n8n/…) for fired alerts; empty = log only.
    alert_webhook_url: str = ""

    # ── Crash recovery (Phase 2) ────────────────────────────────────────────
    # A pipeline run still 'running' (or a send job still 'sending') after
    # this many seconds is an orphan from a crash — no legitimate per-lead
    # run or single send takes anywhere near this long.
    recovery_stale_after_seconds: float = Field(default=300.0, gt=0)

    # ── Pipeline decision thresholds (Phase 1A) ─────────────────────────────
    # Leads scoring below this are scored_rejected. Default sits below the
    # offline backend's floor (0.35) so hermetic runs qualify by default;
    # raise it per deployment to make the gate bite.
    fit_score_threshold: float = Field(default=0.3, ge=0, le=1)

    # ── CRM sync seam (Phase 1A) ────────────────────────────────────────────
    # 'none' disables sync entirely; 'memory' is the hermetic in-process
    # adapter (tests/dev); 'espo' targets an EspoCRM instance.
    crm_backend: Literal["none", "memory", "espo"] = "none"
    espo_base_url: str = ""
    espo_api_key: SecretStr | None = None

    # ── Tenancy primitives ──────────────────────────────────────────────────
    # Dev default only; production uses a KMS-managed key (Phase 3).
    master_key: SecretStr = SecretStr("dev-master-key-not-for-production")

    def pilot_recipient_addresses(self) -> tuple[str, ...]:
        """The parsed pilot allowlist (comma-separated, trimmed, no blanks)."""
        return tuple(
            addr.strip() for addr in self.pilot_recipients.split(",") if addr.strip()
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings (cached; clearable in tests)."""
    return Settings()
