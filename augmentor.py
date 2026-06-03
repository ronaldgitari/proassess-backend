"""
RAG Augmentor — Stage 3 of the pipeline.

Takes retrieved context chunks and generates assessment questions
with structured output validated against a JSON schema.

Supports:
  - MCQ (multiple-choice, 4 options, 1 correct)
  - Written response (open-ended with rubric)
  - Soft-skills / industry-framework questions
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain.schema import Document
from langchain_openai import ChatOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# Output schema (typed dicts for clarity)
# ─────────────────────────────────────────────────────────────────

MCQ_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "required": ["question", "options", "correct_index", "explanation", "source_reference", "difficulty"],
        "properties": {
            "question": {"type": "string"},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 4,
                "maxItems": 4,
            },
            "correct_index": {"type": "integer", "minimum": 0, "maximum": 3},
            "explanation": {"type": "string"},
            "source_reference": {"type": "string"},
            "difficulty": {"type": "integer", "minimum": 1, "maximum": 5},
        },
    },
}

WRITTEN_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "required": ["question", "model_answer", "explanation", "source_reference", "difficulty"],
        "properties": {
            "question": {"type": "string"},
            "model_answer": {"type": "string"},
            "explanation": {"type": "string"},
            "source_reference": {"type": "string"},
            "difficulty": {"type": "integer", "minimum": 1, "maximum": 5},
        },
    },
}


# ─────────────────────────────────────────────────────────────────
# Context assembly
# ─────────────────────────────────────────────────────────────────

def build_context_block(docs: list[Document]) -> str:
    """Format retrieved chunks into a numbered context block."""
    lines = []
    for i, doc in enumerate(docs, start=1):
        source = doc.metadata.get("source", "Unknown")
        section = doc.metadata.get("section", doc.metadata.get("page", ""))
        ref = f"{source}" + (f" — {section}" if section else "")
        lines.append(f"[{i}] SOURCE: {ref}\n{doc.page_content.strip()}")
    return "\n\n---\n\n".join(lines)


# ─────────────────────────────────────────────────────────────────
# MCQ generation
# ─────────────────────────────────────────────────────────────────

MCQ_SYSTEM_PROMPT = """You are an expert assessment author specialising in corporate learning and development.
Your task is to generate high-quality multiple-choice questions (MCQ) for staff assessments.

Rules:
- Base every question strictly on the provided context. Do not invent facts.
- Each question must have exactly 4 options (A–D), with exactly one correct answer.
- Options must be plausible; avoid obviously wrong distractors.
- The explanation must cite the relevant context source.
- The source_reference must be the exact source title / URL from the context.
- Difficulty: 1=basic recall, 3=application, 5=analysis/evaluation.
- Vary difficulty across the question set.
- Questions must be unambiguous and professionally worded.
- Respond ONLY with a valid JSON array matching the provided schema. No preamble.
"""

MCQ_USER_TEMPLATE = """Generate exactly {n} MCQ questions on the topic: "{topic}"
Domain: {domain}
{context_note}

CONTEXT:
{context}

JSON schema for each question:
{{
  "question": "...",
  "options": ["A ...", "B ...", "C ...", "D ..."],
  "correct_index": 0,   // 0-based index of the correct option
  "explanation": "...",
  "source_reference": "...",
  "difficulty": 1-5
}}

Return a JSON array of {n} question objects. No other text.
"""

WRITTEN_SYSTEM_PROMPT = """You are an expert assessment author.
Generate open-ended written-response questions for professional assessment.

Rules:
- Base every question on the provided context.
- Include a detailed model answer for evaluator use.
- The explanation provides marking guidance.
- Respond ONLY with a valid JSON array. No preamble.
"""

WRITTEN_USER_TEMPLATE = """Generate exactly {n} written-response questions on: "{topic}"
Domain: {domain}

CONTEXT:
{context}

