"""
Rich scenario feedback — Increment 2 of the case-study feature.

For each written answer to a case-study question, produce (a) a 0–100 score and
(b) detailed, constructive feedback grounded in the case + marking rubric and,
where available, enriched with CREDIBLE EXTERNAL WEB SOURCES.

Web research is via a pluggable provider (settings.WEB_SEARCH_PROVIDER). When no
provider/key is configured it degrades gracefully to grounded-only feedback —
with the same guardrails as the MCQ/written explanation enrichment (no fabricated
sources, figures, dates, or citations).

The draft score + feedback are NOT final: scenario submissions enter PENDING_REVIEW
and a Line Manager must confirm them (human-assisted verification) before the score
is created.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_openai import ChatOpenAI

from config import settings
from rag.grader import extract_json_object
from rag.web_research import web_search   # shared pluggable web lookup

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# Feedback + scoring
# ─────────────────────────────────────────────────────────────────

FEEDBACK_SYSTEM_PROMPT = """You are an expert assessor giving rich, constructive feedback on a candidate's
written answer to a CASE-STUDY question, and assigning a score from 0 to 100.

Ground your assessment in:
1. THE CASE and the MARKING RUBRIC / MODEL ANSWER (authoritative — judge the answer against these,
   rewarding correct use of evidence from the case).
2. WEB SOURCES provided (credible external references) — use ONLY to enrich the feedback with
   widely-accepted standards / best practice, and cite them by their number [n] when you do.

Rules:
- Score strictly against the rubric and model answer.
- Feedback: 120–220 words. State what the candidate did well, what was missing or weak (referencing
  the rubric criteria), and one or two concrete improvements. Where a web source genuinely adds value,
  weave in a brief, factual, non-controversial point and cite it as [n]. NEVER fabricate sources,
  figures, dates, or citations; cite ONLY from the provided web sources. If none are provided or
  relevant, give grounded feedback with no external citations.
- Return ONE JSON object: {"score": <integer 0-100>, "feedback": "<markdown feedback>"}.
"""

FEEDBACK_USER_TEMPLATE = """TOPIC: {topic}

CASE:
{case}

QUESTION:
{question}

MARKING RUBRIC:
{rubric}

MODEL ANSWER:
{model_answer}

CANDIDATE'S ANSWER:
{answer}

WEB SOURCES (cite by [n] only if used):
{web_block}

Return the single JSON object only.
"""


async def generate_scenario_feedback(
    *,
    topic: str,
    question_text: str,
    rubric: str | None,
    model_answer: str | None,
    staff_response: str | None,
    case_text: str | None,
) -> dict[str, Any]:
    """
    Returns {"score": float 0-100, "feedback": str, "sources": [{title,url,snippet,kind}]}.
    Draft only — confirmed by a Line Manager before the score is finalised.
    """
    # Empty answer → deterministic zero, no GPT/web needed.
    if not staff_response or not staff_response.strip():
        return {"score": 0.0, "feedback": "No answer was provided for this question.", "sources": []}

    sources = await web_search(f"{topic} — {question_text}")
    web_block = "\n".join(
        f"[{i + 1}] {s['title']} — {s['url']}\n{s['snippet']}" for i, s in enumerate(sources)
    ) or "(none available)"

    user = FEEDBACK_USER_TEMPLATE.format(
        topic=topic,
        case=case_text or "(case unavailable)",
        question=question_text,
        rubric=rubric or "(no rubric)",
        model_answer=model_answer or "(no model answer)",
        answer=staff_response,
        web_block=web_block,
    )

    llm = ChatOpenAI(
        # Draft feedback is human-reviewed by an LM before release, so the cheaper
        # model is sufficient and far less costly at scale (configurable).
        model=settings.OPENAI_FEEDBACK_MODEL,
        temperature=0.2,
        max_tokens=settings.OPENAI_MAX_TOKENS,
        openai_api_key=settings.OPENAI_API_KEY,
        request_timeout=120,
    )

    from services import pipeline_tracker as pt
    try:
        async with pt.track_span("openai", "chat.completion · scenario feedback",
                                 phase="feedback", detail=question_text[:60]) as span:
            resp = await llm.ainvoke([
                {"role": "system", "content": FEEDBACK_SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ])
            span.capture(resp)
        obj = extract_json_object(resp.content)
        score = max(0.0, min(100.0, float(obj.get("score", 0))))
        feedback = str(obj.get("feedback", "")).strip() or "Feedback unavailable."
    except Exception as e:
        logger.warning("scenario feedback generation failed: %s", e)
        # Non-fatal: leave a 0 draft with a note; the LM review step will catch it.
        return {"score": 0.0,
                "feedback": "Automated feedback could not be generated — please review manually.",
                "sources": sources}

    return {"score": round(score, 1), "feedback": feedback, "sources": sources}
