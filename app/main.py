# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""FastAPI application entry point."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.routing import Mount

from app.config import get_settings
from app.api.v1 import routes as v1_routes
from app.api.v1 import scheduler_mcp
from app.services.firestore_service import FirestoreService
from app.services.vertex_ai_service import VertexAIService
from app.services.slack_service import SlackService
from app.services.scheduled_job_service import ScheduledJobService
from app.services.gcs_service import GCSService

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
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
