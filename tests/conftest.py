"""
Shared test fixtures.

Phase 1 is pure-logic unit testing: no database, no network. The OpenAI/Chroma
boundary is mocked via `make_fake_chat` / the `fake_chat` fixture, which also
sets the pattern for the Phase 2 integration tests.
"""
import os
import sys

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
