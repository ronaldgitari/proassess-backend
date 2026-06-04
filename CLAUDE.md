# ProAssess — Claude Session Handoff

## Project Locations
- **Backend:** `C:\Users\thedroid\Desktop\files\`
- **Frontend:** `C:\Users\thedroid\Desktop\proassess-frontend\`
- **GitHub repo:** https://github.com/ronaldgitari/proassess-backend

---

## What This Project Is
ProAssess is a staff proficiency assessment platform:
- Line Managers create assessments → RAG pipeline (GPT-4o) auto-generates questions
- Staff take timed assessments → instant MCQ scoring + GPT written evaluation
- HR manages knowledge base (PDF/DOCX/XLSX upload, URL indexing), views stats + audit log
- Full JWT auth with 4 roles: `staff`, `lm`, `hr_admin`, `system_admin`

---

## How To Start Everything
```powershell
# 1. Start all backend services (run from project root)
cd C:\Users\thedroid\Desktop\files
docker compose up -d

# 2. Start frontend (separate terminal)
cd C:\Users\thedroid\Desktop\proassess-frontend
npm run dev
```
- Backend: http://localhost:8000 (health: /health)
- Frontend: http://localhost:3000
- **IMPORTANT:** `docker compose up` must be run from `C:\Users\thedroid\Desktop\files\` not a subdirectory
- **⚠️ `.env` changes need `--force-recreate`, NOT `restart`:** `docker compose restart api` reuses the container's baked-in env vars and will **not** pick up a changed `.env`. After editing `.env` (e.g. the OpenAI key), run `docker compose up -d --force-recreate api` (or `down` + `up -d`). Verify with `docker compose exec api printenv OPENAI_API_KEY`. This caused a long debugging session where a corrected key never loaded.

---

## Architecture
- **Backend:** FastAPI + SQLAlchemy (async) + PostgreSQL + Chroma (vector DB) + Redis + MinIO
- **Frontend:** Next.js 16 (App Router) + TypeScript + Tailwind CSS v4
- All services run via Docker Compose

---

## Critical File Structure
The project was generated with files in the wrong places. These fixes were applied:

### Models (`models/` package)
- `models/user.py` — User, Organisation, Department, UserDepartment, SecurityGroup, GroupMembership
  - `User.start_date` (Date) + `User.force_password_change` (bool) — HR user management. **Existing DBs need:** `ALTER TABLE users ADD COLUMN IF NOT EXISTS start_date date; ALTER TABLE users ADD COLUMN IF NOT EXISTS force_password_change boolean NOT NULL DEFAULT false;` (migration `0005`)
- `models/assessment.py` — Assessment, Question, StaffAssessment, StaffAnswer, AssessmentTarget + all enums
  - `Assessment.is_archived` (bool) — soft-delete flag; hidden from LM `/my` + staff `/available`, but completed results/feedback preserved. **Existing DBs need:** `ALTER TABLE assessments ADD COLUMN IF NOT EXISTS is_archived boolean NOT NULL DEFAULT false;` (migration `0004`)
  - `QuestionType` enum includes `PERSONALITY` and `CODING` (DB enum labels uppercase `PERSONALITY`/`CODING`). **Existing DBs:** `ALTER TYPE questiontype ADD VALUE IF NOT EXISTS 'CODING';` (migration `0006`)
  - For personality questions, `Question.retrieved_chunk_ids` (JSONB) holds `{dimension, direction}` scoring tags; for coding it holds `{language}`
- `models/knowledge.py` — KnowledgeSource, DocumentChunk, AuditLog
  - **Important:** column is `chunk_metadata` (not `metadata` — reserved word in SQLAlchemy)
- `models/system.py` — PipelineRun, PipelineStep, **PipelineSpan** (system-process observability). String status columns (no native enum).
  - `PipelineSpan` = real per-service call (service / operation / phase / status / `duration_ms` / timestamps) — powers the Log Capsule v2 (real spans, not inferred). `PipelineRun` gained `origin_ip` / `server_ip` / `system_id` (capsule metadata).
  - **⚠️ `create_all` only creates missing TABLES, never adds columns to existing ones.** The new `pipeline_spans` table auto-creates, but existing DBs need the run columns added manually: `ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS origin_ip varchar(64); ADD COLUMN IF NOT EXISTS server_ip varchar(64); ADD COLUMN IF NOT EXISTS system_id varchar(128);` — without this, **every `/ops` endpoint 500s** (selects non-existent columns) → "failed to fetch". Migration `0008` covers fresh DBs.

### Schemas (`schemas/__init__.py`)
All Pydantic request/response models. Key ones:
- `_UTCDatetimeMixin` — base that serializes naive-UTC datetimes with a `Z` suffix (`field_serializer("*", mode="wrap")` — must be `wrap` so nested models still recurse). Applied to `UserOut`/`AssessmentOut`/`AssessmentFeedbackOut`. See `timeutil.iso_utc()` for the manual-serializer equivalent
- `UserOut` — includes both `name` and `full_name` (full_name populated via model_validator from name)
- `AssessmentCreateRequest` — optional `source_id: UUID` (KB scoping) + `language` (coding); `model_validator` caps `num_questions` at 60 for personality, 30 otherwise. Coding/personality send `information_source="ai"` (no KB). `language` stored in `assessment.rag_metadata`; `/{id}/start` returns it so the take page sets the editor language
- `AssessmentCancelRequest` — only `reason: Optional[str]` (assessment_id is in URL path, NOT body)
- `AssessmentShareRequest` — `target_type: TargetType` + `target_ids: List[UUID]` (post-deployment sharing)
- `StaffProfileOut` — enriched profile with department, job_title, line_manager
- `AssessmentFeedbackOut` — score fields optional; adds `is_personality: bool` + `personality_result: PersonalityResultOut`
- `PersonalityResultOut` / `PersonalityDimensionOut` — type code, name, identity, per-dimension trait breakdown

### RAG (`rag/` package)
- `rag/indexer.py` — document loading + Chroma indexing (uses `chunk_metadata=` not `metadata=`)
- `rag/retriever.py` — full pipeline: expand_query → dense_search → BM25 → RRF → cross_encode_rerank
  - `dense_search()` accepts `source_id` param (filters Chroma to specific document)
  - Falls back to org-only filter if source/domain filter returns 0 results
  - Cross-encoder loads lazily; returns `None` and falls back to truncation if model unavailable
- `rag/augmentor.py` — GPT-4o question generation (MCQ, written, industry, **personality**, **coding**); all batch in groups of `BATCH_SIZE=10`. Personality uses temp 0.9 + prior-statement avoid-list + de-dup, generated in waves with an `asyncio.Semaphore(MAX_CONCURRENT_BATCHES)` cap — **currently `MAX_CONCURRENT_BATCHES=1` (sequential)**: most stable, best de-dup. Raise it (e.g. 3) to parallelise if the OpenAI key's rate-limit tier allows. MCQ uses `spread_correct_answers()` to even out A/B/C/D. Coding (`generate_coding_questions`) produces problem + reference solution per `language`. `PERSONALITY_TYPE_DESCRIPTIONS` (in `rag/evaluator.py`) supplies a one-line character blurb per 16-type, returned as `description` in the result
  - **Enriched explanations (MCQ + written):** question/correct-answer stay strictly grounded in retrieved context; explanation then appends a brief supplementary note from well-established common knowledge (industry standards, best practices, official docs). Guardrails: relevant + factual, no speculation/fabricated figures/dates/citations, must not contradict context, omitted if nothing truthful to add. Applies to KB & AI sources (industry/personality generators unchanged)
  - **⚠️ "Industry Standard" source REMOVED from the create UI** — `generate_industry_questions` + `FRAMEWORKS` are still in code (and the `industry` enum value kept) so old assessments load, but the option is gone from `/lm/assessments/new`. Why it was poor: no retrieval/grounding — it pasted a **hardcoded list of 3–4 framework NAMES** into the prompt (GPT free-associated → incoherent mashups), AND `FRAMEWORKS.get(domain.lower())` keyed on `assessment_type` (`technical`/`professional`) which never matched the `leadership`/`communication` keys → the default soft-skills set ALWAYS won (those branches were dead code). **Replacement:** use **AI Generated** + name desired frameworks in the **Context Prompt** (e.g. "based on ISO 27001"). The create page shows this tip when AI is selected.
- `rag/evaluator.py` — MCQ scoring (deterministic) + written scoring (GPT-4o) + `compute_personality_result()`; defines `PERSONALITY_DIMENSIONS`, `LIKERT_SCALE`, `PERSONALITY_TYPE_NAMES`
- `rag/__init__.py` — `generate_questions_for_assessment()` orchestrator; personality branch (no retrieval) runs first; reads `source_id` from `assessment.rag_metadata`

### Services (`services/` package)
- `services/auth_service.py` — JWT helpers, get_current_user, role guards (`require_staff/lm/hr/system_admin`); `get_user_from_token()` for SSE query-param auth
- `services/pipeline_tracker.py` — records system-process phases AND real per-service spans. Each write opens its OWN short-lived `AsyncSessionLocal` + commits immediately (so polling/SSE see live progress). Non-fatal (logs + swallows). Phase API: `create_run`, `start_step`, `finish_step`, `finish_run`, `track_step()`. **Span API (capsule v2):** a `contextvars.ContextVar` (`_current_run`, set via `set_current_run()` at each pipeline start) propagates the run id across `await`/`asyncio.gather` so deep client calls record spans **without run_id plumbing**. `track_span(service, operation, phase=, detail=)` wraps a real call (records start→end + true `duration_ms`, ok/error). `set_origin_ip()` + `capture_server_meta()` record capsule metadata (origin IP threaded from the route → background task; server IP/hostname via `socket`)
- `services/assessment_service.py` — assessment lifecycle + submission evaluation
  - `_get_questions_safe()` uses `db.expunge()` + `make_transient()` before nulling answer fields (prevents committing NULLs to DB)
  - `submit_assessment()` queries StaffAnswer explicitly (avoids async lazy-load crash)

### API (`api/__init__.py`)
Re-exports routers from root-level `auth.py`, `assessments.py`, `knowledge.py`, `admin.py`, `ops.py`, `users.py`

---

## All API Endpoints

### Auth (`/api/v1/auth`)
| Method | Path | Description |
|---|---|---|
| POST | `/login` | Email/password → JWT token pair |
| POST | `/refresh` | Refresh access token |
| GET | `/me` | Current user (UserOut — includes `force_password_change`) |
| GET | `/profile` | Enriched profile with dept + line manager (StaffProfileOut) |
| POST | `/change-password` | Change own password; verifies current, clears `force_password_change` |

### Users — HR management (`/api/v1/users`) — hr_admin / system_admin
| Method | Path | Description |
|---|---|---|
| GET | `/` | List org users with dept, job title, line manager, start date, role, status |
| POST | `/` | Create user (sets `force_password_change=True`); dup-email guard; only system_admin can grant system_admin |
| PATCH | `/{id}` | Update name/role/is_active/department/job_title/line_manager/start_date; self-lockout guards |
| POST | `/{id}/reset-password` | Generate temp password (returned once), force change at next login |
| GET | `/departments` | List org departments |
| POST | `/departments` | Create department |
- **Security:** no hard delete (deactivate/reactivate preserves history); temp-password reset + forced change; role-escalation guard; can't deactivate/role-change your own account. Audits `CREATE_USER`, `UPDATE_USER`, `RESET_PASSWORD`, `CREATE_DEPARTMENT`
- **Cross-org validation:** `_validate_org_refs()` rejects `department_id`/`line_manager_id` not in the caller's org (create + update) → clean 400, prevents cross-tenant references
- **Perf:** `GET /users/` uses a single left-joined query (no per-user N+1); `GET /assessments/staff/my-results` batch-loads all personality answers in one query (no per-row N+1)

### Assessments (`/api/v1/assessments`)
| Method | Path | Role | Description |
|---|---|---|---|
| POST | `/` | LM | Create draft + trigger RAG generation |
| GET | `/my` | LM | List LM's own assessments |
| POST | `/{id}/deploy` | LM | Deploy draft to staff |
| POST | `/{id}/cancel` | LM | Cancel deployed/active assessment |
| DELETE | `/{id}` | LM | Delete draft/cancelled assessment. **Archives** (soft-delete, `is_archived=True`) if it has completed attempts — preserving questions + results/feedback; purges incomplete attempts. Hard-deletes only if nobody completed it |
| GET | `/{id}/generation-status` | LM | Poll question generation progress |
| GET | `/{id}/questions` | LM/HR | **Pre-deploy preview** — full generated questions WITH answers/rubric (+ case + web_sources). Creator or HR (`_get_assessment_owned`) |
| POST | `/{id}/regenerate` | LM/HR | Discard current questions + re-run generation (draft only); resets `created_at` timeout baseline; audits `REGENERATE_ASSESSMENT` |
| GET | `/targets/departments` | LM | Org departments — for the create-page audience picker |
| GET | `/targets/users` | LM | Active staff in org — for the create-page audience picker |
| GET | `/{id}/share-candidates?target_type=` | LM/HR | Departments or staff NOT yet targeted (creator or HR via `_get_assessment_owned`) |
| POST | `/{id}/share` | LM/HR | Add new department/individual targets to a deployed assessment (creator or HR); skips dupes; audits `SHARE_ASSESSMENT` |
| GET | `/available` | Staff | List deployed assessments for this org |
| GET | `/staff/my-results` | Staff | Last N evaluated assessments; includes `is_personality` + `personality_type` for personality assessments |
| POST | `/{id}/start` | Staff | Begin assessment session |
| POST | `/submit` | Staff | Submit answers + get scored feedback |
| GET | `/{staff_assessment_id}/feedback` | Staff | Retrieve past feedback |
| GET | `/lm/team-averages` | LM | Avg score by assessment over the LM's **direct reports** (staff whose `line_manager_id` = caller). Mirrors `/admin/assessment-averages` scoped to the team |
| GET | `/lm/team-averages/{id}/scores` | LM | Per-staff scores for one assessment, limited to the LM's reports |

**Note:** `require_staff` already includes LM/HR/sysadmin, so LMs can take assessments + use the staff endpoints (`/available`, `/staff/my-results`, `/staff/my-skills`, `/start`, `/submit`). `/targets/users` now also returns **line managers** (not just `STAFF`), so an LM can be assigned an assessment as an individual.

### Knowledge (`/api/v1/knowledge`)
| Method | Path | Description |
|---|---|---|
| GET | `/` | List indexed sources for org |
| POST | `/upload` | Upload PDF/DOCX/XLSX → background indexing |
| POST | `/url` | Index external URL |
| POST | `/{id}/reindex` | Re-index a source |
| DELETE | `/{id}` | Soft-delete source |

### Admin (`/api/v1/admin`)
- `GET /stats` — org-level stats
- `GET /audit-log?skip=&limit=` — paginated audit log. Joins `User` for `user_name`; returns `detail` (JSONB) + extracted top-level `reason`; ordered by `timestamp DESC, id DESC` (stable tiebreaker so offset pages don't overlap)
- `GET /completion-by-department` — avg score per department
- `GET /assessment-averages?limit=` — avg score per scored assessment (excludes personality), newest first: `{assessment_id, name, created_at, avg_score, attempts}`. Auto-polled by the dashboard
- `GET /assessment-averages/{id}/scores` — per-staff scores for one assessment, highest→lowest: `{name, created_at, scores:[{staff_name, score_pct, submitted_at}]}`
- `GET /staff/{id}/profile` — full staff profile (HR/admin OR the staff's line manager): profile details + personality type/summary, scored results, strengths/developing/weaknesses skill bands
- `GET /staff-assessment/{id}/feedback` — admin-scoped per-question feedback for one completed scored attempt (HR/admin OR the assessed user's line manager). Mirrors the staff feedback shape but bypasses the staff-only ownership check. Powers the expandable result rows on the staff-profile page

### Ops / System Processes (`/api/v1/ops`) — `system.view` capability
**Gated on the `system.view` permission** (was role `system_admin`; changed so an HR_ADMIN/other user added to the **Ops** group — which grants `system.view` — can reach it). All `/ops` endpoints use `require_permission("system.view")`; the SSE `/stream` checks `has_permission(user, "system.view")`; the frontend `/ops` page guards on `can("system.view")` and the nav tab already did — all three now agree. `system_admin` still qualifies via its role-default `system.view`.
| Method | Path | Description |
|---|---|---|
| GET | `/runs?limit=&kind=` | Recent pipeline runs with per-run step-status counts |
| GET | `/runs/{id}` | Single run + ordered steps |
| GET | `/runs/{id}/capsule` | **Log capsule** — transaction trace: metadata (services, origin/server IP, system id, start + last-action timestamps) + the run's logs grouped by backing service |
| GET | `/runs/{id}/stream?token=` | **SSE live-tail** — server polls DB each 1s, pushes run+steps on change until finished. Auth via `token` query param (EventSource can't send headers) |

**Audited actions** (written to `audit_log` table with `detail` JSONB):
- `CREATE_ASSESSMENT`, `DEPLOY_ASSESSMENT`, `SUBMIT_ASSESSMENT`
- `CANCEL_ASSESSMENT` — `detail.reason` = cancellation reason
- `DELETE_ASSESSMENT` — `detail` = `{name, prior_status, reason, archived, completed_preserved}`; `archived=True` when completed attempts existed (soft-deleted instead of purged). `audit_log.resource_id` has no FK so it survives a hard delete
- `SHARE_ASSESSMENT` — `detail` = `{target_type, count}`; post-deployment sharing of a deployed assessment to additional departments/individuals
- `REVIEW_ASSESSMENT` — `detail` = `{final_score, note, adjustments}`; LM confirmation of a scenario (case-study) submission's AI-drafted score (human-assisted verification). See `docs/CASE_STUDY_FEATURE.md`
- `REGENERATE_ASSESSMENT` — LM discarded a draft's generated questions and re-ran generation (pre-deploy preview/approval flow)

---

## Key Bug Fixes Applied

### Backend
1. **`metadata` → `chunk_metadata`** in `models/knowledge.py` AND `rag/indexer.py` (line 248: `DocumentChunk(chunk_metadata=...)`)
2. **`_get_questions_safe` corruption** — was mutating ORM objects and committing `correct_answer_index=NULL` to DB; fixed with `db.expunge()` + `make_transient()` before nulling fields
3. **`sa.answers` lazy-load crash** — `submit_assessment` was iterating `sa.answers` triggering `MissingGreenlet`; fixed with explicit `select(StaffAnswer).where(...)` query
4. **Assessment delete cascade** — replaced `db.delete(assessment)` with explicit SQL DELETEs in order: StaffAnswers → StaffAssessments → Questions → AssessmentTargets → Assessment
5. **`AssessmentCancelRequest` had `assessment_id: UUID`** — removed (ID is already in URL path); frontend was sending only `{reason}` causing 422
6. **Cross-encoder model download** — wrapped `CrossEncoder()` in try/except; falls back to top-K truncation if HuggingFace model unavailable
7. **Domain tag mismatch** — docs indexed with `domain_tag="general"` but retriever was filtering by assessment_type; added fallback to org-only search if 0 results
8. **`OPENAI_MAX_TOKENS=2048`** — raised to `8192` in both `.env` and `config.py`; GPT-4o supports 16K output tokens; 2048 caused JSON truncation for >10 questions
9. **Chroma volume mount** — was `/chroma/.chroma` (wrong); corrected to `/chroma/chroma` in `docker-compose.yml`; embeddings now persist across container restarts
10. **`source_id` in `AssessmentCancelRequest`** — was erroneously required; removed
11. **`UserOut.full_name`** — backend sends `name` but frontend expected `full_name`; added model_validator to populate `full_name` from `name`
12. **passlib dropped → direct `bcrypt`** — `services/auth_service.py` now hashes/verifies with the `bcrypt` library directly (`hash_password`/`verify_password`, truncating to 72 bytes — the bcrypt limit). passlib was removed (unmaintained; it imported the deprecated stdlib `crypt`, removed in Python 3.13, which raised a `DeprecationWarning`). Output is standard `$2b$` bcrypt, so **passwords hashed by the old passlib path still verify** (confirmed: existing logins work). `bcrypt==4.0.1` stays pinned; `passlib[bcrypt]` removed from requirements.
13. **Chroma filter format** — ChromaDB 0.5.x requires `{"field": {"$eq": "value"}}`; fixed in `dense_search()`

14. **Question generation timeout for >10 questions** — single GPT call for all N questions truncated/timed out. Fixed by batching in groups of `BATCH_SIZE=10` (`rag/augmentor.py`), adding `request_timeout=120` per call, and extending the generation-status fail window from 300s → 600s
15. **Personality enum value** — SQLAlchemy binds enums by MEMBER NAME, so DB labels are uppercase (`MCQ`, `WRITTEN`). Adding personality required `ALTER TYPE questiontype ADD VALUE 'PERSONALITY'` (uppercase). See migration `0002`. **One-time manual step on existing DBs** (see Personality section)
16. **Personality question repetition** — independent batches at temp 0.2 produced duplicate statements. Fixed with temp 0.9, feeding prior statements into each batch's "do not repeat" instruction, cross-batch de-dup (normalized), and backfill rounds
17. **Timezone mismatch** — DB stores naive UTC (`datetime.utcnow()`); `.isoformat()` emits NO tz suffix, so the browser's `new Date()` reads it as LOCAL → every timestamp shifted by the viewer's offset. Fixed with `timeutil.iso_utc()` (adds `Z`) on all manual serializers (`ops.py`, `admin.py`, `assessments.py`) + a `_UTCDatetimeMixin` (Pydantic `field_serializer`) on `UserOut`/`AssessmentOut`/`AssessmentFeedbackOut`. Frontend `new Date().toLocaleString()` then auto-converts UTC → viewer local
18. **⚠️ Submit rollback regression (from #17)** — the first cut of `_UTCDatetimeMixin` used `field_serializer("*")` in PLAIN mode, which returned non-datetime fields raw → nested models (`personality_result`, `answers`) serialized wrong → `/submit` 500'd → `get_db` rolled back the txn → personality/scored results silently never saved (no dashboard result). Fixed with `mode="wrap"`: only datetimes transformed, everything else delegates to Pydantic's `handler` (recursion preserved). **Lesson:** a wildcard plain field_serializer must call `handler(v)` for non-target values, or use `mode="wrap"`

### Frontend
1. **Assessment delete** allowed only for DRAFT; extended to include CANCELLED status
2. **`correct_answer_index` display** — feedback page uses `options[correct_answer_index]` for MCQ display
3. **KB document validation** — fired for personality assessments (which default `information_source="kb"`); guarded with `!isPersonality`
4. **Audit log duplicate keys on "Load more"** — timestamp-only ordering overlapped offset pages; fixed with `timestamp DESC, id DESC` ordering (backend) + dedup-by-id when appending (frontend)
5. **Logo `?v=` cache-bust broke** — Next 16 rejects query strings on local `next/image` src unless configured in `images.localPatterns`; removed the query string

---

## Test Credentials (all use `Password123!`)
| Email | Role |
|---|---|
| hr@acme.com | HR Admin |
| lm.eng@acme.com | Line Manager |
| lm.sales@acme.com | Line Manager |
| staff1@acme.com | Staff |
| staff2-4@acme.com | Staff |

**Note:** `user_departments` table may be empty for test users — department/job title/line manager will show `—` on the staff dashboard until the table is seeded.

---

## Enum Values (critical — must match backend exactly)
```
AssessmentType:       "technical" | "professional"
QuestionType:         "mcq" | "written" | "personality" | "coding" | "scenario"   # scenario = KB-grounded case study (migration 0009)
InformationSource:    "kb" | "ai" | "industry" | "url" | "hybrid"   # hybrid = KB doc + credible web case-study sources (migration 0010); "industry" kept for old assessments but REMOVED from create UI
TargetType:           "organisation" | "department" | "individuals"
AssessmentStatus:     "draft" | "deployed" | "active" | "completed" | "cancelled"
UserRole:             "staff" | "lm" | "hr_admin" | "system_admin"
SourceStatus:         "pending" | "indexing" | "active" | "failed" | "stale"
StaffAssessmentStatus:"not_started" | "in_progress" | "submitted" | "pending_review" | "evaluated"   # pending_review = scenario awaiting LM confirmation (migration 0009)
```

---

## Frontend Pages
| Route | Role | Description |
|---|---|---|
| `/login` | All | JWT login form |
| `/dashboard` | All | Role-based redirect |
| `/staff/assessments` | Staff | **Full dashboard**: user profile card, pending assessments, recent results (top 5) |
| `/staff/assessments/[id]/take` | Staff | Timed assessment — MCQ radio / written textarea / **7-point Likert (personality)** / **Monaco code editor (coding)** / **two-pane sticky case + written analysis (scenario/case study)**. Scenario submit → "Submitted for review" screen (no score; awaits LM) |
| `/staff/assessments/[id]/feedback` | Staff | Score + per-question breakdown (code shown in `<pre>` + collapsible reference solution for coding), OR **personality profile** when `is_personality`, OR **case panel + per-answer feedback/score** when scenario (reviewer-confirmed) |
| `/lm/assessments` | LM | List own assessments with status badges |
| `/lm/assessments/new` | LM | Create assessment — KB document picker; formats by type: **Technical** → MCQ/Written/Coding(+language)/**Case Study**; **Professional** → MCQ/Written/Personality/**Case Study**. num-questions 5–30 (personality 60; **case study 5–8, KB-grounded only**); audience checkbox picker |
| `/lm/reviews`, `/lm/reviews/[id]` | LM/HR | **Case-study review queue** (human-assisted verification): pending submissions → detail with case, candidate answers, collapsible rubric, **editable score + AI feedback**, cited sources, **Approve & release** (recomputes final score, sets EVALUATED). Nav "Reviews" tab. See `docs/CASE_STUDY_FEATURE.md` |
| `/lm/team-results` | LM | **Team Results** — average score by assessment for the LM's direct reports (bar chart + expandable per-staff scores, names link to `/hr/staff/[id]`). Auto-polls 6s. Nav "Team Results" tab |
| `/staff/assessments`, `/staff/results` | LM (+ Staff) | LMs are assessed too — the staff dashboard + results pages are reachable for LMs via the nav **"My Assessments"** / **"My Results"** tabs (the staff pages accept any logged-in user) |
| `/lm/assessments/[id]` | LM | Deploy/Cancel/Delete + live RAG generation log (terminal window) with a **latency gauge** (uniform rectangular segment bar; green→amber→red as `elapsed` approaches `maxWindow = max(600, num_questions×15)`). **When ready (draft): question PREVIEW for approval** (questions + answers/rubric, case + web sources) with **"Approve & Deploy"** + **"Regenerate Questions"** (`components/question-preview.tsx`). **Deployed:** "Share with more people" panel (Individuals/Departments checkboxes, excludes already-targeted, avocado-green) |
| `/hr` | HR | Animated stat tiles (count-up; **avg_score_pct tile removed** — covered by the per-assessment list) + **Average Score by Assessment** (top 3, colour-coded, auto-polls 6s, "See more/View all →") + audit log preview (5 most recent) |
| `/hr/assessment-averages` | HR | Full per-assessment averages — name, date created, colour-coded avg bar; each row expands to staff scores (highest→lowest) with **Title + Department columns** and **names hyperlinked** to the staff profile. Auto-polls 6s |
| `/hr/staff/[id]` | HR / LM | Staff profile (HR/admin OR the staff's line manager) — profile card (mirrors staff dashboard incl. personality + character summary), **strengths/developing/weaknesses** skill tiles (dark-mode-aware), and **Assessment Results rows that expand to reveal per-question feedback** (admin feedback endpoint) |
| `/hr/audit` | HR | Full audit log — load-more pagination (25/page), action badges, cancellation/deletion reasons |
| `/hr/knowledge` | HR | Indexed sources list (status/chunks/date) + upload + URL indexing |
| `/hr/users` | HR | **User management** — table with inline edit (role/dept/line manager/start date), activate/deactivate, reset-password (shows temp pw once), add-user form, quick department creator. Nav "Users" tab |
| `/change-password` | All | Forced (or voluntary) password change; nav-level guard redirects here while `force_password_change` is set |
| `/ops` | system_admin | **System Processes dashboard** — run list + phased checklist live-tailed over SSE (green=ok, yellow=warn, red=error, blue=running, empty=pending) |

---

## Frontend Components & Lib
| File | Description |
|---|---|
| `components/nav.tsx` | Sticky backdrop-blur nav; Atocado logo + ProAssess wordmark; **active tab highlighted** (indigo fill pill via `usePathname`); hover on all tabs. **Logo click = role default landing (`homeHref`):** staff + lm → `/staff/assessments` (their assessments dashboard), hr/sysadmin → `/hr` (Stats). The assessments page is reached via the LOGO, so that tab is intentionally **removed** (staff has only **Results**; lm has My Results, not My Assessments). HR/sysadmin tabs: **Stats**, System Processes (sysadmin), Manage Assessments, Knowledge Base, Users. **LM (role `lm`) tabs:** Manage Assessments, Reviews, **Team Results**, **My Results**. Profile avatar (right). `/hr` exact-match. **Guard:** redirects to `/change-password` while `user.force_password_change` |
| `lib/auth-context.tsx` (note) | exposes `refresh()` to re-pull `/auth/me` after password change |
| `components/logo.tsx` | Atocado company logo — `next/image` of `/public/atocado.png`; `size` prop (nav 32px, login 87px). Plain `src` (no query string — Next 16 `localPatterns` rejects it) |
| `components/profile-panel.tsx` | Profile settings dropdown: photo upload, light/dark/system theme, 14/16/18px font size, sign out. **Photo stored per-user** under `profile_photo_<userId>` (localStorage base64) via `photoKey(userId)` helper — `Avatar` reads the logged-in user's key (was a single global key shared across accounts on a browser) |
| `components/spinner.tsx` | Loading spinner |
| `components/code-editor.tsx` | Monaco editor (`@monaco-editor/react`, loads core from CDN) for coding assessments. Theme toggle: Dark (vs-dark) / Solarized / Dimidium (custom `defineTheme`); maps language name → Monaco id. **Requires `npm install @monaco-editor/react`** |
| `components/cube-loader.tsx` | Orbiting-squares loader (28px, flat light-blue squares that interchange corners). Accepts `progress` (0–1) prop to sync motion with generation; falls back to continuous spin without it |
| `lib/auth-context.tsx` | AuthProvider — user state, login, logout |
| `lib/preferences-context.tsx` | PreferencesProvider — theme + font size stored in localStorage; applies `dark` class and `data-font` attribute to `<html>` |
| `lib/api.ts` | All API calls; `auth`, `assessments`, `knowledge`, `admin`, `staff`, `ops` namespaces. `AuditEntry`, `PipelineRun`, `PipelineStep`, `AssessmentAverage`, `StaffScore`, `LogCapsule`/`CapsuleService` types. `admin.assessmentAverages()` / `assessmentScores(id)`; `ops.streamUrl(id)` builds the SSE URL with `?token=`; `ops.getCapsule(id)` |
| `app/ops/page.tsx` | System Processes dashboard — run list (polls 4s) + **dark terminal log** detail view via `EventSource` SSE (`buildLog()`); falls back to one-shot `getRun` on stream end/error. Running ("live") runs sorted to top; live window `order`: top when stacked, right on desktop. **Selection:** no completed run auto-selected (placeholder when idle); a running run auto-selected so new processes show live; manual selection sticks. **Log Capsule** link in terminal title bar (generation/evaluation) toggles the grouped-by-service capsule |

---

## UI / Styling Conventions
- **Branding:** Atocado logo at `/public/atocado.png` (≈2.4 MB; Next.js optimizes on render). Shown on login (87px, centered) and in nav (32px, left of wordmark). If the image looks stale after replacing it, clear `.next/cache/images` + hard-refresh (Next 16 rejects `?v=` cache-busting query strings via `localPatterns`)
- **Active nav tab:** indigo-600 fill pill + soft glow; inactive tabs hover to `indigo-50` / `slate-800`
- **List hover (avocado green) — applied to ALL lists:** LM My Assessments, Staff Pending, Staff Recent Results, HR Knowledge sources, HR Audit Log. Effect: card lifts (`-translate-y-0.5`) + `shadow-lime-900/10` + `lime-600` accent bar slides in from left + `lime-50` tint + title turns `lime-800`. Table rows (audit log) adapt: bg tint + left-edge `lime-500` accent (no lift)

---

## Staff Dashboard Details (`/staff/assessments`)
Three sections:
1. **User profile card** — avatar (photo or initials), full name, role pill, job title, department, line manager (from `GET /auth/profile`). Also shows **Personality Type** (e.g. `INTJ-A · Architect`) + a **Character Summary** blurb — both derived from the most recent personality result in `my-results` (`personality_type` / `personality_summary`); conditionally rendered only when non-null
2. **Pending Assessments** — available assessments not yet completed show "Start →" button; already-completed ones show "Completed" badge (cross-referenced with my-results)
3. **Recent Results** (top 5) — each card shows score badge (green=pass ≥70%, red=fail) OR a violet personality-type badge for personality assessments; date; expand arrow reveals detail + link to full feedback

---

## 16 Personalities (Personality Assessment) Feature
A `question_type="personality"` format under the **Professional** assessment type. Likert-scale, no right/wrong; output is an MBTI-style type code + identity (e.g. `INTJ-A`).

**Does NOT use RAG** — bypasses retrieval/Chroma/KB entirely; a direct GPT-4o call.

### Flow
```
Create (assessment_type=professional, question_type=personality)
  → generate_questions_for_assessment(): personality branch (before any retrieval)
      → generate_personality_questions(topic, n)   [rag/augmentor.py]
          → batches of 10, temp 0.9, prior statements fed forward to avoid repeats
          → cross-batch de-dup (normalized) + backfill rounds
      → Question rows: options=LIKERT_SCALE (7-point), retrieved_chunk_ids={dimension, direction}
        (scoring tags packed into the unused JSONB column — no schema migration needed)
  → Take page: 7-point agree/disagree Likert scale (red→grey→green circles)
  → Submit: compute_personality_result() aggregates Likert answers → type code + trait %
  → Feedback: personality profile card (type code, name, identity, per-dimension trait bars)
