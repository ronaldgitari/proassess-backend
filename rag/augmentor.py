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
import random
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
- Base every question and its correct answer strictly on the provided context. Do not invent facts,
  and never let the correct answer depend on anything outside the provided context.
- Each question must have exactly 4 options (A–D), with exactly one correct answer.
- Options must be plausible; avoid obviously wrong distractors.
- The source_reference must be the exact source title / URL from the context.
- Difficulty: 1=basic recall, 3=application, 5=analysis/evaluation.
- Vary difficulty across the question set.
- Questions must be unambiguous and professionally worded.

Explanation field (two parts):
1. First, state why the correct option is correct, citing the relevant context source.
2. Then add a brief supplementary note (1–2 sentences) that enriches understanding using
   ONLY well-established common knowledge — widely accepted industry standards, best practices,
   or official documentation on the topic. This enrichment must:
     • stay directly relevant to the question,
     • be factual and non-controversial (no speculation, no invented specifics, no fabricated
       figures, dates, or citations),
     • never contradict the provided context.
   If no such common-knowledge enrichment can be added truthfully, omit part 2 entirely.

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
  "options": ["plain option text", "plain option text", "plain option text", "plain option text"],   // do NOT prefix with A/B/C/D
  "correct_index": 0,   // 0-based index of the correct option (position will be re-randomised after generation)
  "explanation": "...",
  "source_reference": "...",
  "difficulty": 1-5
}}

Return a JSON array of {n} question objects. No other text.
"""

WRITTEN_SYSTEM_PROMPT = """You are an expert assessment author.
Generate open-ended written-response questions for professional assessment.

Rules:
- Base every question and the core of its model answer strictly on the provided context.
  Do not invent facts; the assessed substance must come from the context.
- Include a detailed model answer for evaluator use.
- The explanation provides marking guidance.

Enrichment (model_answer + explanation):
- After the context-grounded core, you MAY append a brief supplementary note that deepens
  understanding using ONLY well-established common knowledge — widely accepted industry
  standards, best practices, or official documentation. This enrichment must stay directly
  relevant, be factual and non-controversial (no speculation, no fabricated specifics,
  figures, dates, or citations), and never contradict the provided context. If nothing can
  be added truthfully, omit it.

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


def _strip_option_prefix(opt: str) -> str:
    """Remove any leading 'A) ', 'A. ', 'A - ', 'A: ' style label so option text is
    position-independent before shuffling."""
    return re.sub(r"^\s*[A-Da-d][\).\-:]\s+", "", str(opt)).strip()


