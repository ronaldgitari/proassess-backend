"""
Self-correcting evaluation loop — Maker → Checker → Router.

Replaces the single-pass GPT eval for written, coding, and scenario questions
with an iterative loop that audits its own output before returning a grade.

Flow:
  1. Maker  — draft a grade (score + feedback), reading any scratchpad critiques
              from prior rejected rounds.
  2. Checker — independent auditor: does the score match the rubric? any
               hallucinated requirements? too harsh / too lenient?
  3. Router  — if Checker passes → done; if Checker fails → append critique to
               scratchpad, loop back to Maker; after MAX_EVAL_ATTEMPTS loops →
               circuit breaker fires: flag the answer and return the last grade.

Existing DBs need:
  ALTER TABLE staff_answers
    ADD COLUMN IF NOT EXISTS eval_attempts integer,
    ADD COLUMN IF NOT EXISTS eval_flagged boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS eval_scratchpad jsonb;
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from langchain_openai import ChatOpenAI

from config import settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────

@dataclass
class CaseFile:
    """All context the Maker and Checker need for one evaluated answer."""
    question_text: str
    candidate_answer: str
    correct_reference: str      # correct answer / model answer / rubric
    explanation: str            # the question's explanation / context
    question_type: str          # "written" | "coding" | "scenario"
    max_score: float = 100.0
    scratchpad: list[str] = field(default_factory=list)
    attempt_count: int = 0


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

def _extract_json(raw: str) -> dict:
    """Strip markdown fences and parse JSON. Returns {} on failure."""
    cleaned = re.sub(r"```(?:json)?", "", raw).strip("`").strip()
    # Try the full string first, then the first {...} block
    for candidate in [cleaned, (re.search(r"\{.*\}", cleaned, re.DOTALL) or type("", (), {"group": lambda self, n: ""})()).group(0)]:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
    return {}


# ─────────────────────────────────────────────────────────────────
# Maker
# ─────────────────────────────────────────────────────────────────

_MAKER_SYSTEM = """You are a strict, precise assessor grading staff responses to professional assessments.

Rules:
- Score ONLY against the provided rubric / model answer. Do NOT invent requirements.
- If the candidate's answer addresses all key rubric points → score 80–100.
- If the candidate's answer is partially correct → score 40–79.
- If the candidate's answer is mostly wrong or missing → score 0–39.
- If previous-round critiques are included below, explicitly address each one in your feedback.

Respond ONLY with a valid JSON object:
{
  "score": <integer 0-100>,
  "is_correct": <true if score >= 70>,
  "feedback": "<2–4 sentences of specific, constructive feedback>"
}
No other text outside the JSON object."""

_MAKER_USER_TEMPLATE = """QUESTION ({qtype}):
{question}

MODEL ANSWER / RUBRIC:
{reference}

EXPLANATION / CONTEXT:
{explanation}

CANDIDATE RESPONSE:
{answer}
{scratchpad_block}
Evaluate the candidate response and return the JSON object."""

_SCRATCHPAD_HEADER = """
PREVIOUS ATTEMPTS WERE REJECTED — DO NOT REPEAT THESE ERRORS:
{critiques}

"""


async def run_maker(case_file: CaseFile, llm: ChatOpenAI) -> dict:
    """
    Draft a grade via the Maker LLM.

    Returns {"score": float, "feedback": str, "is_correct": bool}.
    Fails open: on parse error returns score=0, feedback="Evaluation error", is_correct=False.
    """
    from services import pipeline_tracker as pt

    scratchpad_block = ""
    if case_file.scratchpad:
        critiques = "\n".join(f"- {c}" for c in case_file.scratchpad)
        scratchpad_block = _SCRATCHPAD_HEADER.format(critiques=critiques)

    user_prompt = _MAKER_USER_TEMPLATE.format(
        qtype=case_file.question_type,
        question=case_file.question_text,
        reference=case_file.correct_reference,
        explanation=case_file.explanation or "(none)",
        answer=case_file.candidate_answer or "(no response provided)",
        scratchpad_block=scratchpad_block,
    )

    messages = [
        {"role": "system", "content": _MAKER_SYSTEM},
        {"role": "user", "content": user_prompt},
    ]

    try:
        async with pt.track_span(
            "openai",
            f"chat.completion · maker [attempt {case_file.attempt_count + 1}]",
            phase="score",
        ) as span:
            response = await llm.ainvoke(messages)
            span.capture(response)

        data = _extract_json(response.content.strip())
        if not data:
            raise ValueError("Empty JSON from maker")

        score = float(data.get("score", 0))
        is_correct = bool(data.get("is_correct", score >= 70))
        feedback = str(data.get("feedback", ""))
        return {"score": score, "feedback": feedback, "is_correct": is_correct}

    except Exception as e:
        logger.error("Maker eval failed (attempt %d): %s", case_file.attempt_count + 1, e)
        return {"score": 0.0, "feedback": "Evaluation error.", "is_correct": False}


# ─────────────────────────────────────────────────────────────────
# Checker
# ─────────────────────────────────────────────────────────────────

_CHECKER_SYSTEM = """You are an independent auditing assessor. Your sole job is to verify
whether a preliminary grade is consistent with the rubric and the candidate's actual response.
Do NOT consider how the grade was produced — evaluate only the final score and feedback."""

_CHECKER_USER_TEMPLATE = """QUESTION ({qtype}):
{question}

MODEL ANSWER / RUBRIC:
{reference}

CANDIDATE RESPONSE:
{answer}