Return JSON array with fields: question, model_answer, explanation, source_reference, difficulty.
No other text.
"""


# ─────────────────────────────────────────────────────────────────
# JSON extraction helper
# ─────────────────────────────────────────────────────────────────

def extract_json_array(text: str) -> list[dict]:
    """
    Robustly extract the first JSON array from GPT output,
    handling markdown code fences.
    """
    # Strip ```json ... ``` fences
    text = re.sub(r"```(?:json)?\s*", "", text).strip()
    text = text.rstrip("`").strip()

    # Find the first '[' and matching ']'
    start = text.find("[")
    if start == -1:
        raise ValueError("No JSON array found in GPT response")

    # Walk forward to find the matching bracket
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])

    raise ValueError("Unbalanced JSON array in GPT response")


# ─────────────────────────────────────────────────────────────────
# Validators
# ─────────────────────────────────────────────────────────────────

def validate_mcq_item(item: dict) -> bool:
    required = {"question", "options", "correct_index", "explanation", "source_reference", "difficulty"}
    if not required.issubset(item.keys()):
        return False
    if not isinstance(item["options"], list) or len(item["options"]) != 4:
        return False
    if item["correct_index"] not in range(4):
        return False
    return True


def validate_written_item(item: dict) -> bool:
    required = {"question", "model_answer", "explanation", "source_reference", "difficulty"}
    return required.issubset(item.keys())


# ─────────────────────────────────────────────────────────────────
# Main generation function
# ─────────────────────────────────────────────────────────────────

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
async def _call_gpt(system: str, user: str) -> str:
    llm = ChatOpenAI(
        model=settings.OPENAI_CHAT_MODEL,
        temperature=settings.OPENAI_TEMPERATURE,
        max_tokens=settings.OPENAI_MAX_TOKENS,
        openai_api_key=settings.OPENAI_API_KEY,
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    response = await llm.ainvoke(messages)
    return response.content


async def generate_mcq_questions(
    *,
    topic: str,
    domain: str,
    context_docs: list[Document],
    num_questions: int,
    context_prompt: str | None = None,
) -> list[dict[str, Any]]:
    """
    Generate MCQ questions grounded in retrieved context.
    Retries up to 3 times if GPT returns malformed JSON.
    """
    context_block = build_context_block(context_docs)
    context_note = f"Additional context from assessor: {context_prompt}" if context_prompt else ""

    user_prompt = MCQ_USER_TEMPLATE.format(
        n=num_questions,
        topic=topic,
        domain=domain,
        context_note=context_note,
        context=context_block,
    )

    raw = await _call_gpt(MCQ_SYSTEM_PROMPT, user_prompt)

    try:
        items = extract_json_array(raw)
    except (ValueError, json.JSONDecodeError) as e:
        logger.error("MCQ JSON parse failed: %s\nRaw: %s", e, raw[:500])
        raise

    # Filter invalid items
    valid = [item for item in items if validate_mcq_item(item)]
    invalid_count = len(items) - len(valid)
    if invalid_count:
        logger.warning("%d MCQ items failed validation and were dropped", invalid_count)

    logger.info("Generated %d valid MCQ questions for topic '%s'", len(valid), topic)
    return valid[:num_questions]


async def generate_written_questions(
    *,
    topic: str,
    domain: str,
    context_docs: list[Document],
    num_questions: int,
    context_prompt: str | None = None,
) -> list[dict[str, Any]]:
    """Generate written-response questions from retrieved context."""
    context_block = build_context_block(context_docs)

    user_prompt = WRITTEN_USER_TEMPLATE.format(
        n=num_questions,
        topic=topic,
        domain=domain,
        context=context_block,
    )

    raw = await _call_gpt(WRITTEN_SYSTEM_PROMPT, user_prompt)

    try:
        items = extract_json_array(raw)
    except (ValueError, json.JSONDecodeError) as e:
        logger.error("Written Q JSON parse failed: %s", e)
        raise

    valid = [item for item in items if validate_written_item(item)]
    logger.info("Generated %d valid written questions for topic '%s'", len(valid), topic)
    return valid[:num_questions]


# ─────────────────────────────────────────────────────────────────
# Industry / soft-skills generation (no RAG context needed)
# ─────────────────────────────────────────────────────────────────

INDUSTRY_SYSTEM_PROMPT = """You are an organisational psychology and L&D expert.
Generate professional competency assessment questions based on industry-standard frameworks.
Respond ONLY with a valid JSON array. No preamble or explanation outside the JSON.
"""

INDUSTRY_USER_TEMPLATE = """Generate exactly {n} MCQ questions assessing professional competencies on the topic: "{topic}"

Use these frameworks as reference: {frameworks}

Each question must have:
- question: professionally worded behavioural or situational question
- options: exactly 4 options (A–D), one correct
- correct_index: 0-based index of best answer
- explanation: why this is the best professional response
- source_reference: the framework or standard referenced
- difficulty: 1-5

Return JSON array only.
"""

FRAMEWORKS = {
    "professional": "Emotional Intelligence (Goleman), DISC, 16 Personalities, SHL Competency Framework",
    "leadership": "Situational Leadership (Hersey-Blanchard), GROW Model, MindTools Leadership Styles",
    "communication": "Active Listening, Non-Violent Communication (NVC), Mehrabian Communication Model",
}


async def generate_industry_questions(
    *,
    topic: str,
    domain: str,
    num_questions: int,
) -> list[dict[str, Any]]:
    """
    Generate soft-skills / professional competency questions using
    industry-standard frameworks (no RAG context required).
    """
    frameworks = FRAMEWORKS.get(domain.lower(), FRAMEWORKS["professional"])
    user_prompt = INDUSTRY_USER_TEMPLATE.format(
        n=num_questions, topic=topic, frameworks=frameworks
    )
    raw = await _call_gpt(INDUSTRY_SYSTEM_PROMPT, user_prompt)

    try:
        items = extract_json_array(raw)
    except (ValueError, json.JSONDecodeError) as e:
        logger.error("Industry Q JSON parse failed: %s", e)
        raise

    valid = [item for item in items if validate_mcq_item(item)]
    return valid[:num_questions]
