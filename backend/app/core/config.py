from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo-root .env, resolved from this file's location so it is found
# regardless of the process's working directory (backend/ for local
# pytest/uvicorn, repo root for tooling run from there). Docker containers
# get real env vars injected directly and never rely on this file existing.
_REPO_ROOT_ENV = Path(__file__).resolve().parents[3] / ".env"


def _split_origins(value: str | list[str]) -> list[str]:
    if isinstance(value, list):
        return value
    return [origin.strip() for origin in value.split(",") if origin.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(_REPO_ROOT_ENV, ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["local", "test", "staging", "production"] = "local"
    app_name: str = "Email Outreach Platform"
    api_title: str = "Email Outreach API"
    api_version: str = "0.1.0"

    database_url: str = Field(default="", min_length=1)
    redis_url: str = Field(default="", min_length=1)
    backend_cors_origins: str = (
        "http://localhost:3000,http://127.0.0.1:3000"
    )

    platform_operator_emails: str = "operator@example.com,admin@example.com"
    platform_operator_key: str = ""

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "text"] = "json"
    service_name: str = "backend"

    # 0 = keep no idle connections (NullPool); see app/db/session.py.
    db_pool_size: int = Field(default=5, ge=0, le=50)
    db_max_overflow: int = Field(default=5, ge=0, le=50)
    # How long a request waits for a free pooled connection before failing. Long
    # enough to ride out a burst (page load + polling), short enough to fail
    # before the client gives up.
    db_pool_timeout_seconds: int = Field(default=15, ge=1, le=60)
    # Upper bound on opening a NEW connection (the pooler can accept the TCP
    # connection and then stall); without it one bad connect blocks a request.
    db_connect_timeout_seconds: int = Field(default=10, ge=1, le=60)
    db_statement_timeout_ms: int = Field(default=5_000, ge=100, le=60_000)
    readiness_timeout_seconds: float = Field(default=2.0, gt=0, le=10)

    scheduler_enabled: bool = True
    scheduler_poll_seconds: float = Field(default=5.0, gt=0, le=300)
    scheduler_batch_size: int = Field(default=50, ge=1, le=1000)
    scheduler_claim_lease_seconds: int = Field(default=300, ge=10, le=3600)
    outbox_publish_batch_size: int = Field(default=50, ge=1, le=1000)
    outbox_lease_seconds: int = Field(default=60, ge=5, le=600)
    outbox_max_attempts: int = Field(default=10, ge=1, le=100)

    # Follow-up progression (email step N>1 after a Wait). Off by default so that
    # deploying it never starts sending step 2 for campaigns that are already
    # running; enable it deliberately. See docs/adr/0009.
    sequence_progression_enabled: bool = False
    sequence_progression_interval_seconds: float = Field(default=30.0, gt=0, le=3600)
    sequence_progression_campaigns_per_run: int = Field(default=200, ge=1, le=5000)
    sequence_progression_batch_size: int = Field(default=100, ge=1, le=1000)

    # Hyper-personalized campaigns (docs/adr/0011-0013). Off by default: nothing
    # generates, fetches or calls the model until this is enabled deliberately.
    # Enabling it is the operator's assertion that lead data (never email,
    # phone or LinkedIn) may be sent to the model provider as a subprocessor.
    personalization_enabled: bool = False
    # The personalization worker and (ADR-0016) the API process hold the key for
    # interactive AI drafting; it never appears in task payloads, logs or
    # database rows.
    personalization_openai_api_key: SecretStr = SecretStr("")
    personalization_openai_base_url: str = "https://api.openai.com/v1"
    # No baked-in default model: it must be chosen explicitly.
    personalization_model: str = ""
    personalization_request_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    personalization_max_output_tokens: int = Field(default=1200, ge=100, le=8000)
    # Interactive authoring calls made from the API process (ADR-0016): company
    # analysis and reference-email drafting. The deadline bounds one model call;
    # a request may make a few (see personalization_draft_deadline_seconds).
    personalization_draft_timeout_seconds: float = Field(default=45.0, gt=0, le=120)
    personalization_draft_max_output_tokens: int = Field(
        default=4000, ge=500, le=16_000
    )
    personalization_analysis_max_output_tokens: int = Field(
        default=1500, ge=300, le=8000
    )
    # Hard ceiling on one whole drafting request, across its attempts.
    personalization_draft_deadline_seconds: float = Field(default=60.0, gt=0, le=180)
    # Overall ceiling for fetching and reading a company website.
    personalization_analysis_fetch_deadline_seconds: float = Field(
        default=20.0, gt=0, le=60
    )
    # Generate this long before a message's intended due time.
    personalization_lead_time_seconds: int = Field(default=3600, ge=0, le=86_400)
    personalization_max_attempts: int = Field(default=3, ge=1, le=10)
    personalization_max_transient_errors: int = Field(default=8, ge=1, le=50)
    personalization_lease_seconds: int = Field(default=300, ge=30, le=3600)
    personalization_dispatch_interval_seconds: float = Field(
        default=10.0, gt=0, le=3600
    )
    personalization_campaigns_per_run: int = Field(default=100, ge=1, le=5000)
    personalization_chunk_size: int = Field(default=10, ge=1, le=200)
    # Fewer usable facts than this means "thin context": the reference template
    # is sent with ordinary variable substitution instead of a generated email.
    personalization_min_facts: int = Field(default=2, ge=0, le=20)
    personalization_website_research_enabled: bool = True
    # Global requests-per-minute ceiling for model calls, and per-workspace daily
    # caps. Separate from send rate limiting: they never consume send capacity.
    personalization_rpm: int = Field(default=60, ge=1, le=100_000)
    personalization_daily_generation_cap: int = Field(
        default=2_000, ge=1, le=10_000_000
    )
    personalization_daily_preview_cap: int = Field(default=100, ge=1, le=100_000)
    personalization_daily_fetch_cap: int = Field(default=2_000, ge=1, le=10_000_000)
    personalization_preview_ttl_hours: int = Field(default=168, ge=1, le=720)

    # Phase 10: while false, the email.send Celery task keeps the Phase 9
    # placeholder behavior (validates the payload, never calls a provider).
    # Lets the sending-worker/rate-limiter modules land and be tested
    # end-to-end before any environment is allowed to perform a real send.
    sending_worker_enabled: bool = False
    rate_controller_reconcile_poll_seconds: float = Field(default=5.0, gt=0, le=300)

    # Reply Sync & Campaign Safety settings (Phase 13)
    reply_sync_enabled: bool = True
    reply_sync_interval_seconds: int = Field(default=300, ge=30, le=3600)
    reply_sync_lease_seconds: int = Field(default=120, ge=30, le=1800)
    reply_sync_max_pages_per_run: int = Field(default=10, ge=1, le=100)
    reply_sync_poll_seconds: float = Field(default=10.0, gt=0, le=300)
    # How far back the first full sync of a mailbox looks for replies.
    reply_sync_initial_horizon_days: int = Field(default=30, ge=1, le=365)
    # Upper bound of the exponential backoff applied after consecutive failures.
    reply_sync_max_backoff_seconds: int = Field(default=3600, ge=60, le=86_400)

    # Open tracking. The pixel is only injected when all three are set, so an
    # unconfigured environment can never send a broken or unsigned tracking URL.
    open_tracking_enabled: bool = False
    # Public origin that serves /api/v1/t/o/... (same origin as the app).
    tracking_base_url: str = ""
    tracking_signing_key: str = ""
    # A pixel fetch sooner than this after sending is a delivery-time scanner or
    # prefetch, not a person reading the email, and is not counted as an open.
    open_tracking_min_delay_seconds: int = Field(default=60, ge=0, le=3600)

    # Shared secrets for the provider webhooks. When unset the endpoint is
    # disabled (503) instead of accepting unauthenticated events.
    event_webhook_secret: str = ""
    gmail_webhook_secret: str = ""
    microsoft_webhook_client_state: str = ""

    # RevenueOS campaign intake (ADR-0019). The key is the caller's only
    # credential; the route acts as the configured user (a workspace member with
    # the Member role). The route is disabled (503) unless both are set.
    revenueos_intake_key: SecretStr = SecretStr("")
    revenueos_actor_user_id: str = ""
    # RevenueOS user provisioning (ADR-0020): lets the same key create accounts
    # and workspaces. Off by default; also needs SUPABASE_SERVICE_ROLE_KEY.
    revenueos_provisioning_enabled: bool = False
    # RevenueOS campaign start (ADR-0021): lets the same key activate a
    # campaign it stored, so real email is sent with no person reviewing
    # it. Off by default.
    revenueos_auto_start_enabled: bool = False

    supabase_url: str = Field(default="", min_length=1)
    supabase_jwt_secret: str = ""
    supabase_jwt_audience: str = "authenticated"
    supabase_service_role_key: str = ""
    supabase_storage_bucket: str = "imports"
    # Private bucket for sequence-step attachments and inline images.
    supabase_attachments_bucket: str = "email-attachments"

    import_max_file_bytes: int = Field(default=10_000_000, ge=1)
    import_max_rows: int = Field(default=50_000, ge=1)
    import_max_columns: int = Field(default=50, ge=1)
    import_max_field_chars: int = Field(default=4_000, ge=1)
    import_batch_size: int = Field(default=500, ge=1)
    import_preview_row_limit: int = Field(default=20, ge=1)
    import_lease_ttl_seconds: int = Field(default=120, ge=1)
    import_recovery_poll_seconds: float = Field(default=30.0, gt=0)

    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/api/v1/mailboxes/connect/gmail/callback"

    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""
    microsoft_redirect_uri: str = (
        "http://localhost:8000/api/v1/mailboxes/connect/microsoft/callback"
    )
    # "common" supports both work/school (Azure AD) and personal Microsoft
    # accounts. Restrict to "organizations" or a specific tenant GUID only
    # if the product intentionally narrows supported account types.
    microsoft_tenant: str = "common"

    mailbox_encryption_key: str = ""
    mailbox_encryption_key_id: str = "v1"
    frontend_base_url: str = "http://localhost:3000"

    smtp_dns_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    smtp_connect_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    smtp_command_timeout_seconds: float = Field(default=15.0, gt=0, le=60)

    @field_validator("log_level", mode="before")
    @classmethod
    def uppercase_log_level(cls, value: str) -> str:
        return value.upper()

    @field_validator("database_url", mode="after")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        """Force the psycopg (v3) driver, the only Postgres driver this
        project depends on. Supabase's dashboard copies connection strings as
        plain postgresql:// / postgres://, which SQLAlchemy otherwise resolves
        to the psycopg2 driver by default and which is not installed here.
        """
        if value.startswith("postgresql://"):
            return "postgresql+psycopg://" + value[len("postgresql://") :]
        if value.startswith("postgres://"):
            return "postgresql+psycopg://" + value[len("postgres://") :]
        return value

    @model_validator(mode="after")
    def validate_cors(self) -> Settings:
        if not self.cors_origins:
            raise ValueError("At least one CORS origin is required")
        if self.app_env == "production" and "*" in self.cors_origins:
            raise ValueError("Wildcard CORS origins are not allowed in production")
        return self

    @model_validator(mode="after")
    def validate_personalization(self) -> Settings:
        # The key is checked where it is used: at personalization worker start
        # (require_personalization_worker_ready) and when an AI drafting request
        # arrives in the API (personalization_drafting_ready), so an API without
        # the key still serves everything else.
        if self.personalization_enabled and not self.personalization_model.strip():
            raise ValueError("PERSONALIZATION_ENABLED requires PERSONALIZATION_MODEL")
        return self

    @model_validator(mode="after")
    def validate_revenueos_intake(self) -> Settings:
        # The key is the only credential on that route, so a weak one or a
        # malformed acting user must stop the process at boot, not at first use.
        key = self.revenueos_intake_key.get_secret_value()
        if key and len(key) < 32:
            raise ValueError("REVENUEOS_INTAKE_KEY must be at least 32 characters")
        self.revenueos_actor_user_id = self.revenueos_actor_user_id.strip()
        if self.revenueos_actor_user_id:
            try:
                UUID(self.revenueos_actor_user_id)
            except ValueError as exc:
                raise ValueError(
                    "REVENUEOS_ACTOR_USER_ID must be a user id (UUID)"
                ) from exc
        return self

    def require_personalization_worker_ready(self) -> None:
        """Called at personalization worker start: refuse to run without the key."""
        if not self.personalization_enabled:
            return
        if not self.personalization_openai_api_key.get_secret_value():
            raise RuntimeError(
                "PERSONALIZATION_ENABLED requires PERSONALIZATION_OPENAI_API_KEY "
                "in the personalization worker environment"
            )

    @property
    def personalization_drafting_ready(self) -> bool:
        """Whether this process may make interactive AI drafting calls (ADR-0016)."""
        return bool(
            self.personalization_enabled
            and self.personalization_model.strip()
            and self.personalization_openai_api_key.get_secret_value()
        )

    @property
    def cors_origins(self) -> list[str]:
        return _split_origins(self.backend_cors_origins)

    @property
    def microsoft_authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.microsoft_tenant}"

    @property
    def supabase_jwt_issuer(self) -> str:
        return f"{self.supabase_url.rstrip('/')}/auth/v1"

    @property
    def supabase_jwks_url(self) -> str:
        return f"{self.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"

    @classmethod
    def current(cls) -> Settings:
        return get_settings()


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
