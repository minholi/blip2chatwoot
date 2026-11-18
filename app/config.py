from functools import lru_cache

from pydantic import field_validator, model_validator
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
    chatwoot_agent_bot_token: str = ""
    chatwoot_replies_to_blip: bool = False
    # Comma-separated Chatwoot conversation ids. When set, only replies in these conversations are
    # forwarded to BLiP; empty means every conversation of the inbox (a pilot safeguard).
    chatwoot_replies_allowed_conversations: str = ""
    # Assign each conversation to the BLiP Desk agent who last wrote in it, creating that agent's
    # Chatwoot user when missing (Chatwoot e-mails them an invitation). Only calls Chatwoot.
    chatwoot_agent_sync: bool = False
    # Captain (the Chatwoot AI assistant) sessions started by a BLiP icebreaker; empty = off.
    # "|"-separated exact phrases (case and repeated spaces ignored). When a customer sends one, the
    # conversation gets CAPTAIN_SESSION_LABELS and goes `pending` *before* the message is posted, so
    # the Captain answers it. Needs the BLiP router key in BLIP_AUTH_KEY (reads `master-state`).
    captain_trigger_phrases: str = ""
    # Comma-separated labels added at the icebreaker (e.g. "camila,bot-venda-inicio").
    captain_session_labels: str = ""
    # Open a new Chatwoot conversation when a Captain session starts (icebreaker, or the router
    # putting the customer in the sub-bot) unless the customer is mid-session (`pending`, session
    # active). Each session then has its own labels, notes and history, and the Captain starts
    # without the earlier sessions in its context. The old conversation stays in Chatwoot but is
    # no longer mapped: events for it are ignored, so replies typed there never reach BLiP.
    captain_new_conversation_per_session: bool = False
    # Comma-separated label prefixes (e.g. "saida-,bot-venda-") removed when a session starts, or a
    # finished one resumes, in a conversation that is reused (CAPTAIN_NEW_CONVERSATION_PER_SESSION
    # off). Never mid-session.
    # Exit/funnel labels left by an earlier session would otherwise fire the exit automations again
    # (a link sent today resolves the conversation and a stale `saida-*` label sends the customer
    # away). Labels in CAPTAIN_SESSION_LABELS are kept even if they match.
    captain_session_reset_label_prefixes: str = ""
    # Router `master-state` value meaning "the customer is in the Captain's (silent) sub-bot".
    # A customer message in a Captain conversation that isn't open keeps it `pending` only while
    # the router still has the customer there; otherwise the session ends and it opens as usual.
    captain_blip_subbot: str = ""
    # `POST /webhooks/captain/start`: BLiP tells the bridge that it just handed a
    # customer to the Captain's sub-bot (whatever the entry: icebreaker, night window, lead ads...),
    # with the customer's message and the origin. The bridge starts the session and posts that
    # message, so the Captain answers it. Empty = endpoint off (404). The secret goes in the
    # `X-Bridge-Token` header only (a token in the URL would end up in proxy and tunnel logs).
    captain_start_token: str = ""
    # Label added at the start, `<prefix><origin>` (e.g. `origem-leadads`); empty = no origin label.
    captain_start_origin_label_prefix: str = "origem-"
    # Posted instead of the customer's text when it isn't sent, or when it is already in the
    # conversation, so the Captain still has a new message to answer.
    captain_start_fallback_text: str = "[Início automático: lead encaminhado à Camila]"
    # Also start a session for any customer message while the router has the customer in
    # CAPTAIN_BLIP_SUBBOT, not only for the icebreaker phrases (e.g. a night window that BLiP turns
    # on and off on its side). One `master-state` read per customer message that has no active
    # session. An unreadable state starts nothing; the next message tries again.
    captain_follow_master_state: bool = False
    # Forward the Captain's public messages to BLiP, only in conversations with an active session.
    # Independent of CHATWOOT_REPLIES_TO_BLIP, which covers human agents.
    captain_replies_to_blip: bool = False

    blip_contract_id: str = ""
    blip_auth_key: str = ""
    # Key of the bot that owns the BLiP Desk. Commands to postmaster@desk.msging.net (tickets, tags,
    # attendants) use it when set; the main bot's key may have no Desk configured.
    blip_desk_auth_key: str = ""
    blip_bot_identity: str = ""
    blip_inbound_path_token: str = ""
    blip_notification_path_token: str = ""
    blip_ack_messages: bool = False
    blip_ticket_tag_sync_enabled: bool = False
    # Read the operator's full name from BLiP Desk (`get /attendants`) when creating their Chatwoot
    # agent, instead of deriving one from the e-mail. Only used with CHATWOOT_AGENT_SYNC.
    blip_agent_name_lookup: bool = False
    blip_label_poll_seconds: int = 60
    # BLiP media links are short-lived signed URLs; attach the file in Chatwoot instead of linking.
    blip_media_attachments: bool = True
    blip_media_max_bytes: int = 20 * 1024 * 1024
    blip_media_allowed_hosts: str = "blipmediastore.blob.core.windows.net"
    # Copy name, e-mail and a fixed set of CRM/student fields from BLiP contact updates onto the
    # Chatwoot contact. Off drops contact updates unstored, as before.
    blip_contact_sync: bool = True

    worker_poll_seconds: float = 1.0
    max_delivery_attempts: int = 8
    http_timeout_seconds: float = 15.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("chatwoot_replies_allowed_conversations")
    @classmethod
    def _reply_conversations_are_ids(cls, value: str) -> str:
        # Fail at startup: a typo must not silently turn the allow-list into "every conversation".
        items = [item.strip() for item in value.split(",") if item.strip()]
        if (value.strip() and not items) or not all(item.isdigit() for item in items):
            raise ValueError(
                "CHATWOOT_REPLIES_ALLOWED_CONVERSATIONS must be comma-separated conversation ids"
            )
        return value

    @model_validator(mode="after")
    def _captain_needs_subbot(self) -> "Settings":
        # Without the sub-bot every returning customer would be treated as having left the Captain.
        if self.captain_enabled and not self.captain_blip_subbot.strip():
            raise ValueError(
                "CAPTAIN_BLIP_SUBBOT is required when CAPTAIN_TRIGGER_PHRASES, "
                "CAPTAIN_FOLLOW_MASTER_STATE or CAPTAIN_START_TOKEN is set"
            )
        return self

    @property
    def captain_trigger_phrase_set(self) -> frozenset[str]:
        phrases = (normalize_phrase(item) for item in self.captain_trigger_phrases.split("|"))
        return frozenset(phrase for phrase in phrases if phrase)

    @property
    def captain_enabled(self) -> bool:
        return (
            bool(self.captain_trigger_phrase_set)
            or self.captain_follow_master_state
            or bool(self.captain_start_token)
        )

    @property
    def captain_session_label_list(self) -> list[str]:
        labels = (item.strip() for item in self.captain_session_labels.split(","))
        return list(dict.fromkeys(label for label in labels if label))

    @property
    def captain_session_reset_prefix_list(self) -> list[str]:
        prefixes = (item.strip() for item in self.captain_session_reset_label_prefixes.split(","))
        return list(dict.fromkeys(prefix for prefix in prefixes if prefix))

    @property
    def chatwoot_reply_conversation_ids(self) -> frozenset[int]:
        items = (item.strip() for item in self.chatwoot_replies_allowed_conversations.split(","))
        return frozenset(int(item) for item in items if item)

    @property
    def blip_writes_enabled(self) -> bool:
        """Whether the bridge is allowed to call BLiP at all; off means a read-only mirror."""
        return (
            self.blip_ack_messages
            or self.chatwoot_replies_to_blip
            or self.blip_ticket_tag_sync_enabled
            or self.blip_agent_name_lookup
            or self.captain_enabled
            or self.captain_replies_to_blip
        )

    @property
    def blip_media_hosts(self) -> frozenset[str]:
        hosts = (host.strip().lower() for host in self.blip_media_allowed_hosts.split(","))
        return frozenset(host for host in hosts if host)

    def validate_for_environment(self) -> None:
        if self.app_env.lower() != "production":
            return
        required = {
            "DATABASE_URL": self.database_url,
            "CHATWOOT_BASE_URL": self.chatwoot_base_url,
            "CHATWOOT_API_TOKEN": self.chatwoot_api_token,
            "CHATWOOT_WEBHOOK_SECRET": self.chatwoot_webhook_secret,
            "BLIP_BOT_IDENTITY": self.blip_bot_identity,
            "BLIP_INBOUND_PATH_TOKEN": self.blip_inbound_path_token,
            "BLIP_NOTIFICATION_PATH_TOKEN": self.blip_notification_path_token,
        }
        if self.blip_writes_enabled:
            required["BLIP_CONTRACT_ID"] = self.blip_contract_id
            required["BLIP_AUTH_KEY"] = self.blip_auth_key
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
        if self.blip_media_max_bytes <= 0:
            raise ValueError("BLIP_MEDIA_MAX_BYTES must be positive")


def normalize_phrase(text: str) -> str:
    """Icebreaker comparison key: case-insensitive, surrounding and repeated spaces ignored."""
    return " ".join(text.split()).casefold()


@lru_cache
def get_settings() -> Settings:
    return Settings()
