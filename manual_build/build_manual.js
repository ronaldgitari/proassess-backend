const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell, ImageRun,
  Header, Footer, AlignmentType, LevelFormat, TabStopType, TabStopPosition,
  TableOfContents, HeadingLevel, BorderStyle, WidthType, ShadingType,
  VerticalAlign, PageNumber, PageBreak,
} = require("docx");

const DIAG = path.join(__dirname, "diagrams");
const CONTENT_W = 9360; // US Letter, 1" margins

// ── palette ──
const INDIGO = "4f46e5", AVO = "65a30d", SLATE = "334155", GREY = "64748b";
const INDIGO_L = "e0e7ff", AVO_L = "ecfccb", SLATE_L = "f1f5f9", AMBER_L = "fef3c7";

// ── helpers ──
const H1 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun(t)] });
const H2 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun(t)] });
const H3 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_3, children: [new TextRun(t)] });

function P(text, opts = {}) {
  const runs = Array.isArray(text) ? text : [new TextRun({ text, ...opts.run })];
  return new Paragraph({ children: runs, spacing: { after: opts.after ?? 120, line: 276 }, ...opts.para });
}
function bullet(text, level = 0) {
  const runs = Array.isArray(text) ? text : [new TextRun(text)];
  return new Paragraph({ numbering: { reference: "bullets", level }, children: runs, spacing: { after: 60, line: 264 } });
}
function num(text) {
  const runs = Array.isArray(text) ? text : [new TextRun(text)];
  return new Paragraph({ numbering: { reference: "steps", level: 0 }, children: runs, spacing: { after: 80, line: 264 } });
}
const b = (t) => new TextRun({ text: t, bold: true });
const t = (t) => new TextRun({ text: t });
const code = (t) => new TextRun({ text: t, font: "Consolas", size: 19 });

function image(file, w) {
  const data = fs.readFileSync(path.join(DIAG, file));
  // diagrams are 2000px wide @200dpi → scale to content width, preserve aspect from figsize
  return new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 120, after: 60 },
    children: [new ImageRun({ type: "png", data, transformation: { width: w.w, height: w.h },
      altText: { title: file, description: file, name: file } })],
  });
}
function caption(t) {
  return new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 200 },
    children: [new TextRun({ text: t, italics: true, size: 18, color: GREY })] });
}

// callout box (single-cell shaded table)
function callout(titleText, bodyParas, fill = AMBER_L, accent = "f59e0b") {
  const kids = [new Paragraph({ spacing: { after: 60 }, children: [new TextRun({ text: titleText, bold: true, color: SLATE })] }),
    ...bodyParas];
  return new Table({
    width: { size: CONTENT_W, type: WidthType.DXA }, columnWidths: [CONTENT_W],
    rows: [new TableRow({ children: [new TableCell({
      width: { size: CONTENT_W, type: WidthType.DXA },
      shading: { fill, type: ShadingType.CLEAR },
      borders: { left: { style: BorderStyle.SINGLE, size: 18, color: accent },
        top: { style: BorderStyle.SINGLE, size: 2, color: fill }, bottom: { style: BorderStyle.SINGLE, size: 2, color: fill },
        right: { style: BorderStyle.SINGLE, size: 2, color: fill } },
      margins: { top: 120, bottom: 120, left: 160, right: 160 },
      children: kids })] })],
  });
}

// data table with header row
function table(headers, rows, widths) {
  const border = { style: BorderStyle.SINGLE, size: 1, color: "cbd5e1" };
  const borders = { top: border, bottom: border, left: border, right: border };
  const headerRow = new TableRow({
    tableHeader: true,
    children: headers.map((h, i) => new TableCell({
      width: { size: widths[i], type: WidthType.DXA }, borders,
      shading: { fill: INDIGO, type: ShadingType.CLEAR },
      margins: { top: 70, bottom: 70, left: 110, right: 110 },
      children: [new Paragraph({ children: [new TextRun({ text: h, bold: true, color: "ffffff", size: 19 })] })],
    })),
  });
  const bodyRows = rows.map((r, ri) => new TableRow({
    children: r.map((cell, i) => new TableCell({
      width: { size: widths[i], type: WidthType.DXA }, borders,
      shading: { fill: ri % 2 ? "f8fafc" : "ffffff", type: ShadingType.CLEAR },
      margins: { top: 60, bottom: 60, left: 110, right: 110 },
      verticalAlign: VerticalAlign.CENTER,
      children: [new Paragraph({ children: Array.isArray(cell) ? cell : [new TextRun({ text: String(cell), size: 19 })] })],
    })),
  }));
  return new Table({ width: { size: CONTENT_W, type: WidthType.DXA }, columnWidths: widths, rows: [headerRow, ...bodyRows] });
}
const spacer = (h = 120) => new Paragraph({ spacing: { after: h }, children: [] });

