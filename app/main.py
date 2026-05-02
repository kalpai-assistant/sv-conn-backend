"""
FastAPI application factory — lifespan, middleware stack, router registration,
and exception handler wiring. Exact from LLD.
"""

import logging
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from app.config import get_settings
from app.routers import (
    auth,
    users,
    providers,
    services,
    bookings,
    payments,
    reviews,
    tracking,
    admin,
)
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_id import RequestIDMiddleware
from app.middleware.auth import JWTAttachMiddleware
from app.exceptions import register_exception_handlers

# Configure structured application-wide logging
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:\t  [%(name)s] %(message)s",
    stream=sys.stdout,
)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create Redis connection pool and attach to app state
    app.state.redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    yield
    # Shutdown: close connections gracefully
    await app.state.redis.aclose()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        version="1.0.0",
        description="On-demand home services marketplace API",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # -----------------------------------------------------------------------
    # Middleware (applied outermost-first by FastAPI/Starlette)
    # -----------------------------------------------------------------------
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(JWTAttachMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",  # React dev server
            "http://localhost:8080",  # Vite dev (alternate port)
            "http://192.168.1.97:8080",  # Phone testing via Mac IP
            "http://192.168.0.153:8080",  # Phone testing via Mac IP
            "https://app.serviceconnect.in",  # Production web
            # Capacitor mobile app origins
            "capacitor://localhost",  # iOS Capacitor WebView
            "https://localhost",  # Android Capacitor WebView (androidScheme=https)
            "http://localhost",  # Android Capacitor WebView (fallback)
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Rate limiter needs Redis — deferred wiring via dependency, but middleware
    # instance is created with a fresh Redis connection from settings.
    # NOTE: for a production setup, share the app.state.redis pool here.
    rate_redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    app.add_middleware(RateLimitMiddleware, redis=rate_redis, requests_per_minute=600)

    # -----------------------------------------------------------------------
    # Routers
    # -----------------------------------------------------------------------
    prefix = settings.API_V1_PREFIX
    app.include_router(auth.router, prefix=prefix)
    app.include_router(users.router, prefix=prefix)
    app.include_router(providers.router, prefix=prefix)
    app.include_router(services.router, prefix=prefix)
    app.include_router(bookings.router, prefix=prefix)
    app.include_router(payments.router, prefix=prefix)
    app.include_router(reviews.router, prefix=prefix)
    app.include_router(tracking.router, prefix=prefix)
    app.include_router(admin.router, prefix=prefix)

    # -----------------------------------------------------------------------
    # Health check
    # -----------------------------------------------------------------------
    @app.get("/health", tags=["Health"])
    async def health():
        return {"status": "ok"}

    # -----------------------------------------------------------------------
    # Exception handlers
    # -----------------------------------------------------------------------
    register_exception_handlers(app)

    return app


app = create_app()
