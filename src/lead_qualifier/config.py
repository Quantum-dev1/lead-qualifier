"""Runtime settings from environment variables (or a .env file)."""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    icp_path: str = Field(default="config/icp.yaml", description="ICP + scoring rules YAML")
    database_path: str = "data/decisions.sqlite3"

    # LLM: any OpenAI-compatible Chat Completions endpoint. Leave LLM_MODEL empty for rules-only mode.
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: SecretStr | None = None
    llm_model: str = ""
    llm_timeout: float = 30.0
    llm_json_mode: bool = True

    # Inbound webhook security. Comma-separated to allow key rotation.
    webhook_secret: SecretStr | None = None
    allow_unsigned_webhooks: bool = False
    webhook_tolerance_seconds: int = 300
    max_body_bytes: int = 64_000

    # Dashboard / read API. Without a password they are reachable from localhost only.
    admin_password: SecretStr | None = None
    public_url: str | None = None

    # Outputs. An output is active only when configured here AND listed in the ICP routing.
    slack_webhook_url: SecretStr | None = None
    output_webhook_url: str | None = None
    output_webhook_secret: SecretStr | None = None
    hubspot_access_token: SecretStr | None = None
    hubspot_base_url: str = "https://api.hubapi.com"
    pipedrive_api_token: SecretStr | None = None
    pipedrive_base_url: str = "https://api.pipedrive.com"

    def webhook_secrets(self) -> list[str]:
        if not self.webhook_secret:
            return []
        return [s.strip() for s in self.webhook_secret.get_secret_value().split(",") if s.strip()]
