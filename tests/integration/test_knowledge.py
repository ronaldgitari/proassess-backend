"""Knowledge-base upload: duplicate-name guard + in-place replace.

The duplicate check runs before the background indexer is scheduled, so the 409
path needs no mocking. For the paths that DO schedule indexing (replace / new
name), we stub `_index_document_background` so tests never hit Chroma/OpenAI.
"""
import uuid
from datetime import datetime

import pytest

KB = "/api/v1/knowledge"


@pytest.fixture(autouse=True)
def _stub_indexing(monkeypatch):
    """Neutralise the background indexer (Chroma + embeddings over the network)."""
    async def _noop(*args, **kwargs):
        return None
    monkeypatch.setattr("knowledge._index_document_background", _noop)


async def _seed_source(db, org, name="policy.pdf", active=True):
    from models import KnowledgeSource, SourceType, SourceStatus
    src = KnowledgeSource(
        id=uuid.uuid4(), org_id=org.id, name=name, source_type=SourceType.PDF,
        domain_tag="general", status=SourceStatus.ACTIVE, chunk_count=3,
        created_by=org.hr.id, is_active=active, indexed_at=datetime.utcnow(),
    )
    db.add(src)
    await db.commit()
    return src


def _pdf(name="policy.pdf"):
    return {"file": (name, b"%PDF-1.4 minimal test bytes", "application/pdf")}


async def test_duplicate_name_rejected_with_409(client, org, login, db):
    await _seed_source(db, org, "policy.pdf")
    hr = await login("hr@t.com")
    r = await client.post(f"{KB}/upload", headers=hr, files=_pdf("policy.pdf"))
    assert r.status_code == 409, r.text
    assert "document with the same name exists" in r.json()["detail"].lower()
    assert "rename" in r.json()["detail"].lower()


async def test_duplicate_match_is_case_insensitive(client, org, login, db):
    await _seed_source(db, org, "Policy.PDF")
    hr = await login("hr@t.com")
    r = await client.post(f"{KB}/upload", headers=hr, files=_pdf("policy.pdf"))
    assert r.status_code == 409, r.text


async def test_replace_reindexes_in_place_same_id(client, org, login, db):
    existing = await _seed_source(db, org, "policy.pdf")
    hr = await login("hr@t.com")
    r = await client.post(f"{KB}/upload", headers=hr, files=_pdf("policy.pdf"),
                          data={"replace": "true"})
    assert r.status_code == 200, r.text
    body = r.json()
    # Same source id is preserved (assessments referencing it keep resolving) and
    # the record is reset for re-indexing.
    assert body["id"] == str(existing.id)
    assert body["status"] == "pending"
    assert body["chunk_count"] == 0


async def test_distinct_name_creates_new_source(client, org, login, db):
    await _seed_source(db, org, "policy.pdf")
    hr = await login("hr@t.com")
    r = await client.post(f"{KB}/upload", headers=hr, files=_pdf("handbook.pdf"))
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "handbook.pdf"
    assert r.json()["status"] == "pending"


async def test_soft_deleted_duplicate_does_not_block(client, org, login, db):
    """A previously removed (is_active=False) source with the same name is not a
    conflict — the name is free to reuse."""
    await _seed_source(db, org, "policy.pdf", active=False)
    hr = await login("hr@t.com")
    r = await client.post(f"{KB}/upload", headers=hr, files=_pdf("policy.pdf"))
    assert r.status_code == 200, r.text