def spread_correct_answers(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    GPT strongly biases the correct option to position 0. Shuffle each question's
    options and rebalance the correct-answer position across the whole set so the
    key is evenly distributed over A/B/C/D (round-robin of shuffled [0,1,2,3]).
    """
    # Balanced target positions: repeat a freshly-shuffled [0,1,2,3] to cover all items
    targets: list[int] = []
    while len(targets) < len(items):
        block = [0, 1, 2, 3]
        random.shuffle(block)
        targets.extend(block)

    for item, target in zip(items, targets):
        opts = item.get("options")
        ci = item.get("correct_index")
        if not isinstance(opts, list) or len(opts) != 4 or ci not in range(4):
            continue
        cleaned = [_strip_option_prefix(o) for o in opts]
        correct = cleaned[ci]
        distractors = [o for j, o in enumerate(cleaned) if j != ci]
        random.shuffle(distractors)
        new_opts = distractors[:]
        new_opts.insert(target, correct)
        item["options"] = new_opts
        item["correct_index"] = target
    return items


def validate_written_item(item: dict) -> bool:
    required = {"question", "model_answer", "explanation", "source_reference", "difficulty"}
    return required.issubset(item.keys())


def _to_text(v: Any) -> str | None:
    """Coerce a model-returned value to plain text. GPT sometimes returns rubric
    criteria as a JSON list (or dict) where a Text column is expected — flatten
    those to a readable string so persistence doesn't fail."""
    if v is None:
        return None
    if isinstance(v, list):
        return "\n".join(
            (str(x) if str(x).strip().startswith(("-", "•")) else f"- {x}") for x in v
        )
    if isinstance(v, dict):
        return "\n".join(f"{k}: {val}" for k, val in v.items())
    return str(v)


# ─────────────────────────────────────────────────────────────────
# Main generation function
# ─────────────────────────────────────────────────────────────────

BATCH_SIZE = 10   # max questions per GPT call — keeps responses well under token limits
MAX_CONCURRENT_BATCHES = 1   # 1 = fully sequential (most stable; best de-dup; no rate-limit risk)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
async def _call_gpt(system: str, user: str, temperature: float | None = None) -> str:
    from services import pipeline_tracker as pt
    llm = ChatOpenAI(
        model=settings.OPENAI_CHAT_MODEL,
        temperature=settings.OPENAI_TEMPERATURE if temperature is None else temperature,
        max_tokens=settings.OPENAI_MAX_TOKENS,
        openai_api_key=settings.OPENAI_API_KEY,
        request_timeout=120,   # 2-minute hard timeout per call
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    async with pt.track_span("openai", f"chat.completion · {settings.OPENAI_CHAT_MODEL}", detail="question generation"):
        response = await llm.ainvoke(messages)
    return response.content


async def _generate_mcq_batch(
    *,
    topic: str,
    domain: str,
    context_block: str,
    context_note: str,
    n: int,
) -> list[dict[str, Any]]:
    """Single GPT call for one batch of ≤BATCH_SIZE MCQ questions."""
    user_prompt = MCQ_USER_TEMPLATE.format(
        n=n, topic=topic, domain=domain,
        context_note=context_note, context=context_block,
    )
    raw = await _call_gpt(MCQ_SYSTEM_PROMPT, user_prompt)
    try:
        items = extract_json_array(raw)
    except (ValueError, json.JSONDecodeError) as e:
        logger.error("MCQ JSON parse failed: %s\nRaw: %s", e, raw[:500])
        raise
    valid = [item for item in items if validate_mcq_item(item)]
    if len(items) - len(valid):
        logger.warning("%d MCQ items failed validation and were dropped", len(items) - len(valid))
    return valid


async def generate_mcq_questions(
    *,
    topic: str,
    domain: str,
    context_docs: list[Document],
    num_questions: int,
    context_prompt: str | None = None,
) -> list[dict[str, Any]]:
    """
    Generate MCQ questions in batches of BATCH_SIZE to avoid token/timeout limits.
    """
    context_block = build_context_block(context_docs)
    context_note = f"Additional context from assessor: {context_prompt}" if context_prompt else ""

    all_questions: list[dict[str, Any]] = []
    remaining = num_questions

    while remaining > 0:
        batch_n = min(remaining, BATCH_SIZE)
        logger.info("Generating MCQ batch: %d questions (total so far: %d/%d)", batch_n, len(all_questions), num_questions)
        batch = await _generate_mcq_batch(
            topic=topic, domain=domain,
            context_block=context_block, context_note=context_note,
            n=batch_n,
        )
        all_questions.extend(batch)
        remaining -= batch_n

    logger.info("Generated %d total MCQ questions for topic '%s'", len(all_questions), topic)
    return spread_correct_answers(all_questions[:num_questions])


async def _generate_written_batch(
    *, topic: str, domain: str, context_block: str, n: int,
) -> list[dict[str, Any]]:
    """Single GPT call for one batch of ≤BATCH_SIZE written questions."""
    user_prompt = WRITTEN_USER_TEMPLATE.format(
        n=n, topic=topic, domain=domain, context=context_block,
    )
    raw = await _call_gpt(WRITTEN_SYSTEM_PROMPT, user_prompt)
    try:
        items = extract_json_array(raw)
    except (ValueError, json.JSONDecodeError) as e:
        logger.error("Written Q JSON parse failed: %s", e)
        raise
    return [item for item in items if validate_written_item(item)]


async def generate_written_questions(
    *,
    topic: str,
    domain: str,
    context_docs: list[Document],
    num_questions: int,
    context_prompt: str | None = None,
) -> list[dict[str, Any]]:
    """Generate written-response questions in batches of BATCH_SIZE."""
    context_block = build_context_block(context_docs)
    all_questions: list[dict[str, Any]] = []
    remaining = num_questions

    while remaining > 0:
        batch_n = min(remaining, BATCH_SIZE)
        logger.info("Generating written batch: %d questions (total so far: %d/%d)", batch_n, len(all_questions), num_questions)
        batch = await _generate_written_batch(
            topic=topic, domain=domain, context_block=context_block, n=batch_n,
        )
        all_questions.extend(batch)
        remaining -= batch_n

    logger.info("Generated %d total written questions for topic '%s'", len(all_questions), topic)
    return all_questions[:num_questions]


# ─────────────────────────────────────────────────────────────────
# Coding exercise generation (no RAG context needed)
# ─────────────────────────────────────────────────────────────────

CODING_SYSTEM_PROMPT = """You are a senior software engineer writing coding assessment exercises.
Each exercise is a self-contained programming problem the candidate solves in a code editor.

Rules:
- Write a clear problem statement: what to implement, inputs/outputs, and constraints.
- Provide a correct reference solution in the target language as the model answer.
- The explanation gives marking guidance (what a good solution must demonstrate).
- Vary difficulty across the set.
- Respond ONLY with a valid JSON array. No preamble.
"""

CODING_USER_TEMPLATE = """Generate exactly {n} coding exercises on the topic: "{topic}"
Target programming language: {language}

Each object:
{{
  "question": "problem statement (markdown ok)",
  "model_answer": "reference solution in {language}",
  "explanation": "marking guidance",
  "source_reference": "Coding exercise",
  "difficulty": 1-5
}}

Return the JSON array only. No other text.
"""


async def generate_coding_questions(
    *, topic: str, language: str, num_questions: int,
) -> list[dict[str, Any]]:
    """Generate coding exercises (problem + reference solution) in batches."""
    all_items: list[dict[str, Any]] = []
    remaining = num_questions
    while remaining > 0:
        batch_n = min(remaining, BATCH_SIZE)
        user_prompt = CODING_USER_TEMPLATE.format(n=batch_n, topic=topic, language=language)
        raw = await _call_gpt(CODING_SYSTEM_PROMPT, user_prompt)
        try:
            items = extract_json_array(raw)
        except (ValueError, json.JSONDecodeError) as e:
            logger.error("Coding JSON parse failed: %s", e)
            raise
        all_items.extend(it for it in items if validate_written_item(it))
        remaining -= batch_n
    logger.info("Generated %d coding exercises (%s) for '%s'", len(all_items), language, topic)
    return all_items[:num_questions]


# ─────────────────────────────────────────────────────────────────
# Scenario / case-study generation (KB-grounded)
# ─────────────────────────────────────────────────────────────────

SCENARIO_CASE_SYSTEM_PROMPT = """You are an expert assessment author who writes realistic professional CASE STUDIES
for workplace competency assessment.

Write a single, self-contained case narrative grounded STRICTLY in the provided source material.
Do not invent facts, figures, names, regulations, or events that aren't supported by the context;
where you add ordinary connective detail (a fictional company/role to frame the situation) keep it
clearly generic and never let any assessable fact depend on something outside the context.

The case must give the candidate enough to analyse:
- Background & context (the organisation/situation, grounded in the source domain)
- The concrete situation or problem to be addressed
- Stakeholders and their concerns
- Relevant constraints, data, or tensions drawn from the source material

Write 250–500 words of clear, professional prose (markdown allowed: a short title, paragraphs,
optional bullet list). Output ONLY the case narrative text — no preamble, no questions, no JSON.
"""

SCENARIO_CASE_USER_TEMPLATE = """Write one case study on the topic: "{topic}"
Domain: {domain}
{context_note}

SOURCE MATERIAL (ground the case strictly in this):
{context}

Return only the case narrative.
"""

SCENARIO_Q_SYSTEM_PROMPT = """You are an expert assessment author. Given a CASE STUDY and its source material,
write open-ended ANALYTICAL questions that require the candidate to read the case and apply judgement —
analysing, evaluating, recommending, and justifying. No simple recall.

For each question provide:
- question: the analytical prompt, explicitly referring to the case.
- model_answer: a strong reference answer grounded in the case + source material.
- explanation: a MARKING RUBRIC — the key criteria a good answer must demonstrate
  (e.g. problem identification, use of evidence from the case, quality of recommendation,
  justification). Phrase as concise bullet-style criteria.
- source_reference: the source title/section the answer draws on.
- difficulty: 1-5 (vary across the set).

Ground every question and its model answer in the case and the source material. Do not require
information the candidate wasn't given. Respond ONLY with a valid JSON array. No preamble.
"""

SCENARIO_Q_USER_TEMPLATE = """CASE STUDY:
{case}

SOURCE MATERIAL:
{context}

Generate exactly {n} analytical questions about the case above on the topic "{topic}".
Return a JSON array; each object has: question, model_answer, explanation, source_reference, difficulty.
No other text.
"""


async def generate_scenario_assessment(
    *,
    topic: str,
    domain: str,
    context_docs: list[Document],
    num_questions: int,
    context_prompt: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """
    Two-stage, KB-grounded case-study generation:
      1. Author ONE case narrative grounded in the retrieved context.
      2. Author `num_questions` analytical questions (with model answer + rubric)
         referencing that case.
    Returns (case_text, questions[]). Questions reuse the written-question shape
    (question / model_answer / explanation / source_reference / difficulty).
    """
    context_block = build_context_block(context_docs)
    context_note = f"Additional context from assessor: {context_prompt}" if context_prompt else ""

    # Stage 1 — author the case (grounded prose, not JSON)
    case_user = SCENARIO_CASE_USER_TEMPLATE.format(
        topic=topic, domain=domain, context_note=context_note, context=context_block,
    )
    case_text = (await _call_gpt(SCENARIO_CASE_SYSTEM_PROMPT, case_user, temperature=0.5)).strip()
    # Strip any stray code fences the model may wrap prose in
    case_text = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", case_text).strip()
    logger.info("Scenario case authored (%d chars) for '%s'", len(case_text), topic)

    # Stage 2 — author analytical questions about the case (batched)
    all_questions: list[dict[str, Any]] = []
    remaining = num_questions
    while remaining > 0:
        batch_n = min(remaining, BATCH_SIZE)
        q_user = SCENARIO_Q_USER_TEMPLATE.format(
            case=case_text, context=context_block, n=batch_n, topic=topic,
        )
        raw = await _call_gpt(SCENARIO_Q_SYSTEM_PROMPT, q_user)
        try:
            items = extract_json_array(raw)
        except (ValueError, json.JSONDecodeError) as e:
            logger.error("Scenario Q JSON parse failed: %s", e)
            raise
        for it in items:
            if not validate_written_item(it):
                continue
            # GPT may return the rubric/answer as a list or dict → flatten to text.
            it["model_answer"] = _to_text(it.get("model_answer"))
            it["explanation"] = _to_text(it.get("explanation"))
            it["source_reference"] = _to_text(it.get("source_reference"))
            all_questions.append(it)
        remaining -= batch_n

    logger.info("Generated %d scenario questions for '%s'", len(all_questions), topic)
    return case_text, all_questions[:num_questions]


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


# ─────────────────────────────────────────────────────────────────
# Personality (16 Personalities / MBTI-style) generation
# ─────────────────────────────────────────────────────────────────

PERSONALITY_SYSTEM_PROMPT = """You are an organisational psychologist designing a workplace personality
assessment in the style of the 16 Personalities / MBTI framework.

You write first-person Likert statements (the respondent rates agreement on a 7-point scale).
There are NO right or wrong answers — each statement simply leans toward one pole of a trait.

The five trait dimensions and their poles are:
  - mind:     Extraverted (E)  vs  Introverted (I)
  - energy:   Intuitive (N)    vs  Observant (S)
  - nature:   Thinking (T)     vs  Feeling (F)
  - tactics:  Judging (J)      vs  Prospecting (P)
  - identity: Assertive (A)    vs  Turbulent (T)

Rules:
- Write natural, first-person statements ("I ...", "I tend to ...", "I feel ...").
- Each statement maps to exactly ONE dimension and indicates ONE pole (the "keyed_pole")
  that AGREEING with the statement points toward.
- Distribute statements as evenly as possible across all five dimensions.
- Vary the keyed pole within each dimension (some keyed to each side) to avoid acquiescence bias.
- Tailor the wording to the given professional topic / audience where natural.
- Respond ONLY with a valid JSON array. No preamble.
"""

PERSONALITY_USER_TEMPLATE = """Generate exactly {n} workplace personality statements for the topic/audience: "{topic}"

Return a JSON array where each object is:
{{
  "statement": "I tend to ...",
  "dimension": "mind" | "energy" | "nature" | "tactics" | "identity",
  "keyed_pole": "<single letter: one of E/I, N/S, T/F, J/P, or A/T matching the dimension>"
}}

Distribute the {n} statements roughly evenly across the five dimensions.
Each statement must be DISTINCT in wording and in the specific situation/behaviour it describes.
Draw on a wide variety of workplace contexts (meetings, deadlines, teamwork, conflict, planning,
learning, feedback, change, decision-making, social events) so no two statements feel alike.
{avoid_block}
Return the JSON array only. No other text.
"""

_AVOID_TEMPLATE = """
IMPORTANT — do NOT repeat or paraphrase any of these statements already generated:
{prior}
Produce statements that are clearly different from every item above."""

_VALID_POLES = {
    "mind": {"E", "I"}, "energy": {"N", "S"}, "nature": {"T", "F"},
    "tactics": {"J", "P"}, "identity": {"A", "T"},
}


def validate_personality_item(item: dict) -> bool:
    if not {"statement", "dimension", "keyed_pole"}.issubset(item.keys()):
        return False
    dim = item["dimension"]
    if dim not in _VALID_POLES:
        return False
    return str(item["keyed_pole"]).strip().upper() in _VALID_POLES[dim]


def _normalise_statement(s: str) -> str:
    """Lowercased, punctuation-stripped form for duplicate detection."""
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


async def _generate_personality_batch(
    *, topic: str, n: int, prior_statements: list[str],
) -> list[dict[str, Any]]:
    # Feed up to the last 40 prior statements so GPT can actively avoid repeats
    if prior_statements:
        prior = "\n".join(f"- {s}" for s in prior_statements[-40:])
        avoid_block = _AVOID_TEMPLATE.format(prior=prior)
    else:
        avoid_block = ""

    user_prompt = PERSONALITY_USER_TEMPLATE.format(n=n, topic=topic, avoid_block=avoid_block)
    # Higher temperature → more lexical/contextual variety across batches
    raw = await _call_gpt(PERSONALITY_SYSTEM_PROMPT, user_prompt, temperature=0.9)
    try:
        items = extract_json_array(raw)
    except (ValueError, json.JSONDecodeError) as e:
        logger.error("Personality JSON parse failed: %s | raw: %s", e, raw[:300])
        raise
    return [it for it in items if validate_personality_item(it)]


async def generate_personality_questions(
    *,
    topic: str,
    num_questions: int,
) -> list[dict[str, Any]]:
    """
    Generate Likert personality statements using PARALLEL batch waves.

    All batches in a wave fire concurrently (asyncio.gather), then results are
    de-duplicated. Any shortfall (from dropped duplicates) is filled by a smaller
    follow-up wave that is told which statements already exist. This cuts wall-clock
    time from ~N sequential calls to ~1-2 waves.
    """
    import asyncio

    all_items: list[dict[str, Any]] = []
    seen: set[str] = set()
    prior_statements: list[str] = []
    max_waves = 4
    sem = asyncio.Semaphore(MAX_CONCURRENT_BATCHES)   # cap simultaneous GPT calls

    async def _guarded_batch() -> list[dict[str, Any]]:
        # Snapshot the avoid-list at call time so concurrent calls still get the
        # statements completed so far (limits — but doesn't fully prevent — overlap).
        async with sem:
            return await _generate_personality_batch(
                topic=topic, n=BATCH_SIZE, prior_statements=list(prior_statements),
            )

    for wave in range(max_waves):
        needed = num_questions - len(all_items)
        if needed <= 0:
            break
        # Slightly over-request on the first wave to absorb intra-wave duplicates
        target = int(needed * 1.2) if wave == 0 else needed
        n_batches = max(1, (target + BATCH_SIZE - 1) // BATCH_SIZE)
        logger.info("Personality wave %d: %d batches, max %d concurrent (have %d/%d)",
                    wave + 1, n_batches, MAX_CONCURRENT_BATCHES, len(all_items), num_questions)

        results = await asyncio.gather(*[_guarded_batch() for _ in range(n_batches)],
                                       return_exceptions=True)

        for r in results:
            if isinstance(r, Exception):
                logger.warning("Personality batch failed in wave: %s", r)
                continue
            for it in r:
                key = _normalise_statement(it["statement"])
                if not key or key in seen:
                    continue
                seen.add(key)
                all_items.append(it)
                prior_statements.append(it["statement"])

    logger.info("Generated %d unique personality statements for '%s' in ≤%d waves",
                len(all_items), topic, max_waves)
    return all_items[:num_questions]



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
    return spread_correct_answers(valid[:num_questions])
