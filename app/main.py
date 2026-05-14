# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""FastAPI application entry point."""
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.routing import Mount

from app.config import get_settings
from app.api.v1 import routes as v1_routes
from app.api.v1 import scheduler_mcp
from app.core.dependencies import AdminAuthRequired
from app.services.firestore_service import FirestoreService
from app.services.vertex_ai_service import VertexAIService
from app.services.slack_service import SlackService
from app.services.scheduled_job_service import ScheduledJobService
from app.services.gcs_service import GCSService
from app.services.admin_auth_service import AdminAuthService
from app.services.admin_logging_service import AdminLoggingService
from app.services.admin_vertex_service import AdminVertexService

# Configure logging.
#
# On Cloud Run we route Python's stdlib logging through google-cloud-logging
# so logger.info()/.warning()/.error() produce Cloud Logging entries with
# proper severity levels (not flattened to default/INFO via stdout capture).
# Without this, the admin UI's "Last Error" card finds nothing because no
# entries match severity>=ERROR even when the app is logging at ERROR.
#
# K_SERVICE is set automatically by Cloud Run; locally we fall back to a
# text stdout formatter so dev and tests don't need API credentials.
def _configure_logging():
    if os.environ.get("K_SERVICE"):
        try:
            from google.cloud import logging as cloud_logging
            cloud_logging.Client().setup_logging(log_level=logging.INFO)
            return
        except Exception as e:  # pragma: no cover - boot-time best effort
            logging.basicConfig(level=logging.INFO)
            logging.getLogger(__name__).warning(
                f"Cloud Logging setup failed, falling back to stdout: {e}"
            )
            return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )


_configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan context manager for startup/shutdown events.

    Initializes services on startup and stores them in app.state
    for dependency injection.
    """
    settings = get_settings()

    # Configure logging level
    logging.getLogger().setLevel(settings.log_level)

    logger.info(f"Starting {settings.app_name} in {settings.environment} mode")

    # Initialize services
    logger.info("Initializing services...")

    app.state.firestore = FirestoreService()
    app.state.vertex_ai = VertexAIService()
    app.state.slack = SlackService()
    app.state.scheduled_job_service = ScheduledJobService(firestore=app.state.firestore)

    if settings.admin_ui_enabled:
        app.state.admin_auth = AdminAuthService(
            project_id=settings.gcp_project_id,
            required_role=settings.admin_required_role,
        )
        app.state.admin_logging = AdminLoggingService()
        app.state.admin_vertex = AdminVertexService()
        logger.info("Admin UI services initialized")

    # Initialize GCS service if configured
    if settings.gcs_enabled:
        app.state.gcs = GCSService()
        logger.info(f"GCS file upload enabled (bucket: {settings.gcs_bucket_name})")
    else:
        app.state.gcs = None
        logger.info("GCS file upload disabled (no bucket configured)")

    logger.info("Services initialized successfully")

    # The MCP Streamable HTTP session manager has its own lifecycle — it
    # spawns an anyio task group for in-flight requests and must be torn
    # down when the app shuts down. Nesting its run() context here ensures
    # the task group lives for the duration of the FastAPI lifespan.
    async with scheduler_mcp.session_manager.run():
        yield

    # Shutdown
    logger.info("Shutting down application...")
    # Close any open connections if needed
    # (Firestore AsyncClient and other clients handle cleanup automatically)


def create_app() -> FastAPI:
    """
    Application factory pattern.

    Returns:
        FastAPI: Configured FastAPI application
    """
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        lifespan=lifespan,
        docs_url="/docs" if settings.environment != "production" else None,
        redoc_url=None,
    )

    # Include API routers
    app.include_router(v1_routes.router, prefix=settings.api_v1_prefix)

    # Mount the scheduler MCP server as a raw ASGI sub-app — the MCP Streamable
    # HTTP transport writes directly to the ASGI send() and doesn't fit
    # FastAPI's request/response model.
    app.routes.append(
        Mount(f"{settings.api_v1_prefix}/mcp/scheduler", app=scheduler_mcp.asgi_app)
    )

    if settings.admin_ui_enabled:
        from app.api.admin import routes as admin_routes
        from app.api.admin.auth import build_oauth_client

        app.add_middleware(
            SessionMiddleware,
            secret_key=settings.session_secret,
            same_site="lax",
            https_only=settings.environment == "production",
        )

        base_dir = Path(__file__).parent
        app.state.templates = Jinja2Templates(directory=str(base_dir / "templates"))
        app.state.oauth = build_oauth_client()

        app.include_router(admin_routes.router)
        app.mount(
            "/admin/static",
            StaticFiles(directory=str(base_dir / "static")),
            name="admin-static",
        )

        @app.exception_handler(AdminAuthRequired)
        async def _admin_auth_redirect(request: Request, _exc: AdminAuthRequired):
            return RedirectResponse("/admin/login", status_code=303)

    # Health check endpoint
    @app.get("/health")
    async def health_check():
        """Health check endpoint for monitoring."""
        return JSONResponse(content={"status": "healthy"})

    @app.get("/")
    async def root():
        """Root endpoint."""
        return JSONResponse(
            content={
                "service": settings.app_name,
                "environment": settings.environment,
                "status": "running",
            }
        )

    @app.get("/source")
    async def source_code():
        """AGPL-3.0 compliance: Link to source code."""
        return JSONResponse(
            content={
                "license": "AGPL-3.0",
                "repository": "https://github.com/Comites-ai/the-forum",
                "message": "This software is licensed under AGPL-3.0. You are entitled to the source code.",
            }
        )

    return app


# Create app instance
app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8080,
        reload=True,
        log_level="info",
    )
