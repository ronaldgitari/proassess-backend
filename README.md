# ProAssess — Backend

FastAPI backend with Agentic RAG pipeline (LangChain + Chroma + OpenAI GPT-4o).

---

## Prerequisites

- Python 3.12+
- Docker & Docker Compose
- OpenAI API key

---

## Quick Start

### 1. Clone and configure

```bash
cp .env.example .env
# Edit .env and set your OPENAI_API_KEY
```

### 2. Start all services

```bash
docker compose up -d
```

This starts PostgreSQL, Redis, Chroma, MinIO, and the FastAPI app.

### 3. Verify

```
GET http://localhost:8000/health
→ {"status": "ok", "env": "development"}

GET http://localhost:8000/docs
→ Swagger UI with all endpoints
```

---

## Local Development (without Docker)

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

# Start only the infrastructure
docker compose up postgres redis chroma minio -d

uvicorn main:app --reload
```

---

## Project Structure

```
backend/
├── main.py                  # FastAPI app, lifespan, routers, health check
├── config.py                # Pydantic Settings (reads .env)
├── database.py              # Async SQLAlchemy engine + session dependency
│
├── models/
│   ├── user.py              # User, Organisation, Department, SecurityGroup
│   ├── assessment.py        # Assessment, Question, StaffAssessment, StaffAnswer
│   └── knowledge.py         # KnowledgeSource, DocumentChunk, AuditLog
│
├── schemas/
│   └── __init__.py          # All Pydantic request/response models
│
├── api/
│   ├── auth.py              # POST /auth/login, /auth/refresh, GET /auth/me
│   ├── assessments.py       # Full assessment lifecycle + staff submission
│   ├── knowledge.py         # Document upload, URL indexing, re-index
│   └── admin.py             # Org stats, audit log, completion charts
│
├── rag/
│   ├── __init__.py          # Pipeline orchestrator
│   ├── indexer.py           # Stage 1: load → chunk → embed → Chroma
│   ├── retriever.py         # Stage 2: query expansion → dense+BM25 → RRF → rerank
│   ├── augmentor.py         # Stage 3: GPT question generation (MCQ + written)
│   └── evaluator.py         # Stage 4: deterministic MCQ scoring + GPT written eval
│
└── services/
    ├── auth_service.py      # JWT creation/validation, role guards
    └── assessment_service.py # Business logic: create, deploy, cancel, submit
```

---

## RAG Pipeline

```
LM creates assessment
        │
        ▼
[Retriever] Query expansion (GPT)
        │   Dense search (Chroma + OpenAI embeddings)
        │   BM25 keyword search
        │   Reciprocal Rank Fusion
        │   Cross-encoder re-ranking
        ▼
[Augmentor] Build context block → GPT-4o prompt
        │   JSON schema validation
        │   Retry on malformed output (up to 3x)
        ▼
[Questions stored in DB] — assessment ready to deploy
        │
        ▼
Staff submits answers
        │
        ▼
[Evaluator] MCQ: deterministic
            Written: GPT-4o rubric scoring (0-100)
        ▼
Feedback + citations returned to staff
```

---

## API Reference (key endpoints)

| Method | Path | Role | Description |
|---|---|---|---|
| POST | `/api/v1/auth/login` | Any | JWT login |
| GET | `/api/v1/auth/me` | Any | Current user profile |
| POST | `/api/v1/assessments/` | LM+ | Create draft + trigger RAG |
| POST | `/api/v1/assessments/{id}/deploy` | LM+ | Deploy to targets |
| POST | `/api/v1/assessments/{id}/cancel` | LM+ | Cancel deployed |
| DELETE | `/api/v1/assessments/{id}` | LM+ | Delete draft only |
| GET | `/api/v1/assessments/available` | Staff | List available to take |
| POST | `/api/v1/assessments/{id}/start` | Staff | Begin session + get questions |
| POST | `/api/v1/assessments/submit` | Staff | Submit + get instant feedback |
| GET | `/api/v1/assessments/{id}/feedback` | Staff | Retrieve past feedback |
| POST | `/api/v1/knowledge/upload` | HR | Upload PDF/DOCX/XLSX |
| POST | `/api/v1/knowledge/url` | HR | Index external URL |
| POST | `/api/v1/knowledge/{id}/reindex` | HR | Re-index a source |
| GET | `/api/v1/admin/stats` | HR | Org-wide statistics |
| GET | `/api/v1/admin/audit-log` | HR | Audit trail |

Full interactive docs at `http://localhost:8000/docs` (dev only).

> **Roles:** `Any` = unauthenticated or any role · `Staff` = staff member · `LM` = line manager (includes Staff) · `HR` = HR admin (full access)

---

## Environment Variables

See `.env.example` for all variables. Required before first run:

```env
SECRET_KEY=<random 64-char string>
OPENAI_API_KEY=sk-...
```

---

## Database Migrations

```bash
# Generate a new migration after model changes
alembic revision --autogenerate -m "add_new_field"

# Apply migrations
alembic upgrade head
```

---

## Running Tests

> **Note:** A `tests/` directory does not exist yet. Create it and add your test files before running the command below.

```bash
pip install pytest pytest-asyncio httpx
pytest tests/ -v
```
