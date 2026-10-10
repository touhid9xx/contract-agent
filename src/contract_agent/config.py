"""Application configuration — pydantic-settings, fail-fast validation.

Why pydantic-settings?
    - Type-safe env loading with defaults
    - Validators run at startup → app refuses to boot with bad config
    - Nested settings groups keep .env flat but code structured
"""

from __future__ import annotations

import base64
from functools import lru_cache
from typing import Literal
from urllib.parse import quote

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Root settings — all env vars."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- APP ----------
    app_name: str = Field(default="contract-agent", alias="APP_NAME")
    app_env: Literal["dev", "staging", "prod", "test"] = Field(default="dev", alias="APP_ENV")
    app_debug: bool = Field(default=True, alias="APP_DEBUG")
    # nosec B104 — `0.0.0.0` is a DEV DEFAULT. In production, `APP_HOST` is
    # overridden via `.env` or container env to bind to a specific address.
    # Docker containers legitimately bind `0.0.0.0` to accept external
    # traffic (port is restricted at the host firewall / reverse proxy level).
    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")  # nosec B104
    app_port: int = Field(default=8000, alias="APP_PORT")
    app_log_level: str = Field(default="INFO", alias="APP_LOG_LEVEL")

    # ---------- API ----------
    api_v1_prefix: str = Field(default="/api/v1", alias="API_V1_PREFIX")
    api_cors_origins: str = Field(
        default="http://localhost:3000,http://127.0.0.1:3000",
        alias="API_CORS_ORIGINS",
    )

    # ---------- JWT ----------
    jwt_secret_key: str = Field(alias="JWT_SECRET_KEY")
    jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    jwt_access_token_expire_minutes: int = Field(
        default=15, alias="JWT_ACCESS_TOKEN_EXPIRE_MINUTES"
    )
    jwt_refresh_token_expire_days: int = Field(default=7, alias="JWT_REFRESH_TOKEN_EXPIRE_DAYS")

    # ---------- ENCRYPTION ----------
    encryption_key: str = Field(default="", alias="ENCRYPTION_KEY")

    # ---------- SECRETS ----------
    secrets_provider: Literal["env", "aws", "azure"] = Field(
        default="env", alias="SECRETS_PROVIDER"
    )

    # ---------- DB ----------
    mysql_host: str = Field(default="localhost", alias="MYSQL_HOST")
    mysql_port: int = Field(default=3306, alias="MYSQL_PORT")
    mysql_user: str = Field(default="contract", alias="MYSQL_USER")
    mysql_password: str = Field(default="contract", alias="MYSQL_PASSWORD")
    mysql_db: str = Field(default="contract_agent", alias="MYSQL_DB")
    mysql_pool_size: int = Field(default=10, alias="MYSQL_POOL_SIZE")
    mysql_pool_recycle: int = Field(default=3600, alias="MYSQL_POOL_RECYCLE")

    # ---------- REDIS ----------
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    # ---------- KAFKA ----------
    kafka_bootstrap_servers: str = Field(default="localhost:9092", alias="KAFKA_BOOTSTRAP_SERVERS")
    kafka_client_id: str = Field(default="contract-agent", alias="KAFKA_CLIENT_ID")
    kafka_consumer_group: str = Field(default="contract-agent-cg", alias="KAFKA_CONSUMER_GROUP")
    kafka_enabled: bool = Field(default=False, alias="KAFKA_ENABLED")
    kafka_topic_contract_ingested: str = Field(
        default="contract.ingested", alias="KAFKA_TOPIC_CONTRACT_INGESTED"
    )
    kafka_topic_fields_extracted: str = Field(
        default="fields.extracted", alias="KAFKA_TOPIC_FIELDS_EXTRACTED"
    )
    kafka_topic_notification_sent: str = Field(
        default="notification.sent", alias="KAFKA_TOPIC_NOTIFICATION_SENT"
    )
    kafka_topic_customer_responded: str = Field(
        default="customer.responded", alias="KAFKA_TOPIC_CUSTOMER_RESPONDED"
    )
    kafka_topic_escalation_triggered: str = Field(
        default="escalation.triggered", alias="KAFKA_TOPIC_ESCALATION_TRIGGERED"
    )

    # ---------- MLFLOW ----------
    mlflow_tracking_uri: str = Field(default="http://localhost:5001", alias="MLFLOW_TRACKING_URI")
    mlflow_experiment_extraction: str = Field(
        default="extraction-quality", alias="MLFLOW_EXPERIMENT_EXTRACTION"
    )
    mlflow_experiment_agent: str = Field(default="agent-decisions", alias="MLFLOW_EXPERIMENT_AGENT")
    mlflow_experiment_voice: str = Field(default="voice-amd", alias="MLFLOW_EXPERIMENT_VOICE")

    # ---------- OBSERVABILITY ----------
    otel_enabled: bool = Field(default=False, alias="OTEL_ENABLED")
    otel_exporter_otlp_endpoint: str = Field(
        default="http://localhost:4317", alias="OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    otel_service_name: str = Field(default="contract-agent", alias="OTEL_SERVICE_NAME")
    prometheus_enabled: bool = Field(default=True, alias="PROMETHEUS_ENABLED")

    # ---------- MULTI-TENANCY ----------
    tenant_default_id: str = Field(
        default="00000000-0000-0000-0000-000000000001", alias="TENANT_DEFAULT_ID"
    )
    tenant_enforce: bool = Field(default=True, alias="TENANT_ENFORCE")

    # ---------- EXTRACTION ----------
    extract_confidence_threshold: float = Field(default=0.80, alias="EXTRACT_CONFIDENCE_THRESHOLD")
    extract_max_file_size_mb: int = Field(default=10, alias="EXTRACT_MAX_FILE_SIZE_MB")
    extract_storage_dir: str = Field(default="./data/raw", alias="EXTRACT_STORAGE_DIR")
    skip_virus_scan: bool = Field(default=True, alias="SKIP_VIRUS_SCAN")

    # ---------- LLM ----------
    ollama_base_url: str = Field(default="http://localhost:11434", alias="OLLAMA_BASE_URL")
    ollama_model: str = Field(default="qwen2:1.5b", alias="OLLAMA_MODEL")
    llm_mock: bool = Field(default=True, alias="LLM_MOCK")
    agent_enabled: bool = Field(default=False, alias="AGENT_ENABLED")
    prompt_ab_split: float = Field(default=0.0, alias="PROMPT_AB_SPLIT")

    # ---------- SMS ----------
    textbee_base_url: str = Field(default="http://localhost:3001", alias="TEXTBEE_BASE_URL")
    textbee_api_key: str = Field(default="dev-key", alias="TEXTBEE_API_KEY")
    textbee_device_id: str = Field(default="dev-device", alias="TEXTBEE_DEVICE_ID")
    textbee_webhook_secret: str = Field(
        default="dev-webhook-secret", alias="TEXTBEE_WEBHOOK_SECRET"
    )
    sms_mock: bool = Field(default=True, alias="SMS_MOCK")

    # ---------- EMAIL ----------
    smtp_host: str = Field(default="localhost", alias="SMTP_HOST")
    smtp_port: int = Field(default=2525, alias="SMTP_PORT")
    smtp_user: str = Field(default="", alias="SMTP_USER")
    smtp_password: str = Field(default="", alias="SMTP_PASSWORD")
    smtp_from: str = Field(default="noreply@contract-agent.local", alias="SMTP_FROM")
    smtp_tls: bool = Field(default=False, alias="SMTP_TLS")
    email_mock: bool = Field(default=True, alias="EMAIL_MOCK")

    # ---------- VOICE ----------
    voice_enabled: bool = Field(default=False, alias="VOICE_ENABLED")
    voice_mock: bool = Field(default=True, alias="VOICE_MOCK")
    voice_provider: str = Field(default="pyvoip", alias="VOICE_PROVIDER")
    voice_recording_dir: str = Field(default="./data/voice", alias="VOICE_RECORDING_DIR")
    voice_recording_retention_days: int = Field(default=90, alias="VOICE_RECORDING_RETENTION_DAYS")

    # ---------- SCHEDULE ----------
    schedule_enabled: bool = Field(default=False, alias="SCHEDULE_ENABLED")
    schedule_cron_hour_utc: int = Field(default=3, alias="SCHEDULE_CRON_HOUR_UTC")
    schedule_offsets_days: str = Field(default="60,30,14", alias="SCHEDULE_OFFSETS_DAYS")

    # ---------- ESCALATION ----------
    escalation_response_window_days: int = Field(
        default=14, alias="ESCALATION_RESPONSE_WINDOW_DAYS"
    )
    escalation_operator_email: str = Field(
        default="ops@contract-agent.local", alias="ESCALATION_OPERATOR_EMAIL"
    )

    # ---------- COMPLIANCE ----------
    gdpr_hard_delete_after_days: int = Field(default=30, alias="GDPR_HARD_DELETE_AFTER_DAYS")
    retention_contracts_years: int = Field(default=7, alias="RETENTION_CONTRACTS_YEARS")
    retention_audit_years: int = Field(default=2, alias="RETENTION_AUDIT_YEARS")
    retention_notifications_years: int = Field(default=1, alias="RETENTION_NOTIFICATIONS_YEARS")

    # ---------- DNC ----------
    dnc_enabled: bool = Field(default=False, alias="DNC_ENABLED")
    dnc_api_url: str = Field(default="", alias="DNC_API_URL")
    dnc_local_csv: str = Field(default="./data/dnc.csv", alias="DNC_LOCAL_CSV")

    # ---------- RATE LIMIT ----------
    rate_limit_login_per_min: int = Field(default=5, alias="RATE_LIMIT_LOGIN_PER_MIN")
    rate_limit_upload_per_min: int = Field(default=10, alias="RATE_LIMIT_UPLOAD_PER_MIN")
    rate_limit_webhook_per_min: int = Field(default=100, alias="RATE_LIMIT_WEBHOOK_PER_MIN")

    # ---------- FEATURE FLAGS ----------
    feature_voice_channel: bool = Field(default=False, alias="FEATURE_VOICE_CHANNEL")
    feature_ai_agent: bool = Field(default=False, alias="FEATURE_AI_AGENT")
    feature_ab_prompts: bool = Field(default=False, alias="FEATURE_AB_PROMPTS")

    # ============================================================
    # VALIDATORS
    # ============================================================
    @field_validator("jwt_secret_key")
    @classmethod
    def _jwt_secret_min_length(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError("JWT_SECRET_KEY must be at least 32 characters")
        return v

    @field_validator("encryption_key")
    @classmethod
    def _encryption_key_valid(cls, v: str) -> str:
        if not v:
            # Allow empty in dev/test — the encryption module will warn.
            return v
        try:
            raw = base64.urlsafe_b64decode(v.encode())
        except Exception as exc:
            raise ValueError("ENCRYPTION_KEY must be base64-urlsafe encoded") from exc
        if len(raw) != 32:
            raise ValueError("ENCRYPTION_KEY must decode to exactly 32 bytes (Fernet key)")
        return v

    @field_validator("extract_confidence_threshold")
    @classmethod
    def _threshold_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("EXTRACT_CONFIDENCE_THRESHOLD must be between 0.0 and 1.0")
        return v

    @field_validator("prompt_ab_split")
    @classmethod
    def _ab_split_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("PROMPT_AB_SPLIT must be between 0.0 and 1.0")
        return v

    @field_validator("schedule_cron_hour_utc")
    @classmethod
    def _cron_hour_range(cls, v: int) -> int:
        if not 0 <= v <= 23:
            raise ValueError("SCHEDULE_CRON_HOUR_UTC must be 0..23")
        return v

    # ============================================================
    # DERIVED PROPERTIES
    # ============================================================
    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.api_cors_origins.split(",") if o.strip()]

    @property
    def schedule_offsets_list(self) -> list[int]:
        return [int(x.strip()) for x in self.schedule_offsets_days.split(",") if x.strip()]

    @property
    def database_url(self) -> str:
        """Build DSN with URL-encoded credentials.

        Why quote()?
            Passwords may contain special characters (`@`, `:`, `/`, `#`, `?`,
            `&`, `%`, etc.) that break URL parsing. Quote them to keep the DSN
            unambiguous.
        """
        user = quote(self.mysql_user, safe="")
        password = quote(self.mysql_password, safe="")
        return (
            f"mysql+pymysql://{user}:{password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_db}"
            "?charset=utf8mb4"
        )

    @property
    def is_dev(self) -> bool:
        return self.app_env == "dev"

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    @property
    def is_test(self) -> bool:
        return self.app_env == "test"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor — call this everywhere (never construct Settings directly)."""
    return Settings()