PRELIMINARY GRADE:
  Score: {score}/100
  Feedback: {feedback}

Audit checklist — check ALL four:
1. Does the numeric score match the qualitative feedback? (e.g. feedback says "comprehensive"
   but score is 30 would be inconsistent)
2. Is every deduction traceable to a rubric item? (no hallucinated requirements the rubric
   never mentioned)
3. Is the tone calibrated? (not unduly harsh for a good response, not lenient for a poor one)
4. Does the feedback accurately reflect what the candidate actually wrote?

If ALL four pass → verdict is "pass".
If ANY fail → verdict is "fail" and explain the specific issue(s) concisely.

Respond ONLY with a valid JSON object:
{{
  "verdict": "pass" or "fail",
  "critique": "<one-paragraph explanation of failures, or empty string if pass>"
}}
No other text."""


async def run_checker(case_file: CaseFile, preliminary_grade: dict, llm: ChatOpenAI) -> dict:
    """
    Audit the Maker's grade.

    Returns {"verdict": "pass"|"fail", "critique": str}.
    Fails open: on parse error returns {"verdict": "pass", "critique": ""}.
    """
    from services import pipeline_tracker as pt

    user_prompt = _CHECKER_USER_TEMPLATE.format(
        qtype=case_file.question_type,
        question=case_file.question_text,
        reference=case_file.correct_reference,
        answer=case_file.candidate_answer or "(no response provided)",
        score=preliminary_grade.get("score", 0),
        feedback=preliminary_grade.get("feedback", ""),
    )

    messages = [
        {"role": "system", "content": _CHECKER_SYSTEM},
        {"role": "user", "content": user_prompt},
    ]

    try:
        async with pt.track_span(
            "openai",
            f"chat.completion · checker [attempt {case_file.attempt_count + 1}]",
            phase="score",
        ) as span:
            response = await llm.ainvoke(messages)
            span.capture(response)

        data = _extract_json(response.content.strip())
        if not data:
            raise ValueError("Empty JSON from checker")

        verdict = str(data.get("verdict", "pass")).lower()
        if verdict not in ("pass", "fail"):
            verdict = "pass"
        critique = str(data.get("critique", ""))
        return {"verdict": verdict, "critique": critique}

    except Exception as e:
        logger.error("Checker eval failed (attempt %d): %s", case_file.attempt_count + 1, e)
        # Fail open: don't block on checker failure
        return {"verdict": "pass", "critique": ""}


# ─────────────────────────────────────────────────────────────────
# Router / orchestrator
# ─────────────────────────────────────────────────────────────────

async def evaluate_with_correction(case_file: CaseFile) -> dict:
    """
    Run the Maker → Checker → Router loop.

    Returns:
    {
      "score": float,
      "feedback": str,
      "is_correct": bool,
      "eval_attempts": int,       # total Maker passes used
      "eval_flagged": bool,       # True if circuit breaker fired
      "eval_scratchpad": list[str],  # critique history
    }

    Always returns a result — never raises. If the loop itself errors, falls
    back to a safe default so a submission is never lost.
    """
    from services import pipeline_tracker as pt

    maker_llm = ChatOpenAI(
        model=settings.EVAL_MAKER_MODEL,
        temperature=0.1,
        max_tokens=512,
        openai_api_key=settings.OPENAI_API_KEY,
        request_timeout=60,
    )
    checker_llm = ChatOpenAI(
        model=settings.EVAL_CHECKER_MODEL,
        temperature=0.0,
        max_tokens=512,
        openai_api_key=settings.OPENAI_API_KEY,
        request_timeout=60,
    )

    grade: dict = {"score": 0.0, "feedback": "Evaluation error.", "is_correct": False}

    try:
        for attempt in range(settings.MAX_EVAL_ATTEMPTS):
            grade = await run_maker(case_file, maker_llm)
            audit = await run_checker(case_file, grade, checker_llm)
            case_file.attempt_count = attempt + 1

            if audit["verdict"] == "pass":
                logger.debug(
                    "Self-correcting eval passed on attempt %d (score=%.0f)",
                    attempt + 1, grade["score"],
                )
                return {
                    **grade,
                    "eval_attempts": attempt + 1,
                    "eval_flagged": False,
                    "eval_scratchpad": list(case_file.scratchpad),
                }

            # Checker rejected — append critique and loop
            critique = audit.get("critique", "")
            if critique:
                case_file.scratchpad.append(critique)
            logger.debug(
                "Self-correcting eval attempt %d rejected by checker: %s",
                attempt + 1, critique[:120],
            )

        # ── Circuit breaker ──────────────────────────────────────
        run_id = pt.get_current_run()
        await pt.finish_step(
            run_id, "score",
            status="warn",
            detail=(
                f"Circuit breaker: {settings.MAX_EVAL_ATTEMPTS} attempts exhausted, "
                f"grade flagged for review"
            ),
        )
        logger.warning(
            "Self-correcting eval circuit breaker fired after %d attempts",
            settings.MAX_EVAL_ATTEMPTS,
        )
        return {
            **grade,
            "eval_attempts": settings.MAX_EVAL_ATTEMPTS,
            "eval_flagged": True,
            "eval_scratchpad": list(case_file.scratchpad),
        }

    except Exception as e:
        logger.error("evaluate_with_correction loop error: %s", e)
        # Return a safe fallback — never crash a submission
        return {
            **grade,
            "eval_attempts": case_file.attempt_count or 1,
            "eval_flagged": True,
            "eval_scratchpad": list(case_file.scratchpad),
        }