```

### Scoring (`rag/evaluator.py`)
- 5 dimensions: `mind` (E/I), `energy` (N/S), `nature` (T/F), `tactics` (J/P), `identity` (A/T)
- Likert answer_index 0..6 → centered −3..+3, × direction, summed per dimension
- Type code from first 4 dimensions + `-A`/`-T` identity suffix; `PERSONALITY_TYPE_NAMES` maps 16 codes → names; `PERSONALITY_TYPE_DESCRIPTIONS` gives a one-line character blurb (returned as `description`)
- Result recomputed on demand (submit, feedback GET, my-results) — nothing extra persisted

### Character summary (added)
- `my-results` returns `personality_summary` (the type description). The **staff dashboard profile card** renders it as a full-width italic "Character Summary" blurb beneath the detail grid (violet accent bar); the **feedback profile page** shows it under the type name. Both conditional on a completed personality result.

### Key specifics
- **Fixed at 60 questions** (12 per dimension) — UI disables the count input; backend validator caps personality at 60, others at 30 (`model_validator` in `AssessmentCreateRequest`)
- Creation page: format option only shows under Professional; hides KB picker + source picker; sends `information_source="ai"`, no `source_id` (AI-generated, no knowledge base)
- **REQUIRED one-time DB step on existing databases:**
  ```powershell
  docker compose exec postgres psql -U proassess -d proassess -c "ALTER TYPE questiontype ADD VALUE IF NOT EXISTS 'PERSONALITY';"
  ```
  (uppercase `PERSONALITY` — SQLAlchemy binds enums by member name; see migration `0002`)

---

## System Processes / Observability (ops dashboard)
Phased log of system processes for `system_admin`, persisted + live-tailed.

- **Tracked process kinds:** `generation` (RAG/personality), `indexing` (KB upload/URL → storage), `evaluation` (submission scoring)
- **Where instrumented:** `rag/__init__.py` (generation phases), `knowledge.py` `_index_document_background` (load + index), `services/assessment_service.py` `submit_assessment` (score + persist)
- **Data flow:** each phase writes a `PipelineStep` row via `pipeline_tracker` (own session, immediate commit) → `/ops/runs/{id}/stream` SSE polls the DB every 1s and pushes changes → the `/ops` page renders it
- **Detail view = dark terminal log** (replaced the old phased checklist). `buildLog()` turns each run's `PipelineStep`s into timestamped terminal lines (`· ✓ ✗ ⚠ ▌` icons, sky/green/red/amber colours) with a terminal title bar; live runs stream + auto-scroll, completed/failed replay their recorded log. The run-list rows are the clickable "individual logs"
- **Status semantics:** step `ok`=green ✓, `warn`=amber ⚠ (e.g. "0 chunks retrieved"), `error`=red ✗ (critical, stops the run), `running`=blinking cursor, `pending`=grey. Run status: `running`/`completed`/`failed`
- **Log Capsule (v2 — real spans):** `🔗 capsule:<id8>` link in the terminal title bar (generation + evaluation runs) toggles the capsule panel — metadata header (services, **real** origin/server IP, system id, start + last-action, total span count, **information source + reference document/URL** and web-source count, plus **assessment name** and — for evaluation runs — the **candidate** being scored; resolved via `_resolve_reference()`/`_assessment_provenance()` from the run's assessment/KB source: uploaded-doc name or custom URL) + the run's **`PipelineSpan` rows grouped by backing service**, each with its operation + true `duration_ms`; per-service header shows call count + total time. **Instrumented call sites** (via `track_span`): OpenAI (`_call_gpt`, `expand_query`, written/coding eval, embeddings), Chroma (`dense_search`, `index_source` add_documents), Postgres (question/eval/chunk flushes), app (cross-encoder rerank). `SERVICE_META` in `ops.py` maps service→label+colour. `GET /ops/runs/{id}/capsule` builds it from real spans. Note: runs created before the feature have no spans
- **Setup:** no `system_admin` in seed — promote one:
  ```powershell
  docker compose exec postgres psql -U proassess -d proassess -c "UPDATE users SET role='SYSTEM_ADMIN' WHERE email='staff4@acme.com';"
  ```
  (`SYSTEM_ADMIN` uppercase — SQLAlchemy binds enums by member name)
- **Phase 2 (not built):** true event-driven push (WebSocket/broker) to replace the 1s server-side DB poll inside the SSE generator. (Real per-service span instrumentation for the capsule is **done** — see Log Capsule v2.)

---

## RAG Pipeline Flow
```
Assessment created (status: DRAFT, rag_metadata: {source_id?: UUID})
  → background task: _generate_questions_background()
    → generate_questions_for_assessment(assessment, db)
      → [PERSONALITY] branch first — direct GPT, no retrieval (see Personality section)
      → reads source_id from assessment.rag_metadata
      → context_prompt is enriched with the assessment TITLE ("Assessment title: …" + LM prompt)
        and threaded into expand_query, grade_context, and the augmentors — so KB retrieval +
        grading judge coverage against the title + assessor intent, not the (often narrow) topic
        alone. Reduces false "insufficient context" failures on KB sources.
      → [HYBRID source] KB retrieve + gather_web_context() (Tavily, domain/industry case-study
        sources) → interleave KB+web docs → grade is NON-FATAL (warns + proceeds; web/model
        supplement the KB by design) → augment on the combined context. Web sources saved to
        rag_metadata.web_sources for provenance. Ops adds a "web" step + Web Search capsule spans.
        rag/web_research.py owns web_search (shared with feedback) + gather_web_context.
      → [KB source] retrieve(topic, domain, org_id, source_id)
          → expand_query()         — GPT rewrites topic into 4 sub-queries
          → dense_search()         — Chroma filter: source_id > domain_tag > org_id only
          → bm25_search()          — keyword search over dense candidates
          → reciprocal_rank_fusion()
          → cross_encode_rerank()  — falls back to truncation if model unavailable
      → [KB source] grade_context() reflection loop (gpt-4o-mini, temp 0.0):
          sufficient → augment · partial → refine + retrieve again (≤MAX_REGRADE, accumulate)
          insufficient → record on rag_metadata + raise InsufficientContext (honest fail)
      → [AI/Industry source] skips retrieval + grading
      → augmentor: GPT-4o generates questions in BATCHES of 10 (max_tokens=8192,
        request_timeout=120s per call) — avoids truncation/timeout for large counts
      → Question rows inserted via db.flush()
    → db.commit()
  → generation-status endpoint: ready=true when question_count >= num_questions_requested;
    failed=true if 0 questions after 600s (timeout) OR rag_metadata.generation_error is
    insufficient_context (grader stopped) — the latter returns error_kind + missing[]