// ════════════════════════════════════════════════════════════════
// COVER
// ════════════════════════════════════════════════════════════════
const cover = [
  new Paragraph({ spacing: { before: 2600, after: 0 }, alignment: AlignmentType.CENTER,
    children: [new TextRun({ text: "ProAssess", bold: true, size: 96, color: INDIGO })] }),
  new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 120 },
    children: [new TextRun({ text: "Staff Proficiency Assessment Platform", size: 32, color: SLATE })] }),
  new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 600 },
    children: [new TextRun({ text: "User Guide & Technical Manual", size: 26, color: AVO, bold: true })] }),
  new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 40 },
    children: [new TextRun({ text: "AI-generated assessments · grounded RAG with a grading loop · MCQ, written, coding, personality & case-study formats", size: 20, color: GREY })] }),
  new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 40 },
    children: [new TextRun({ text: "human-assisted case-study review · configurable security groups · live observability with log capsules", size: 20, color: GREY })] }),
  new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 1400 },
    children: [new TextRun({ text: "by Atocado", size: 22, color: AVO, bold: true })] }),
  new Paragraph({ alignment: AlignmentType.CENTER,
    children: [new TextRun({ text: "Version 1.1", size: 18, color: GREY })] }),
  new Paragraph({ children: [new PageBreak()] }),
];

// ════════════════════════════════════════════════════════════════
// TOC
// ════════════════════════════════════════════════════════════════
const toc = [
  H1("Contents"),
  new TableOfContents("Contents", { hyperlink: true, headingStyleRange: "1-2" }),
  new Paragraph({ children: [new PageBreak()] }),
];

// ════════════════════════════════════════════════════════════════
// PART 1 — INTRODUCTION
// ════════════════════════════════════════════════════════════════
const intro = [
  H1("1. Introduction"),
  P("ProAssess is a staff proficiency assessment platform. Line Managers create assessments whose questions are auto-generated by an AI pipeline; staff take them under a timer; results are scored instantly (multiple-choice) or evaluated by AI (written and coding); and HR oversees the knowledge base, users, organisation-wide statistics, and an audit trail. System Administrators additionally have a live observability dashboard for every system process."),
  H2("1.1  Who this manual is for"),
  P("This document has two parts:"),
  bullet([b("Part A — User Guide: "), t("task-focused walkthroughs for each of the four roles (Staff, Line Manager, HR Admin, System Admin).")]),
  bullet([b("Part B — Technical Manual: "), t("architecture, the RAG generation pipeline, data model, observability, deployment, and troubleshooting.")]),
  H2("1.2  What makes ProAssess distinct"),
  bullet([b("AI question generation grounded in your own documents "), t("(Retrieval-Augmented Generation), with a grading-and-re-query reflection loop that fails honestly when a source does not cover the topic — so questions reflect your organisation's material rather than confidently-wrong guesses.")]),
  bullet([b("Five assessment formats "), t("in one platform: multiple-choice, written, coding (embedded editor), 16-Personalities profiling, and KB-grounded case studies (scenarios).")]),
  bullet([b("Hybrid grounding "), t("— a case or question set can draw on your knowledge base and credible, domain-relevant web case-study sources together.")]),
  bullet([b("Human-assisted verification "), t("— case-study answers receive rich AI feedback (grounded, with cited sources where available) plus a draft score that a Line Manager reviews and confirms before it counts.")]),
  bullet([b("Configurable access control "), t("— security groups define capabilities; users inherit permissions from their groups with per-individual overrides, on top of the four base roles.")]),
  bullet([b("Transparent observability: "), t("a System Processes dashboard live-tails every generation, indexing, and evaluation as a terminal log, with a per-transaction “Log Capsule” trace grouped by backing service and stamped with its source document/URL and (for scoring) the candidate.")]),
  spacer(),
  image("01_architecture.png", { w: 600, h: 432 }),
  caption("Figure 1 — High-level system architecture."),
];

