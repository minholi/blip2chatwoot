from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import router
from app.config import get_settings
from app.db import init_db
from app.runtime import create_runtime


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime = create_runtime(get_settings())
    if runtime.settings.auto_create_schema:
        await init_db(runtime.engine)
    app.state.runtime = runtime
    try:
        yield
    finally:
        await runtime.close()


app = FastAPI(title="BLiP to Chatwoot Bridge", lifespan=lifespan)
app.include_router(router)
