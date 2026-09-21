from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.models import Base


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
def settings() -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        chatwoot_base_url="https://chatwoot.example",
        chatwoot_account_id=10,
        chatwoot_api_token="chatwoot-token",
        chatwoot_inbox_id=20,
        chatwoot_webhook_secret="chatwoot-secret",
        blip_contract_id="contract",
        blip_auth_key="blip-key",
        blip_bot_identity="mybot@msging.net",
        blip_inbound_path_token="route-secret",
    )