// ════════════════════════════════════════════════════════════════
// PART A — USER GUIDE
// ════════════════════════════════════════════════════════════════
const partA = [
  new Paragraph({ children: [new PageBreak()] }),
  H1("Part A — User Guide"),
  P("ProAssess has four roles. After signing in, each role is taken to its own landing page. The diagram below summarises what each role can do."),
  image("02_roles.png", { w: 600, h: 396 }),
  caption("Figure 2 — Roles and their capabilities."),

  H2("A.1  Signing in"),
  num("Open the application and enter your email and password on the login screen."),
  num("On first login (or after an HR password reset) you are required to set a new password before continuing."),
  num("You are then routed to your role's landing page automatically."),
  callout("Test credentials (demo environment)", [
    P([code("hr@acme.com"), t("  ·  HR Admin     "), code("lm.eng@acme.com"), t("  ·  Line Manager")], { after: 40 }),
    P([code("staff1@acme.com"), t("  ·  Staff     "), t("(all demo accounts use "), code("Password123!"), t(")")], { after: 0 }),
  ], SLATE_L, GREY),

  H2("A.2  Staff — taking an assessment"),
  P([b("Landing page: "), code("/staff/assessments"), t(", reached at any time by clicking the Atocado · ProAssess logo. The dashboard has a profile card and your pending assessments; your profile card also summarises your Strengths, Developing areas, and Weaknesses across completed assessments. Past results live on their own "), b("Results"), t(" tab.")]),
  H3("Pre-flight system check"),
  P("Before any assessment begins, a system check runs automatically as an ordered checklist. Each item shows a spinning icon while testing, then a green tick when it passes:"),
  bullet([b("Internet connection & latency "), t("— confirms the server is reachable and round-trip time is within the recommended threshold.")]),
  bullet([b("Browser compatibility "), t("— verifies your browser and version support the required features.")]),
  bullet([b("Service reachability "), t("— confirms a firewall or proxy is not blocking the assessment's domain or port.")]),
  P("If any check fails, the specific problem is shown and you can re-run the checks. The assessment — and its timer — only start once the check passes, so connectivity validation never consumes your assessment time."),
  spacer(),
  image("05_take_flow.png", { w: 600, h: 312 }),
  caption("Figure 3 — The candidate flow, from pre-flight check to feedback."),
  H3("Answering questions"),
  P("The answer interface adapts to the format:"),
  bullet([b("Multiple choice "), t("— select one option.")]),
  bullet([b("Written "), t("— type a free-text response; it is graded by AI against a model answer.")]),
  bullet([b("Coding "), t("— write your solution in the embedded VS Code-style editor (syntax highlighting; Dark / Solarized / Dimidium themes).")]),
  bullet([b("Personality (16 Personalities) "), t("— rate first-person statements on a 7-point agree/disagree scale. There are no right or wrong answers.")]),
  bullet([b("Case study (scenario) "), t("— read a realistic case shown in a sticky side panel, then write your analysis for each linked question. Your answers are graded by AI and confirmed by a reviewer before a score is released.")]),
  H3("Submitting"),
  P("A confirmation dialog shows how many questions you answered and how many were skipped (skipped questions are scored as incorrect). When the timer reaches zero the assessment submits automatically."),
  H3("Results"),
  P("Scored assessments show a percentage with a pass/fail badge (pass ≥ 70%) and a per-question breakdown. Personality assessments show your type (for example, INTJ-A — Architect), a trait breakdown, and a character summary that also appears on your dashboard profile card."),
  P([b("Case studies are different: "), t("on submit you see a “Submitted for review” screen with no score. The case is graded by AI, then a Line Manager reviews and confirms the feedback and score. Only after that confirmation does the result appear under your Results, showing the case, your answers, and the confirmed per-answer feedback.")]),

  H2("A.3  Line Manager — creating and managing assessments"),
  P([b("Landing page: "), code("/staff/assessments"), t(" — Line Managers are assessed too, so they share the staff dashboard (reached via the logo). Management tools live on dedicated tabs: "), b("Manage Assessments"), t(" (create / deploy), "), b("Reviews"), t(" (confirm case-study results), "), b("Team Results"), t(", and "), b("My Results"), t(".")]),
  H3("Creating an assessment"),
  num([t("From "), b("Manage Assessments"), t(", click "), b("New Assessment"), t(" and give it a name and topic.")]),
  num([t("Choose the "), b("type"), t(" (Technical or Professional) and "), b("format"), t(". Both types offer MCQ, Written, and "), b("Case Study"), t("; Technical also offers Coding; Professional also offers Personality.")]),
  num([t("Pick a "), b("knowledge source"), t(": a specific uploaded document (Knowledge Base), "), b("Hybrid"), t(" (that document plus credible domain-relevant web case-study sources), AI general knowledge, or a custom URL. Personality and Coding are AI-generated; Case Study is KB- or Hybrid-grounded only.")]),
  num([t("Set the "), b("number of questions"), t(" (5–30; Personality is fixed at 60; Case Study is 5–8) and a "), b("time limit"), t(".")]),
  num([t("Choose the "), b("audience"), t(": the whole organisation, specific departments, or named individuals. A Line Manager may only target "), b("departments in their charge"), t(" — those containing staff they line-manage.")]),
  num([t("Submit. The question-generation pipeline runs in the background; the manage page live-tails progress with a latency gauge.")]),
  H3("Reviewing generated questions before deploy"),
  P("When generation finishes, the manage page shows a preview of every generated question with its answers and marking rubric (and, for case studies, the case narrative and any web sources used). Approve and deploy when satisfied, or click Regenerate Questions to discard the set and produce a fresh one."),
  H3("Deploy, cancel, delete, share"),
  bullet([b("Deploy "), t("makes a generated draft visible to its targeted staff.")]),
  bullet([b("Cancel "), t("stops a deployed assessment (a reason is recorded in the audit log).")]),
  bullet([b("Delete "), t("removes a draft or cancelled assessment. If anyone has already completed it, it is archived instead — their results and feedback are preserved.")]),
  bullet([b("Share with more people "), t("adds further departments or individuals to a deployed assessment (already-targeted recipients are excluded from the picker).")]),
  spacer(),
  image("03_lifecycle.png", { w: 600, h: 336 }),
  caption("Figure 4 — The assessment lifecycle and the archive-on-delete rule."),

  H3("Reviews — confirming case-study results (human-assisted verification)"),
  P([t("Case-study submissions are graded by AI but not released until a Line Manager confirms them. The "), b("Reviews"), t(" tab lists pending submissions. Opening one shows the case, the candidate's answers, the collapsible marking rubric, an editable AI draft score and feedback per answer, and any cited sources. The reviewer adjusts as needed, then clicks "), b("Approve & release"), t(" — which recomputes the final score, marks the attempt evaluated, and makes the result visible to the candidate and to statistics. This step is recorded in the audit log.")]),
  H3("Team Results"),
  P([t("The "), b("Team Results"), t(" tab shows the average score by assessment across the manager's direct reports — a bar chart plus rows that expand to each report's score; names link to the staff profile.")]),
  H3("Knowledge Base (read-only)"),
  P("Line Managers can browse the Knowledge Base to see which documents are available to ground their assessments, but cannot upload, re-index, or delete sources (those are restricted to People & Culture / Ops). A “Read-only” badge marks the view."),

  H2("A.4  HR Admin (People & Culture) — oversight"),
  P([b("Landing page: "), code("/hr"), t(" (Stats), also reached via the logo. Tabs: Stats, Knowledge Base, Users, and Groups.")]),
  bullet([b("Statistics "), t("— animated tiles (Total Assessments incl. drafts, Active (deployed) Assessments, Staff Assessed, Knowledge Sources) plus an "), b("Average Score by Department"), t(" chart. The Knowledge Chunks tile is an Ops-only metric and is hidden from HR.")]),
  bullet([b("Results by department "), t("— the primary results page is arranged by department, each showing its name and line manager(s) and its average, expanding to the individuals in that department (sorted high→low, names hyperlinked to the staff profile). This replaces the older by-assessment arrangement.")]),
  bullet([b("Security Groups "), t("— the Groups page configures access: create or edit groups, toggle their capabilities, manage members, and set per-individual permission overrides (see B.10).")]),
  bullet([b("Knowledge Base "), t("— upload PDF/DOCX/XLSX or index a URL; each source shows status, chunk count, and indexed date. The list auto-refreshes every 15 seconds while a source is still indexing.")]),
  bullet([b("Users "), t("— create users, assign role / department / job title / line manager / start date, deactivate (never hard-delete, to preserve history), and reset passwords (a one-time temporary password is shown, with a copy button).")]),
  bullet([b("Audit log "), t("— the dashboard shows the five most recent actions; a full page provides load-more pagination and captures cancellation/deletion reasons.")]),
  H3("Staff profile page"),
  P("Opening a staff member from any score list shows their profile (the same details as the staff dashboard, including personality type and summary), all scored results, and a skill assessment that buckets their assessments into Strengths (≥ 70%), Developing (50–69%), and Weaknesses (< 50%). This page is available to HR, System Admins, and the staff member's own Line Manager."),

  H2("A.5  System Admin (Ops) — the System Processes dashboard"),
  P([b("Landing page: "), code("/hr"), t(" (Stats); the System Processes dashboard is at "), code("/ops"), t(". Ops can do everything People & Culture can, plus observe every system process live and see the Ops-only Knowledge Chunks metric.")]),
  bullet([b("Run list "), t("— recent generation, indexing, and evaluation runs; live runs float to the top with a progress bar, finished runs collapse to compact rows.")]),
  bullet([b("Terminal log "), t("— selecting a run shows its recorded log as a timestamped terminal window; live runs stream in real time.")]),
  bullet([b("Log Capsule "), t("— on generation and evaluation runs, a capsule link reveals a transaction trace: a metadata header and the run's spans grouped by backing service, each with its true duration. The header lists the services involved, origin/server IP, system id, and timestamps, plus full provenance: the information source and the reference document or URL behind the run (and any web-source count), the assessment name, and — for evaluation runs — the candidate being scored.")]),
  spacer(),
  image("06_capsule.png", { w: 600, h: 396 }),
  caption("Figure 5 — Observability: phased steps drive the live log; real spans build the Log Capsule."),
];

