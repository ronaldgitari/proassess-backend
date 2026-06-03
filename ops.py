"""
Ops / system-process observability API.

Exposes pipeline runs (generation, indexing, evaluation) and their phased
steps for the System Operations dashboard. Restricted to system_admin.

Endpoints:
  GET /ops/runs              — recent runs with step summary
  GET /ops/runs/{id}         — single run with ordered steps
  GET /ops/runs/{id}/stream  — Server-Sent Events live-tail of a run
"""
import asyncio
import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import get_db, AsyncSessionLocal
from models import PipelineRun, PipelineStep, PipelineSpan, User, Assessment, KnowledgeSource, StaffAssessment
from services.auth_service import require_system_admin, get_user_from_token
from models import UserRole
from timeutil import iso_utc

router = APIRouter(prefix="/ops", tags=["ops"])


# ── Log capsule: backing-service display metadata ──
SERVICE_META = {
    "app":      {"label": "Application (FastAPI)", "color": "#6366f1"},
    "openai":   {"label": "OpenAI (GPT-4o)",       "color": "#10a37f"},
    "chroma":   {"label": "Chroma (vector DB)",    "color": "#8b5cf6"},
    "postgres": {"label": "PostgreSQL",            "color": "#336791"},
    "redis":    {"label": "Redis",                 "color": "#dc382d"},
    "minio":    {"label": "MinIO (object store)",  "color": "#c72c48"},
    "web":      {"label": "Web Search",            "color": "#0ea5e9"},
}


def _reference_block(ks: KnowledgeSource) -> dict:
    """Capsule provenance for a KB source: the reference document name (uploaded
    docs) or the URL (custom URL / web)."""
    st = ks.source_type.value if hasattr(ks.source_type, "value") else str(ks.source_type)
    is_url = st in ("url", "web")
    return {
        "kind": "url" if is_url else "document",
        "label": (ks.url if (is_url and ks.url) else ks.name),
        "name": ks.name,
        "url": ks.url,
        "source_type": st,
    }


async def _assessment_provenance(db: AsyncSession, a: Assessment | None) -> dict:
    """Source mode + reference doc/URL + web-source count for an assessment."""
    out = {"information_source": None, "reference": None, "web_sources_count": None,
           "assessment_name": None}
    if not a:
        return out
    out["assessment_name"] = a.name
    out["information_source"] = a.information_source.value
    meta_a = a.rag_metadata or {}
    sid = meta_a.get("source_id")
    if sid:
        ks = await db.get(KnowledgeSource, uuid.UUID(str(sid)))
        if ks:
            out["reference"] = _reference_block(ks)
    ws = meta_a.get("web_sources")
    if isinstance(ws, list) and ws:
        out["web_sources_count"] = len(ws)
    return out


async def _resolve_reference(db: AsyncSession, run: PipelineRun) -> dict:
    """
    Resolve provenance for a run's capsule:
      - generation → the assessment's source mode + reference doc/URL (+ web count)
      - evaluation → the assessment (+ source) AND the candidate being scored
      - indexing   → the KB source being indexed
    """
    extra = {"information_source": None, "reference": None, "web_sources_count": None,
             "assessment_name": None, "candidate": None}
    try:
        if run.kind == "generation" and run.ref_id:
            extra.update(await _assessment_provenance(db, await db.get(Assessment, run.ref_id)))
        elif run.kind == "evaluation" and run.ref_id:
            sa = await db.get(StaffAssessment, run.ref_id)
            if sa:
                u = await db.get(User, sa.user_id)
                if u:
                    extra["candidate"] = u.name
                extra.update(await _assessment_provenance(db, await db.get(Assessment, sa.assessment_id)))
        elif run.kind == "indexing" and run.ref_id:
            ks = await db.get(KnowledgeSource, run.ref_id)
            if ks:
                extra["information_source"] = "kb"
                extra["reference"] = _reference_block(ks)
    except Exception:
        pass
    return extra


def _step_dict(s: PipelineStep) -> dict:
    return {
        "id": str(s.id),
        "order_index": s.order_index,
        "phase": s.phase,
        "label": s.label,
        "status": s.status,
        "detail": s.detail,
        "started_at": iso_utc(s.started_at),
        "finished_at": iso_utc(s.finished_at),
    }


def _run_dict(r: PipelineRun, steps: list[PipelineStep] | None = None) -> dict:
    d = {
        "id": str(r.id),
        "kind": r.kind,
        "label": r.label,
        "status": r.status,
        "error": r.error,
        "ref_id": str(r.ref_id) if r.ref_id else None,
        "started_at": iso_utc(r.started_at),
        "finished_at": iso_utc(r.finished_at),
    }
    if steps is not None:
        d["steps"] = [_step_dict(s) for s in steps]
    return d


@router.get("/runs")
async def list_runs(
    limit: int = Query(30, ge=1, le=200),
    kind: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_system_admin),
):
    q = select(PipelineRun).order_by(PipelineRun.started_at.desc()).limit(limit)
    if kind:
        q = q.where(PipelineRun.kind == kind)
    runs = (await db.execute(q)).scalars().all()

    # Per-run step status counts for the summary chips
    out = []
    for r in runs:
        counts = dict(
            (status, cnt)
            for status, cnt in (await db.execute(
                select(PipelineStep.status, func.count())
                .where(PipelineStep.run_id == r.id)
                .group_by(PipelineStep.status)
            )).all()
        )
        total = sum(counts.values())
        done = counts.get("ok", 0) + counts.get("warn", 0) + counts.get("error", 0)
        d = _run_dict(r)
        d["step_counts"] = counts
        d["steps_total"] = total
        d["steps_done"] = done
        out.append(d)
    return out


