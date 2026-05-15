"""
Enterprise FastAPI application entry point for Ghana Goethe Booking Automation.
Implements lifecycle management, middleware, exception handlers, and background tasks.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Dict, Any

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import redis.asyncio as redis
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.logging_config import setup_logging, get_logger
from src.core.exceptions import (
    AppException,
    NotFoundError,
    AuthenticationError,
    AutomationError,
    handle_app_exception,
)
from src.database.session import async_session_maker, init_db
from src.core.cache.redis_client import RedisClient
from src.core.events.event_bus import EventBus
from src.services.task_queue.background_tasks import BackgroundTaskManager
from src.services.automation.playwright_manager import PlaywrightManager
from src.services.proxy.proxy_manager import ProxyManager
from src.api.v1.router import api_router

# Initialize logging
logger = setup_logging()
access_logger = get_logger("access")

# Rate limiter
limiter = Limiter(key_func=get_remote_address, default_limits=["100/minute"])
app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    description="Enterprise Automation Backend for Goethe Institut Ghana Booking System",
    docs_url="/docs" if settings.ENVIRONMENT != "production" else None,
    redoc_url="/redoc" if settings.ENVIRONMENT != "production" else None,
    openapi_url="/openapi.json" if settings.ENVIRONMENT != "production" else None,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Handle startup and shutdown events with proper resource cleanup."""
    logger.info(f"Starting {settings.APP_NAME} in {settings.ENVIRONMENT} mode")
    
    # Initialize database
    await init_db()
    logger.info("Database initialized")
    
    # Initialize Redis connection
    redis_client = await RedisClient.get_instance()
    await redis_client.ping()
    logger.info("Redis connection established")
    
    # Initialize event bus
    event_bus = EventBus()
    app.state.event_bus = event_bus
    asyncio.create_task(event_bus.start())
    
    # Initialize background task manager
    task_manager = BackgroundTaskManager()
    app.state.task_manager = task_manager
    await task_manager.start()
    
    # Initialize Playwright manager
    playwright_manager = PlaywrightManager()
    app.state.playwright_manager = playwright_manager
    await playwright_manager.initialize()
    
    # Initialize proxy manager
    proxy_manager = ProxyManager()
    app.state.proxy_manager = proxy_manager
    await proxy_manager.initialize()
    
    logger.info("All services initialized successfully")
    yield
    
    # Shutdown gracefully
    logger.info("Shutting down services...")
    await task_manager.stop()
    await playwright_manager.close_all()
    await proxy_manager.close()
    await redis_client.close()
    logger.info("Shutdown complete")


app.router.lifespan_context = lifespan

# Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS if hasattr(settings, "CORS_ORIGINS") else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=settings.ALLOWED_HOSTS if hasattr(settings, "ALLOWED_HOSTS") else ["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Request logging middleware with timing."""
    import time
    start_time = time.time()
    
    response = await call_next(request)
    
    process_time = time.time() - start_time
    access_logger.info(
        f"{request.method} {request.url.path} - {response.status_code} - {process_time:.3f}s"
    )
    response.headers["X-Process-Time"] = str(process_time)
    return response


@app.exception_handler(AppException)
async def app_exception_handler(request: Request, exc: AppException):
    """Global exception handler for application-specific errors."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error_code": exc.error_code,
            "message": exc.message,
            "details": exc.details,
        },
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    """Catch-all exception handler for unhandled errors."""
    logger.exception(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "success": False,
            "error_code": "INTERNAL_SERVER_ERROR",
            "message": "An unexpected error occurred",
        },
    )


@app.get("/health", tags=["System"])
async def health_check() -> Dict[str, Any]:
    """Comprehensive health check endpoint."""
    health_status = {
        "status": "healthy",
        "app": settings.APP_NAME,
        "environment": settings.ENVIRONMENT,
        "services": {}
    }
    
    # Check database
    try:
        from sqlalchemy import text
        async with async_session_maker() as session:
            await session.execute(text("SELECT 1"))
        health_status["services"]["database"] = "up"
    except Exception as e:
        health_status["services"]["database"] = f"down: {str(e)}"
        health_status["status"] = "unhealthy"
    
    # Check Redis
    try:
        redis_client = await RedisClient.get_instance()
        await redis_client.ping()
        health_status["services"]["redis"] = "up"
    except Exception as e:
        health_status["services"]["redis"] = f"down: {str(e)}"
        health_status["status"] = "unhealthy"
    
    # Check Playwright
    playwright_manager: PlaywrightManager = app.state.playwright_manager
    active_sessions = playwright_manager.get_active_session_count()
    health_status["services"]["playwright"] = f"up (active sessions: {active_sessions})"
    
    return health_status


@app.get("/metrics", tags=["System"])
async def get_metrics():
    """Expose Prometheus-style metrics (simplified)."""
    from src.services.automation.playwright_manager import PlaywrightManager
    from src.core.cache.redis_client import RedisClient
    
    playwright_manager: PlaywrightManager = app.state.playwright_manager
    redis_client = await RedisClient.get_instance()
    
    metrics = {
        "active_browser_sessions": playwright_manager.get_active_session_count(),
        "total_bookings_today": await redis_client.get("metrics:bookings_today") or 0,
        "successful_bookings": await redis_client.get("metrics:successful_bookings") or 0,
        "failed_automations": await redis_client.get("metrics:failed_automations") or 0,
        "proxy_rotation_count": await redis_client.get("metrics:proxy_rotations") or 0,
    }
    return {"metrics": metrics}


@app.get("/", tags=["System"])
async def root():
    """Root endpoint."""
    return {
        "app": settings.APP_NAME,
        "version": "1.0.0",
        "status": "operational",
        "documentation": "/docs" if settings.ENVIRONMENT != "production" else "disabled",
    }


# Include API routers
app.include_router(api_router, prefix=settings.API_V1_PREFIX)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.DEBUG,
        workers=settings.WORKERS_COUNT if hasattr(settings, "WORKERS_COUNT") else 1,
        log_level=settings.LOG_LEVEL.lower(),
    )