// ════════════════════════════════════════════════════════════════
// PART B — TECHNICAL MANUAL
// ════════════════════════════════════════════════════════════════
const partB = [
  new Paragraph({ children: [new PageBreak()] }),
  H1("Part B — Technical Manual"),

  H2("B.1  Technology stack"),
  table(["Layer", "Technology"], [
    ["Frontend", "Next.js 16 (App Router), TypeScript, Tailwind CSS v4"],
    ["Backend", "FastAPI (async), SQLAlchemy (async ORM)"],
    ["Database", "PostgreSQL (pgvector image)"],
    ["Vector store", "Chroma (document embeddings for RAG)"],
    ["Cache / queue", "Redis (available)"],
    ["Object storage", "MinIO (planned for uploaded files)"],
    ["AI", "OpenAI GPT-4o (generation & evaluation) + text-embedding-3-large"],
    ["Runtime", "Docker Compose (all services)"],
  ], [2400, 6960]),
  spacer(),

  H2("B.2  Authentication & roles"),
  P("Authentication is JWT-based with access and refresh tokens. There are four roles, enforced by FastAPI dependencies:"),
  table(["Role", "Reach"], [
    ["staff", "Take assessments; view own results and personality profile."],
    ["lm (Line Manager)", "Create / deploy / cancel / delete / share assessments; view managed staff profiles."],
    ["hr_admin", "All LM reach + users, departments, knowledge base, org stats, audit log, staff profiles."],
    ["system_admin", "All HR reach + the System Processes dashboard and Log Capsules; can grant system_admin."],
  ], [2400, 6960]),
  P("New accounts and password resets set a force-password-change flag; a navigation guard redirects such users to the change-password page until they set their own password. For shared-screen safety, an idle timer signs a user out after three minutes of no activity.", { para: { spacing: { before: 120, after: 120 } } }),
  P([t("The four roles remain the baseline, but access is actually resolved through a configurable "), b("capability permission layer"), t(" (security groups) layered on top — see B.10. The role of each base account maps to a default capability set, so existing behaviour is preserved while custom groups and per-individual overrides become possible.")]),

  H2("B.3  The question-generation pipeline (RAG)"),
  P("When an assessment is created, generation runs as a background task. Knowledge-base and Hybrid sources run the full Retrieval-Augmented Generation pipeline (with a grading loop); AI sources skip retrieval; and Personality and Coding call GPT-4o directly. Retrieval and grading are driven by the assessment title plus the assessor's context prompt — not the bare topic alone — which broadens coverage and reduces false “insufficient context” failures."),
  image("04_rag_pipeline.png", { w: 600, h: 444 }),
  caption("Figure 6 — The RAG generation pipeline and its branches."),
  H3("Pipeline phases"),
  num([b("Retrieve "), t("— the topic is expanded into several sub-queries; Chroma performs dense vector search; BM25 keyword search runs over the candidates; Reciprocal Rank Fusion merges the two rankings; a cross-encoder re-ranks the shortlist (falling back to truncation if the model is unavailable).")]),
  num([b("Grade (reflection loop) "), t("— a single, inexpensive GPT-4o-mini call judges whether the retrieved context actually covers the topic well enough to author the requested questions. Sufficient → proceed; partial → reformulate and re-query (accumulating documents, capped at two re-queries); insufficient → stop and fail honestly, recording what was missing so the Line Manager sees “the source doesn't cover this topic” rather than receiving confidently-wrong questions.")]),
  num([b("Augment "), t("— GPT-4o generates questions in batches of ten. Multiple-choice correct answers are spread evenly across A/B/C/D. Explanations stay grounded in the retrieved context, then add a brief note from well-established common knowledge.")]),
  num([b("Persist "), t("— question rows are written to PostgreSQL.")]),
  H3("Hybrid grounding (KB + web case studies)"),
  P("A Hybrid source retrieves from the chosen knowledge-base document and, in parallel, gathers credible, domain/industry-relevant case-study sources from the web (via a pluggable search provider). The two sets are interleaved into one context, the web sources are saved for provenance, and the grading step is non-fatal for Hybrid — because web and model knowledge are meant to supplement the document by design. If no web provider is configured, Hybrid gracefully degrades to knowledge-base-only."),
  callout("Personality & coding generation", [
    P("Personality assessments generate 60 Likert statements (balanced across five trait dimensions) via direct GPT-4o calls, de-duplicated across batches. Coding assessments generate a problem plus a reference solution per language. Neither uses retrieval.", { after: 0 }),
  ], AVO_L, AVO),

  H2("B.4  Evaluation"),
  bullet([b("Multiple choice "), t("— scored deterministically (instant).")]),
  bullet([b("Written & coding "), t("— evaluated by GPT-4o against a model answer / reference solution, returning a score and feedback.")]),
  bullet([b("Personality "), t("— Likert responses are aggregated into a type code, identity, and per-dimension trait percentages; recomputed on demand, nothing extra persisted.")]),
  bullet([b("Case study (scenario) "), t("— each written answer is graded against the case and rubric. The feedback is enriched with credible external sources (via the configured web-search provider; grounded-only if none is set), and the cited sources are stored alongside it.")]),
  P("Skipped questions are evaluated as unanswered (score 0) so the denominator is always the full question count.", { para: { spacing: { before: 120 } } }),
  callout("Human-assisted verification (case studies)", [
    P("A case-study submission does not receive a final score automatically. The AI produces a draft per-answer score and feedback, and the attempt enters a pending-review state — excluded from the candidate's results and from statistics. A Line Manager opens it from the Reviews queue, optionally edits the per-answer scores and feedback, and approves. Only on approval is the final score computed, the attempt marked evaluated, and the result released. The reviewer and timestamp are recorded.", { after: 0 }),
  ], INDIGO_L, INDIGO),

  H2("B.5  Observability — runs, steps, and the Log Capsule"),
  P("Every generation, indexing, and evaluation is recorded as a PipelineRun. Two layers of detail hang off it:"),
  bullet([b("PipelineStep "), t("— ordered, human-readable phases (Initialise, Retrieve, Augment, Persist…). Their status transitions (ok / warn / error / running / pending) drive the live terminal log.")]),
  bullet([b("PipelineSpan "), t("— a real backing-service call (an OpenAI request, a Chroma query, a Postgres flush) with its true millisecond duration. These build the Log Capsule.")]),
  P("A context variable propagates the active run id across async boundaries, so deeply-nested client calls record spans without passing the run id through every function. The capsule groups spans by backing service — OpenAI (including the grading and case-study-feedback calls), Chroma, PostgreSQL, the cross-encoder, and Web Search — with per-service call counts and total time. Its metadata header carries the origin IP that triggered the transaction, the server IP and host id, start / last-action timestamps, and full provenance: the information source and the reference document or URL behind the run, any web-source count, the assessment name, and (for evaluation runs) the candidate scored.", { para: { spacing: { before: 60 } } }),

  H2("B.6  Data model (selected tables)"),
  table(["Table", "Purpose"], [
    ["users / organisations / departments", "Accounts, tenancy, and department membership (with job title + line manager). Users carry per-individual permission overrides (extra / denied)."],
    ["security_groups / group_memberships", "Configurable capability groups (a list of permission keys) and who belongs to them."],
    ["assessments / questions", "Assessment config and generated questions (is_archived soft-delete flag). For case studies the shared case narrative and any web sources are kept in the assessment's rag_metadata."],
    ["staff_assessments / staff_answers", "A staff member's attempt, answers, score, and status (including pending_review for case studies; reviewed_by / reviewed_at on confirmation; per-answer feedback_sources)."],
    ["knowledge_sources / document_chunks", "Indexed documents and their chunk provenance (vectors live in Chroma)."],
    ["audit_log", "Every significant action with a JSONB detail payload."],
    ["pipeline_runs / pipeline_steps / pipeline_spans", "Observability: transactions, phases, and real per-service spans."],
  ], [3200, 6160]),

  H2("B.7  Deployment & operations"),
  H3("Starting the system"),
  P([code("docker compose up -d"), t("   (run from the backend project root) starts all services; the frontend runs with "), code("npm run dev"), t(". A "), code("proassess.bat"), t(" launcher automates both (paths are derived from the script's location): "), code("proassess.bat"), t(" starts everything, "), code("proassess.bat reload"), t(" force-recreates the API so .env changes take effect and restarts the frontend, and "), code("proassess.bat stop"), t(" shuts both down.")]),
  callout("Critical operational note — environment changes", [
    P([code("docker compose restart"), t(" does NOT reload the .env file — it reuses the container's baked-in environment. After editing .env (for example, the OpenAI key), recreate the container:")], { after: 40 }),
    P([code("docker compose up -d --force-recreate api")], { after: 40 }),
    P([t("Verify with "), code("docker compose exec api printenv OPENAI_API_KEY"), t(".")], { after: 0 }),
  ], AMBER_L, "f59e0b"),
  H3("Schema changes"),
  P([t("Alembic migrations live in "), code("alembic/versions/"), t(". Note that the development convenience of auto-creating tables only creates "), b("missing tables"), t(" — it never adds columns to an existing table. New columns on an existing table must be added by migration (or a one-off "), code("ALTER TABLE … ADD COLUMN IF NOT EXISTS"), t(").")]),
  H3("Time zones"),
  P("Timestamps are stored as UTC and serialized with a 'Z' suffix, so the browser converts them to each viewer's local time automatically."),

  H2("B.8  Troubleshooting"),
  table(["Symptom", "Likely cause & fix"], [
    ["“Question generation timed out with no output.”", "Usually an OpenAI auth/quota issue or a stale key — check the API logs for AuthenticationError; recreate the api container after fixing .env."],
    ["Every /ops endpoint returns “failed to fetch.”", "A new column was added to an existing table without an ALTER — every SELECT then references a missing column. Run the ALTER TABLE for the new columns."],
    ["Results don't appear after submitting.", "A serialization error rolls back the submit transaction. Check the API logs around /submit."],
    ["Knowledge-base questions feel generic.", "The document may not be indexed (re-upload / re-index), or retrieval returned no chunks and fell back to GPT general knowledge."],
    ["Timestamps look shifted by hours.", "A datetime is being serialized without the UTC marker; all timestamps should end in 'Z'."],
  ], [3100, 6260]),

  H2("B.9  Case-study (scenario) assessments"),
  P("A case study is a single, knowledge-base-grounded case (the shared stimulus) followed by a few open-ended analytical questions. It is available under both assessment types and uses the KB or Hybrid source only."),
  bullet([b("Generation "), t("— two stages on the graded context: GPT-4o first authors one realistic case grounded in the document (and, for Hybrid, web case-study sources), then authors 5–8 analytical questions, each with a model answer and a multi-criterion marking rubric.")]),
  bullet([b("Taking "), t("— the candidate reads the case in a sticky side panel and writes an analysis for each question; on submit they see a “Submitted for review” screen.")]),
  bullet([b("Feedback & review "), t("— each answer receives a draft AI score and rich feedback (grounded, enriched with credible web sources where available). The attempt stays in pending-review until a Line Manager confirms it (B.4). Generation and feedback are fully observable in the Log Capsule, including the grading decision and any web-search spans.")]),
  P([t("The grading reflection loop (B.3) protects quality here too: a case will not be generated from a document that does not cover the topic. Full design notes live in "), code("docs/CASE_STUDY_FEATURE.md"), t(".")]),

  H2("B.10  Security groups & permissions"),
  P("Access is resolved through a configurable capability layer on top of the four roles. A fixed catalogue of capability keys is gated throughout the app; security groups hold a set of those keys; users inherit the union of their groups' permissions, plus per-individual grants and denials."),
  P([b("Effective permissions"), t(" = role default  ∪  permissions of every group the user belongs to  ∪  the user's extra grants  −  the user's denials. The role→default mapping means existing accounts work with no group membership, while custom groups and overrides take effect immediately. Three default groups are seeded per organisation:")]),
  table(["Default group", "Capabilities"], [
    ["Ops (≈ system_admin)", "All capabilities, including system-process observability and the Knowledge Chunks metric."],
    ["People & Culture (≈ hr_admin)", "Everything except system-process observability: stats, org-wide user & department management, knowledge-base read/write/delete, assessment creation & distribution, and org-wide per-individual results."],
    ["Line Managers (≈ lm)", "Knowledge-base read-only, create / distribute / review assessments, and team (own-reports) results."],
  ], [2700, 6660]),
  P([t("Enforcement examples: Line Managers can browse but not modify the knowledge base ("), code("kb.view"), t(" vs "), code("kb.manage"), t("), and may only target departments they line-manage. The "), b("Groups"), t(" admin page (for holders of "), code("users.manage"), t(") creates and edits groups, toggles their capabilities, manages membership, and sets per-individual overrides; default groups cannot be deleted.")]),

  H2("B.11  Known limitations & roadmap"),
  bullet([b("Done since v1.0: "), t("take-time target enforcement (only targeted staff can open an assessment), the RAG grading loop, case-study assessments with human-assisted review, Hybrid grounding, pre-deploy question preview & regenerate, configurable security groups, and by-department results.")]),
  bullet("MinIO object storage: uploaded files are processed in memory and not yet persisted to object storage."),
  bullet("Live web sourcing for case-study feedback requires a configured search provider; without one, feedback is grounded-only."),
  bullet("Observability streaming: the live-tail polls the database each second; a future event-driven push (broker/WebSocket) would remove the poll."),
  bullet("Profile photos: stored per-user in browser local storage, not yet synced to the backend."),
  bullet("Automated tests: a formal test suite is not yet in place."),
];

