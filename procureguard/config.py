"""Application configuration.

Every external dependency is reachable through a port with two adapters: a
managed cloud adapter (CockroachDB Cloud, S3, Bedrock, SES, KMS, Temporal Cloud)
and a credential-free local adapter. That is what lets the complete fifteen-stage
pipeline run in CI and on a laptop while production keeps identical code paths.

Precedence: process environment > .env file > defaults below.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "dev", "staging", "prod"]

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.getenv("PROCUREGUARD_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ runtime
    app_env: Environment = "local"
    app_name: str = "procureguard"
    app_version: str = "1.0.0"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    default_tenant_id: str = "ACME-MFG"
    default_company_code: str = "1000"

    # ----------------------------------------------------------------- database
    # CockroachDB speaks the PostgreSQL wire protocol; psycopg3 is the driver.
    database_url: str = (
        "postgresql+psycopg://root@localhost:26257/procureguard?sslmode=disable"
    )
    db_pool_size: int = Field(default=10, ge=1, le=200)
    db_max_overflow: int = Field(default=20, ge=0, le=200)
    db_pool_timeout_seconds: int = Field(default=30, ge=1)
    db_pool_recycle_seconds: int = Field(default=1800, ge=60)
    db_statement_timeout_ms: int = Field(default=30_000, ge=1_000)
    db_echo: bool = False
    # CockroachDB uses optimistic concurrency; serialization failures (40001) are
    # normal and must be retried by the application with backoff.
    db_max_retries: int = Field(default=5, ge=0, le=20)
    db_retry_base_delay_ms: int = Field(default=50, ge=1)
    db_retry_max_delay_ms: int = Field(default=2_000, ge=10)

    # ------------------------------------------------------------- object store
    object_store_backend: Literal["local", "s3"] = "local"
    object_store_local_root: str = str(REPO_ROOT / "var" / "objectstore")
    s3_bucket: str = "procureguard-documents"
    s3_inbound_email_bucket: str = "procureguard-inbound-email"
    s3_kms_key_id: str = ""
    aws_region: str = "us-west-2"
    presigned_url_ttl_seconds: int = Field(default=900, ge=60, le=43_200)

    # --------------------------------------------------------------------- llm
    llm_backend: Literal["deterministic", "bedrock"] = "deterministic"
    bedrock_model_id: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    bedrock_fast_model_id: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    bedrock_embedding_model_id: str = "amazon.titan-embed-text-v2:0"
    bedrock_guardrail_id: str = ""
    bedrock_guardrail_version: str = ""
    bedrock_max_tokens: int = Field(default=4_096, ge=256, le=64_000)
    bedrock_temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    bedrock_timeout_seconds: int = Field(default=120, ge=5)
    bedrock_max_attempts: int = Field(default=4, ge=1, le=10)

    # ---------------------------------------------------------------- embedding
    embedding_backend: Literal["hashing", "bedrock"] = "hashing"
    embedding_dimensions: int = Field(default=1024, ge=64, le=4_096)
    vector_backend: Literal["auto", "native", "json"] = "auto"
    vector_search_probe_limit: int = Field(default=200, ge=10, le=5_000)

    # -------------------------------------------------------------------- email
    email_backend: Literal["filesystem", "smtp", "ses"] = "filesystem"
    email_outbox_dir: str = str(REPO_ROOT / "var" / "outbox")
    email_from_address: str = "procurement@procureguard.example.com"
    email_from_name: str = "ACME Manufacturing Procurement"
    email_reply_to_domain: str = "rfq.procureguard.example.com"
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = False
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_mailbox: str = "INBOX"
    # Hard safety switch. When false the mailroom writes an APPROVAL-PENDING
    # record instead of transmitting anything to a real supplier.
    allow_automated_email_send: bool = False

    # ----------------------------------------------------------------- temporal
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "procureguard-procurement"
    temporal_tls_cert_path: str = ""
    temporal_tls_key_path: str = ""
    temporal_api_key: str = ""
    temporal_max_concurrent_activities: int = Field(default=32, ge=1, le=512)
    temporal_workflow_execution_timeout_days: int = Field(default=180, ge=1, le=3_650)

    # ------------------------------------------------------- procurement policy
    max_rfq_reminders: int = Field(default=2, ge=0, le=10)
    reminder_interval_hours: int = Field(default=72, ge=1)
    quote_window_days: int = Field(default=10, ge=1, le=120)
    min_suppliers_per_rfq: int = Field(default=3, ge=1, le=25)
    max_suppliers_per_rfq: int = Field(default=6, ge=1, le=50)
    min_quotes_to_evaluate: int = Field(default=2, ge=1, le=25)
    max_negotiation_rounds: int = Field(default=3, ge=0, le=10)
    negotiation_target_savings_pct: float = Field(default=7.5, ge=0.0, le=90.0)
    single_source_justification_required: bool = True
    # Award value (base currency) above which a second approver is mandatory.
    dual_approval_threshold: float = Field(default=50_000.0, ge=0.0)
    executive_approval_threshold: float = Field(default=250_000.0, ge=0.0)
    price_increase_alert_pct: float = Field(default=10.0, ge=0.0)
    allow_automated_po_creation: bool = False

    # ---------------------------------------------------------------- financial
    base_currency: str = "USD"
    # Weighted average cost of capital, used to discount payment terms when
    # normalising commercially non-comparable offers.
    wacc_annual_pct: float = Field(default=9.0, ge=0.0, le=60.0)
    default_duty_rate_pct: float = Field(default=0.0, ge=0.0, le=100.0)
    inventory_carrying_cost_annual_pct: float = Field(default=18.0, ge=0.0, le=100.0)

    # ----------------------------------------------------------------- security
    auth_mode: Literal["dev", "oidc", "static"] = "dev"
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""
    oidc_jwks_cache_seconds: int = Field(default=3_600, ge=60)
    static_api_keys: str = ""  # "key:actor_id:role1|role2,key2:..."
    session_secret: str = "local-development-secret-do-not-use-in-production"
    encryption_backend: Literal["local", "kms"] = "local"
    kms_key_id: str = ""
    local_encryption_key: str = ""  # base64 32 bytes; derived from session_secret if blank
    sealed_bid_enabled: bool = True
    document_max_bytes: int = Field(default=64 * 1024 * 1024, ge=1024)

    # ------------------------------------------------------------ observability
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str = ""
    otel_service_name: str = "procureguard"
    metrics_enabled: bool = True

    # ------------------------------------------------------------------- seeding
    seed_random_seed: int = 20260810
    seed_scale: Literal["tiny", "small", "medium", "large", "xlarge"] = "medium"

    @model_validator(mode="after")
    def _validate_consistency(self) -> Settings:
        if self.min_suppliers_per_rfq > self.max_suppliers_per_rfq:
            raise ValueError("min_suppliers_per_rfq cannot exceed max_suppliers_per_rfq")
        if self.executive_approval_threshold < self.dual_approval_threshold:
            raise ValueError(
                "executive_approval_threshold must be >= dual_approval_threshold"
            )
        if self.app_env == "prod":
            if self.auth_mode == "dev":
                raise ValueError("auth_mode=dev is forbidden in prod")
            if self.session_secret.startswith("local-development"):
                raise ValueError("session_secret must be overridden in prod")
            if self.object_store_backend != "s3":
                raise ValueError("prod requires object_store_backend=s3")
            if self.encryption_backend != "kms":
                raise ValueError("prod requires encryption_backend=kms")
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env in ("staging", "prod")

    @property
    def daily_discount_rate(self) -> float:
        """Daily discount rate derived from WACC, for payment-term normalisation."""
        return (self.wacc_annual_pct / 100.0) / 365.0

    def temporal_workflow_id(self, case_id: str) -> str:
        return f"procurement-{case_id}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Test helper: drop the memoised Settings instance."""
    get_settings.cache_clear()
