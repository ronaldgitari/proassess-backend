"""
RAG Grader — the reflection step between retrieval and augmentation.

A single cheap GPT-4o-mini call judges whether the retrieved context actually
covers the assessment topic well enough to author `n` grounded questions. Its
verdict drives a bounded grade → re-query loop in `rag/__init__.py`:

    sufficient   → augment immediately (as before)
    partial      → reformulate (grader returns refined_query), retrieve again,
                   ACCUMULATE docs, re-grade (capped at settings.MAX_REGRADE)
    insufficient → STOP and raise InsufficientContext — an honest failure instead
                   of generating confidently-wrong questions from weak context.

This is deliberately NOT full agentic RAG: one grader, a hard retry cap, no
planning or multi-tool reasoning. For a curated KB the marginal cost is ~1 cheap
call in the common (sufficient) case, and it only does extra work exactly when
retrieval was weak — the cases that produce bad questions.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain.schema import Document
from langchain_openai import ChatOpenAI

from config import settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# Honest-failure signal
# ─────────────────────────────────────────────────────────────────

class InsufficientContext(Exception):
    """
    Raised when the grader concludes the selected source does not cover the
    topic well enough to author grounded questions. Carries the grader's
    reasoning so the run, the assessment record, and the LM UI can all explain
    *what* was missing rather than just "failed".
    """

    def __init__(self, message: str, *, covered: list[str] | None = None,
                 missing: list[str] | None = None):
        super().__init__(message)
        self.covered = covered or []
        self.missing = missing or []


# ─────────────────────────────────────────────────────────────────
# Prompt
# ─────────────────────────────────────────────────────────────────

GRADE_SYSTEM_PROMPT = """You are a strict retrieval-quality grader for an assessment-authoring pipeline.
Given an assessment topic and the context chunks retrieved from a knowledge base, you judge
whether that context is sufficient to write the requested number of accurate, well-grounded
assessment questions — questions whose correct answers are fully supported by the context.

Be conservative: this context will be used to test real employees, so a question grounded in
thin or off-topic material mis-measures a person. Prefer "partial" or "insufficient" over
optimism when coverage is shallow.

Return ONE JSON object (no array, no markdown, no preamble) with exactly these fields:
{
  "verdict": "sufficient" | "partial" | "insufficient",
  "covered": ["sub-topic the context DOES support", ...],
  "missing": ["sub-topic needed for the requested questions but NOT covered", ...],
  "refined_query": "a reformulated retrieval query targeting the missing sub-topics, or null"
}

Guidance:
- "sufficient": the context broadly covers the topic; enough distinct, on-topic material to
  author the requested number of grounded questions.
- "partial": the context covers some of the topic but has clear gaps; a re-query targeting the
  missing sub-topics could plausibly close them. ALWAYS provide a non-null "refined_query".
- "insufficient": the context is largely off-topic, empty, or far too thin for the topic; no
  reasonable re-query of THIS source would fix it. "refined_query" may be null.
"""

GRADE_USER_TEMPLATE = """Assessment topic: "{topic}"
Domain: {domain}
Number of questions to author: {n}
{context_note}

RETRIEVED CONTEXT ({n_docs} chunks):
{context}

Grade whether this context is sufficient to author {n} grounded questions on the topic.
Return the single JSON object only.
"""

_EMPTY_CONTEXT = "(no chunks retrieved)"

_VALID_VERDICTS = {"sufficient", "partial", "insufficient"}


# ─────────────────────────────────────────────────────────────────
# JSON object extraction (grader returns one object, not an array)
# ─────────────────────────────────────────────────────────────────

def extract_json_object(text: str) -> dict:
    """Robustly extract the first JSON object from GPT output, handling code fences."""
    text = re.sub(r"```(?:json)?\s*", "", text).strip()
    text = text.rstrip("`").strip()

    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in grader response")

    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])

    raise ValueError("Unbalanced JSON object in grader response")


def _build_context_preview(docs: list[Document], max_chars: int = 9000) -> tuple[str, int]:
    """Compact numbered context block for grading (truncated — the grader only needs the gist)."""
    if not docs:
        return _EMPTY_CONTEXT, 0
    lines: list[str] = []
    used = 0
    for i, doc in enumerate(docs, start=1):
        source = doc.metadata.get("source", "Unknown")
        snippet = doc.page_content.strip()
        block = f"[{i}] {source}: {snippet}"
        if used + len(block) > max_chars:
            block = block[: max(0, max_chars - used)]
            lines.append(block)
            break
        lines.append(block)
        used += len(block)
    return "\n\n".join(lines), len(docs)


# ─────────────────────────────────────────────────────────────────
# Public interface
# ─────────────────────────────────────────────────────────────────

async def grade_context(
    *,
    topic: str,
    context_prompt: str | None,
    domain: str,
    num_questions: int,
    docs: list[Document],
) -> dict[str, Any]:
    """
    One cheap, deterministic grading call.

    Returns {"verdict", "covered": [...], "missing": [...], "refined_query": str|None}.
    Fails OPEN: if the grader call/parse errors, returns a "sufficient" verdict so a
    grader hiccup never blocks generation that would otherwise have succeeded.
    """
    if not docs:
        # No retrieval at all — nothing to grade as sufficient.
        return {
            "verdict": "insufficient",
            "covered": [],
            "missing": [topic],
            "refined_query": None,
        }

    context_block, n_docs = _build_context_preview(docs)
    context_note = f"Assessor context prompt: {context_prompt}" if context_prompt else ""
    user_prompt = GRADE_USER_TEMPLATE.format(
        topic=topic, domain=domain, n=num_questions,
        context_note=context_note, context=context_block, n_docs=n_docs,
    )

    llm = ChatOpenAI(
        model=settings.OPENAI_GRADER_MODEL,
        temperature=0.0,                    # deterministic → testable
        openai_api_key=settings.OPENAI_API_KEY,
        request_timeout=60,
    )

    from services import pipeline_tracker as pt
    try:
        async with pt.track_span("openai", f"chat.completion · grade ({settings.OPENAI_GRADER_MODEL})",
                                 phase="grade", detail="grade retrieved context") as span:
            response = await llm.ainvoke([
                {"role": "system", "content": GRADE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ])
            span.capture(response)
        result = extract_json_object(response.content)
    except Exception as e:
        logger.warning("Grader call/parse failed (%s) — failing open to 'sufficient'", e)
        return {"verdict": "sufficient", "covered": [], "missing": [], "refined_query": None}

    verdict = str(result.get("verdict", "")).strip().lower()
    if verdict not in _VALID_VERDICTS:
        logger.warning("Grader returned unknown verdict %r — failing open to 'sufficient'", verdict)
        verdict = "sufficient"

    covered = [str(x) for x in (result.get("covered") or []) if str(x).strip()]
    missing = [str(x) for x in (result.get("missing") or []) if str(x).strip()]
    refined = result.get("refined_query")
    refined = str(refined).strip() if refined else None

    logger.info("Grade verdict=%s covered=%d missing=%d refined=%s",
                verdict, len(covered), len(missing), bool(refined))
    return {"verdict": verdict, "covered": covered, "missing": missing, "refined_query": refined}