// closing
const closing = [
  new Paragraph({ children: [new PageBreak()] }),
  H1("Appendix — Glossary"),
  table(["Term", "Meaning"], [
    ["RAG", "Retrieval-Augmented Generation — grounding AI output in retrieved source documents."],
    ["Embedding", "A numeric vector representing text meaning, used for similarity search in Chroma."],
    ["BM25", "A keyword-ranking algorithm combined with vector search for better retrieval."],
    ["RRF", "Reciprocal Rank Fusion — merges two ranked lists into one."],
    ["Cross-encoder", "A model that re-scores query/document pairs for precise final ranking."],
    ["Grading loop", "A reflection step that judges whether retrieved context covers the topic, re-queries on partial coverage, and fails honestly on insufficient coverage."],
    ["Likert scale", "A 7-point agree/disagree response scale used by personality assessments."],
    ["Case study (scenario)", "A shared, KB-grounded case followed by written analytical questions, graded by AI and confirmed by a reviewer."],
    ["Hybrid source", "Grounding that combines a knowledge-base document with credible, domain-relevant web case-study sources."],
    ["Human-assisted verification", "The review gate where a Line Manager confirms (or edits) AI-drafted case-study scores before they are released."],
    ["Security group / capability", "A configurable set of permission keys; users inherit the union of their groups' capabilities, with per-individual overrides."],
    ["Pipeline run / step / span", "A tracked transaction, its phases, and its real per-service calls."],
    ["Log Capsule", "A self-contained transaction trace: metadata (incl. source document/URL and candidate) + spans grouped by service."],
  ], [2400, 6960]),
  spacer(200),
  new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 400 },
    children: [new TextRun({ text: "ProAssess — by Atocado", bold: true, color: AVO, size: 22 })] }),
  new Paragraph({ alignment: AlignmentType.CENTER,
    children: [new TextRun({ text: "End of manual.", italics: true, color: GREY, size: 18 })] }),
];

