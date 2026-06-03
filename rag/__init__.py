"""
RAG Pipeline package — retrieval, augmentation, evaluation, and the
top-level orchestrator used by the assessment service.
"""
from __future__ import annotations

import sys
import os
import logging
import uuid
from typing import Any

# Ensure project root is on path so root-level modules are importable
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from langchain.schema import Document
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings

from rag.indexer import load_pdf, load_docx, load_xlsx, load_web, index_source, get_chroma
from rag.retriever import retrieve
from rag.augmentor import (
    generate_mcq_questions,
    generate_written_questions,
    generate_industry_questions,
    generate_personality_questions,
    generate_coding_questions,
    generate_scenario_assessment,
)
from rag.evaluator import (
    evaluate_mcq, evaluate_written,
    PERSONALITY_DIMENSIONS, LIKERT_SCALE,
)
from rag.grader import grade_context, InsufficientContext
from rag.web_research import gather_web_context

logger = logging.getLogger(__name__)


def _doc_key(doc: Document) -> str:
    """Stable-enough identity for a chunk, for dedup across re-queries."""
    return doc.page_content[:100]


async def _record_generation_error(assessment_id, info: dict) -> None:
    """
    Persist a structured generation failure onto the assessment's rag_metadata via
    its OWN committed session, so it survives the background task's rolled-back
    transaction and the /generation-status endpoint can surface it to the LM.
    """
    from sqlalchemy import select as _select
    from database import AsyncSessionLocal
    from models import Assessment
    try:
        async with AsyncSessionLocal() as db:
            a = (await db.execute(_select(Assessment).where(Assessment.id == assessment_id))).scalar_one_or_none()
            if not a:
                return
            meta = dict(a.rag_metadata or {})
            meta["generation_error"] = info
            a.rag_metadata = meta            # reassign so SQLAlchemy detects the JSONB change
            db.add(a)
            await db.commit()
    except Exception as e:
        logger.warning("could not record generation_error for %s: %s", assessment_id, e)


async def _grade_and_refine(
    *, topic, context_prompt, domain, org_id, source_id, n,
    context_docs: list, assessment, ctl, fail_on_insufficient: bool = True,
) -> list:
    """
    Bounded grade → re-query reflection loop for grounded generation.

    Grades the retrieved context; on "partial" it re-queries the same source with
    the grader's refined query (accumulating + deduping docs) up to
    settings.MAX_REGRADE times; on "sufficient" it returns the (possibly enriched) docs.

    On "insufficient" (or exhausted budget):
      - fail_on_insufficient=True  (pure KB) → record + raise InsufficientContext
        (honest "source doesn't cover the topic" failure).
      - fail_on_insufficient=False (HYBRID)  → warn and PROCEED with what we have, since
        the web sources + model knowledge are meant to supplement the KB by design.
    """
    seen = {_doc_key(d) for d in context_docs}
    requeries = 0
    grade_n = 0

    while True:
        grade = await grade_context(
            topic=topic, context_prompt=context_prompt, domain=domain,
            num_questions=n, docs=context_docs,
        )
        grade_n += 1
        verdict = grade["verdict"]

        if verdict == "sufficient":
            note = f"sufficient · {len(context_docs)} chunks"
            if requeries:
                note += f" (after {requeries} re-quer{'y' if requeries == 1 else 'ies'}, {grade_n} grades)"
            ctl.note(note)
            return context_docs

        if verdict == "insufficient" or requeries >= settings.MAX_REGRADE or not grade["refined_query"]:
            missing = grade["missing"]
            covered = grade["covered"]
            detail = ", ".join(missing) if missing else topic
            if not fail_on_insufficient:
                # HYBRID: web + model knowledge supplement the KB by design — don't
                # hard-fail; proceed to generation with whatever context we have.
                ctl.warn(f"weak KB coverage ({len(context_docs)} chunks) — supplementing with web + model knowledge"
                         + (f"; light on: {detail}" if missing else ""))
                return context_docs
            await _record_generation_error(assessment.id, {
                "kind": "insufficient_context",
                "missing": missing,
                "covered": covered,
                "grades": grade_n,
                "requeries": requeries,
            })
            raise InsufficientContext(
                f"Selected source does not cover the topic well enough: {detail}",
                covered=covered, missing=missing,
            )

        # "partial" with budget remaining → re-query for the missing sub-topics
        refined = grade["refined_query"]
        new_docs = await retrieve(
            topic=refined, context_prompt=context_prompt,
            domain=domain, org_id=org_id, source_id=source_id,
        )
        added = 0
        for d in new_docs:
            k = _doc_key(d)
            if k not in seen:
                seen.add(k)
                context_docs.append(d)
                added += 1
        requeries += 1
        ctl.warn(f"partial (grade {grade_n}) — re-queried '{refined[:60]}', +{added} chunks (now {len(context_docs)})")


