"""
Phase 2 — API integration test harness.

Drives the real FastAPI app over an in-process ASGI transport against a real
(but disposable) Postgres database. Only the AI seam is mocked.

Design decisions (see CLAUDE.md "Testing — remaining phases"):

* **Real Postgres, throwaway DB.** The app uses JSONB columns + native enums, so
  SQLite can't stand in. The root `tests/conftest.py` forces DATABASE_URL at the
  `proassess_test` database; here we create that DB + schema with a *sync* engine
  (psycopg2) so setup/teardown never touch an asyncio event loop.

* **Committed isolation, not savepoints.** The app's `get_db` commits, and
  background tasks / `pipeline_tracker` open their *own* `AsyncSessionLocal`
  sessions — a SAVEPOINT-on-one-connection scheme can't span those. Instead we
  TRUNCATE every table before each test; each test seeds the org it needs.

* **Async engine disposed per test.** pytest-asyncio runs each test in its own
  (function-scoped) event loop. The app's global async engine pools connections,
  and asyncpg connections are bound to the loop that created them — so we dispose
  the engine after every test to avoid "Future attached to a different loop".

* **AI seam mocked.** Background question generation is stubbed to a no-op
  (tests insert questions directly via `add_questions`); GPT written-eval / scenario
  feedback are monkeypatched per-test where exercised.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime
from types import SimpleNamespace

import httpx
import psycopg2
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import create_engine, text


PASSWORD = "Password123!"


# ─────────────────────────────────────────────────────────────────
# Safe-by-construction test-DB binding (runs at import, before any test)
# ─────────────────────────────────────────────────────────────────
# History: a stray /app/__init__.py once caused `config` (and its cached Settings)
# to be imported BEFORE the root conftest's env override ran, so the override was a
# no-op and the suite hit the REAL `proassess` database — the per-test TRUNCATE then
# wiped live data. That root cause is removed, but isolation must NOT depend on
# import order. So here, at import time, we:
#   1. derive a `*_test` database URL ourselves (never trust the ambient value),
#   2. ASSERT the database name ends in `_test` — making it impossible to truncate a
#      real DB even if the ambient config is wrong,
#   3. rebind config.settings + database.engine/AsyncSessionLocal to it, so the app's
#      `get_db`, background tasks and `pipeline_tracker` all resolve to the test DB.

def _as_test_url(url: str) -> str:
    """Force the database name in a SQLAlchemy URL to end with `_test`."""
    base, name = url.rsplit("/", 1)
    name = name.split("?", 1)[0]
    if not name.endswith("_test"):
        name += "_test"
    return f"{base}/{name}"


import config as _config

_TEST_ASYNC_URL = _as_test_url(os.environ.get("DATABASE_URL") or _config.settings.DATABASE_URL)
_TEST_SYNC_URL = _as_test_url(os.environ.get("DATABASE_URL_SYNC") or _config.settings.DATABASE_URL_SYNC)

# Hard safety net — refuse to operate on anything that isn't a *_test database.
assert _TEST_ASYNC_URL.rsplit("/", 1)[1].endswith("_test"), f"refusing non-test DB: {_TEST_ASYNC_URL}"
assert _TEST_SYNC_URL.rsplit("/", 1)[1].endswith("_test"), f"refusing non-test DB: {_TEST_SYNC_URL}"

os.environ["DATABASE_URL"] = _TEST_ASYNC_URL
os.environ["DATABASE_URL_SYNC"] = _TEST_SYNC_URL
_config.get_settings.cache_clear()
_config.settings = _config.get_settings()

# Rebind the app's async engine + sessionmaker. `database.get_db`, background tasks
# and pipeline_tracker look these names up on the `database` module at call time, so
# reassigning them here redirects the whole app at the test DB regardless of how the
# original engine was bound.
import database as _database
from sqlalchemy.ext.asyncio import (
    create_async_engine as _create_async_engine,
    async_sessionmaker as _async_sessionmaker,
    AsyncSession as _AsyncSession,
)

_database.engine = _create_async_engine(_TEST_ASYNC_URL, pool_pre_ping=True)
_database.AsyncSessionLocal = _async_sessionmaker(
    _database.engine, class_=_AsyncSession, expire_on_commit=False, autoflush=False,
)


# ─────────────────────────────────────────────────────────────────
# Database lifecycle (sync — no event loop involved)
# ─────────────────────────────────────────────────────────────────

def _assert_test_db(url: str) -> str:
    """Guard every destructive operation: the target DB name MUST end in `_test`."""
    name = url.rsplit("/", 1)[1].split("?", 1)[0]
    assert name.endswith("_test"), f"refusing to operate on non-test database: {name!r}"
    return name


@pytest.fixture(scope="session", autouse=True)
def _test_database():
    """Create the throwaway `*_test` database + full schema once per session."""
    import models  # noqa: F401 — registers every table on Base.metadata
    from database import Base

    db_name = _assert_test_db(_TEST_SYNC_URL)
    admin_url = _TEST_SYNC_URL.rsplit("/", 1)[0] + "/postgres"   # maintenance DB

    conn = psycopg2.connect(admin_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(f'CREATE DATABASE "{db_name}"')
            except psycopg2.errors.DuplicateDatabase:
                pass
    finally:
        conn.close()

    sync_engine = create_engine(_TEST_SYNC_URL)
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    yield


@pytest.fixture(scope="session")
def _sync_engine():
    _assert_test_db(_TEST_SYNC_URL)
    eng = create_engine(_TEST_SYNC_URL)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def _clean_tables(_test_database, _sync_engine):
    """Wipe all rows before each test so every test starts from a known-empty DB."""
    from database import Base
    _assert_test_db(_TEST_SYNC_URL)   # re-check at every truncate — belt and suspenders
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with _sync_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    """Drop pooled async connections after each test (they're bound to this loop)."""
    yield
    from database import engine
    await engine.dispose()


@pytest.fixture(autouse=True)
def _stub_generation(monkeypatch):
    """Neutralise background RAG generation — it would hit OpenAI/Chroma over the
    network when `create` fires its BackgroundTask. Tests that need questions insert
    them directly via `add_questions`."""
    async def _noop(*args, **kwargs):
        return None
    monkeypatch.setattr(
        "services.assessment_service._generate_questions_background", _noop
    )


# ─────────────────────────────────────────────────────────────────
# HTTP client + DB session
# ─────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client():
    from main import app
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def db():
    """A session for test-side seeding and assertions (sees committed app writes)."""
    from database import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        yield session


# ─────────────────────────────────────────────────────────────────
# Seed: one organisation with a user per role (mirrors seed.py, smaller)
# ─────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def org(_clean_tables, db):
    """Seed an org with default security groups, two departments and one user per
    role. staff1 → Engineering (reports to lm_eng); staff2 → Sales (reports to
    lm_sales). Returns a namespace of ORM objects + convenience emails."""
    from models import User, Organisation, Department, UserDepartment, UserRole
    from services.permissions import ensure_default_groups
    from services.auth_service import hash_password

    now = datetime.utcnow()
    organisation = Organisation(id=uuid.uuid4(), name="Test Org", slug="test-org", created_at=now)
    db.add(organisation)
    await db.flush()

    await ensure_default_groups(db, organisation.id)

    eng = Department(id=uuid.uuid4(), org_id=organisation.id, name="Engineering", created_at=now)
    sales = Department(id=uuid.uuid4(), org_id=organisation.id, name="Sales", created_at=now)
    db.add_all([eng, sales])
    await db.flush()

    def mk(email, name, role):
        return User(
            id=uuid.uuid4(), email=email, name=name,
            hashed_password=hash_password(PASSWORD), role=role,
            org_id=organisation.id, is_active=True, created_at=now, updated_at=now,
        )

    sysadmin = mk("sysadmin@t.com", "Sys Admin", UserRole.SYSTEM_ADMIN)
    hr       = mk("hr@t.com",       "HR Admin",  UserRole.HR_ADMIN)
    lm_eng   = mk("lm.eng@t.com",   "LM Eng",    UserRole.LINE_MANAGER)
    lm_sales = mk("lm.sales@t.com", "LM Sales",  UserRole.LINE_MANAGER)
    staff1   = mk("staff1@t.com",   "Staff One", UserRole.STAFF)
    staff2   = mk("staff2@t.com",   "Staff Two", UserRole.STAFF)
    db.add_all([sysadmin, hr, lm_eng, lm_sales, staff1, staff2])
    await db.flush()

    db.add_all([
        UserDepartment(user_id=lm_eng.id,   department_id=eng.id,   title="Engineering Manager", line_manager_id=None),
        UserDepartment(user_id=staff1.id,   department_id=eng.id,   title="Software Engineer",   line_manager_id=lm_eng.id),
        UserDepartment(user_id=lm_sales.id, department_id=sales.id, title="Sales Manager",       line_manager_id=None),
        UserDepartment(user_id=staff2.id,   department_id=sales.id, title="Account Executive",   line_manager_id=lm_sales.id),
    ])
    await db.commit()

    return SimpleNamespace(
        id=organisation.id,
        dept_eng=eng, dept_sales=sales,
        sysadmin=sysadmin, hr=hr, lm_eng=lm_eng, lm_sales=lm_sales,
        staff1=staff1, staff2=staff2,
    )


@pytest_asyncio.fixture
def login(client):
    """`await login(email)` → Authorization header dict for that user."""
    async def _login(email: str, password: str = PASSWORD) -> dict[str, str]:
        r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200, f"login failed for {email}: {r.status_code} {r.text}"
        return {"Authorization": f"Bearer {r.json()['access_token']}"}
    return _login


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

async def add_questions(db, assessment_id, n=3, qtype=None, correct_index=0):
    """Insert `n` MCQ questions directly (simulating generation completing).
    Returns the created Question rows (committed)."""
    from models import Question, QuestionType
    qtype = qtype or QuestionType.MCQ
    created = []
    for i in range(n):
        q = Question(
            id=uuid.uuid4(), assessment_id=assessment_id, order_index=i,
            text=f"Question {i+1}?", question_type=qtype,
            options=["A", "B", "C", "D"],
            correct_answer_index=correct_index,
            correct_answer_text="A",
            explanation="Because A.", difficulty=3,
        )
        db.add(q)
        created.append(q)
    await db.commit()
    return created
