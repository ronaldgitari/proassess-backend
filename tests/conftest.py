"""
Shared test fixtures.

Phase 1 is pure-logic unit testing: no database, no network. The OpenAI/Chroma
boundary is mocked via `make_fake_chat` / the `fake_chat` fixture, which also
sets the pattern for the Phase 2 integration tests.
"""
import os
import sys

# ─────────────────────────────────────────────────────────────────
# Test database wiring (MUST run before `config`/`database` are imported)
# ─────────────────────────────────────────────────────────────────
# Phase 2 integration tests talk to a REAL but DISPOSABLE Postgres database
# (JSONB / native enums mean SQLite won't do). We point the whole app at a
# dedicated `proassess_test` database so the demo data is never touched.
#
# This MUST happen at import time of the root conftest — before anything imports
# `config` (which builds a cached Settings singleton) or `database` (which builds
# the async engine from that singleton). The container ships a real DATABASE_URL
# as an OS env var, so we *force* (not setdefault) the test value over it.
#
# Setting these env vars is harmless for the pure-logic unit tests: they never
# open a connection. Override the target with TEST_DATABASE_URL if needed.
_TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://proassess:proassess@postgres:5432/proassess_test",
)
os.environ["DATABASE_URL"] = _TEST_DB_URL
os.environ["DATABASE_URL_SYNC"] = _TEST_DB_URL.replace("+asyncpg", "")
os.environ["APP_ENV"] = "test"          # quietens SQL echo; not "development"/"production"
# Ensure JWT signing works even if the container didn't pass a SECRET_KEY through.
os.environ.setdefault("SECRET_KEY", "integration-test-secret-key")

# Make the project root (the dir containing rag/, services/, models/, schemas/) importable
# regardless of where pytest is invoked from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


class _FakeResponse:
    """Mimics a LangChain chat response — only `.content` is used by the app."""
    def __init__(self, content: str):
        self.content = content


def make_fake_chat(content: str = "{}", raises: BaseException | None = None):
    """
    Build a drop-in replacement for `ChatOpenAI`. Instances' `.ainvoke(...)`
    return a response whose `.content` is `content` (or raise `raises`).

    Install where a module imported the symbol, e.g.:
        monkeypatch.setattr("rag.grader.ChatOpenAI", make_fake_chat(content='{"verdict":"sufficient"}'))
    """
    class _FakeChatOpenAI:
        def __init__(self, *args, **kwargs):
            pass

        async def ainvoke(self, *args, **kwargs):
            if raises is not None:
                raise raises
            return _FakeResponse(content)

    return _FakeChatOpenAI


@pytest.fixture
def fake_chat():
    """Factory fixture: `fake_chat(content=..., raises=...)` -> a fake ChatOpenAI class."""
    return make_fake_chat
