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
    # Self-correcting eval fields (None for MCQ / single-pass paths)
    eval_attempts: int | None = None
    eval_flagged: bool = False
    eval_scratchpad: list | None = None


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
    explanation: str | None = None,
    question_type: str = "written",   # "written" | "coding" | "scenario"
) -> AnswerResult:
    """
    Score a written/coding/scenario response against the model answer.

    When settings.ENABLE_SELF_CORRECTING_EVAL is True and the question type is
    written, coding, or scenario, the Maker → Checker → Router loop is used.
    Falls back to a single-pass GPT call on loop errors or when disabled.
    Returns a score (0–100) and qualitative feedback.
    """
    if not staff_response or not staff_response.strip():
        return AnswerResult(
            question_id=question_id,
            is_correct=False,
            score=0.0,
            ai_feedback="No response provided.",
        )

    # ── Self-correcting path ──────────────────────────────────────
    if settings.ENABLE_SELF_CORRECTING_EVAL and question_type in ("written", "coding", "scenario"):
        try:
            from rag.self_correcting_evaluator import CaseFile, evaluate_with_correction
            case_file = CaseFile(
                question_text=question_text,
                candidate_answer=staff_response,
                correct_reference=model_answer,
                explanation=explanation or "",
                question_type=question_type,
            )
            result = await evaluate_with_correction(case_file)
            logger.debug(
                "Self-correcting eval Q%s: score=%.0f correct=%s attempts=%s flagged=%s",
                question_id, result["score"], result["is_correct"],
                result["eval_attempts"], result["eval_flagged"],
            )
            return AnswerResult(
                question_id=question_id,
                is_correct=result["is_correct"],
                score=result["score"],
                ai_feedback=result["feedback"],
                eval_attempts=result["eval_attempts"],
                eval_flagged=result["eval_flagged"],
                eval_scratchpad=result["eval_scratchpad"] or None,
            )
        except Exception as e:
            logger.error(
                "Self-correcting eval failed for Q%s, falling back to single-pass: %s",
                question_id, e,
            )
            # Fall through to single-pass below

    # ── Single-pass fallback ──────────────────────────────────────
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
    from services import pipeline_tracker as pt

    async with pt.track_span("openai", f"chat.completion · {settings.OPENAI_CHAT_MODEL}", phase="score", detail="written/coding evaluation") as span:
        response = await llm.ainvoke(messages)
        span.capture(response)
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
# Personality assessment (16 Personalities / MBTI-style)
# ─────────────────────────────────────────────────────────────────

# Each dimension: positive pole letter / negative pole letter + friendly labels.
# "Agreeing" with a question keyed to `pos` pushes the score toward the positive pole.
PERSONALITY_DIMENSIONS: dict[str, dict[str, str]] = {
    "mind":     {"pos": "E", "neg": "I", "pos_label": "Extraverted", "neg_label": "Introverted"},
    "energy":   {"pos": "N", "neg": "S", "pos_label": "Intuitive",   "neg_label": "Observant"},
    "nature":   {"pos": "T", "neg": "F", "pos_label": "Thinking",    "neg_label": "Feeling"},
    "tactics":  {"pos": "J", "neg": "P", "pos_label": "Judging",     "neg_label": "Prospecting"},
    "identity": {"pos": "A", "neg": "T", "pos_label": "Assertive",   "neg_label": "Turbulent"},
}

# Order of the 4 type letters (identity is appended as a suffix)
_TYPE_ORDER = ["mind", "energy", "nature", "tactics"]

# 7-point Likert scale presented to the staff member (answer_index 0..6)
LIKERT_SCALE = [
    "Strongly Disagree", "Disagree", "Slightly Disagree",
    "Neutral",
    "Slightly Agree", "Agree", "Strongly Agree",
]

PERSONALITY_TYPE_NAMES = {
    "INTJ": "Architect",   "INTP": "Logician",   "ENTJ": "Commander",  "ENTP": "Debater",
    "INFJ": "Advocate",    "INFP": "Mediator",   "ENFJ": "Protagonist","ENFP": "Campaigner",
    "ISTJ": "Logistician", "ISFJ": "Defender",   "ESTJ": "Executive",  "ESFJ": "Consul",
    "ISTP": "Virtuoso",    "ISFP": "Adventurer", "ESTP": "Entrepreneur","ESFP": "Entertainer",
}

