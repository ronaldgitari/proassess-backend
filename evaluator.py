"""
RAG Evaluator — Stage 4 of the pipeline.

Scores submitted staff answers and generates feedback.

  - MCQ: deterministic (correct_index comparison), O(1)
  - Written: GPT-4o rubric scoring, returns 0–100 score + qualitative feedback
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from langchain_openai import ChatOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class AnswerResult:
    question_id: str
    is_correct: bool
    score: float           # 0–100
    ai_feedback: str | None = None


# ─────────────────────────────────────────────────────────────────
# MCQ evaluation — deterministic
# ─────────────────────────────────────────────────────────────────

def evaluate_mcq(
    *,
    question_id: str,
    correct_index: int,
    given_index: int | None,
) -> AnswerResult:
    """Score a single MCQ answer. Binary: 100 correct, 0 wrong."""
    if given_index is None:
        return AnswerResult(question_id=question_id, is_correct=False, score=0.0)

    is_correct = given_index == correct_index
    return AnswerResult(
        question_id=question_id,
        is_correct=is_correct,
        score=100.0 if is_correct else 0.0,
    )


# ─────────────────────────────────────────────────────────────────
# Written evaluation — GPT-4o scoring
# ─────────────────────────────────────────────────────────────────

EVAL_SYSTEM_PROMPT = """You are an expert assessor evaluating staff responses to professional assessment questions.
Score the response on a scale of 0–100 and provide concise constructive feedback.

Scoring guide:
  90–100: Comprehensive, accurate, demonstrates deep understanding
  70–89:  Mostly correct with minor gaps or omissions
  50–69:  Partially correct; key concepts present but incomplete
  30–49:  Some relevant points but significant misunderstanding
  0–29:   Incorrect or no meaningful response

Respond ONLY with a JSON object:
{
  "score": <integer 0-100>,
  "is_correct": <true if score >= 70>,
  "feedback": "<2–3 sentences of specific, constructive feedback>"
}
No other text.
"""

EVAL_USER_TEMPLATE = """QUESTION: {question}

MODEL ANSWER: {model_answer}

STAFF RESPONSE: {staff_response}

Evaluate the staff response against the model answer and return the JSON object.
"""


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=8))
async def evaluate_written(
    *,
    question_id: str,
    question_text: str,
    model_answer: str,
    staff_response: str,
) -> AnswerResult:
    """
    Use GPT-4o to score a written response against the model answer.
    Returns a score (0–100) and qualitative feedback.
    """
    if not staff_response or not staff_response.strip():
        return AnswerResult(
            question_id=question_id,
            is_correct=False,
            score=0.0,
            ai_feedback="No response provided.",
        )

    llm = ChatOpenAI(
        model=settings.OPENAI_CHAT_MODEL,
        temperature=0.1,    # low temp for consistent scoring
        max_tokens=300,
        openai_api_key=settings.OPENAI_API_KEY,
    )

    user_prompt = EVAL_USER_TEMPLATE.format(
        question=question_text,
        model_answer=model_answer,
        staff_response=staff_response,
    )

    messages = [
        {"role": "system", "content": EVAL_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    import json, re

    response = await llm.ainvoke(messages)
    raw = response.content.strip()

    # Strip markdown fences if present
    raw = re.sub(r"```(?:json)?", "", raw).strip("`").strip()

    try:
        data = json.loads(raw)
        score = float(data.get("score", 0))
        is_correct = bool(data.get("is_correct", score >= 70))
        feedback = str(data.get("feedback", ""))
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.error("Written eval JSON parse failed: %s | raw: %s", e, raw[:300])
        # Fallback: try to extract score with regex
        match = re.search(r'"score"\s*:\s*(\d+)', raw)
        score = float(match.group(1)) if match else 50.0
        is_correct = score >= 70
        feedback = "Evaluation completed."

    logger.debug("Written eval Q%s: score=%.0f correct=%s", question_id, score, is_correct)
    return AnswerResult(
        question_id=question_id,
        is_correct=is_correct,
        score=score,
        ai_feedback=feedback,
    )


# ─────────────────────────────────────────────────────────────────
# Batch evaluation
# ─────────────────────────────────────────────────────────────────

async def evaluate_submission(
    answers: list[dict[str, Any]],   # list of {question, staff_answer}
) -> list[AnswerResult]:
    """
    Evaluate a full list of submitted answers.

    Each item in `answers` should contain:
      {
        "question_id": str,
        "question_type": "mcq" | "written",
        "question_text": str,
        "correct_answer_index": int | None,     # MCQ
        "correct_answer_text": str | None,      # written model answer
        "given_index": int | None,
        "given_text": str | None,
      }
    """
    import asyncio

    tasks = []
    for item in answers:
        qtype = item["question_type"]
        if qtype == "mcq":
            # Synchronous — wrap in coroutine for uniform handling
            result = evaluate_mcq(
                question_id=item["question_id"],
                correct_index=item["correct_answer_index"],
                given_index=item.get("given_index"),
            )
            tasks.append(asyncio.coroutine(lambda r=result: r)())
        else:
            tasks.append(evaluate_written(
                question_id=item["question_id"],
                question_text=item["question_text"],
                model_answer=item.get("correct_answer_text") or "",
                staff_response=item.get("given_text") or "",
            ))

    results = await asyncio.gather(*tasks, return_exceptions=False)
    return list(results)


# ─────────────────────────────────────────────────────────────────
# Summary stats
# ─────────────────────────────────────────────────────────────────

def compute_summary(results: list[AnswerResult]) -> dict[str, Any]:
    """Compute aggregate score from a list of AnswerResult."""
    if not results:
        return {"score_pct": 0.0, "questions_correct": 0, "questions_total": 0}

    total = len(results)
    correct = sum(1 for r in results if r.is_correct)
    avg_score = sum(r.score for r in results) / total

    return {
        "score_pct": round(avg_score, 1),
        "questions_correct": correct,
        "questions_total": total,
    }
