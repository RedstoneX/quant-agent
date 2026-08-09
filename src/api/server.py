"""FastAPI app wiring for the Mission Control API.

This module owns the ONLY place the two route modules are assembled into a
running app. It adds two structural, app-level safety guarantees on top of
what each router already does individually:

1. **GET-only enforcement, at the app level.** A middleware rejects any
   request whose method is not GET/HEAD/OPTIONS with 405 before it can reach
   ANY route — even a future route added carelessly to `routes_live.py` /
   `routes_history.py` without a matching safety test would still be
   incapable of accepting a write. This is belt-and-suspenders on top of the
   fact that neither router registers anything but `@router.get(...)`.
2. **A global exception handler.** Any unhandled exception anywhere below
   this app (a genuine bug, a corrupted DB file, whatever) becomes a plain
   `{"detail": "internal error"}` 500 — never a raw traceback, which could
   leak file paths or other internals. This is what "malformed/read
   failures fail at the API boundary" (Stage 2 Checkpoint C) means in
   practice: the API process itself stays up and answers a clean error.

Nothing in this file, `routes_live.py`, `routes_history.py`, `broker_reads.py`,
or `db_reads.py` imports `src.pipeline`, `src.pipeline_stages`,
`src.risk.*`, or any write-capable method of `src.execution.broker` /
`src.storage.db`. That is the Stage 2 isolation invariant, verified by
`tests/test_api_isolation.py`.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from src.api.routes_history import router as history_router
from src.api.routes_live import router as live_router

logger = logging.getLogger(__name__)

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class _GetOnlyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method not in _SAFE_METHODS:
            return JSONResponse(
                status_code=405,
                content={"detail": "Mission Control API is read-only: GET only"},
            )
        return await call_next(request)


def create_app() -> FastAPI:
    app = FastAPI(
        title="QAMC Mission Control API",
        description=(
            "Stage 2 — thin, read-only adapter over quant-agent's existing "
            "canonical state. Never places, cancels, or modifies broker "
            "orders; never calls an AI agent or the Portfolio Manager; "
            "trading runs unaffected by this process's presence or absence."
        ),
        version="0.1.0",
    )
    app.add_middleware(_GetOnlyMiddleware)
    app.include_router(live_router, tags=["live"])
    app.include_router(history_router, tags=["history"])

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception):
        logger.warning("Unhandled exception on %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(status_code=500, content={"detail": "internal error"})

    @app.get("/", tags=["meta"])
    def root() -> dict:
        return {"service": "qamc-mission-control-api", "status": "ok"}

    return app


app = create_app()
