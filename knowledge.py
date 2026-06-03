"""
Knowledge source API — document upload, URL indexing, re-indexing.
All routes require HR Admin or LM privileges.
"""

import io
import uuid
from typing import List

from fastapi import APIRouter, Depends, File, Form, UploadFile, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models import KnowledgeSource, DocumentChunk, User, SourceType, SourceStatus
from schemas import KnowledgeSourceOut, AddUrlSourceRequest
from services.auth_service import require_hr, require_lm, get_current_user, require_permission
from rag.indexer import load_pdf, load_docx, load_xlsx, load_web, index_source

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


# ─────────────────────────────────────────────────────────────────
# List sources
# ─────────────────────────────────────────────────────────────────

@router.get("/", response_model=List[KnowledgeSourceOut])
async def list_sources(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("kb.view")),
):
    result = await db.execute(
        select(KnowledgeSource)
        .where(
            KnowledgeSource.org_id == current_user.org_id,
            KnowledgeSource.is_active == True,
        )
        .order_by(KnowledgeSource.created_at.desc())
    )
    return result.scalars().all()


# ─────────────────────────────────────────────────────────────────
# Upload a document file
# ─────────────────────────────────────────────────────────────────

ALLOWED_CONTENT_TYPES = {
    "application/pdf": SourceType.PDF,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": SourceType.DOCX,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": SourceType.XLSX,
    "application/octet-stream": None,   # infer from extension
}

EXTENSION_MAP = {
    ".pdf": SourceType.PDF,
    ".docx": SourceType.DOCX,
    ".xlsx": SourceType.XLSX,
}


@router.post("/upload", response_model=KnowledgeSourceOut)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    domain_tag: str = Form(default="general"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("kb.manage")),
):
    """Upload a PDF, DOCX, or XLSX document and trigger indexing."""

    # Determine source type
    import os
    ext = os.path.splitext(file.filename or "")[1].lower()
    source_type = EXTENSION_MAP.get(ext)
    if not source_type:
        raise HTTPException(400, f"Unsupported file type: {ext}. Use PDF, DOCX, or XLSX.")

    file_bytes = await file.read()
    if len(file_bytes) > 50 * 1024 * 1024:  # 50 MB limit
        raise HTTPException(400, "File exceeds 50 MB limit")

    # Create source record
    source = KnowledgeSource(
        id=uuid.uuid4(),
        org_id=current_user.org_id,
        name=file.filename,
        source_type=source_type,
        domain_tag=domain_tag,
        status=SourceStatus.PENDING,
        created_by=current_user.id,
    )
    db.add(source)
    await db.flush()

    # TODO: upload to S3/MinIO
    # s3_key = f"{current_user.org_id}/{source.id}/{file.filename}"
    # await s3_client.put_object(Bucket=settings.S3_BUCKET, Key=s3_key, Body=file_bytes)
    # source.s3_key = s3_key

    background_tasks.add_task(
        _index_document_background,
        source_id=str(source.id),
        source_type=source_type.value,
        file_bytes=file_bytes,
        org_id=str(current_user.org_id),
        domain_tag=domain_tag,
        filename=file.filename,
    )

    return source


async def _index_document_background(
    source_id: str,
    source_type: str,
    file_bytes: bytes,
    org_id: str,
    domain_tag: str,
    filename: str,
):
    from database import AsyncSessionLocal
    from services import pipeline_tracker as pt

    run_id = await pt.create_run(
        kind="indexing",
        label=f"Index · {filename}",
        steps=[("load", "Load & parse document"), ("index", "Chunk, embed & store (Chroma)")],
        org_id=uuid.UUID(org_id),
        ref_id=uuid.UUID(source_id),
    )
    pt.set_current_run(run_id)
    await pt.capture_server_meta(run_id)

    async with AsyncSessionLocal() as db:
        source = await db.get(KnowledgeSource, uuid.UUID(source_id))
        if not source:
            await pt.finish_run(run_id, "failed", error="Source record not found")
            return

        source.status = SourceStatus.INDEXING
        db.add(source)
        await db.flush()

        try:
            async with pt.track_step(run_id, "load") as s:
                if source_type == "pdf":
                    docs = load_pdf(file_bytes, filename)
                elif source_type == "docx":
                    docs = load_docx(file_bytes, filename)
                elif source_type == "xlsx":
                    docs = load_xlsx(file_bytes, filename)
                else:
                    raise ValueError(f"Unsupported type: {source_type}")
                if not docs:
                    s.warn("No extractable text found in document")
                else:
                    s.note(f"{len(docs)} sections parsed")

            async with pt.track_step(run_id, "index") as s:
                chunk_ids = await index_source(
                    source_id=source_id, org_id=org_id,
                    domain_tag=domain_tag, docs=docs, db_session=db,
                )
                s.note(f"{len(chunk_ids)} chunks embedded & stored")

            from datetime import datetime
            source.status = SourceStatus.ACTIVE
            source.chunk_count = len(chunk_ids)
            source.indexed_at = datetime.utcnow()
            db.add(source)
            await db.commit()
            await pt.finish_run(run_id, "completed")

        except Exception as e:
            import logging
            logging.getLogger(__name__).error("Indexing failed for %s: %s", source_id, e)
            source.status = SourceStatus.FAILED
            source.index_error = str(e)
            db.add(source)
            await db.commit()
            await pt.finish_run(run_id, "failed", error=str(e)[:500])