// ════════════════════════════════════════════════════════════════
// DOCUMENT
// ════════════════════════════════════════════════════════════════
const doc = new Document({
  creator: "Atocado",
  title: "ProAssess — User Guide & Technical Manual",
  styles: {
    default: { document: { run: { font: "Arial", size: 22, color: "1e293b" } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 34, bold: true, color: INDIGO, font: "Arial" },
        paragraph: { spacing: { before: 360, after: 200 }, outlineLevel: 0,
          border: { bottom: { style: BorderStyle.SINGLE, size: 8, color: AVO, space: 6 } } } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 27, bold: true, color: SLATE, font: "Arial" },
        paragraph: { spacing: { before: 260, after: 130 }, outlineLevel: 1 } },
      { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 23, bold: true, color: AVO, font: "Arial" },
        paragraph: { spacing: { before: 180, after: 90 }, outlineLevel: 2 } },
    ],
  },
  numbering: {
    config: [
      { reference: "bullets", levels: [
        { level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 460, hanging: 280 } } } },
        { level: 1, format: LevelFormat.BULLET, text: "◦", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 920, hanging: 280 } } } },
      ] },
      { reference: "steps", levels: [
        { level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 460, hanging: 280 } } } },
      ] },
    ],
  },
  sections: [
    // Cover (no header/footer, no page number)
    { properties: { page: { size: { width: 12240, height: 15840 }, margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 } } },
      children: cover },
    // Body
    { properties: { page: { size: { width: 12240, height: 15840 }, margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 } } },
      headers: { default: new Header({ children: [new Paragraph({
        border: { bottom: { style: BorderStyle.SINGLE, size: 4, color: "e2e8f0", space: 4 } },
        children: [
          new TextRun({ text: "ProAssess", bold: true, color: INDIGO, size: 18 }),
          new TextRun({ text: "\tUser Guide & Technical Manual", color: GREY, size: 18 }),
        ],
        tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX }],
      })] }) },
      footers: { default: new Footer({ children: [new Paragraph({
        alignment: AlignmentType.CENTER,
        children: [new TextRun({ text: "Page ", color: GREY, size: 18 }),
          new TextRun({ children: [PageNumber.CURRENT], color: GREY, size: 18 }),
          new TextRun({ text: " · by Atocado", color: GREY, size: 18 })],
      })] }) },
      children: [...toc, ...intro, ...partA, ...partB, ...closing] },
  ],
});

Packer.toBuffer(doc).then((buf) => {
  const out = path.join(__dirname, "..", "ProAssess_Manual.docx");
  fs.writeFileSync(out, buf);
  console.log("wrote", out, (buf.length / 1024).toFixed(0) + "KB");
});
