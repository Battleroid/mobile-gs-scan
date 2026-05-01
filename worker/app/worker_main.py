"""Worker container entrypoint.

Initializes the shared sqlite store + runs the polling job loop.
"""
from __future__ import annotations

import asyncio
import logging

from app.config import get_settings
from app.jobs.runner import run_forever
from app.jobs.store import init_store, shutdown_store


async def _main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    await init_store(settings)
    try:
        await run_forever(settings)
    finally:
        await shutdown_store()


if __name__ == "__main__":
    asyncio.run(_main())