# ─────────────────────────────────────────────────────────────────
# Add a URL source
# ─────────────────────────────────────────────────────────────────

@router.post("/url", response_model=KnowledgeSourceOut)
async def add_url(
    req: AddUrlSourceRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("kb.manage")),
):
    """Add and index an external URL."""
    source = KnowledgeSource(
        id=uuid.uuid4(),
        org_id=current_user.org_id,
        name=req.name or req.url,
        source_type=SourceType.URL,
        url=req.url,
        domain_tag=req.domain_tag or "general",
        status=SourceStatus.PENDING,
        created_by=current_user.id,
    )
    db.add(source)
    await db.flush()

    background_tasks.add_task(
        _index_url_background,
        source_id=str(source.id),
        url=req.url,
        org_id=str(current_user.org_id),
        domain_tag=req.domain_tag or "general",
    )
    return source


async def _index_url_background(source_id: str, url: str, org_id: str, domain_tag: str):
    from database import AsyncSessionLocal
    from datetime import datetime

    async with AsyncSessionLocal() as db:
        source = await db.get(KnowledgeSource, uuid.UUID(source_id))
        if not source:
            return

        source.status = SourceStatus.INDEXING
        db.add(source)
        await db.flush()

        try:
            docs = await load_web(url, source.name)
            chunk_ids = await index_source(
                source_id=source_id, org_id=org_id,
                domain_tag=domain_tag, docs=docs, db_session=db,
            )
            source.status = SourceStatus.ACTIVE
            source.chunk_count = len(chunk_ids)
            source.indexed_at = datetime.utcnow()
            db.add(source)
            await db.commit()
        except Exception as e:
            import logging
            logging.getLogger(__name__).error("URL indexing failed %s: %s", url, e)
            source.status = SourceStatus.FAILED
            source.index_error = str(e)
            db.add(source)
            await db.commit()


# ─────────────────────────────────────────────────────────────────
# Re-index an existing source
# ─────────────────────────────────────────────────────────────────

@router.post("/{source_id}/reindex", response_model=KnowledgeSourceOut)
async def reindex(
    source_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("kb.manage")),
):
    """Delete existing chunks and re-index a source."""
    result = await db.execute(
        select(KnowledgeSource).where(
            KnowledgeSource.id == source_id,
            KnowledgeSource.org_id == current_user.org_id,
        )
    )
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(404, "Source not found")

    # Delete old chunks from Chroma
    from rag.indexer import get_chroma
    chroma = get_chroma()
    old_chunks = await db.execute(
        select(DocumentChunk).where(DocumentChunk.source_id == source_id)
    )
    old_ids = [c.chroma_id for c in old_chunks.scalars().all()]
    if old_ids:
        chroma.delete(ids=old_ids)

    # Delete from DB
    await db.execute(
        DocumentChunk.__table__.delete().where(DocumentChunk.source_id == source_id)
    )
    source.status = SourceStatus.PENDING
    source.chunk_count = 0
    db.add(source)
    await db.flush()

    # Re-trigger indexing if URL source
    if source.source_type == SourceType.URL and source.url:
        background_tasks.add_task(
            _index_url_background,
            source_id=str(source.id),
            url=source.url,
            org_id=str(current_user.org_id),
            domain_tag=source.domain_tag or "general",
        )

    return source


# ─────────────────────────────────────────────────────────────────
# Remove a source
# ─────────────────────────────────────────────────────────────────

@router.delete("/{source_id}", status_code=204)
async def remove_source(
    source_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("kb.manage")),
):
    result = await db.execute(
        select(KnowledgeSource).where(
            KnowledgeSource.id == source_id,
            KnowledgeSource.org_id == current_user.org_id,
        )
    )
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(404, "Source not found")

    # Soft-delete
    source.is_active = False
    db.add(source)
