"""
Web research — pluggable credible-source lookup, shared by:
  - rich scenario feedback (rag/feedback.py)
  - HYBRID question generation (KB doc + domain/industry web case-study sources)

Provider is configured via settings.WEB_SEARCH_PROVIDER (Tavily wired). When no
provider/key is set, every call returns [] and callers degrade gracefully
(grounded-only feedback / KB-only generation). Errors are non-fatal.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain.schema import Document

from config import settings

logger = logging.getLogger(__name__)


async def web_search(query: str, max_results: int | None = None, *, phase: str = "feedback") -> list[dict[str, Any]]:
    """Return credible external sources for `query` as
    [{title, url, snippet, kind:"web"}], or [] when disabled/failed."""
    provider = (settings.WEB_SEARCH_PROVIDER or "").strip().lower()
    if not provider or not settings.WEB_SEARCH_API_KEY:
        return []
    n = max_results or settings.WEB_SEARCH_MAX_RESULTS

    from services import pipeline_tracker as pt
    try:
        if provider == "tavily":
            import httpx
            async with pt.track_span("web", "tavily.search", phase=phase, detail=query[:80]):
                async with httpx.AsyncClient(timeout=20) as client:
                    resp = await client.post(
                        "https://api.tavily.com/search",
                        json={
                            "api_key": settings.WEB_SEARCH_API_KEY,
                            "query": query,
                            "max_results": n,
                            "search_depth": "basic",
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
            out = []
            for it in (data.get("results") or [])[:n]:
                out.append({
                    "title": it.get("title") or it.get("url", ""),
                    "url": it.get("url", ""),
                    "snippet": (it.get("content") or "")[:500],
                    "kind": "web",
                })
            return out

        logger.warning("Unknown WEB_SEARCH_PROVIDER %r — skipping web research", provider)
        return []
    except Exception as e:
        logger.warning("web_search failed (%s) — degrading to no web sources", e)
        return []


async def gather_web_context(
    topic: str, domain: str, context_prompt: str | None = None,
) -> tuple[list[Document], list[dict[str, Any]]]:
    """
    For HYBRID generation: fetch credible, domain/industry-relevant case-study
    sources for the topic and return them as (context Documents, raw sources).
    The Documents slot straight into the augmentor's context block alongside KB
    chunks; the raw sources are kept for provenance (assessment.rag_metadata).
    """
    queries = [
        f"{topic} {domain} case study",
        f"{topic} {domain} best practices industry standards",
    ]
    if context_prompt:
        queries.append(f"{topic} {context_prompt}")

    seen: set[str] = set()
    docs: list[Document] = []
    sources: list[dict[str, Any]] = []
    for q in queries:
        for s in await web_search(q, phase="web"):
            url = s.get("url") or ""
            key = url or s.get("title", "")
            if not key or key in seen or not s.get("snippet"):
                continue
            seen.add(key)
            docs.append(Document(
                page_content=f"{s['title']}\n{s['snippet']}",
                metadata={"source": s["title"], "url": url, "kind": "web"},
            ))
            sources.append(s)
    logger.info("gather_web_context: %d unique web sources for '%s' (%s)", len(docs), topic, domain)
    return docs, sources
