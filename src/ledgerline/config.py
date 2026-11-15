"""12-factor settings. Every value comes from the environment (see .env.example)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    debug: bool = False
    log_level: str = "INFO"
    log_format: str = "json"  # json | console

    database_url: str = "postgresql+asyncpg://ledgerline:ledgerline@localhost:5440/ecl_db"
    database_pool_size: int = 10
    database_max_overflow: int = 10
    database_pool_timeout: int = 30
    # Schema that holds the web app's tables (users, tenants, memberships, audit_logs). Engine tables live in `engine`.
    web_schema: str = "public"

    # Auth: the web app (Next.js) mints RS256 tokens; we verify against its JWKS.
    web_jwks_url: str = "http://localhost:3300/api/auth/jwks"
    web_jwt_issuer: str = "ledgerline-web"
    web_jwt_audience: str = "ecl-engine"
    # Shared secret for service calls from the web app's scheduler (ops endpoints only).
    service_token: str = Field(default="dev-service-token-change-me", min_length=16)

    # Object storage (Cloudflare R2 in production, MinIO locally). Path-style S3 API.
    storage_endpoint_url: str | None = "http://localhost:9010"
    storage_public_endpoint_url: str | None = None  # browser-facing endpoint for presigned URLs (defaults to endpoint)
    storage_access_key: str = "ledgerline"
    storage_secret_key: str = "ledgerline-secret"  # noqa: S105 (local MinIO default; real keys come from the environment)
    storage_region: str = "auto"
    storage_bucket_name: str = "ledgerline-dev"
    presign_ttl_seconds: int = 900
    multipart_part_mb: int = 32
    max_upload_bytes: int = 5 * 1024**3

    # Compute
    max_concurrent_runs_per_tenant: int = 2
    worker_concurrency: int = 2

    ip_hash_salt: str = "dev-ip-hash-salt-change-me"
    sentry_dsn: str | None = None
    otel_exporter_otlp_endpoint: str | None = None
    app_version: str = "1.0.0"

    # Legacy names some ported modules read; kept so behaviour is unchanged.
    redis_url: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
