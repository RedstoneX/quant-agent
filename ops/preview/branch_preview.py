"""Ephemeral, read-only branch-preview server for QAMC Mission Control.

Lets an operator view the CURRENT feature branch's frontend (whatever is
actually committed in this `dev` checkout — both the legacy `/ui` dashboard
and the new `/cockpit` React build) rendered against real, truthful QAMC
data, without duplicating or faking any runtime state and without touching
the `qamc` production account in any way.

How it gets real data: every JSON API request (anything other than /ui/*
or /cockpit/*) is proxied read-only, GET-only, to the already-running
`qamc` production Mission Control API on 127.0.0.1:8800 (reachable over
loopback regardless of account boundary — that is how this repo's own
frontend-verification tooling already reads live data; see
docs/verification/stage-6-react/README.md). This process never reads
`qamc`'s SQLite database or credentials directly (it has no filesystem
permission to — the `dev`/`qamc` account boundary is unaffected), never
writes anything, and never starts, stops, or restarts any `qamc` service.

Safety boundaries:
- GET-only, enforced in this process's own middleware BEFORE a request is
  ever forwarded upstream — belt-and-suspenders on top of the upstream
  API's own independently-tested GET-only enforcement
  (tests/test_api_safety.py::test_api_routes_are_get_only).
- Binds ONLY to an explicit interface IP you pass in (the VPS's Tailscale
  IP in normal use) — never 0.0.0.0, never the public interface. A socket
  bound to one address never accepts connections arriving on another;
  this is not a firewall rule, it is how the bind itself works.
- No systemd unit, no auto-start; this is a plain foreground/background
  process you start for a review session and kill afterward — see
  ops/preview/README.md for the exact commands.
- Does not modify, restart, or depend on any `qamc`-owned process, file,
  or credential.

Usage (from the `dev` account, this checkout):
    source .venv/bin/activate
    python ops/preview/branch_preview.py --host 100.111.170.97 --port 8810

Then from another tailnet device:
    http://ovh-vps.wallaby-bowfin.ts.net:8810/cockpit/
    http://ovh-vps.wallaby-bowfin.ts.net:8810/ui/

Stop: Ctrl-C in the foreground, or `kill <pid>` if backgrounded.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("branch_preview")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_UI = REPO_ROOT / "src" / "api" / "static"
STATIC_COCKPIT = REPO_ROOT / "src" / "api" / "static_cockpit"

UPSTREAM_BASE = "http://127.0.0.1:8800"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class _GetOnlyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method not in _SAFE_METHODS:
            return JSONResponse(
                status_code=405,
                content={"detail": "branch preview is read-only: GET only"},
            )
        return await call_next(request)


def _current_branch_sha() -> tuple[str, str]:
    import subprocess

    def _git(*args: str) -> str:
        try:
            return subprocess.check_output(
                ["git", *args], cwd=REPO_ROOT, text=True, stderr=subprocess.DEVNULL
            ).strip()
        except Exception:
            return "unknown"

    return _git("rev-parse", "--abbrev-ref", "HEAD"), _git("rev-parse", "--short", "HEAD")


def create_app() -> FastAPI:
    app = FastAPI(
        title="QAMC Mission Control — BRANCH PREVIEW (not production)",
        description=(
            "Ephemeral operator preview of the current feature branch's "
            "frontend, proxying read-only GET requests to the real qamc "
            "production Mission Control API. Never writes anything; never "
            "touches qamc's runtime, database, or credentials."
        ),
    )
    app.add_middleware(_GetOnlyMiddleware)

    branch, sha = _current_branch_sha()
    client = httpx.AsyncClient(base_url=UPSTREAM_BASE, timeout=10.0)

    @app.get("/")
    def root() -> dict:
        return {
            "service": "qamc-mission-control-BRANCH-PREVIEW",
            "warning": "This is a read-only preview of an unmerged branch, not the qamc production service.",
            "branch": branch,
            "commit": sha,
            "proxied_data_from": UPSTREAM_BASE,
            "ui": "/ui/",
            "cockpit": "/cockpit/",
        }

    if STATIC_UI.is_dir():
        app.mount("/ui", StaticFiles(directory=str(STATIC_UI), html=True), name="ui")
    if STATIC_COCKPIT.is_dir():
        app.mount("/cockpit", StaticFiles(directory=str(STATIC_COCKPIT), html=True), name="cockpit")

    @app.get("/{path:path}")
    async def proxy(path: str, request: Request):
        try:
            upstream_resp = await client.get(f"/{path}", params=request.query_params)
        except httpx.RequestError as exc:
            logger.warning("upstream proxy request failed for /%s: %s", path, exc)
            return JSONResponse(
                status_code=502,
                content={"detail": f"branch preview could not reach the read-only qamc API: {exc}"},
            )
        # This branch's new endpoints (added this tranche, not yet deployed
        # to qamc production) 404 against the upstream proxy target — that
        # is real, expected version skew when previewing an unmerged
        # branch against live production, not a bug in this proxy. Where
        # the response shape has its own honest error field (PriceBarsResponse),
        # rewrite the bare 404 into that shape rather than leaving a raw
        # fetch failure — still zero fabricated data, just an accurate
        # message instead of an opaque status code. Endpoints without an
        # error-carrying shape (e.g. RunFunnelResponse) are left as an
        # honest 404 passthrough; the frontend already degrades that to a
        # readable "could not load" state without crashing.
        if upstream_resp.status_code == 404 and path.startswith("prices/"):
            symbol = path.removeprefix("prices/").split("?")[0].upper()
            return JSONResponse(
                status_code=200,
                content={
                    "symbol": symbol,
                    "bars": [],
                    "source": "alpaca_market_data",
                    "error": (
                        "GET /prices/{symbol} is new on this branch and not yet deployed "
                        "to qamc production — no live data available in this preview."
                    ),
                },
            )
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            media_type=upstream_resp.headers.get("content-type"),
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception):
        logger.warning("Unhandled exception on %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(status_code=500, content={"detail": "internal error"})

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        required=True,
        help="Bind address — pass the VPS's Tailscale interface IP explicitly (e.g. 100.111.170.97). "
        "Refuses 0.0.0.0 and any address matching a known public-interface pattern is your responsibility "
        "to avoid; this script does not guess your network layout for you.",
    )
    parser.add_argument("--port", type=int, default=8810)
    args = parser.parse_args()

    if args.host in ("0.0.0.0", "::"):
        raise SystemExit("Refusing to bind 0.0.0.0/:: — pass the Tailscale interface IP explicitly.")

    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
