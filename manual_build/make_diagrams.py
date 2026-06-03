"""
Generate well-designed diagrams for the ProAssess manual.
Consistent theme: rounded boxes, brand palette, clean sans-serif, soft shadows.
Output: PNG @ 200 DPI into ./diagrams
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Circle
from matplotlib.lines import Line2D
import os

OUT = os.path.join(os.path.dirname(__file__), "diagrams")
os.makedirs(OUT, exist_ok=True)

# ── Palette ───────────────────────────────────────────────────────
INDIGO   = "#4f46e5"
INDIGO_L = "#e0e7ff"
AVO      = "#65a30d"
AVO_L    = "#ecfccb"
SKY      = "#0ea5e9"
SKY_L    = "#e0f2fe"
VIOLET   = "#8b5cf6"
VIOLET_L = "#ede9fe"
AMBER    = "#f59e0b"
AMBER_L  = "#fef3c7"
RED      = "#dc2626"
SLATE    = "#334155"
SLATE_L  = "#f1f5f9"
GREY     = "#94a3b8"
WHITE    = "#ffffff"

plt.rcParams["font.family"] = "DejaVu Sans"


def box(ax, x, y, w, h, text, fc, ec, tc="#1e293b", fs=11, bold=False, rad=0.06):
    p = FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0.01,rounding_size={rad}",
                       fc=fc, ec=ec, lw=1.6, mutation_aspect=1)
    ax.add_patch(p)
    ax.text(x + w/2, y + h/2, text, ha="center", va="center",
            fontsize=fs, color=tc, fontweight="bold" if bold else "normal",
            zorder=5, wrap=True)
    return (x + w/2, y + h/2)


def arrow(ax, p1, p2, color=SLATE, style="-|>", lw=1.8, rad=0.0, ls="-"):
    a = FancyArrowPatch(p1, p2, arrowstyle=style, mutation_scale=16,
                        color=color, lw=lw, connectionstyle=f"arc3,rad={rad}",
                        linestyle=ls, zorder=2)
    ax.add_patch(a)


def title(ax, t, sub=None):
    ax.text(0.5, 0.97, t, transform=ax.transAxes, ha="center", va="top",
            fontsize=16, fontweight="bold", color=INDIGO)
    if sub:
        ax.text(0.5, 0.925, sub, transform=ax.transAxes, ha="center", va="top",
                fontsize=10.5, color=GREY)


def finish(fig, name):
    fig.savefig(os.path.join(OUT, name), dpi=200, bbox_inches="tight",
                facecolor="white", pad_inches=0.25)
    plt.close(fig)
    print("wrote", name)


# ══════════════════════════════════════════════════════════════════
# 1. System architecture
# ══════════════════════════════════════════════════════════════════
def diagram_architecture():
    fig, ax = plt.subplots(figsize=(10, 7.2))
    ax.set_xlim(0, 100); ax.set_ylim(0, 100); ax.axis("off")
    title(ax, "ProAssess — System Architecture",
          "Next.js frontend · FastAPI backend · five backing services, all on Docker Compose")

    # Frontend
    box(ax, 6, 78, 88, 9, "Frontend  —  Next.js 16 (App Router) · TypeScript · Tailwind v4",
        INDIGO_L, INDIGO, INDIGO, 11.5, bold=True)
    ax.text(50, 74.5, "Staff · Line Manager · HR Admin · System Admin  (JWT, 4 roles)",
            ha="center", fontsize=9.5, color=GREY)

    # API
    box(ax, 6, 56, 88, 12, "", AVO_L, AVO, SLATE, 12, bold=True)
    ax.text(50, 65.4, "Backend API  —  FastAPI (async)  ·  /api/v1",
            ha="center", fontsize=12, fontweight="bold", color=SLATE)
    for i, r in enumerate(["auth", "assessments", "knowledge", "admin", "users", "ops"]):
        box(ax, 9 + i*14.2, 57.4, 12.6, 4.6, r, WHITE, AVO, SLATE, 9)

    # connect FE-API
    arrow(ax, (50, 78), (50, 68), INDIGO, lw=2.2)
    ax.text(53, 73, "HTTPS / JWT", fontsize=8.5, color=GREY)

    # Services row
    svcs = [
        ("PostgreSQL", "users, assessments,\nresults, audit, spans", "#336791", "#dbeafe"),
        ("Chroma", "vector store\n(embeddings)", VIOLET, VIOLET_L),
        ("Redis", "cache / sessions\n(available)", "#dc382d", "#fee2e2"),
        ("MinIO", "object storage\n(planned)", "#c72c48", "#fce7f3"),
        ("OpenAI", "GPT-4o + embeddings\n(external API)", AVO, AVO_L),
    ]
    w = 16.5; gap = (88 - 5*w) / 4
    for i, (n, d, ec, fc) in enumerate(svcs):
        x = 6 + i*(w+gap)
        cx, _ = box(ax, x, 38, w, 12, "", fc, ec, rad=0.05)
        ax.text(x + w/2, 47, n, ha="center", fontsize=10.5, fontweight="bold", color=ec)
        ax.text(x + w/2, 42.5, d, ha="center", fontsize=8, color=SLATE)
        arrow(ax, (50, 56), (x + w/2, 50), GREY, lw=1.3, rad=0.0)

    # Docker boundary
    db = FancyBboxPatch((3, 33), 94, 55, boxstyle="round,pad=0.2,rounding_size=1.2",
                        fc="none", ec=SKY, lw=1.6, ls=(0, (6, 4)))
    ax.add_patch(db)
    ax.text(5, 86.2, "[ Docker Compose ]", fontsize=9.5, color=SKY, fontweight="bold")

    # RAG note
    box(ax, 18, 19, 64, 9,
        "RAG pipeline (GPT-4o):  query expansion → Chroma dense search → BM25 → RRF → cross-encoder rerank → generate",
        SLATE_L, GREY, SLATE, 8.6)
    arrow(ax, (50, 38), (50, 28), GREY, lw=1.3)

    ax.text(50, 13, "Observability: every generation / indexing / evaluation is recorded as a PipelineRun\n"
                    "with phased steps + real per-service spans (the Log Capsule).",
            ha="center", fontsize=8.5, color=GREY, style="italic")
    finish(fig, "01_architecture.png")


# ══════════════════════════════════════════════════════════════════
# 2. Roles & permissions
# ══════════════════════════════════════════════════════════════════
def diagram_roles():
    fig, ax = plt.subplots(figsize=(10, 6.6))
    ax.set_xlim(0, 100); ax.set_ylim(0, 100); ax.axis("off")
    title(ax, "Roles & Capabilities", "Four roles, each with a tailored landing page and permission set")

    roles = [
        ("Staff", SKY, SKY_L, "/staff/assessments", [
            "Take assigned assessments", "7-pt Likert / MCQ / written / code editor",
            "View own scores & feedback", "Personality profile + summary"]),
        ("Line Manager", AVO, AVO_L, "/lm/assessments", [
            "Create assessments (RAG-generated)", "Deploy / cancel / delete",
            "Share with depts / individuals", "View managed staff profiles"]),
        ("HR Admin", INDIGO, INDIGO_L, "/hr", [
            "Org stats & per-assessment averages", "Manage users & departments",
            "Knowledge base (upload / index)", "Audit log · staff profiles"]),
        ("System Admin", VIOLET, VIOLET_L, "/ops", [
            "Everything HR can do", "System Processes dashboard",
            "Live logs + Log Capsule traces", "Grant system_admin role"]),
    ]
    w = 22; gap = (92 - 4*w) / 3
    for i, (name, ec, fc, land, caps) in enumerate(roles):
        x = 4 + i*(w+gap)
        box(ax, x, 16, w, 70, "", fc, ec, rad=0.05)
        ax.text(x + w/2, 80, name, ha="center", fontsize=12.5, fontweight="bold", color=ec)
        ax.text(x + w/2, 75.5, land, ha="center", fontsize=8.5, color=SLATE,
                family="monospace")
        ax.plot([x+2, x+w-2], [73, 73], color=ec, lw=1)
        for j, c in enumerate(caps):
            ax.text(x + 1.6, 68 - j*9, "•", fontsize=11, color=ec, va="top")
            ax.text(x + 4, 68 - j*9, c, fontsize=8.3, color=SLATE, va="top", wrap=True)
    ax.text(50, 9, "Roles are hierarchical in reach: System Admin ⊇ HR Admin ⊇ Line Manager privileges; Staff is the assessed user.",
            ha="center", fontsize=8.6, color=GREY, style="italic")
    finish(fig, "02_roles.png")


# ══════════════════════════════════════════════════════════════════
# 3. Assessment lifecycle (state flow)
# ══════════════════════════════════════════════════════════════════
def diagram_lifecycle():
    fig, ax = plt.subplots(figsize=(10, 5.6))
    ax.set_xlim(0, 100); ax.set_ylim(0, 100); ax.axis("off")
    title(ax, "Assessment Lifecycle", "From creation through evaluation, with archival of completed results")

    states = [
        ("DRAFT", 6, AMBER, AMBER_L, "RAG generates\nquestions"),
        ("DEPLOYED", 30, AVO, AVO_L, "visible to\ntargeted staff"),
        ("IN PROGRESS", 54, SKY, SKY_L, "staff taking\n(per session)"),
        ("EVALUATED", 78, INDIGO, INDIGO_L, "scored + feedback\nstored"),
    ]
    y = 55; w = 17; h = 12
    centers = []
    for name, x, ec, fc, sub in states:
        box(ax, x, y, w, h, name, fc, ec, ec, 11, bold=True)
        ax.text(x + w/2, y - 5, sub, ha="center", fontsize=8.2, color=GREY)
        centers.append((x + w/2, y + h/2, x, x + w))
    for i in range(len(centers)-1):
        arrow(ax, (centers[i][3], y + h/2), (centers[i+1][2], y + h/2), SLATE, lw=2)

    # branch: cancel + delete
    box(ax, 30, 26, 17, 10, "CANCELLED", "#fee2e2", RED, RED, 10.5, bold=True)
    arrow(ax, (38.5, y), (38.5, 36), RED, lw=1.6, ls="--")
    ax.text(48.5, 31, "LM cancels (reason logged)", fontsize=8, color=GREY, va="center")

    box(ax, 66, 26, 26, 10,
        "DELETE → archive if completed\n(results preserved) · else purge",
        SLATE_L, GREY, SLATE, 8.4)
    arrow(ax, (86.5, y), (84, 36), GREY, lw=1.4, ls="--")

    ax.text(50, 12, "Deleting an assessment that has completed attempts ARCHIVES it (is_archived=True): questions + results + feedback are kept,\nhidden from lists; only incomplete attempts are purged.",
            ha="center", fontsize=8.4, color=GREY, style="italic")
    finish(fig, "03_lifecycle.png")


# ══════════════════════════════════════════════════════════════════
# 4. RAG generation pipeline (sequence-ish)
# ══════════════════════════════════════════════════════════════════
def diagram_rag():
    fig, ax = plt.subplots(figsize=(10, 7.4))
    ax.set_xlim(0, 100); ax.set_ylim(0, 100); ax.axis("off")
    title(ax, "Question Generation Pipeline (RAG)",
          "How a knowledge-base assessment is generated — each phase is tracked for the ops dashboard")

    steps = [
        ("1 · Initialise", "Read topic, source_id, format from the draft", SLATE_L, GREY),
        ("2 · Retrieve  (Chroma + OpenAI)", "expand query → dense vector search → BM25 → RRF fusion → cross-encoder rerank", VIOLET_L, VIOLET),
        ("3 · Augment  (GPT-4o)", "generate questions in batches of 10 · MCQ answer-spread · grounded + enriched explanations", AVO_L, AVO),
        ("4 · Persist  (PostgreSQL)", "write Question rows · flush", "#dbeafe", "#336791"),
    ]
    y = 70; h = 9.5
    cy = []
    for i, (t, d, fc, ec) in enumerate(steps):
        yy = y - i*15
        box(ax, 12, yy, 76, h, "", fc, ec, rad=0.05)
        ax.text(16, yy + h/2, t, ha="left", va="center", fontsize=11, fontweight="bold", color=ec)
        ax.text(16, yy - 2.4, d, ha="left", va="center", fontsize=8.4, color=SLATE)
        cy.append(yy)
        if i > 0:
            arrow(ax, (50, cy[i-1]), (50, yy + h), SLATE, lw=1.8)

    # side notes
    box(ax, 90.5, 70, 8.5, h, "AI / Industry\nskip retrieval", WHITE, GREY, GREY, 7.4)
    arrow(ax, (88, 55), (90.5, 70), GREY, lw=1.2, rad=-0.3, ls="--")
    box(ax, 90.5, 24.5, 8.5, h, "Personality\n& Coding:\ndirect GPT", WHITE, AVO, AVO, 7.4)

    ax.text(50, 9, "Branches:  Personality (60 Likert items) and Coding both bypass retrieval — a direct GPT-4o call.\n"
                   "Generation runs in a background task; the manage page live-tails progress with a latency gauge.",
            ha="center", fontsize=8.4, color=GREY, style="italic")
    finish(fig, "04_rag_pipeline.png")


# ══════════════════════════════════════════════════════════════════
# 5. Take → submit → evaluate → feedback (candidate flow)
# ══════════════════════════════════════════════════════════════════
def diagram_takeflow():
    fig, ax = plt.subplots(figsize=(10, 5.2))
    ax.set_xlim(0, 100); ax.set_ylim(0, 100); ax.axis("off")
    title(ax, "Candidate Flow — Take, Submit, Evaluate", "What a staff member experiences, and what happens server-side")

    steps = [
        ("Pre-flight\nsystem check", SKY, SKY_L),
        ("Take\nassessment\n(timed)", AVO, AVO_L),
        ("Confirm &\nsubmit", INDIGO, INDIGO_L),
        ("Evaluate\n(MCQ instant ·\nwritten/code GPT)", VIOLET, VIOLET_L),
        ("Feedback /\nprofile", AMBER, AMBER_L),
    ]
    w = 16; y = 50; h = 16
    gap = (92 - 5*w)/4
    cs = []
    for i, (t, ec, fc) in enumerate(steps):
        x = 4 + i*(w+gap)
        box(ax, x, y, w, h, t, fc, ec, ec, 9.2, bold=True)
        cs.append((x, x+w))
    for i in range(4):
        arrow(ax, (cs[i][1], y+h/2), (cs[i+1][0], y+h/2), SLATE, lw=2)

    # annotations
    ann = [
        (12, "latency · browser ·\nfirewall checklist"),
        (35.5, "session + timer\nstart only after\ncheck passes"),
        (58.5, "skipped Qs\ncount as 0"),
        (82, "personality →\ntype + summary;\nscored → strengths\n/ weaknesses"),
    ]
    for x, t in ann:
        ax.text(x, 40, t, ha="center", fontsize=7.6, color=GREY, va="top")

    ax.text(50, 11, "The timer (and the StaffAssessment session) only begins once the pre-flight check passes,\nso connectivity validation never eats into assessment time.",
            ha="center", fontsize=8.4, color=GREY, style="italic")
    finish(fig, "05_take_flow.png")


# ══════════════════════════════════════════════════════════════════
# 6. Observability — runs, steps, capsule
# ══════════════════════════════════════════════════════════════════
def diagram_capsule():
    fig, ax = plt.subplots(figsize=(10, 6.6))
    ax.set_xlim(0, 100); ax.set_ylim(0, 100); ax.axis("off")
    title(ax, "Observability — System Processes & the Log Capsule",
          "Every transaction is traced: phased steps for the live log, real spans for the capsule")

    # PipelineRun
    box(ax, 8, 74, 84, 11, "PipelineRun   (one transaction: generation · indexing · evaluation)",
        AVO_L, AVO, SLATE, 11, bold=True)
    ax.text(50, 70.5, "id · kind · label · status · origin_ip · server_ip · system_id · started/finished",
            ha="center", fontsize=8.2, color=GREY)

    # Steps (left) and Spans (right)
    box(ax, 8, 40, 40, 24, "", SLATE_L, GREY, rad=0.04)
    ax.text(28, 60, "PipelineStep  ×N", ha="center", fontsize=10.5, fontweight="bold", color=SLATE)
    ax.text(28, 56.5, "phased checklist → live terminal log", ha="center", fontsize=8, color=GREY)
    for i, (s, c) in enumerate([("✓ Initialise", AVO), ("✓ Retrieve", AVO),
                                 ("▌ Augment (running)", SKY), ("· Persist (pending)", GREY)]):
        ax.text(12, 52 - i*3.4, s, fontsize=8.6, color=c, family="monospace", va="center")

    box(ax, 52, 40, 40, 24, "", VIOLET_L, VIOLET, rad=0.04)
    ax.text(72, 60, "PipelineSpan  ×M", ha="center", fontsize=10.5, fontweight="bold", color=VIOLET)
    ax.text(72, 56.5, "real per-service call + true duration", ha="center", fontsize=8, color=GREY)
    for i, (s, c) in enumerate([("openai  chat.completion  1.8s", AVO),
                                 ("chroma  similarity_search  240ms", VIOLET),
                                 ("postgres  INSERT questions  12ms", "#336791")]):
        ax.text(55, 51.5 - i*3.6, s, fontsize=7.7, color=c, family="monospace", va="center")

    arrow(ax, (40, 74), (28, 64), GREY, lw=1.5)
    arrow(ax, (60, 74), (72, 64), GREY, lw=1.5)

    # Capsule output
    box(ax, 20, 14, 60, 16, "", INDIGO_L, INDIGO, rad=0.05)
    ax.text(50, 26, "Log Capsule", ha="center", fontsize=12, fontweight="bold", color=INDIGO)
    ax.text(50, 21.5, "metadata header (services · origin/server IP · system id · timestamps)",
            ha="center", fontsize=8.3, color=SLATE)
    ax.text(50, 18, "+ spans grouped by service, each with call count & total time",
            ha="center", fontsize=8.3, color=SLATE)
    arrow(ax, (72, 40), (62, 30), VIOLET, lw=1.8)
    ax.text(50, 7.5, "The capsule link appears on generation & evaluation runs in the ops dashboard — a self-contained transaction trace.",
            ha="center", fontsize=8.4, color=GREY, style="italic")
    finish(fig, "06_capsule.png")


if __name__ == "__main__":
    diagram_architecture()
    diagram_roles()
    diagram_lifecycle()
    diagram_rag()
    diagram_takeflow()
    diagram_capsule()
    print("\nAll diagrams generated in", OUT)
