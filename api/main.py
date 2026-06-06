"""FastAPI application entrypoint for lead-intel.

Wires up routes, starts the APScheduler on startup via the lifespan handler, and
exposes health + manual-trigger admin endpoints.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from api.auth import require_admin
from api.routes import auth_routes, clients, companies, dashboard, leads, onboarding, views
from api.supabase_auth import AuthRedirect, auth_middleware
from pipeline import LAST_RUN, run_pipeline
from scheduler import get_next_run, shutdown_scheduler, start_scheduler

logger = logging.getLogger("lead-intel.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the scheduler on startup and shut it down on exit."""
    start_scheduler()
    logger.info("lead-intel API started")
    try:
        yield
    finally:
        shutdown_scheduler()
        logger.info("lead-intel API stopped")


app = FastAPI(title="lead-intel", version="1.0.0", lifespan=lifespan)

# Supabase-Auth session middleware: validates/refreshes the login cookie and
# stashes the user on request.state.user for the dashboard routes.
app.middleware("http")(auth_middleware)


@app.exception_handler(AuthRedirect)
async def _auth_redirect_handler(request: Request, exc: AuthRedirect) -> Response:
    """Bounce anonymous visitors to the login page.

    For HTMX requests we use the ``HX-Redirect`` header so the browser performs
    a full-page navigation instead of swapping the login HTML into a fragment.
    """
    if request.headers.get("HX-Request"):
        resp = Response(status_code=204)
        resp.headers["HX-Redirect"] = exc.location
        return resp
    return RedirectResponse(exc.location, status_code=303)


# Static brand assets (logo, favicon).
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

# Auth pages (login/signup/logout) + dashboard (HTML) + JSON API routers.
app.include_router(auth_routes.router)
app.include_router(onboarding.router)
app.include_router(dashboard.router)
app.include_router(views.router)
app.include_router(leads.router)
app.include_router(companies.router)
app.include_router(clients.router)


@app.get("/health", tags=["system"])
def health() -> dict[str, Any]:
    """Liveness probe with last/next pipeline run timestamps."""
    last = LAST_RUN.get("last_pipeline_run")
    return {
        "status": "ok",
        "last_pipeline_run": last.isoformat() if last else None,
        "next_run": get_next_run(),
    }


@app.post("/pipeline/trigger", tags=["system"])
async def trigger_pipeline(_admin: bool = Depends(require_admin)) -> dict[str, Any]:
    """Manually trigger a pipeline run in the background (admin only)."""
    run_id = str(uuid4())
    asyncio.create_task(run_pipeline())
    logger.info("Manual pipeline trigger %s", run_id)
    return {"status": "started", "run_id": run_id}