# Short character summaries shown on the staff profile after a personality test
PERSONALITY_TYPE_DESCRIPTIONS = {
    "INTJ": "Strategic and independent, with a relentless drive to turn long-range plans into reality.",
    "INTP": "Inventive and analytical, endlessly curious and happiest when untangling complex problems.",
    "ENTJ": "Decisive and commanding, a natural organiser who thrives on leading toward bold goals.",
    "ENTP": "Quick-witted and bold, energised by debate, fresh ideas, and challenging assumptions.",
    "INFJ": "Insightful and principled, quietly determined to make a meaningful difference for others.",
    "INFP": "Idealistic and empathetic, guided by deeply held values and a rich inner world.",
    "ENFJ": "Warm and inspiring, a natural mentor who brings people together around a shared purpose.",
    "ENFP": "Enthusiastic and imaginative, drawn to possibilities, connection, and creative expression.",
    "ISTJ": "Practical and dependable, valuing order, responsibility, and follow-through on commitments.",
    "ISFJ": "Caring and meticulous, a steady supporter who quietly keeps things running for everyone.",
    "ESTJ": "Organised and direct, a results-focused manager who values structure and clear standards.",
    "ESFJ": "Sociable and conscientious, attentive to others' needs and skilled at building harmony.",
    "ISTP": "Pragmatic and hands-on, a calm problem-solver who learns by doing and stays cool under pressure.",
    "ISFP": "Gentle and spontaneous, an aesthetic, present-minded spirit who lives by personal values.",
    "ESTP": "Energetic and perceptive, a bold improviser who thrives on action and real-world results.",
    "ESFP": "Vivacious and friendly, bringing spontaneity, warmth, and fun to everyone around them.",
}


def compute_personality_result(
    questions: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Aggregate Likert responses into a 16Personalities-style profile.

    Each item in `questions` must contain:
      {
        "dimension": "mind" | "energy" | "nature" | "tactics" | "identity",
        "direction": +1 | -1,            # +1: agree → positive pole
        "answer_index": int | None,      # 0..6 Likert response (None = unanswered)
      }
    """
    sums: dict[str, float] = {k: 0.0 for k in PERSONALITY_DIMENSIONS}
    maxabs: dict[str, float] = {k: 0.0 for k in PERSONALITY_DIMENSIONS}

    for q in questions:
        dim = q.get("dimension")
        if dim not in PERSONALITY_DIMENSIONS:
            continue
        direction = q.get("direction", 1) or 1
        ai = q.get("answer_index")
        if ai is None:
            ai = 3  # treat blank as Neutral
        centered = ai - 3                  # range -3..+3
        sums[dim] += centered * direction
        maxabs[dim] += 3.0

    dimensions_out = []
    letters: dict[str, str] = {}
    for dim, meta in PERSONALITY_DIMENSIONS.items():
        s = sums[dim]
        m = maxabs[dim] or 1.0
        toward_pos = (s / m + 1) / 2 * 100      # 0..100 toward positive pole
        letter = meta["pos"] if s >= 0 else meta["neg"]
        letters[dim] = letter
        # Strength shown as the % toward the *winning* pole
        winning_pct = toward_pos if s >= 0 else (100 - toward_pos)
        dimensions_out.append({
            "key": dim,
            "pos_label": meta["pos_label"],
            "neg_label": meta["neg_label"],
            "pos_letter": meta["pos"],
            "neg_letter": meta["neg"],
            "letter": letter,
            "toward_pos_pct": round(toward_pos, 1),
            "winning_label": meta["pos_label"] if s >= 0 else meta["neg_label"],
            "winning_pct": round(winning_pct, 1),
        })

    code = "".join(letters[d] for d in _TYPE_ORDER)
    identity_letter = letters["identity"]
    type_code = f"{code}-{identity_letter}"
    type_name = PERSONALITY_TYPE_NAMES.get(code, "Unknown")

    return {
        "type_code": type_code,
        "base_code": code,
        "identity": PERSONALITY_DIMENSIONS["identity"]["pos_label"]
                    if identity_letter == "A" else PERSONALITY_DIMENSIONS["identity"]["neg_label"],
        "type_name": type_name,
        "description": PERSONALITY_TYPE_DESCRIPTIONS.get(code, ""),
        "dimensions": dimensions_out,
    }



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
