import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, DateTime, ForeignKey,
    Enum as SAEnum, Text, Integer, Boolean
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from database import Base
import enum


class SourceType(str, enum.Enum):
    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"
    WEB = "web"          # crawled website
    URL = "url"          # single external URL


class SourceStatus(str, enum.Enum):
    PENDING = "pending"
    INDEXING = "indexing"
    ACTIVE = "active"
    FAILED = "failed"
    STALE = "stale"


class KnowledgeSource(Base):
    __tablename__ = "knowledge_sources"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"), nullable=False)
    name = Column(String(500), nullable=False)           # display name / filename
    source_type = Column(SAEnum(SourceType), nullable=False)
    url = Column(Text, nullable=True)                    # for web/url sources
    s3_key = Column(String(500), nullable=True)          # for uploaded files
    domain_tag = Column(String(100), nullable=True)      # technical | governance | soft-skills
    status = Column(SAEnum(SourceStatus), nullable=False, default=SourceStatus.PENDING)
    chunk_count = Column(Integer, default=0)
    indexed_at = Column(DateTime, nullable=True)
    index_error = Column(Text, nullable=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)

    org = relationship("Organisation", back_populates="knowledge_sources")
    chunks = relationship("DocumentChunk", back_populates="source", cascade="all, delete-orphan")


class DocumentChunk(Base):
    """
    Stores metadata about each chunk that has been embedded into
    the vector store. The actual embedding lives in Chroma; this
    table is the relational source of truth for provenance.
    """
    __tablename__ = "document_chunks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id = Column(UUID(as_uuid=True), ForeignKey("knowledge_sources.id"), nullable=False)
    chroma_id = Column(String(100), unique=True, nullable=False)  # Chroma document ID
    content = Column(Text, nullable=False)
    chunk_metadata = Column(JSONB, nullable=True)   # page number, section heading, URL, etc.
    token_count = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    source = relationship("KnowledgeSource", back_populates="chunks")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    action = Column(String(100), nullable=False)           # e.g. DEPLOY_ASSESSMENT
    resource_type = Column(String(100), nullable=True)     # e.g. assessment
    resource_id = Column(UUID(as_uuid=True), nullable=True)
    detail = Column(JSONB, nullable=True)                  # extra context
    ip_address = Column(String(45), nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
