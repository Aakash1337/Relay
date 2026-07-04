"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy import text

from relay import __version__
from relay.api.routes import router
from relay.db.engine import untenanted_app_session
from relay.logs import setup_logging


def create_app() -> FastAPI:
    setup_logging()
    application = FastAPI(
        title="RELAY",
        version=__version__,
        description=(
            "Autonomous B2B sales prospecting and outreach — Phase 0 "
            "skeleton. Approval never sends; the send path is internal "
            "and structurally gated."
        ),
    )
    application.include_router(router)

    @application.get("/health")
    def health() -> dict[str, str]:
        with untenanted_app_session() as session:
            session.execute(text("SELECT 1"))
        return {"status": "ok", "version": __version__}

    return application


app = create_app()
