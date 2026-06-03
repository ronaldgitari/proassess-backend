# Case Study (Scenario) Assessment — Build & Retrospective Log

A living record of the case-study assessment feature: design decisions, what was
built, how it was verified, bugs hit, and what remains. Purpose: if an issue
surfaces later, this is the retrospective trail (decisions + diffs + test results).

Last updated: 2026-06-03 — **Increments 1 (generation) + 2 (feedback + LM review) + 3 (frontend) complete & verified. Feature shippable (grounded-only feedback until a web provider key is set).**

---

## 1. Product decisions (locked with the user)

| Decision | Choice | Why |
|---|---|---|
| Case structure | **One shared case per assessment** | Simplest, ~80% reuse, no question-grouping schema. Testlets (multi-case) deferred. |
| Answer format | **Written analysis only** | Reuses GPT rubric evaluation; fits the one-question-type-per-assessment model. |
| Grounding | **KB-grounded only** | Case must derive from an indexed document → defensible + the grading loop applies. |
| Availability | **Technical & Professional** | Tech (incident/design scenarios) + Professional (business/leadership cases). |

Three capabilities requested, built in increments:
1. **Scenario generation** (case + analytical questions). ← *Increment 1, DONE*
2. **Rich AI feedback** grounded in KB **+ credible web sources**. ← *Increment 2, pending*
3. **Human-in-the-loop**: LM reviews/confirms before a score is finalised. ← *Increment 2, pending*

---

## 2. Architecture

```
Create (question_type=scenario, source=kb, source_id required, 5–8 Qs)
  → background generation_for_assessment()
      → KB branch: retrieve() → grade_context() reflection loop  (quality gate)
      → augment (SCENARIO): generate_scenario_assessment()
          stage 1: author ONE case narrative grounded in retrieved chunks  (prose)
          stage 2: author N analytical questions about the case
                   (question + model_answer + rubric + source_reference + difficulty)
      → case stored on assessment.rag_metadata["scenario"]
      → questions persisted as Question rows (question_type=SCENARIO)
  → /{id}/start returns the case text (shared stimulus) alongside questions
```

### Data model
- `QuestionType.SCENARIO = "scenario"` (DB enum label `SCENARIO`, uppercase).
- Case narrative lives in `assessment.rag_metadata["scenario"]` (no new column — same
  pattern as `source_id` / `language`).
- Scenario questions reuse the **written-question shape**: `correct_answer_text` =
  model answer, `explanation` = marking rubric. `options`/`correct_answer_index` are null.
- **Review-workflow fields (added now to avoid a 2nd migration; used in Increment 2):**
  - `StaffAssessmentStatus.PENDING_REVIEW` — AI-drafted score awaiting LM confirmation.
  - `staff_assessments.reviewed_by_id`, `staff_assessments.reviewed_at`.
  - `staff_answers.feedback_sources` (JSONB) — credible KB+web citations behind feedback.

### Migration `0009_scenario_and_review`
Adds the two enum values + the three columns. On the (non-alembic-stamped) dev DB the
equivalent raw SQL was applied directly (project convention — `create_all` + manual ALTERs):
```sql
ALTER TYPE questiontype ADD VALUE IF NOT EXISTS 'SCENARIO';
ALTER TYPE staffassessmentstatus ADD VALUE IF NOT EXISTS 'PENDING_REVIEW';
ALTER TABLE staff_assessments ADD COLUMN IF NOT EXISTS reviewed_by_id uuid;
ALTER TABLE staff_assessments ADD COLUMN IF NOT EXISTS reviewed_at timestamp;
ALTER TABLE staff_answers ADD COLUMN IF NOT EXISTS feedback_sources jsonb;
```
> ⚠️ After adding an enum value, **restart the API** so asyncpg re-introspects the type;
> stale pooled connections won't recognise the new label (`invalid input value for enum`).

---

## 3. Files touched (Increment 1)

