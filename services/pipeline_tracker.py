"""
Pipeline tracker — records system-process phases for the ops dashboard.

Every write opens its OWN short-lived AsyncSession and commits immediately,
so the steps become visible to the polling / SSE endpoints in real time,
independent of whatever transaction the pipeline itself is running in.

Designed to be non-fatal: if tracking ever errors, it logs and swallows so
it can never break the actual pipeline it observes.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from contextlib import asynccontextmanager
from contextvars import ContextVar

from dataclasses import dataclass, field
from sqlalchemy import select, update

logger = logging.getLogger(__name__)


@dataclass
class _SpanCtx:
    """Yielded by track_span so callers can attach token counts before the span is written."""
    span_id: str | None
    _prompt_tokens: int | None = field(default=None, repr=False)
    _completion_tokens: int | None = field(default=None, repr=False)

    def capture(self, response) -> None:
        """Read token usage from a LangChain AIMessage and store it for the span write."""
        try:
            um = getattr(response, "usage_metadata", None) or {}
            p = int(um.get("input_tokens") or um.get("prompt_tokens") or 0) or None
            c = int(um.get("output_tokens") or um.get("completion_tokens") or 0) or None
            self._prompt_tokens, self._completion_tokens = p, c
        except Exception:
            pass

# Current run id for the active transaction. A ContextVar propagates across
# `await` boundaries and into asyncio.gather() tasks automatically, so deep
# client calls (OpenAI / Chroma / Postgres) can record spans against the
# right run WITHOUT threading run_id through every function signature.
_current_run: ContextVar[str | None] = ContextVar("_current_run", default=None)


def set_current_run(run_id: str | None) -> None:
    _current_run.set(run_id)


def get_current_run() -> str | None:
    return _current_run.get()


# Origin IP of the request that triggered the current transaction. Set by the
# API route (create/submit) before the background work starts; read when the
# run is created so it lands in capsule metadata.
_origin_ip: ContextVar[str | None] = ContextVar("_origin_ip", default=None)


def set_origin_ip(ip: str | None) -> None:
    _origin_ip.set(ip)


async def capture_server_meta(run_id: str | None) -> None:
    """Record server IP + system id (and the origin IP from the contextvar) on the run."""
    import socket
    if not run_id:
        return
    try:
        host = socket.gethostname()
        server_ip = socket.gethostbyname(host) if host else None
    except Exception:
        host, server_ip = None, None
    await set_run_meta(run_id, origin_ip=_origin_ip.get(), server_ip=server_ip, system_id=host)


@asynccontextmanager
async def _session():
    # Use the shared pooled sessionmaker: connections are REUSED (cheap), and the
    # enlarged request pool (database.py) gives enough headroom that tracking writes
    # don't starve `get_db`. An earlier attempt to isolate tracking on a NullPool
    # engine backfired — opening a fresh connection per write added more latency
    # under load than the pool-wait it removed.
    from database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        yield db


async def create_run(
    *,
    kind: str,
    label: str,
    steps: list[tuple[str, str]],     # [(phase_key, human_label), ...]
    org_id=None,
    ref_id=None,
) -> str | None:
    """Create a run with its full set of pending steps. Returns run_id (str) or None."""
    from models import PipelineRun, PipelineStep
    try:
        run_id = uuid.uuid4()
        async with _session() as db:
            db.add(PipelineRun(
                id=run_id, org_id=org_id, kind=kind, ref_id=ref_id,
                label=label, status="running", started_at=datetime.utcnow(),
            ))
            for i, (phase, lbl) in enumerate(steps):
                db.add(PipelineStep(
                    id=uuid.uuid4(), run_id=run_id, order_index=i,
                    phase=phase, label=lbl, status="pending",
                ))
            await db.commit()
        return str(run_id)
    except Exception as e:
        logger.warning("pipeline_tracker.create_run failed: %s", e)
        return None


async def start_step(run_id: str | None, phase: str) -> None:
    if not run_id:
        return
    from models import PipelineStep
    try:
        async with _session() as db:
            await db.execute(
                update(PipelineStep)
                .where(PipelineStep.run_id == uuid.UUID(run_id), PipelineStep.phase == phase)
                .values(status="running", started_at=datetime.utcnow())
            )
            await db.commit()
    except Exception as e:
        logger.warning("pipeline_tracker.start_step failed: %s", e)


async def finish_step(
    run_id: str | None, phase: str, status: str = "ok", detail: str | None = None
) -> None:
    """status: ok | warn | error"""
    if not run_id:
        return
    from models import PipelineStep
    try:
        async with _session() as db:
            await db.execute(
                update(PipelineStep)
                .where(PipelineStep.run_id == uuid.UUID(run_id), PipelineStep.phase == phase)
                .values(status=status, detail=detail, finished_at=datetime.utcnow())
            )
            await db.commit()
    except Exception as e:
        logger.warning("pipeline_tracker.finish_step failed: %s", e)


async def finish_run(run_id: str | None, status: str, error: str | None = None) -> None:
    """status: completed | failed"""
    if not run_id:
        return
    from models import PipelineRun
    try:
        async with _session() as db:
            await db.execute(
                update(PipelineRun)
                .where(PipelineRun.id == uuid.UUID(run_id))
                .values(status=status, error=error, finished_at=datetime.utcnow())
            )
            await db.commit()
    except Exception as e:
        logger.warning("pipeline_tracker.finish_run failed: %s", e)


@asynccontextmanager
async def track_step(run_id: str | None, phase: str):
    """
    Context manager: marks a step running, then ok on success or error on exception.
    Use `warn()` inside to downgrade the final status to a warning.

        async with track_step(run_id, "retrieval") as step:
            ...
            step.warn("0 chunks; fell back to org-wide search")
    """
    class _Ctl:
        def __init__(self):
            self._status = "ok"
            self._detail = None
        def warn(self, detail: str):
            self._status = "warn"
            self._detail = detail
        def note(self, detail: str):
            self._detail = detail

    ctl = _Ctl()
    await start_step(run_id, phase)
    try:
        yield ctl
    except Exception as e:
        await finish_step(run_id, phase, status="error", detail=str(e)[:500])
        raise
    else:
        await finish_step(run_id, phase, status=ctl._status, detail=ctl._detail)


# ─────────────────────────────────────────────────────────────────
# Real per-service spans (capsule v2)
# ─────────────────────────────────────────────────────────────────

async def set_run_meta(run_id: str | None, *, origin_ip=None, server_ip=None, system_id=None) -> None:
    """Attach origin/server capsule metadata to a run."""
    if not run_id:
        return
    from models import PipelineRun
    try:
        vals = {}
        if origin_ip is not None: vals["origin_ip"] = origin_ip
        if server_ip is not None: vals["server_ip"] = server_ip
        if system_id is not None: vals["system_id"] = system_id
        if not vals:
            return
        async with _session() as db:
            await db.execute(update(PipelineRun).where(PipelineRun.id == uuid.UUID(run_id)).values(**vals))
            await db.commit()
    except Exception as e:
        logger.warning("pipeline_tracker.set_run_meta failed: %s", e)


async def record_span(
    *, span_id=None, service: str, operation: str, status: str, duration_ms: float,
    detail: str | None = None, phase: str | None = None,
    started_at: datetime | None = None, finished_at: datetime | None = None,
    run_id: str | None = None,
    prompt_tokens: int | None = None, completion_tokens: int | None = None,
) -> None:
    """Persist one finished span. Uses the contextvar run if run_id not given."""
    run_id = run_id or get_current_run()
    if not run_id:
        return
    from models import PipelineSpan
    try:
        async with _session() as db:
            db.add(PipelineSpan(
                id=span_id or uuid.uuid4(), run_id=uuid.UUID(run_id),
                service=service, operation=operation, phase=phase,
                status=status, detail=(detail[:500] if detail else None),
                duration_ms=round(duration_ms, 1),
                started_at=started_at, finished_at=finished_at,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            ))
            await db.commit()
    except Exception as e:
        logger.warning("pipeline_tracker.record_span failed: %s", e)


@asynccontextmanager
async def track_span(service: str, operation: str, *, phase: str | None = None, detail: str | None = None):
    """
    Wrap a real backing-service call to record a span with true timing.
    Reads the active run from the contextvar — no run_id plumbing needed.

        async with track_span("openai", "chat.completion", phase="augment") as span:
            response = await llm.ainvoke(...)
            span.capture(response)   # attaches token counts to the span

    Non-fatal: span-recording errors never propagate; the wrapped call's own
    exceptions DO propagate (after recording an error span).
    """
    if get_current_run() is None:
        # Not inside a tracked run — no-op passthrough; _SpanCtx(None) is safe to call .capture() on
        yield _SpanCtx(None)
        return

    sid = uuid.uuid4()
    ctx = _SpanCtx(str(sid))
    start = time.perf_counter()
    started_at = datetime.utcnow()
    try:
        yield ctx
    except Exception as e:
        await record_span(
            span_id=sid, service=service, operation=operation, status="error",
            duration_ms=(time.perf_counter() - start) * 1000,
            detail=str(e)[:500], phase=phase,
            started_at=started_at, finished_at=datetime.utcnow(),
        )
        raise
    else:
        await record_span(
            span_id=sid, service=service, operation=operation, status="ok",
            duration_ms=(time.perf_counter() - start) * 1000,
            detail=detail, phase=phase,
            started_at=started_at, finished_at=datetime.utcnow(),
            prompt_tokens=ctx._prompt_tokens, completion_tokens=ctx._completion_tokens,
        )
