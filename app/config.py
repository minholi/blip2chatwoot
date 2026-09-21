from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "blip2chatwoot"
    app_env: str = "development"
    log_level: str = "INFO"
    auto_create_schema: bool = True

    database_url: str = "sqlite+aiosqlite:///./bridge.db"

    chatwoot_base_url: str = ""
    chatwoot_account_id: int = 0
    chatwoot_api_token: str = ""
    chatwoot_inbox_id: int = 0
    chatwoot_webhook_secret: str = ""

    blip_contract_id: str = ""
    blip_auth_key: str = ""
    blip_bot_identity: str = ""
    blip_inbound_path_token: str = ""
    blip_notification_path_token: str = ""
    blip_ticket_tag_sync_enabled: bool = False
    blip_label_poll_seconds: int = 60

    worker_poll_seconds: float = 1.0
    max_delivery_attempts: int = 8
    http_timeout_seconds: float = 15.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    def validate_for_environment(self) -> None:
        if self.app_env.lower() != "production":
            return
        required = {
            "DATABASE_URL": self.database_url,
            "CHATWOOT_BASE_URL": self.chatwoot_base_url,
            "CHATWOOT_API_TOKEN": self.chatwoot_api_token,
            "CHATWOOT_WEBHOOK_SECRET": self.chatwoot_webhook_secret,
            "BLIP_CONTRACT_ID": self.blip_contract_id,
            "BLIP_AUTH_KEY": self.blip_auth_key,
            "BLIP_BOT_IDENTITY": self.blip_bot_identity,
            "BLIP_INBOUND_PATH_TOKEN": self.blip_inbound_path_token,
            "BLIP_NOTIFICATION_PATH_TOKEN": self.blip_notification_path_token,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"Missing production configuration: {', '.join(missing)}")
        if not self.database_url.startswith("postgresql+asyncpg://"):
            raise ValueError("Production deployments must use postgresql+asyncpg://")
        if self.chatwoot_account_id <= 0 or self.chatwoot_inbox_id <= 0:
            raise ValueError("CHATWOOT_ACCOUNT_ID and CHATWOOT_INBOX_ID must be positive")
        if self.max_delivery_attempts <= 0:
            raise ValueError("MAX_DELIVERY_ATTEMPTS must be positive")
        if self.worker_poll_seconds <= 0 or self.http_timeout_seconds <= 0:
            raise ValueError("Worker polling and HTTP timeout values must be positive")


@lru_cache
def get_settings() -> Settings:
    return Settings()