| File | Change |
|---|---|
| `models/assessment.py` | `QuestionType.SCENARIO`, `StaffAssessmentStatus.PENDING_REVIEW`, review columns, `StaffAnswer.feedback_sources` |
| `alembic/versions/0009_scenario_and_review.py` | migration (enum values + columns) |
| `rag/augmentor.py` | `generate_scenario_assessment()` (2-stage) + `_to_text()` list/dict→text coercion |
| `rag/__init__.py` | SCENARIO branch in the KB augment step (stores case on assessment) |
| `schemas/__init__.py` | num-questions cap 8 for scenario; KB+source_id required |
| `assessments.py` | `/{id}/start` now returns `scenario` (and `language`) from rag_metadata |
| `config.py` | `WEB_SEARCH_PROVIDER` / `WEB_SEARCH_API_KEY` / `WEB_SEARCH_MAX_RESULTS` (for Increment 2) |

---

## 4. Verification log (Increment 1)

Tested live against the running stack (LM `lm.eng@acme.com`, KB doc = `2020-Scrum-Guide-US.pdf`):

- ✅ **Quality gate works**: topic "facilitating events when the team is *struggling to meet
  the Sprint Goal*" → grader returned **insufficient** (Scrum Guide covers roles/events/Sprint
  Goal but not "struggling-team techniques") → honest fail, no junk case generated.
- ✅ **Happy path**: topic "Applying Scrum roles, events, and artifacts to run an effective
  Sprint" → grade **sufficient** → case (~2k chars, "Tech Solutions Inc." adopting Scrum) +
  5 analytical questions each with model answer + rubric, persisted (5/5), ops-traced.

### Bugs found & fixed during verification
1. **List rubric → persist DataError.** GPT returned `explanation` (and sometimes
   `model_answer`) as a JSON **array** of bullet criteria; the `explanation`/`correct_answer_text`
   columns are `Text`. asyncpg raised `expected str, got list`. **Fix:** `_to_text()` flattens
   list/dict values to bullet text in `generate_scenario_assessment` before they reach persist.
2. **Reload race.** `uvicorn --reload` sometimes served a create request on the *old* worker
   before reloading the edited file, so a fix appeared not to take. **Lesson:** after editing
   pipeline code, `docker compose restart api` + wait-for-healthy before re-testing generation.
3. **num_questions floor.** The shared `Field(ge=5)` floors *all* formats at 5, so scenarios
   are **5–8** (not 3–8). Acceptable; relaxing to 3 would need a per-type lower bound.

---

## 5. Remaining work

### Increment 2 — feedback + human review (backend/API)  ✅ DONE & verified
- **Rich AI feedback** (`rag/feedback.py`): for each scenario answer, GPT produces a 0–100 draft
  score + detailed feedback grounded in the case + rubric/model answer, enriched with **credible
  web sources** when available.
  - `web_search()` is a **pluggable provider** (`WEB_SEARCH_PROVIDER`; Tavily wired). When unset/keyless
    it returns `[]` and feedback is grounded-only — same "no fabricated citations" guardrails as the
    MCQ/written enrichment. Web calls are ops-traced as a `web` service span. Sources persisted to
    `staff_answers.feedback_sources`.
- **Submit → PENDING_REVIEW** (scenario branch in `submit_assessment`): drafts per-answer scores +
  feedback and an overall draft `score_pct`, status = `PENDING_REVIEW` (NOT `EVALUATED`) → excluded
  from staff results + stats (both filter on `EVALUATED`); the submit response withholds the score
  (`pending_review=True`). Ops-traced as a `kind="evaluation"` run (feedback → persist steps).
