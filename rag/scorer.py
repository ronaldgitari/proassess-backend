"""
RAG-quality scorer (RAGAS-style) — faithfulness + context precision.

A lightweight LLM-judge that mirrors `rag.grader`, but uses a **Qwen** model via any
OpenAI-compatible endpoint (Alibaba DashScope / OpenRouter / Together / local vLLM).
Configure via RAG_SCORER_MODEL / RAG_SCORER_BASE_URL / RAG_SCORER_API_KEY (.env).

Metrics (both 0..1):
  • faithfulness       — are the generated questions + correct answers fully grounded
                         in the retrieved context (penalises any ungrounded fact)?
  • context_precision  — fraction of retrieved chunks actually relevant to the topic/items.

Fails OPEN/soft: any judge error returns None scores (never raises into the pipeline).
"""
from __future__ import annotations

import logging
from typing import Any

from langchain_openai import ChatOpenAI

from config import settings
from rag.grader import extract_json_object

logger = logging.getLogger(__name__)

_SYSTEM = ("You are a strict, impartial RAG evaluation judge. Judge ONLY from the evidence "
           "provided. Do not use outside knowledge. Respond with a single JSON object, nothing else.")

_TEMPLATE = """Evaluate one retrieval-augmented generation result for an assessment platform.

TOPIC / QUERY:
{query}

RETRIEVED CONTEXT CHUNKS (numbered):
{contexts}

GENERATED ITEMS (each = a question, its correct answer, and the explanation):
{items}

Score strictly and return ONLY this JSON:
{{
  "faithfulness": <number 0..1 — fraction of generated items whose question AND correct
                   answer are fully supported by the context; penalise any fact, figure,
                   or claim not grounded in the chunks>,
  "context_precision": <number 0..1 — fraction of the numbered chunks that are actually
                        relevant to the topic and the generated items>,
  "unsupported": [<=3 short notes naming any ungrounded claims, else []],
  "rationale": "<one short sentence>"
}}"""


def is_configured() -> bool:
    return bool(settings.RAG_SCORER_API_KEY)


def _clamp(v: Any) -> float | None:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return None


async def score_sample(query: str, contexts: list[str], items: list[dict]) -> dict:
    """Return {available, faithfulness, context_precision, unsupported, rationale, [error]}."""
    if not is_configured():
        return {"available": False, "reason": "RAG_SCORER_API_KEY not set"}
    if not contexts or not items:
        return {"available": True, "faithfulness": None, "context_precision": None, "reason": "no context/items"}

    ctx_block = "\n".join(f"[{i + 1}] {(c or '')[:900]}" for i, c in enumerate(contexts))
    items_block = "\n".join(
        f"- Q: {it.get('question', '')}\n  A: {it.get('answer', '')}\n  Why: {(it.get('explanation') or '')[:300]}"
        for it in items
    )
    prompt = _TEMPLATE.format(query=query, contexts=ctx_block, items=items_block)

    llm = ChatOpenAI(
        model=settings.RAG_SCORER_MODEL,
        base_url=settings.RAG_SCORER_BASE_URL,
        api_key=settings.RAG_SCORER_API_KEY,
        temperature=0.0,
        request_timeout=90,
    )

    from services import pipeline_tracker as pt
    try:
        async with pt.track_span("qwen", "chat.completion · rag-score", phase="score"):
            resp = await llm.ainvoke([
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ])
        obj = extract_json_object(resp.content)
        return {
            "available": True,
            "faithfulness": _clamp(obj.get("faithfulness")),
            "context_precision": _clamp(obj.get("context_precision")),
            "unsupported": (obj.get("unsupported") or [])[:3],
            "rationale": str(obj.get("rationale", ""))[:300],
        }
    except Exception as e:
        logger.warning("rag scorer failed: %s", e)
        return {"available": True, "faithfulness": None, "context_precision": None, "error": str(e)[:200]}
