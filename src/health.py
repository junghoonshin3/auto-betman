"""Minimal health-check HTTP server for Render Web Service."""
from __future__ import annotations

import os
import logging

from aiohttp import web

logger = logging.getLogger(__name__)


async def health_handler(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def start_health_server() -> web.AppRunner:
    """Start a lightweight HTTP server for Render health checks."""
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)

    port = int(os.environ.get("PORT", "10000"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Health check server started on port %d", port)
    return runner
