"""
Ops / system-process observability API.

Exposes pipeline runs (generation, indexing, evaluation) and their phased
steps for the System Operations dashboard. Gated on the `system.view`
capability (system_admin by role default, or any user in a group granting it,
e.g. Ops) — matches the nav tab + the /ops page guard.

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
from services.auth_service import require_permission, has_permission, get_user_from_token
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
    current_user: User = Depends(require_permission("system.view")),
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
    current_user: User = Depends(require_permission("system.view")),
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
    current_user: User = Depends(require_permission("system.view")),
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


# ─────────────────────────────────────────────────────────────────
# Platform health metrics (Platform Health dashboard)
# ─────────────────────────────────────────────────────────────────

# Threshold rules surfaced on the dashboard (also used to colour each panel).
THRESHOLDS = {
    "pool_util_warn": 70.0, "pool_util_crit": 90.0,        # % of max DB connections in use
    "eval_p95_warn_ms": 2000.0, "eval_p95_crit_ms": 8000.0, # submit/eval latency p95
    "gen_fail_warn_pct": 20.0, "gen_fail_crit_pct": 50.0,   # generation failure rate
    "cpu_warn": 85.0, "cpu_crit": 100.0,                    # api CPU %
}


def _state(value, warn, crit):
    if value is None:
        return "unknown"
    return "crit" if value >= crit else ("warn" if value >= warn else "ok")


def _pctl(sorted_vals, q):
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, int(len(sorted_vals) * q))
    return round(sorted_vals[i], 1)


@router.get("/metrics")
async def platform_metrics(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("system.view")),
):
    """Live platform-health snapshot for the dashboard: DB pool, process CPU/mem,
    per-service call mix (from real spans), generation success rate, eval latency,
    and threshold evaluation. Cheap + read-mostly; safe to poll every few seconds."""
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    since_1h = now - timedelta(hours=1)
    since_24h = now - timedelta(hours=24)

    # ── DB connection pool (the stress-test bottleneck) ───────────
    pool_block = {"available": False}
    try:
        from database import engine
        pool = engine.pool
        checked_out = pool.checkedout()
        size = pool.size()
        max_overflow = getattr(pool, "_max_overflow", 0)
        max_cap = size + max(max_overflow, 0)
        util = round(100.0 * checked_out / max_cap, 1) if max_cap else 0.0
        pool_block = {
            "available": True, "checked_out": checked_out, "pool_size": size,
            "max_connections": max_cap, "utilization_pct": util,
            "status": _state(util, THRESHOLDS["pool_util_warn"], THRESHOLDS["pool_util_crit"]),
        }
    except Exception as e:
        pool_block["error"] = str(e)[:120]

    # ── API process CPU / memory (psutil; optional) ───────────────
    proc_block = {"available": False}
    try:
        import psutil
        proc_block = {
            "available": True,
            "cpu_pct": round(psutil.cpu_percent(interval=0.15), 1),
            "mem_mb": round(psutil.Process().memory_info().rss / (1024 * 1024), 1),
            "status": _state(psutil.cpu_percent(interval=0.0), THRESHOLDS["cpu_warn"], THRESHOLDS["cpu_crit"]),
        }
    except Exception as e:
        proc_block["error"] = str(e)[:120]

    # ── Per-service call mix from real spans (last hour) ──────────
    from models import PipelineSpan, PipelineRun
    span_rows = (await db.execute(
        select(
            PipelineSpan.service,
            func.count().label("calls"),
            func.avg(PipelineSpan.duration_ms).label("avg_ms"),
            func.sum(PipelineSpan.duration_ms).label("total_ms"),
        )
        .where(PipelineSpan.started_at >= since_1h)
        .group_by(PipelineSpan.service)
        .order_by(func.count().desc())
    )).all()
    services = [{
        "service": s, "calls": c,
        "avg_ms": round(float(a or 0), 1), "total_ms": round(float(t or 0), 1),
    } for s, c, a, t in span_rows]

    # ── Generation success rate (last 24h) ────────────────────────
    gen_rows = dict((st, c) for st, c in (await db.execute(
        select(PipelineRun.status, func.count())
        .where(PipelineRun.kind == "generation", PipelineRun.started_at >= since_24h)
        .group_by(PipelineRun.status)
    )).all())
    gen_completed = gen_rows.get("completed", 0)
    gen_failed = gen_rows.get("failed", 0)
    gen_running = gen_rows.get("running", 0)
    gen_total = gen_completed + gen_failed
    gen_fail_pct = round(100.0 * gen_failed / gen_total, 1) if gen_total else 0.0
    generation = {
        "completed": gen_completed, "failed": gen_failed, "running": gen_running,
        "fail_pct": gen_fail_pct,
        "status": _state(gen_fail_pct, THRESHOLDS["gen_fail_warn_pct"], THRESHOLDS["gen_fail_crit_pct"]),
    }

    # ── Evaluation (submit) latency p50/p95 from real runs (last hour) ──
    durs = sorted(float(d) * 1000 for (d,) in (await db.execute(
        select(func.extract("epoch", PipelineRun.finished_at - PipelineRun.started_at))
        .where(
            PipelineRun.kind == "evaluation",
            PipelineRun.finished_at.isnot(None),
            PipelineRun.started_at >= since_1h,
        )
    )).all() if d is not None)
    p95 = _pctl(durs, 0.95)
    eval_latency = {
        "count": len(durs), "p50_ms": _pctl(durs, 0.50), "p95_ms": p95,
        "status": _state(p95, THRESHOLDS["eval_p95_warn_ms"], THRESHOLDS["eval_p95_crit_ms"]),
    }

    return {
        "generated_at": iso_utc(now),
        "pool": pool_block,
        "process": proc_block,
        "services": services,
        "generation": generation,
        "eval_latency": eval_latency,
        "thresholds": THRESHOLDS,
    }


# ─────────────────────────────────────────────────────────────────
# RAG quality scoring (RAGAS-style; Qwen judge) — for the RAG Quality dashboard
# ─────────────────────────────────────────────────────────────────

RAG_THRESHOLDS = {
    "faithfulness_warn": 0.80, "faithfulness_crit": 0.60,
    "context_precision_warn": 0.70, "context_precision_crit": 0.50,
}


def _state_low(v, warn, crit):
    """Higher is better: crit below `crit`, warn below `warn`, else ok."""
    if v is None:
        return "unknown"
    return "crit" if v < crit else ("warn" if v < warn else "ok")


@router.post("/rag-eval/run")
async def rag_eval_run(
    n: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("system.view")),
):
    """Sample `n` random un-scored RAG transactions and score them (faithfulness +
    context precision) with the Qwen judge. Stores the scores; returns a summary."""
    from datetime import datetime
    from models import RagSample
    from rag.scorer import score_sample, is_configured
    from config import settings

    if not is_configured():
        raise HTTPException(400, "RAG scorer not configured — set RAG_SCORER_API_KEY in .env (then force-recreate the api container).")

    rows = (await db.execute(
        select(RagSample)
        .where(RagSample.scored_at.is_(None), RagSample.contexts.isnot(None))
        .order_by(func.random())
        .limit(n)
    )).scalars().all()

    scored, results = 0, []
    for s in rows:
        res = await score_sample(s.topic or "", s.contexts or [], s.items or [])
        if res.get("faithfulness") is not None or res.get("context_precision") is not None:
            s.faithfulness = res.get("faithfulness")
            s.context_precision = res.get("context_precision")
            s.rationale = res.get("rationale")
            s.scored_at = datetime.utcnow()
            db.add(s)
            scored += 1
            results.append({"id": str(s.id), "topic": s.topic,
                            "faithfulness": s.faithfulness, "context_precision": s.context_precision})
    await db.flush()
    return {"model": settings.RAG_SCORER_MODEL, "sampled": len(rows), "scored": scored, "results": results}


@router.get("/rag-eval")
async def rag_eval_summary(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("system.view")),
):
    """RAG-quality dashboard data: configured flag, coverage, average scores (with
    threshold states), and the most recently scored transactions."""
    from models import RagSample
    from rag.scorer import is_configured
    from config import settings

    total = await db.scalar(select(func.count()).select_from(RagSample))
    scored = await db.scalar(select(func.count()).select_from(RagSample).where(RagSample.scored_at.isnot(None)))
    avg_f = await db.scalar(select(func.avg(RagSample.faithfulness)).where(RagSample.faithfulness.isnot(None)))
    avg_cp = await db.scalar(select(func.avg(RagSample.context_precision)).where(RagSample.context_precision.isnot(None)))
    recent = (await db.execute(
        select(RagSample).where(RagSample.scored_at.isnot(None))
        .order_by(RagSample.scored_at.desc()).limit(20)
    )).scalars().all()

    avg_f = round(float(avg_f), 3) if avg_f is not None else None
    avg_cp = round(float(avg_cp), 3) if avg_cp is not None else None
    return {
        "configured": is_configured(),
        "model": settings.RAG_SCORER_MODEL,
        "total": total or 0, "scored": scored or 0, "unscored": (total or 0) - (scored or 0),
        "avg_faithfulness": avg_f, "avg_context_precision": avg_cp,
        "faithfulness_status": _state_low(avg_f, RAG_THRESHOLDS["faithfulness_warn"], RAG_THRESHOLDS["faithfulness_crit"]),
        "context_precision_status": _state_low(avg_cp, RAG_THRESHOLDS["context_precision_warn"], RAG_THRESHOLDS["context_precision_crit"]),
        "thresholds": RAG_THRESHOLDS,
        "samples": [{
            "id": str(s.id), "topic": s.topic, "question_type": s.question_type,
            "information_source": s.information_source,
            "faithfulness": s.faithfulness, "context_precision": s.context_precision,
            "rationale": s.rationale, "scored_at": iso_utc(s.scored_at),
        } for s in recent],
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
        if not await has_permission(user, "system.view", db):
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