async def generate_questions_for_assessment(assessment, db: AsyncSession) -> list:
    """
    Full RAG pipeline:
      1. Retrieve relevant context (unless source is INDUSTRY).
      2. Augment — generate questions via GPT.
      3. Persist Question rows to the database.
      4. Return the created Question objects.
    """
    from models import Assessment, Question, QuestionType, InformationSource
    from services import pipeline_tracker as pt

    topic = assessment.topic
    domain = assessment.assessment_type.value
    n = assessment.num_questions
    # Derive the generation/retrieval context from the assessment TITLE + the LM's
    # context prompt (not the topic alone). Folding the title in widens query
    # expansion + the grading judgement, so a narrowly-phrased topic on a KB doc is
    # less likely to falsely fail the "insufficient context" gate.
    _title = (assessment.name or "").strip()
    _prompt = (assessment.context_prompt or "").strip()
    context_prompt = " ".join(p for p in (
        f"Assessment title: {_title}." if _title else "",
        _prompt,
    ) if p).strip() or None
    org_id = str(assessment.org_id)
    source = assessment.information_source
    source_id = (assessment.rag_metadata or {}).get("source_id")
    language = (assessment.rag_metadata or {}).get("language", "Python")
    is_personality = assessment.question_type == QuestionType.PERSONALITY
    is_coding = assessment.question_type == QuestionType.CODING

    # Build the phase list for this run
    if is_personality:
        steps = [("init", "Initialise"), ("generate", "Generate statements (GPT-4o)"), ("persist", "Persist to database")]
    elif is_coding:
        steps = [("init", "Initialise"), ("generate", "Generate coding exercises (GPT-4o)"), ("persist", "Persist to database")]
    elif source == InformationSource.INDUSTRY:
        steps = [("init", "Initialise"), ("augment", "Generate questions (industry frameworks)"), ("persist", "Persist to database")]
    elif source == InformationSource.HYBRID:
        steps = [("init", "Initialise"), ("retrieve", "Retrieve context (Chroma)"), ("web", "Gather web case studies"), ("grade", "Grade combined context (GPT-4o-mini)"), ("augment", "Generate questions (GPT-4o)"), ("persist", "Persist to database")]
    elif source == InformationSource.KNOWLEDGE_BASE:
        steps = [("init", "Initialise"), ("retrieve", "Retrieve context (Chroma)"), ("grade", "Grade retrieved context (GPT-4o-mini)"), ("augment", "Generate questions (GPT-4o)"), ("persist", "Persist to database")]
    else:
        steps = [("init", "Initialise"), ("augment", "Generate questions (GPT-4o)"), ("persist", "Persist to database")]

    run_id = await pt.create_run(
        kind="generation", label=f"Generate · {assessment.name}",
        steps=steps, org_id=assessment.org_id, ref_id=assessment.id,
    )
    pt.set_current_run(run_id)            # propagates to deep client calls for span capture
    await pt.capture_server_meta(run_id)  # server IP / system id

    try:
        async with pt.track_step(run_id, "init") as s:
            s.note(f"{n} × {assessment.question_type.value} · source={source.value}")

        # ── Personality branch (Likert, no retrieval) ────────────
        if is_personality:
            async with pt.track_step(run_id, "generate") as s:
                items = await generate_personality_questions(topic=topic, num_questions=n)
                s.note(f"{len(items)} statements generated")
            async with pt.track_step(run_id, "persist") as s:
                question_objects = []
                for i, it in enumerate(items):
                    dim = it["dimension"]
                    keyed = str(it["keyed_pole"]).strip().upper()
                    direction = 1 if keyed == PERSONALITY_DIMENSIONS[dim]["pos"] else -1
                    q = Question(
                        id=uuid.uuid4(), assessment_id=assessment.id, order_index=i,
                        text=it["statement"], question_type=QuestionType.PERSONALITY,
                        options=LIKERT_SCALE, correct_answer_index=None, correct_answer_text=None,
                        explanation=None, source_reference="16 Personalities framework",
                        difficulty=1, retrieved_chunk_ids={"dimension": dim, "direction": direction},
                    )
                    db.add(q)
                    question_objects.append(q)
                async with pt.track_span("postgres", "INSERT questions", phase="persist",
                                         detail=f"{len(question_objects)} rows"):
                    await db.flush()
                s.note(f"{len(question_objects)} rows written")
            await pt.finish_run(run_id, "completed")
            return question_objects

        # ── Coding branch (embedded editor, no retrieval) ────────
        if is_coding:
            async with pt.track_step(run_id, "generate") as s:
                items = await generate_coding_questions(topic=topic, language=language, num_questions=n)
                s.note(f"{len(items)} exercises generated ({language})")
            async with pt.track_step(run_id, "persist") as s:
                question_objects = []
                for i, it in enumerate(items):
                    q = Question(
                        id=uuid.uuid4(), assessment_id=assessment.id, order_index=i,
                        text=it["question"], question_type=QuestionType.CODING,
                        options=None, correct_answer_index=None,
                        correct_answer_text=it.get("model_answer"),
                        explanation=it.get("explanation"),
                        source_reference=it.get("source_reference", "Coding exercise"),
                        difficulty=int(it.get("difficulty", 3)),
                        retrieved_chunk_ids={"language": language},   # per-question language tag
                    )
                    db.add(q)
                    question_objects.append(q)
                async with pt.track_span("postgres", "INSERT questions", phase="persist",
                                         detail=f"{len(question_objects)} rows"):
                    await db.flush()
                s.note(f"{len(question_objects)} rows written")
            await pt.finish_run(run_id, "completed")
            return question_objects

        # ── Retrieval + augmentation paths ───────────────────────
        raw_questions: list[dict[str, Any]] = []

        if source == InformationSource.INDUSTRY:
            async with pt.track_step(run_id, "augment") as s:
                raw_questions = await generate_industry_questions(topic=topic, domain=domain, num_questions=n)
                s.note(f"{len(raw_questions)} questions generated")
        else:
            context_docs: list[Document] = []
            if source in (InformationSource.KNOWLEDGE_BASE, InformationSource.HYBRID):
                async with pt.track_step(run_id, "retrieve") as s:
                    context_docs = await retrieve(
                        topic=topic, context_prompt=context_prompt,
                        domain=domain, org_id=org_id, source_id=source_id,
                    )
                    if not context_docs:
                        s.warn("0 KB chunks retrieved" + (" — relying on web sources" if source == InformationSource.HYBRID else " — generation relies on GPT knowledge"))
                    else:
                        s.note(f"{len(context_docs)} KB chunks")

                # ── HYBRID: supplement KB with credible domain/industry web case studies ──
                if source == InformationSource.HYBRID:
                    async with pt.track_step(run_id, "web") as s:
                        web_docs, web_sources = await gather_web_context(topic, domain, context_prompt)
                        # Interleave KB + web so the grader's truncated preview (and the
                        # augmentor's context block) sample BOTH sources, not just the KB
                        # chunks that happen to come first.
                        kb_docs = context_docs
                        merged: list[Document] = []
                        for i in range(max(len(kb_docs), len(web_docs))):
                            if i < len(kb_docs):
                                merged.append(kb_docs[i])
                            if i < len(web_docs):
                                merged.append(web_docs[i])
                        context_docs = merged
                        if web_sources:
                            meta = dict(assessment.rag_metadata or {})
                            meta["web_sources"] = web_sources          # provenance
                            assessment.rag_metadata = meta
                            db.add(assessment)
                            s.note(f"{len(web_docs)} web case-study sources")
                        else:
                            s.warn("no web sources (provider disabled or none found) — KB only")

                # ── Grade → re-query reflection loop over the COMBINED context ──
                # One cheap grader call judges coverage of KB (+web for hybrid); a
                # bounded loop re-queries the KB for missing sub-topics before either
                # augmenting or failing honestly with InsufficientContext.
                async with pt.track_step(run_id, "grade") as s:
                    context_docs = await _grade_and_refine(
                        topic=topic, context_prompt=context_prompt, domain=domain,
                        org_id=org_id, source_id=source_id, n=n,
                        context_docs=context_docs, assessment=assessment, ctl=s,
                        fail_on_insufficient=(source == InformationSource.KNOWLEDGE_BASE),
                    )

            async with pt.track_step(run_id, "augment") as s:
                if assessment.question_type == QuestionType.SCENARIO:
                    # Case study: author one shared case (stored on the assessment)
                    # + analytical questions about it.
                    case_text, raw_questions = await generate_scenario_assessment(
                        topic=topic, domain=domain, context_docs=context_docs,
                        num_questions=n, context_prompt=context_prompt,
                    )
                    meta = dict(assessment.rag_metadata or {})
                    meta["scenario"] = case_text
                    assessment.rag_metadata = meta   # persisted by the outer db.commit()
                    db.add(assessment)
                    s.note(f"case ({len(case_text)} chars) + {len(raw_questions)} questions")
                elif assessment.question_type == QuestionType.WRITTEN:
                    raw_questions = await generate_written_questions(
                        topic=topic, domain=domain, context_docs=context_docs,
                        num_questions=n, context_prompt=context_prompt,
                    )
                else:
                    raw_questions = await generate_mcq_questions(
                        topic=topic, domain=domain, context_docs=context_docs,
                        num_questions=n, context_prompt=context_prompt,
                    )
                s.note(f"{len(raw_questions)} questions generated")

        async with pt.track_step(run_id, "persist") as s:
            question_objects = []
            for i, q in enumerate(raw_questions):
                question = Question(
                    id=uuid.uuid4(), assessment_id=assessment.id, order_index=i,
                    text=q["question"], question_type=assessment.question_type,
                    options=q.get("options"), correct_answer_index=q.get("correct_index"),
                    correct_answer_text=q.get("model_answer"), explanation=q.get("explanation"),
                    source_reference=q.get("source_reference"), difficulty=int(q.get("difficulty", 3)),
                )
                db.add(question)
                question_objects.append(question)
            async with pt.track_span("postgres", "INSERT questions", phase="persist",
                                     detail=f"{len(question_objects)} rows"):
                await db.flush()
            s.note(f"{len(question_objects)} rows written")

        await pt.finish_run(run_id, "completed")
        logger.info("Pipeline complete: %d questions for assessment %s", len(question_objects), assessment.id)
        return question_objects

    except Exception as e:
        await pt.finish_run(run_id, "failed", error=str(e)[:500])
        raise


__all__ = [
    "load_pdf", "load_docx", "load_xlsx", "load_web", "index_source", "get_chroma",
    "retrieve",
    "generate_mcq_questions", "generate_written_questions", "generate_industry_questions",
    "evaluate_mcq", "evaluate_written",
    "generate_questions_for_assessment",
    "grade_context", "InsufficientContext",
]