- **LM review API** (`assessments.py`, `require_lm` + `_get_assessment_owned`):
  - `GET /assessments/reviews/pending` — queue (owner's assessments; HR/admin see org-wide).
  - `GET /assessments/reviews/{sa_id}` — case + per-question staff answer, draft score, AI feedback, sources.
  - `POST /assessments/reviews/{sa_id}/approve` — optional per-answer score/feedback overrides →
    recompute final `score_pct`, set `EVALUATED` + `reviewed_by_id`/`reviewed_at`; audit `REVIEW_ASSESSMENT`.

**Verification (live):** create→generate(5)→deploy→staff start (case returned)→submit
(`pending_review=True`, score withheld, absent from staff results)→LM queue shows it→detail returns
draft 34% + per-answer AI feedback→approve with a Q1 override(85)→final **41%**, `EVALUATED`, now in
staff results. Feedback run ops-traced (OpenAI + Postgres spans in the capsule; `web` span would
appear with a provider key). Test data hard-deleted afterwards.

### Increment 3 — frontend  ✅ DONE & verified
- **Create page** (`/lm/assessments/new`): "Case Study" format under both types, KB-locked (forces
  `information_source=kb`, hides the source selector, shows the document picker), 5–8 Qs, info card.
- **Take page** (`/staff/assessments/[id]/take`): two-pane — sticky `CasePanel` (left) + written
  answers (right) when `start.scenario` is present; otherwise unchanged. On submit, if
  `pending_review` → a "Submitted for review" confirmation screen (no score shown).
- **Feedback page** (`/staff/.../feedback`): scenario-aware — shows the case (`CasePanel`), a
  reviewer-confirmed score, and per-answer response + AI feedback + score badge (no ✓/✗; rubric hidden).
- **LM review queue** (`/lm/reviews` + `/lm/reviews/[id]`): list of pending submissions → detail with
  the case, each candidate answer, collapsible rubric/model answer, **editable score + feedback**,
  cited sources, and **Approve & release** (recomputes final score). Nav gained a **Reviews** tab (LM/HR).
- **Shared**: `components/case-panel.tsx` (light markdown), `lib/api.ts` types (`PendingReview`,
  `ReviewDetail`, `AssessmentFeedback.pending_review`/`scenario`) + `pendingReviews`/`reviewDetail`/`approveReview`.
- **Verified live (UI-backing APIs):** create→generate→deploy→start (case returned)→submit
  (`pending_review`)→LM detail→approve(with override)→staff feedback carries case + per-answer scores/feedback.

---

## 5b. Hybrid grounding (KB + web case studies) — ✅ added & verified

A new **`hybrid`** information source = KB document **+** credible domain/industry web case-study
sources (Tavily). Available for MCQ, Written, and Case Study; requires a `source_id`.

- **`rag/web_research.py`** — `web_search` (moved here, shared with `rag/feedback.py`) +
  `gather_web_context(topic, domain, context_prompt)` → returns web `Document`s + raw sources.
- **Orchestrator** (`rag/__init__.py`): HYBRID adds a **`web`** step (after `retrieve`), **interleaves**
  KB+web docs (so the grader's truncated preview and the augmentor see both), and the **grade step is
  NON-FATAL** for hybrid (`fail_on_insufficient=False`) — it warns and proceeds, because web/model
  knowledge supplement the KB by design. Web sources persisted to `rag_metadata.web_sources`.
- **Grader** preview budget raised 6000→9000 chars (so combined KB+web context is sampled).
- **Schema**: `validate_grounding` accepts `kb`/`hybrid` for scenarios; `hybrid` requires `source_id`.
- **Migration `0010`**: `ALTER TYPE informationsource ADD VALUE 'HYBRID'`.
- **Create page**: "Hybrid (KB doc + web case studies)" source option (scenario limited to kb/hybrid).
- **Verified live:** hybrid scenario → retrieve 10 KB + web 12 sources → grade `warn` (non-fatal) →
  5/5 generated; 12 web sources stored (e.g. *Scrum.org Case Studies*); capsule shows the Web Search service.

## 6. Known limitations / watch-list
- KB-grounded-only ⇒ a case study **can't** be created if no indexed doc covers the topic
  (the grading loop will fail it). By design (defensibility), but raises the bar on KB content.
- Single shared case per assessment (no multi-case testlets yet).
- Written-only (no mixed MCQ decision points) — would break one-type-per-assessment.
- Live web sourcing needs a configured `WEB_SEARCH_PROVIDER` + key; otherwise feedback is
  grounded-only (still useful, just no external citations).
