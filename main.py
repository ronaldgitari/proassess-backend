"""
ProAssess API — FastAPI application entry point.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import settings
from database import create_tables
from api import auth_router, assessments_router, knowledge_router, admin_router, ops_router, users_router, groups_router, system_settings_router

# ─────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO if settings.APP_ENV == "production" else logging.DEBUG,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# Lifespan (startup / shutdown)
# ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting ProAssess API [env=%s]", settings.APP_ENV)

    if settings.APP_ENV == "development":
        # Auto-create tables in dev (use Alembic migrations in production)
        await create_tables()
        logger.info("Database tables verified")

    # Warm up re-ranker model on first request avoidance
    try:
        from rag.retriever import get_reranker
        get_reranker()
        logger.info("Cross-encoder re-ranker loaded")
    except Exception as e:
        logger.warning("Could not preload re-ranker: %s", e)

    yield

    logger.info("ProAssess API shutting down")


# ─────────────────────────────────────────────────────────────────
# Application
# ─────────────────────────────────────────────────────────────────

app = FastAPI(
    title="ProAssess API",
    description="Staff proficiency assessment platform with Agentic RAG",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.APP_ENV != "production" else None,
    redoc_url="/redoc" if settings.APP_ENV != "production" else None,
)

# ── CORS ──────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Global exception handlers ─────────────────────────────────────

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal error occurred. Please try again."},
    )

# ── Routers ───────────────────────────────────────────────────────

app.include_router(auth_router,            prefix="/api/v1")
app.include_router(assessments_router,     prefix="/api/v1")
app.include_router(knowledge_router,       prefix="/api/v1")
app.include_router(admin_router,           prefix="/api/v1")
app.include_router(ops_router,             prefix="/api/v1")
app.include_router(users_router,           prefix="/api/v1")
app.include_router(groups_router,          prefix="/api/v1")
app.include_router(system_settings_router, prefix="/api/v1")


# ── Health check ──────────────────────────────────────────────────

@app.get("/health", tags=["system"])
async def health():
    return {"status": "ok", "env": settings.APP_ENV}


@app.get("/api/v1/pre-check", tags=["system"])
async def pre_check(latency_ms: float = 0, browser: str = "", os: str = ""):
    """
    Client-side latency and browser compatibility check endpoint.
    Returns pass/warn status.
    """
    issues = []

    if latency_ms > 500:
        issues.append(f"High latency detected: {latency_ms:.0f}ms (threshold: 500ms)")

    SUPPORTED_BROWSERS = {
        "Chrome": 110, "Firefox": 115, "Edge": 110, "Safari": 16
    }
    for name, min_ver in SUPPORTED_BROWSERS.items():
        if name.lower() in browser.lower():
            try:
                version = int(browser.lower().split(name.lower())[1].split("/")[1].split(".")[0])
                if version < min_ver:
                    issues.append(f"{name} {version} is below minimum version {min_ver}")
            except (IndexError, ValueError):
                pass

    return {
        "passed": len(issues) == 0,
        "issues": issues,
        "latency_ms": latency_ms,
    }
