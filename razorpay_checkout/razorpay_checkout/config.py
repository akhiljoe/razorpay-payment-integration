"""Per-tenant Razorpay configuration.

`RazorpayConfig` is a Pydantic v2 `BaseSettings` model. One instance per tenant.
All secret values use `SecretStr` so they never appear in `repr()` or default
serialization. On construction, the secret string values are registered into
the logging module's redaction set so any accidental log of the raw value gets
masked with `***`.

Environment variables are read with the prefix `RAZORPAY_` (e.g.
`RAZORPAY_KEY_ID`). For multi-tenant deployments, construct multiple instances
from per-tenant SSM Parameter Store / Secrets Manager values and register each
into `TenantConfigRegistry`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from razorpay_checkout.logging_config import register_secret


class RazorpayConfig(BaseSettings):
    """Settings for a single tenant's Razorpay integration."""

    model_config = SettingsConfigDict(
        env_prefix="RAZORPAY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    tenant_id: str = Field(..., description="Stable per-tenant identifier; used in URL path")
    key_id: str = Field(..., description="Razorpay API key id (rzp_test_* or rzp_live_*)")
    key_secret: SecretStr = Field(..., description="Razorpay API key secret")
    webhook_secret: SecretStr = Field(..., description="Active webhook signing secret")
    webhook_secret_previous: SecretStr | None = Field(
        default=None,
        description="Previous webhook secret retained during rotation; tried after current",
    )
    mode: Literal["test", "live"] = Field(default="test")

    db_dsn: PostgresDsn = Field(..., description="Postgres DSN for the consumer / fulfillment")
    pool_min_size: int = Field(default=2, ge=1)
    pool_max_size: int = Field(default=10, ge=1)

    razorpay_api_timeout: float = Field(default=10.0, gt=0)

    otel_exporter: Literal["stdout", "xray", "otlp"] = Field(default="stdout")
    otel_endpoint: str | None = Field(default=None)

    admin_api_key: SecretStr | None = Field(
        default=None,
        description="Admin route auth key; routes return 403 when None",
    )

    def model_post_init(self, _context: object) -> None:
        """Register secret values into the logging redaction set."""
        register_secret(self.key_secret.get_secret_value())
        register_secret(self.webhook_secret.get_secret_value())
        if self.webhook_secret_previous is not None:
            register_secret(self.webhook_secret_previous.get_secret_value())
        if self.admin_api_key is not None:
            register_secret(self.admin_api_key.get_secret_value())
