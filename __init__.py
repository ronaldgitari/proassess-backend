"""
RAG Pipeline orchestrator — combines all four stages into a single
callable used by the assessment service.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from langchain.schema import Document
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models import Assessment, Question, QuestionType, InformationSource
from rag.retriever import retrieve

try:
    from rag.augmentor import (
        generate_mcq_questions,
        generate_written_questions,
        generate_industry_questions,
    )
except ImportError as e:
    logger.warning("Failed to import from rag.augmentor: %s", e)
    generate_mcq_questions = None
    generate_written_questions = None
    generate_industry_questions = None

logger = logging.getLogger(__name__)


async def generate_questions_for_assessment(
    assessment: Assessment,
    db: AsyncSession,
) -> list[Question]:
    """
    Full pipeline:
      1. Retrieve relevant context (unless source is INDUSTRY).
      2. Augment — generate questions via GPT.
      3. Persist Question rows to the database.
      4. Return the created Question objects.
    """
    topic = assessment.topic
    domain = assessment.assessment_type.value
    n = assessment.num_questions
    context_prompt = assessment.context_prompt
    org_id = str(assessment.org_id)
    source = assessment.information_source

    # ── Stage 1 & 2: Retrieve + Augment ──────────────────────────
    raw_questions: list[dict[str, Any]] = []

    if source == InformationSource.INDUSTRY:
        raw_questions = await generate_industry_questions(
            topic=topic, domain=domain, num_questions=n
        )
    else:
        context_docs: list[Document] = []
        if source == InformationSource.KNOWLEDGE_BASE:
            context_docs = await retrieve(
                topic=topic,
                context_prompt=context_prompt,
                domain=domain,
                org_id=org_id,
            )
        # AI_GENERATED and CUSTOM_URL: use empty context (GPT uses domain knowledge)

        if assessment.question_type == QuestionType.WRITTEN:
            raw_questions = await generate_written_questions(
                topic=topic,
                domain=domain,
                context_docs=context_docs,
                num_questions=n,
                context_prompt=context_prompt,
            )
        else:
            raw_questions = await generate_mcq_questions(
                topic=topic,
                domain=domain,
                context_docs=context_docs,
                num_questions=n,
                context_prompt=context_prompt,
            )

    # ── Stage 3: Persist questions ────────────────────────────────
    question_objects: list[Question] = []
    for i, q in enumerate(raw_questions):
        question = Question(
            id=uuid.uuid4(),
            assessment_id=assessment.id,
            order_index=i,
            text=q["question"],
            question_type=assessment.question_type,
            options=q.get("options"),
            correct_answer_index=q.get("correct_index"),
            correct_answer_text=q.get("model_answer"),
            explanation=q.get("explanation"),
            source_reference=q.get("source_reference"),
            difficulty=int(q.get("difficulty", 3)),
        )
        db.add(question)
        question_objects.append(question)

    await db.flush()
    logger.info(
        "Pipeline complete: %d questions generated for assessment %s",
        len(question_objects),
        assessment.id,
    )
    return question_objects
