"""FastAPI app wiring for the Mission Control API.

This module owns the ONLY place the two route modules are assembled into a
running app. It also mounts the Stage 3 cockpit (`src/api/static/`) — a
static HTML/CSS/JS bundle with no build step and no server-side rendering,
so it carries none of the read/write risk the JSON routes below are guarded
against; it only ever calls this same GET-only API from the browser. It
adds two structural, app-level safety guarantees on top of what each router
already does individually:

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
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from src.api.routes_evidence import router as evidence_router
from src.api.routes_history import router as history_router
from src.api.routes_journal import router as journal_router
from src.api.routes_live import router as live_router

_STATIC_DIR = Path(__file__).parent / "static"
# Stage 6 — the richer cockpit (docs/visual/MISSION_CONTROL_VISION_BOARD.png
# as layout/hierarchy reference). A second, independent static bundle, not
# a replacement: /ui keeps serving the Stage 3-5 dashboard byte-for-byte as
# a safe fallback while this one is verified. Same no-build-step, no-
# framework, no-new-dependency posture — see docs/WORK.md's explicit
# "no framework migration without architecture approval" boundary.
_COCKPIT_DIR = Path(__file__).parent / "static_cockpit"

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
    app.include_router(evidence_router, tags=["evidence"])
    app.include_router(journal_router, tags=["journal"])

    if _STATIC_DIR.is_dir():
        # Stage 3 cockpit — static assets only, no templating/server state.
        # Mounted last and under its own path so it never shadows the JSON
        # routes above or the `/` service-meta route below.
        app.mount("/ui", StaticFiles(directory=str(_STATIC_DIR), html=True), name="ui")

    if _COCKPIT_DIR.is_dir():
        # Stage 6 cockpit — same static-assets-only posture as /ui above.
        app.mount("/cockpit", StaticFiles(directory=str(_COCKPIT_DIR), html=True), name="cockpit")

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception):
        logger.warning("Unhandled exception on %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(status_code=500, content={"detail": "internal error"})

    @app.get("/", tags=["meta"])
    def root() -> dict:
        return {"service": "qamc-mission-control-api", "status": "ok"}

    return app


app = create_app()