```

### RAG classification & the lightweight grading loop (✅ BUILT)
**Base pipeline is Advanced / Classic RAG** (expand → dense → BM25 → RRF → cross-encoder → generate). On top of that, the **KB branch now has ONE grading + re-query reflection loop** — a lightweight step toward agentic RAG without full multi-hop/planning agents (deliberately avoided: too much cost/latency/non-determinism for a curated KB). This converts *confidently-wrong questions* (from sparse/off-topic retrieval) into either correctly-retrieved ones or an **honest "source doesn't cover the topic" failure** — worth more than average-case lift for an assessment tool, where a wrong question mis-measures a real person.

```
KB source → retrieve()
          → grade_context(topic, n, docs)         ← ONE cheap gpt-4o-mini call, temp=0.0
              ├─ "sufficient"   → augment (as before)
              ├─ "partial"      → reformulate (grader returns refined_query) → retrieve() again,
              │                    ACCUMULATE + dedupe docs; cap settings.MAX_REGRADE=2 re-queries
              └─ "insufficient" → STOP. record on rag_metadata, finish_run "failed",
                                   raise InsufficientContext; LM sees "source doesn't cover topic"
```

**How it's implemented:**
- **`rag/grader.py`** — `grade_context(topic, context_prompt, domain, num_questions, docs) -> {verdict, covered[], missing[], refined_query}` via **gpt-4o-mini** (`settings.OPENAI_GRADER_MODEL`) at **temp 0.0**. Single-object JSON parse (`extract_json_object`). **Fails OPEN** (grader call/parse error → "sufficient") so a grader hiccup never blocks otherwise-good generation; empty docs → "insufficient". Also defines **`InsufficientContext`** exception (carries `covered`/`missing`).
- **Loop** = `_grade_and_refine()` in `rag/__init__.py`, KB branch only, between the `retrieve` and `augment` `track_step`s. KB `steps` list gained `("grade", "Grade retrieved context (GPT-4o-mini)")`. Bounded by `settings.MAX_REGRADE` (=2); dedupes docs across re-queries by `page_content[:100]`.
- **Honest-failure path:** on insufficient (or exhausted budget / no refined_query), `_record_generation_error()` writes `{kind:"insufficient_context", missing, covered, grades, requeries}` to `assessment.rag_metadata` via its **own committed session** (survives the background task's rolled-back txn), then raises `InsufficientContext` → the `grade` step goes red (error) and `finish_run("failed")` carries the message.
- **`/generation-status`** now reads `rag_metadata.generation_error` and returns `failed:true` + `error_kind:"insufficient_context"` + `missing[]`/`covered[]` (distinct from the generic timeout `failed`).
- **Observability:** the grader call is wrapped in `pt.track_span("openai", "chat.completion · grade (…)", phase="grade")` → the decision shows in the **ops terminal log** (the `grade` PipelineStep) AND the **Log Capsule** (an OpenAI span, phase `grade`). The capsule is what makes the loop *safe to watch* — no black box.
- **Frontend** (`/lm/assessments/[id]`): a distinct **"Source doesn't cover this topic"** state (separate from generic `failed`) shows the grader's `missing[]` and prompts to pick another document or use AI Generated. `generationStatus` type in `lib/api.ts` gained `error_kind`/`missing`/`covered`.
- **Cost/latency:** best case +1 mini call (~1–2s, sub-cent); worst case +3 grades +2 re-retrievals (~+10–20s, a few cents). Only adds work when retrieval was weak.

---

## Theme & Font Size System
- **Theme:** `PreferencesProvider` sets `dark` class on `<html>`; `globals.css` has `.dark` overrides for key Tailwind classes
- **Font size:** `data-font="sm|lg"` on `<html>`; root font-size set to 87.5% (14px) / 100% (16px) / 112.5% (18px); all Tailwind `rem` values scale accordingly
- **Persistence:** both stored in `localStorage` under keys `theme` and `fontSize`
- **System theme:** listens to `prefers-color-scheme` media query change events

### ⚠️ Dark-mode text-color gotcha (`gray` vs `slate`)
`globals.css` has `.dark .text-gray-900 { ... !important }` overrides (also 800/700/600/500/400) that force those colors light in dark mode. This means:
- A `text-gray-900` element on a card whose background stays light in dark mode becomes **invisible** (light text on light card), and a `dark:text-white` on a `text-gray-*` element **won't win** because the global rule is `!important`.
- **Fix / convention:** for any element needing explicit per-mode text color, use a palette the overrides DON'T touch — e.g. `text-slate-900 dark:text-white`. `slate` sidesteps the `!important` rule entirely. (This is how the `/hr` stat-card numbers are done.)

---

## Knowledge Base Notes
- Documents indexed via `POST /knowledge/upload` or `POST /knowledge/url`
- Chroma data persists in Docker volume `chroma_data` mounted to `/chroma/chroma`
- Indexing runs as background task; source `status` transitions: `pending → indexing → active | failed`
- On assessment creation with `information_source="kb"`, LM selects a specific document from the KB dropdown
- `source_id` stored in `assessment.rag_metadata` JSON column; used to scope Chroma retrieval to that document

---

## Security / Hardening Status

### Applied (Tier 1 — zero lockout risk)
- **Secrets removed from source** — `config.py` defaults for `SECRET_KEY` + `OPENAI_API_KEY` are now empty strings; values come only from `.env` (loaded via `SettingsConfigDict(env_file=".env")`). OpenAI key **rotated**; strong `SECRET_KEY` set in `.env`.
- **`.gitignore` added** — ignores `.env`, `*.bak`, `__pycache__`, `.venv`, editor dirs. ⚠️ Old secrets still exist in **git history** — make repo private and/or rewrite history (`git filter-repo`).
- **Cross-org validation** (`users.py` `_validate_org_refs`) — dept/line-manager must belong to caller's org (create + update) → clean 400.
- **N+1 fixes** — `GET /users/` single left-joined query; `GET /assessments/staff/my-results` batch-loads personality answers in one query.

### Recommended next (NOT applied — do interactively, test between each)
- **Tier 2 (medium risk):** implement `_verify_user_is_target` (log-only → enforce); login rate-limiting via Redis (auto-expiring lockouts); new-password complexity policy.
- **Tier 3 (high lockout risk — staging first, keep break-glass):** shorten JWT lifetimes + refresh rotation/revocation (rotation first, shorten expiry later); MFA for admins (recovery codes + DB-disable script from day one).
- **Principles:** always keep a break-glass admin/CLI path; one auth change at a time; test access control with two sessions (admin + test user); additive-then-enforce; back up before irreversible steps.

---

## Security Groups & Permissions (configurable RBAC) — Phase 1 DONE
A capability-based permission layer (configurable security groups), **additive on top of the role enum** (labels unchanged). Built in phases.

- **Catalog** (`services/permissions.py`): 10 capability keys — `stats.view`, `system.view`, `users.manage`, `kb.view`, `kb.manage`, `assessment.create`, `assessment.distribute`, `assessment.review`, `results.team`, `results.org`.
- **3 default groups** (mirror the existing roles): **Ops** = all 10 (≈ system_admin); **People & Culture** = all − `system.view` (≈ hr_admin); **Line Managers** = `kb.view`+`assessment.create/distribute/review`+`results.team` (≈ lm). Seeded per-org by `ensure_default_groups` (inside `seed.py`), which also assigns each user to the capability group matching their role (see Seeding note below).
- **Effective permissions** = `role_default` ∪ (every `SecurityGroup.permissions` the user is a member of) ∪ `user.extra_permissions` − `user.denied_permissions`. The **role→default mapping** (`ROLE_DEFAULTS`) is the fallback so existing users work with zero group membership. Resolver: `get_effective_permissions(user, db)`.
- **Model** (`models/user.py`): `SecurityGroup` gained `slug`/`permissions`(JSONB)/`is_system`/`description`; `User` gained `extra_permissions`/`denied_permissions` (JSONB). `GroupMembership` (user_id, group_id) already existed. Migration `0011`.
- **Exposed** on `GET /auth/me` as `permissions: string[]` (+ frontend `User.permissions`). Verified: Ops=10, P&C=9 (no system.view), LM=5, staff=0.
- **Scoping rules** (orthogonal to permission flags, applied at query time): *own* (creator-only), *dept-charge* (LM limited to departments containing their direct reports — derived from `user_departments.line_manager_id`), *org* (P&C sees everything).
- **Seeding:** `ensure_default_groups()` runs inside `seed.py` for new orgs (no standalone script — the `_seed_groups.py` referenced above never existed in history). **`seed.py` also now assigns capability-group memberships by role** (`SYSTEM_ADMIN`→Ops, `HR_ADMIN`→People & Culture, `LINE_MANAGER`→Line Managers, `STAFF`→none) so the `/hr/groups` admin UI shows real members and effective permissions visibly flow through groups — role defaults stay the fallback (the two coincide, so perm *counts* are unchanged). Before this, seeded users were only members of the legacy `owner.*`/`member.*`/`collaborator.*` GroupType groups, which carry **no** capability permissions (the resolver skips null-permission groups) and are hidden from `/hr/groups` — so the capability groups showed 0 members. The legacy GroupType groups are otherwise vestigial (scoping uses `user_departments.line_manager_id`, not them).

### Phase 2 — Enforce ✅ DONE
- **`require_permission(*keys)`** dependency factory (`services/auth_service.py`) — checks effective permissions; 403 otherwise. `has_permission(user, key, db)` helper too.
- **KB guards → permissions**: `GET /knowledge/` = `kb.view` (so **LMs get read-only KB**); upload/url/reindex/delete = `kb.manage` (HR/ops). Verified: LM lists (200) but can't mutate (403); staff blocked (403).
- **LM dept-restricted targeting**: `GET /assessments/targets/departments` returns only the LM's *charge* departments (those they line-manage) unless the caller has `users.manage` (org-wide). `create_assessment` validates department targets ⊆ charge for non-`users.manage` callers → **403** otherwise. Verified.
- **Frontend**: `can(perm)` on `useAuth()` (reads `user.permissions`). Nav tabs now permission-driven: Stats=`stats.view`, System Processes=`system.view`, **Knowledge Base=`kb.view`** (LMs see it), Users=`users.manage`. The KB page (`/hr/knowledge`) shows the source list to `kb.view`; **all mutation UI (upload/URL/delete/activity) is gated on `kb.manage`** with a "Read-only" badge for view-only users.
### Phase 3 — Admin UI ✅ DONE
- **`groups.py`** router (`/groups`, all gated on `users.manage`): `GET /catalog`, `GET/POST /groups`, `PATCH/DELETE /groups/{id}` (default `is_system` groups can't be deleted; legacy null-permission groups hidden from the list), `GET/PUT /groups/{id}/members` (PUT replaces the membership set), `GET /groups/users` (org users + effective/extra/denied perms + group_ids — uses an explicit membership query, **not** `User.memberships` lazy-load), `PATCH /groups/users/{id}/overrides`.
- **Page `/hr/groups`** (nav "Groups" tab for `users.manage`): two modes — **Groups** (list → edit name + permission toggles + member checklist + create/delete) and **User overrides** (pick a user → grant-extra / deny permission grids). `can()`-gated.
- Verified end-to-end: adding a user to a group **grants** its permissions on `/auth/me`; a per-user **deny** revokes one back; default groups protected.

### Phase 4 — Results by department ✅ DONE
- **`GET /admin/department-results`** (`results.org`): each department with its line manager(s), member count, dept average, and members (each member's overall avg across EVALUATED non-personality assessments), sorted high→low.
- **`/hr/assessment-averages` REPLACED** with a **by-department** view (department → LM → individuals; names link to `/hr/staff/[id]`). The **`/hr` dashboard** section is now **"Average Score by Department"** (same bar chart, dept-mapped). Verified (Engineering 77.5% → Diana 87.5% / Charlie 67.5%; LM-without-`results.org` → 403).

---

## Known Remaining Issues / TODO
1. **`user_departments` not seeded** — department, job title, line manager will show `—` for all test users; needs data in `user_departments` table
2. **S3/MinIO upload** — `knowledge.py` indexer processes files in-memory only; files not stored to MinIO (TODO comment in knowledge.py)
3. ~~**`_verify_user_is_target`** stub~~ ✅ **IMPLEMENTED** — take-time gate in `start_assessment`: staff may only begin an assessment they're covered by (organisation → any org user; department → via `user_departments`; individuals → exact id match). 403 otherwise; HR/admins bypass; fails closed if no targets. `/available` list mirrors the same logic (batched query) so the list and the gate agree
4. **Profile picture storage** — base64 in localStorage, keyed per-user (`profile_photo_<userId>`); frontend-only, not persisted to backend/S3, so it doesn't follow a user to another device
5. **Test suite — Phases 1 (unit) + 2 (API integration) DONE; Phase 3 (frontend) + CI pending.**
   - **Phase 1 — `tests/unit/` (37 pure-logic tests):** evaluator/MCQ + personality, the permissions resolver, schema validators, JSON extractors, `spread_correct_answers`, `_to_text`, grader fail-open, `iso_utc` — no DB/network, OpenAI/Chroma mocked via `tests/conftest.py` (`make_fake_chat`/`fake_chat`).
   - **Phase 2 — `tests/integration/` (41 tests):** drives the real FastAPI app over `httpx.ASGITransport` against a **real throwaway Postgres** (`proassess_test` DB, auto-created + schema via a sync engine). Covers auth (login/refresh/change-password/inactive), `/auth/me` effective permissions per role, RBAC 403s (LM read-only KB, kb.manage gating, dept-charge targeting, staff-blocked), the full MCQ lifecycle (create→deploy→start→submit→feedback + partial score + take-time targeting gate), `generation-status` insufficient_context, security-group CRUD + membership/overrides changing effective perms (+ system-group protection), and the scenario review queue (PENDING_REVIEW→approve→EVALUATED). Replaces the manual curl verification.
   - **Harness design (`tests/integration/conftest.py`):** the root `tests/conftest.py` **force-sets `DATABASE_URL` → `proassess_test`** at import (before `config`/`database` load — the container ships a real `DATABASE_URL` env var, so `setdefault` won't do; override target with `TEST_DATABASE_URL`). Isolation is **TRUNCATE-between-tests + committed writes** (not SAVEPOINT — background tasks / `pipeline_tracker` open their own `AsyncSessionLocal` sessions a savepoint can't span). The app's async engine is **disposed after every test** (`_dispose_async_engine`) because pytest-asyncio gives each test its own loop and asyncpg connections are loop-bound. **Background RAG generation is stubbed** to a no-op (`_stub_generation` monkeypatches `_generate_questions_background`); tests insert questions directly via the `add_questions` helper. Per-role users seeded by the `org` fixture; `login(email)` → auth header.
   - **Run:** `docker compose exec api pip install -r requirements-dev.txt && docker compose exec api python -m pytest tests` (unit ≈5s; integration ≈4–5 min — the slowness is first-import + per-test engine disposal, not flakiness).
   - **Pending:** Phase 3 = frontend (Vitest/RTL for `can()` gating + key components); CI (GitHub Actions: Postgres service container + pytest unit+integration + frontend tsc/lint/vitest).
6. **seed.py** uses `bcrypt` directly — now consistent with `auth_service.py`, which also dropped passlib for direct bcrypt (see Bug Fix #12)
7. **Dark mode partial** — `.dark` overrides in `globals.css` cover core classes; LM/HR pages not fully dark-mode styled (staff pages are)
8. **Personality dedup is exact-match only** — normalized string comparison catches identical/near-identical statements but not semantic paraphrases; embedding-based similarity was avoided to keep personality out of the vector pipeline
9. **Personality `ALTER TYPE` is manual on existing DBs** — fresh DBs via `create_all` are fine; existing DBs need the one-time `ALTER TYPE questiontype ADD VALUE 'PERSONALITY'` (see Personality section / migration 0002)
10. **No `system_admin` seed user** — the `/ops` dashboard needs one; promote via SQL (see System Processes section)
11. **Ops SSE is server-poll-backed** — the live-tail streams real-time to the browser but the server polls the DB every 1s inside the SSE generator (no broker). Fine for a few concurrent admins; Phase 2 would make it event-driven
12. **Personality submit not tracked** — only scored (MCQ/written) evaluation writes an `evaluation` run; personality submissions skip ops tracking
13. ~~**Sharing records but doesn't enforce**~~ ✅ **NOW ENFORCED** — `POST /{id}/share` adds `AssessmentTarget` rows, and `_verify_user_is_target` (see #3) now gates take-time access + filters `/available`. Sharing to a dept/individual genuinely grants access; non-targeted staff get 403 and don't see it listed
14. **No "co-creator" field** — share permission = creator OR HR/system_admin (via `_get_assessment_owned`); there's no true multi-creator join table
15. **HR can't reach the manage page UI** — share API authorizes HR, but HR has no route to open an arbitrary assessment (no HR assessment list / `GET /assessments/{id}` single-fetch). Creator uses `/lm/assessments/[id]`
16. **MBTI was built then fully removed** — a classic forced-choice Myers-Briggs format (93 items, 4 dichotomies) was added and then removed at the user's request (too slow/redundant vs. 16Personalities). Do NOT re-add unless asked. The `questiontype` DB enum may contain an unused `MBTI` label — harmless. 16Personalities (`PERSONALITY`) is the only typology format.
17. **`coding` is Technical-only** — the Coding format is hidden under the Professional assessment type (only MCQ/Written/Personality there); a guard resets the format if you switch to Professional with Coding selected.

### Roadmap (consolidated — from the manual §B.9)
The user manual's "Known limitations & roadmap" maps to the canonical items above:
- **Target enforcement** (#3, #13) — ✅ DONE: take-time gate + `/available` filter enforce org/department/individual targeting.
- **MinIO object storage** (#2) — uploads processed in memory, not persisted to object storage.
- **Observability streaming** (#11) — live-tail polls the DB every 1s; event-driven push (broker/WebSocket) is the next step.
- **Profile photos** (#4) — per-user localStorage, not synced to the backend.
- **Automated tests** (#5) — unit (Phase 1) + API integration (Phase 2) suites exist; frontend tests + CI still pending.

---

## Session Handoff — Next Phases & Open Threads (pick up here)

### Testing — remaining phases (Phases 1 + 2 DONE; see Known Issues #5)
- **Phase 2 — API integration ✅ DONE.** `tests/integration/` (41 tests) drives the real app via `httpx.ASGITransport` against a real throwaway `proassess_test` Postgres. Isolation is TRUNCATE-between-tests + committed writes (NOT savepoints — background tasks / `pipeline_tracker` open their own sessions); the app's async engine is disposed per test (loop-bound asyncpg connections). Background generation stubbed; questions inserted directly via `add_questions`. Covers auth/`/auth/me` perms, RBAC 403s, full MCQ lifecycle, scenario review (pending→approve→evaluated), groups CRUD + membership/override perm changes, `generation-status` insufficient_context. See Known Issues #5 for the full harness writeup + run command.
- **Phase 3 — frontend (pending).** Vitest + React Testing Library for `can()` gating + key components (assessment bar chart, case panel, idle-logout); keep `tsc --noEmit` + `eslint` as the cheap first gate.
- **CI (pending).** GitHub Actions on PR: a Postgres service container + `pytest` (unit+integration) + frontend tsc/lint/vitest.

### Other open threads
- **Manual diagrams not regenerated (v1.1):** Fig 2 (roles) doesn't show security groups; Fig 6 (RAG) doesn't show the grade step. Edit `manual_build/make_diagrams.py` (matplotlib) + rebuild to refresh them.
- Roadmap leftovers: MinIO object storage (#2), observability event-driven push (#11), profile-photo backend sync (#4).

### Environment state (demo DB) — fresh sessions should know
- Stack is running (`docker compose` + `npm run dev`); `proassess.bat stop` shuts it down. `proassess.bat reload` force-recreates the API after `.env` edits.
- **`staff4@acme.com` is promoted to `system_admin`** (for `/ops`). **`lm.sales@acme.com` has `force_password_change` set** (its password was reset during testing — it can NOT log in with `Password123!`; reset via SQL or the HR UI if you need the second LM).
- Both repos pushed: `proassess-backend` (main) + `proassess-frontend` (**private**, main) — except the uncommitted backend changes above. Tavily `WEB_SEARCH_API_KEY` is live in `.env` (gitignored).

---

## Documentation Artifacts
- **`ProAssess_Manual.docx`** (≈878 KB, **v1.1**) — polished Word manual: cover, auto TOC, header/footer + page numbers. **Part A User Guide** (role-by-role walkthroughs) + **Part B Technical Manual** (architecture, RAG pipeline, evaluation, observability, data model, deployment/ops gotchas, troubleshooting, roadmap) + glossary. Contains 6 custom diagrams. **v1.1 adds:** the RAG grading loop, Hybrid grounding, case-study (scenario) assessments + human-assisted review (B.9), configurable security groups (B.10), by-department results, pre-deploy preview/regenerate, capsule provenance, `proassess.bat`. (Regenerated from `manual_build/build_manual.js`; the 6 diagrams are unchanged from v1.0 — Fig 2 roles & Fig 6 RAG don't yet show groups/the grade step.)
- **`manual_build/`** — regeneration sources: `make_diagrams.py` (matplotlib, brand palette → 6 PNGs in `manual_build/diagrams/`) and `build_manual.js` (docx-js). To rebuild: `python manual_build/make_diagrams.py` then `cd manual_build && npm install docx && node build_manual.js`.
- **`DOCUMENTATION.md`** / **`ProAssess_Documentation.pdf`** — earlier (pre-feature) documentation; the .docx manual supersedes them.
