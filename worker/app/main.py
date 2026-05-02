"""FastAPI broker entrypoint.

Mounts:
  /api/health
  /api/captures   (HTTP + WS for streaming + WS for events)
  /api/scenes     (HTTP + WS for events)
  /api/jobs       (read-only)
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import captures, health, jobs, scenes
from app.config import get_settings
from app.jobs.store import init_store, shutdown_store


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.info("api: data_dir=%s", settings.data_dir)
    await init_store(settings)
    try:
        yield
    finally:
        await shutdown_store()


app = FastAPI(title="mobile-gs-scan", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(health.router)
app.include_router(captures.router)
app.include_router(scenes.router)
app.include_router(jobs.router)