@router.get("/runs/{run_id}")
async def get_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_system_admin),
):
    run = (await db.execute(select(PipelineRun).where(PipelineRun.id == run_id))).scalar_one_or_none()
    if not run:
        raise HTTPException(404, "Run not found")
    steps = (await db.execute(
        select(PipelineStep).where(PipelineStep.run_id == run_id).order_by(PipelineStep.order_index)
    )).scalars().all()
    return _run_dict(run, steps)


@router.get("/runs/{run_id}/capsule")
async def get_capsule(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_system_admin),
):
    """
    Log capsule for one transaction (a pipeline run): metadata header +
    the run's REAL per-service spans grouped by the backing service that
    produced them (OpenAI / Chroma / Postgres / …), each with true timing.
    """
    run = (await db.execute(select(PipelineRun).where(PipelineRun.id == run_id))).scalar_one_or_none()
    if not run:
        raise HTTPException(404, "Run not found")

    spans = (await db.execute(
        select(PipelineSpan).where(PipelineSpan.run_id == run_id).order_by(PipelineSpan.started_at)
    )).scalars().all()

    # ── Group real spans by service ──────────────────────────────
    grouped: dict[str, list[dict]] = {}
    services_involved: list[str] = []
    total_ms_by_svc: dict[str, float] = {}
    for sp in spans:
        svc = sp.service
        if svc not in grouped:
            grouped[svc] = []
            services_involved.append(svc)
            total_ms_by_svc[svc] = 0.0
        total_ms_by_svc[svc] += sp.duration_ms or 0.0
        grouped[svc].append({
            "operation": sp.operation,
            "phase": sp.phase,
            "status": sp.status,
            "detail": sp.detail,
            "duration_ms": sp.duration_ms,
            "started_at": iso_utc(sp.started_at),
            "finished_at": iso_utc(sp.finished_at),
        })

    services_block = [{
        "key": svc,
        "label": SERVICE_META.get(svc, {}).get("label", svc),
        "color": SERVICE_META.get(svc, {}).get("color", "#64748b"),
        "total_ms": round(total_ms_by_svc[svc], 1),
        "call_count": len(grouped[svc]),
        "logs": grouped[svc],
    } for svc in services_involved]

    # Last-action timestamp = latest span finish (fallback to run finish)
    stamps = [sp.finished_at or sp.started_at for sp in spans if (sp.finished_at or sp.started_at)]
    last_action = iso_utc(max(stamps)) if stamps else iso_utc(run.finished_at)

    # Provenance: source mode + reference doc/URL (+ candidate for evaluation runs).
    prov = await _resolve_reference(db, run)

    return {
        "capsule_id": str(run.id),
        "kind": run.kind,
        "label": run.label,
        "status": run.status,
        "ref_id": str(run.ref_id) if run.ref_id else None,
        "metadata": {
            "services": [SERVICE_META.get(s, {}).get("label", s) for s in services_involved],
            "service_keys": services_involved,
            "origin_ip": run.origin_ip,                 # real client IP captured at trigger
            "server_ip": run.server_ip,                 # host the API ran on
            "system_id": run.system_id,                 # server/container id
            "started_at": iso_utc(run.started_at),
            "last_action_at": last_action,
            "total_spans": len(spans),
            "information_source": prov["information_source"],   # kb | hybrid | ai | url | industry
            "reference": prov["reference"],                     # {kind, label, name, url, source_type} or None
            "web_sources_count": prov["web_sources_count"],     # hybrid: # of web case-study sources
            "assessment_name": prov["assessment_name"],         # generation + evaluation
            "candidate": prov["candidate"],                     # evaluation: who was scored
        },
        "services": services_block,
    }


@router.get("/runs/{run_id}/stream")
async def stream_run(
    run_id: uuid.UUID,
    token: str = Query(..., description="Access token (EventSource can't send headers)"),
):
    """Server-Sent Events: pushes the run + steps whenever they change, until the run finishes."""
    # Auth via query-param token (EventSource limitation)
    async with AsyncSessionLocal() as db:
        user = await get_user_from_token(token, db)
        if user.role != UserRole.SYSTEM_ADMIN:
            raise HTTPException(403, "Insufficient permissions")

    async def event_gen():
        last_payload = None
        # Stream for up to ~10 minutes (matches generation timeout window)
        for _ in range(600):
            async with AsyncSessionLocal() as db:
                run = (await db.execute(select(PipelineRun).where(PipelineRun.id == run_id))).scalar_one_or_none()
                if not run:
                    yield f"event: error\ndata: {json.dumps({'detail': 'Run not found'})}\n\n"
                    return
                steps = (await db.execute(
                    select(PipelineStep).where(PipelineStep.run_id == run_id).order_by(PipelineStep.order_index)
                )).scalars().all()
                payload = json.dumps(_run_dict(run, steps))

            if payload != last_payload:
                last_payload = payload
                yield f"data: {payload}\n\n"

            if run.status in ("completed", "failed"):
                yield "event: done\ndata: {}\n\n"
                return

            await asyncio.sleep(1)

        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
