import pytest

from app.config import Settings

_PRODUCTION = {
    "app_env": "production",
    "database_url": "postgresql+asyncpg://bridge:bridge@db/bridge",
    "chatwoot_base_url": "https://chatwoot.example",
    "chatwoot_account_id": 1,
    "chatwoot_api_token": "chatwoot-token",
    "chatwoot_inbox_id": 1,
    "chatwoot_webhook_secret": "chatwoot-secret",
    "blip_bot_identity": "mybot@msging.net",
    "blip_inbound_path_token": "inbound-token",
    "blip_notification_path_token": "notification-token",
}


def test_read_only_mirror_does_not_require_blip_credentials() -> None:
    settings = Settings(_env_file=None, **_PRODUCTION)

    settings.validate_for_environment()

    assert settings.blip_writes_enabled is False


@pytest.mark.parametrize(
    "flag",
    [
        "blip_ack_messages",
        "chatwoot_replies_to_blip",
        "blip_ticket_tag_sync_enabled",
        "blip_agent_name_lookup",
    ],
)
def test_any_blip_write_requires_blip_credentials(flag) -> None:
    settings = Settings(_env_file=None, **_PRODUCTION, **{flag: True})

    with pytest.raises(ValueError, match="BLIP_CONTRACT_ID, BLIP_AUTH_KEY"):
        settings.validate_for_environment()


def test_production_rejects_a_non_positive_media_size_limit() -> None:
    settings = Settings(_env_file=None, **_PRODUCTION, blip_media_max_bytes=0)

    with pytest.raises(ValueError, match="BLIP_MEDIA_MAX_BYTES"):
        settings.validate_for_environment()


def test_agent_sync_is_off_by_default_and_needs_no_blip_credentials() -> None:
    assert Settings(_env_file=None, **_PRODUCTION).chatwoot_agent_sync is False

    enabled = Settings(_env_file=None, **_PRODUCTION, chatwoot_agent_sync=True)

    enabled.validate_for_environment()
    assert enabled.blip_writes_enabled is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", frozenset()),
        ("4888", frozenset({4888})),
        (" 4888 , 12,,", frozenset({4888, 12})),
    ],
)
def test_reply_allow_list_is_parsed_into_conversation_ids(raw, expected) -> None:
    settings = Settings(_env_file=None, chatwoot_replies_allowed_conversations=raw)

    assert settings.chatwoot_reply_conversation_ids == expected


@pytest.mark.parametrize("raw", ["4888,abc", "#4888", "-1", ",", "48 88"])
def test_a_malformed_reply_allow_list_stops_the_startup(raw) -> None:
    # Falling back to "empty" would silently allow replies in every conversation.
    with pytest.raises(ValueError, match="CHATWOOT_REPLIES_ALLOWED_CONVERSATIONS"):
        Settings(_env_file=None, chatwoot_replies_allowed_conversations=raw)
