from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.db import create_engine, create_session_factory
from app.integrations.blip import BlipClient
from app.integrations.chatwoot import ChatwootClient


@dataclass
class Runtime:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    http_client: httpx.AsyncClient
    blip: BlipClient
    chatwoot: ChatwootClient

    async def close(self) -> None:
        await self.http_client.aclose()
        await self.engine.dispose()


def create_runtime(settings: Settings) -> Runtime:
    settings.validate_for_environment()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    http_client = httpx.AsyncClient(timeout=settings.http_timeout_seconds)
    return Runtime(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        http_client=http_client,
        blip=BlipClient(settings, http_client),
        chatwoot=ChatwootClient(settings, http_client),
    )
