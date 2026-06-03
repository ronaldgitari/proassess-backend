"""
ProAssess Documentation PDF Generator
Produces a professionally styled PDF from DOCUMENTATION.md
"""

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm, cm
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle,
    HRFlowable, KeepTogether, Preformatted
)
from reportlab.platypus.tableofcontents import TableOfContents
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.lib.colors import HexColor
import re
import os

# ── Colour palette ────────────────────────────────────────────────
INDIGO       = HexColor("#4F46E5")   # primary brand
INDIGO_DARK  = HexColor("#3730A3")   # headings
INDIGO_LIGHT = HexColor("#EEF2FF")   # section bg / table header
SLATE        = HexColor("#1E293B")   # body text
SLATE_LIGHT  = HexColor("#64748B")   # secondary text / captions
BORDER       = HexColor("#E2E8F0")   # table borders
CODE_BG      = HexColor("#F1F5F9")   # code block background
CODE_TEXT    = HexColor("#0F172A")   # code text
GREEN        = HexColor("#16A34A")
AMBER        = HexColor("#D97706")
RED          = HexColor("#DC2626")
WHITE        = colors.white

PAGE_W, PAGE_H = A4

# ── Page template with header/footer ─────────────────────────────

class DocCanvas:
    def __init__(self, title="ProAssess Documentation"):
        self.title = title

    def __call__(self, canvas, doc):
        canvas.saveState()
        w, h = PAGE_W, PAGE_H

        # Header bar
        canvas.setFillColor(INDIGO_DARK)
        canvas.rect(0, h - 18*mm, w, 18*mm, fill=1, stroke=0)

        canvas.setFillColor(WHITE)
        canvas.setFont("Helvetica-Bold", 9)
        canvas.drawString(20*mm, h - 11*mm, self.title)
        canvas.setFont("Helvetica", 8)
        canvas.drawRightString(w - 20*mm, h - 11*mm, "Confidential — Internal Use")

        # Footer bar
        canvas.setFillColor(INDIGO_LIGHT)
        canvas.rect(0, 0, w, 12*mm, fill=1, stroke=0)
        canvas.setFillColor(SLATE_LIGHT)
        canvas.setFont("Helvetica", 7.5)
        canvas.drawString(20*mm, 4*mm, "ProAssess Platform  ·  Full Project Documentation")
        canvas.setFont("Helvetica-Bold", 8)
        canvas.setFillColor(INDIGO_DARK)
        canvas.drawRightString(w - 20*mm, 4*mm, f"Page {doc.page}")

        # Thin accent line under header
        canvas.setStrokeColor(INDIGO)
        canvas.setLineWidth(0.5)
        canvas.line(0, h - 18*mm, w, h - 18*mm)

        canvas.restoreState()


# ── Custom TOC with clickable entries ────────────────────────────

class ProTOC(TableOfContents):
    def notify(self, kind, stuff):
        if kind == "TOCEntry":
            self.addEntry(*stuff)

    def addEntry(self, level, text, pageNum, key=None):
        style = self.levelStyles[min(level, len(self.levelStyles)-1)]
        if key:
            e = Paragraph(f'<a href="#{key}">{text}</a>', style)
        else:
            e = Paragraph(text, style)
        self._entries.append((level, text, pageNum, e))


# ── Style definitions ─────────────────────────────────────────────

def make_styles():
    base = getSampleStyleSheet()

    styles = {
        "cover_title": ParagraphStyle(
            "cover_title",
            fontName="Helvetica-Bold",
            fontSize=32,
            textColor=WHITE,
            leading=40,
            alignment=TA_CENTER,
        ),
        "cover_subtitle": ParagraphStyle(
            "cover_subtitle",
            fontName="Helvetica",
            fontSize=14,
            textColor=HexColor("#C7D2FE"),
            leading=20,
            alignment=TA_CENTER,
        ),
        "cover_meta": ParagraphStyle(
            "cover_meta",
            fontName="Helvetica",
            fontSize=10,
            textColor=HexColor("#A5B4FC"),
            alignment=TA_CENTER,
        ),
        "toc_title": ParagraphStyle(
            "toc_title",
            fontName="Helvetica-Bold",
            fontSize=20,
            textColor=INDIGO_DARK,
            leading=28,
            spaceAfter=16,
        ),
        "toc_h1": ParagraphStyle(
            "toc_h1",
            fontName="Helvetica-Bold",
            fontSize=10,
            textColor=SLATE,
            leading=16,
            leftIndent=0,
            spaceBefore=4,
        ),
        "toc_h2": ParagraphStyle(
            "toc_h2",
            fontName="Helvetica",
            fontSize=9,
            textColor=SLATE_LIGHT,
            leading=14,
            leftIndent=16,
        ),
        "h1": ParagraphStyle(
            "h1",
            fontName="Helvetica-Bold",
            fontSize=20,
            textColor=WHITE,
            leading=26,
            spaceBefore=0,
            spaceAfter=6,
            backColor=INDIGO_DARK,
            leftIndent=-20*mm,
            rightIndent=-20*mm,
            firstLineIndent=0,
            borderPadding=(8, 20, 8, 20),
        ),
        "h2": ParagraphStyle(
            "h2",
            fontName="Helvetica-Bold",
            fontSize=14,
            textColor=INDIGO_DARK,
            leading=20,
            spaceBefore=18,
            spaceAfter=6,
            borderPadding=(0, 0, 4, 0),
        ),
        "h3": ParagraphStyle(
            "h3",
            fontName="Helvetica-Bold",
            fontSize=11,
            textColor=SLATE,
            leading=16,
            spaceBefore=12,
            spaceAfter=4,
        ),
        "body": ParagraphStyle(
            "body",
            fontName="Helvetica",
            fontSize=9.5,
            textColor=SLATE,
            leading=15,
            spaceAfter=6,
            alignment=TA_JUSTIFY,
        ),
        "bullet": ParagraphStyle(
            "bullet",
            fontName="Helvetica",
            fontSize=9.5,
            textColor=SLATE,
            leading=15,
            spaceAfter=3,
            leftIndent=14,
            bulletIndent=4,
        ),
        "code": ParagraphStyle(
            "code",
            fontName="Courier",
            fontSize=8,
            textColor=CODE_TEXT,
            leading=12,
            backColor=CODE_BG,
            leftIndent=0,
            rightIndent=0,
            spaceBefore=6,
            spaceAfter=6,
            borderPadding=(8, 10, 8, 10),
        ),
        "caption": ParagraphStyle(
            "caption",
            fontName="Helvetica-Oblique",
            fontSize=8,
            textColor=SLATE_LIGHT,
            leading=12,
            alignment=TA_CENTER,
            spaceAfter=8,
        ),
        "note": ParagraphStyle(
            "note",
            fontName="Helvetica-Oblique",
            fontSize=9,
            textColor=HexColor("#1E40AF"),
            leading=14,
            backColor=HexColor("#EFF6FF"),
            borderPadding=(6, 8, 6, 8),
            spaceAfter=8,
        ),
    }
    return styles


# ── Table helpers ─────────────────────────────────────────────────

def make_table(rows, col_widths=None, header=True):
    """Build a styled ReportLab table from a list of row lists."""
    styles_list = [
        ("BACKGROUND", (0, 0), (-1, 0), INDIGO_DARK if header else INDIGO_LIGHT),
        ("TEXTCOLOR",  (0, 0), (-1, 0), WHITE if header else INDIGO_DARK),
        ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",   (0, 0), (-1, 0), 8.5),
        ("ALIGN",      (0, 0), (-1, 0), "LEFT"),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
        ("TOPPADDING",    (0, 0), (-1, 0), 8),
        ("BACKGROUND", (0, 1), (-1, -1), WHITE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, INDIGO_LIGHT]),
        ("FONTNAME",   (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",   (0, 1), (-1, -1), 8.5),
        ("TEXTCOLOR",  (0, 1), (-1, -1), SLATE),
        ("ALIGN",      (0, 1), (-1, -1), "LEFT"),
        ("TOPPADDING",    (0, 1), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 6),
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
        ("GRID",       (0, 0), (-1, -1), 0.5, BORDER),
        ("ROWHEIGHT",  (0, 0), (-1, -1), None),
    ]
    t = Table(rows, colWidths=col_widths, repeatRows=1 if header else 0)
    t.setStyle(TableStyle(styles_list))
    return t


# ── Markdown parser → ReportLab flowables ─────────────────────────

def parse_markdown(md_text, styles):
    """
    Converts a subset of Markdown into a list of ReportLab flowables.
    Handles: H1/H2/H3, bold/italic inline, bullet lists, code blocks,
    tables, blockquotes, horizontal rules, and plain paragraphs.
    """
    flowables = []
    lines = md_text.split("\n")
    i = 0
    usable_width = PAGE_W - 40*mm  # left+right margins 20mm each

    def inline(text):
        """Convert inline markdown (**bold**, *italic*, `code`) to ReportLab XML."""
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
        text = re.sub(r'\*(.+?)\*',     r'<i>\1</i>', text)
        text = re.sub(r'`([^`]+)`',     r'<font name="Courier" size="8.5" color="#0F172A"><b>\1</b></font>', text)
        return text

    while i < len(lines):
        line = lines[i]

        # ── Horizontal rule
        if re.match(r'^---+$', line.strip()):
            flowables.append(HRFlowable(width="100%", thickness=0.5,
                                        color=BORDER, spaceAfter=6, spaceBefore=6))
            i += 1
            continue

        # ── H1
        m = re.match(r'^# (.+)', line)
        if m:
            text = m.group(1)
            # Strip numbering for display  e.g. "1. Project Overview"
            clean = re.sub(r'^\d+\.\s*', '', text)
            flowables.append(Spacer(1, 8))
            p = Paragraph(clean.upper(), styles["h1"])
            p._bookmarkName = re.sub(r'[^a-z0-9]', '-', clean.lower())
            flowables.append(p)
            flowables.append(Spacer(1, 6))
            i += 1
            continue

        # ── H2
        m = re.match(r'^## (.+)', line)
        if m:
            text = inline(m.group(1))
            flowables.append(Paragraph(text, styles["h2"]))
            flowables.append(HRFlowable(width="100%", thickness=1,
                                        color=INDIGO, spaceAfter=4, spaceBefore=0))
            i += 1
            continue

        # ── H3
        m = re.match(r'^### (.+)', line)
        if m:
            flowables.append(Paragraph(inline(m.group(1)), styles["h3"]))
            i += 1
            continue

        # ── Code block
        if line.strip().startswith("```"):
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing ```
            code_text = "\n".join(code_lines)
            # Render each line as a separate Preformatted so the block can split across pages
            code_style = ParagraphStyle(
                "code_line",
                fontName="Courier",
                fontSize=7.5,
                textColor=CODE_TEXT,
                leading=11,
                backColor=CODE_BG,
                leftIndent=8,
                rightIndent=8,
                spaceBefore=0,
                spaceAfter=0,
                borderPadding=(0, 0, 0, 0),
            )
            # Top border row
            top = Table([[""]], colWidths=[usable_width])
            top.setStyle(TableStyle([
                ("BACKGROUND",    (0,0),(-1,-1), CODE_BG),
                ("TOPPADDING",    (0,0),(-1,-1), 6),
                ("BOTTOMPADDING", (0,0),(-1,-1), 0),
                ("LINEABOVE",     (0,0),(-1,-1), 0.5, HexColor("#CBD5E1")),
                ("LINEBEFORE",    (0,0),(-1,-1), 0.5, HexColor("#CBD5E1")),
                ("LINEAFTER",     (0,0),(-1,-1), 0.5, HexColor("#CBD5E1")),
            ]))
            flowables.append(top)
            for cl in code_lines:
                safe = cl.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
                if not safe:
                    safe = " "
                flowables.append(Preformatted(safe, code_style))
            # Bottom border row
            bot = Table([[""]], colWidths=[usable_width])
            bot.setStyle(TableStyle([
                ("BACKGROUND",    (0,0),(-1,-1), CODE_BG),
                ("TOPPADDING",    (0,0),(-1,-1), 0),
                ("BOTTOMPADDING", (0,0),(-1,-1), 6),
                ("LINEBELOW",     (0,0),(-1,-1), 0.5, HexColor("#CBD5E1")),
                ("LINEBEFORE",    (0,0),(-1,-1), 0.5, HexColor("#CBD5E1")),
                ("LINEAFTER",     (0,0),(-1,-1), 0.5, HexColor("#CBD5E1")),
            ]))
            flowables.append(bot)
            flowables.append(Spacer(1, 4))
            continue

        # ── Blockquote
        if line.startswith(">"):
            text = line.lstrip("> ").strip()
            flowables.append(Paragraph(inline(text), styles["note"]))
            i += 1
            continue

        # ── Markdown table
        if "|" in line and i + 1 < len(lines) and re.match(r'^\|[-| :]+\|', lines[i+1]):
            table_lines = []
            while i < len(lines) and "|" in lines[i]:
                table_lines.append(lines[i])
                i += 1
            # Skip separator row
            header_row = [c.strip() for c in table_lines[0].strip("|").split("|")]
            data_rows = []
            for tl in table_lines[2:]:
                if re.match(r'^\|[-| :]+\|', tl):
                    continue
                row = [Paragraph(inline(c.strip()), ParagraphStyle(
                    "td", fontName="Helvetica", fontSize=8.5,
                    textColor=SLATE, leading=13
                )) for c in tl.strip("|").split("|")]
                data_rows.append(row)

            header_cells = [Paragraph(f"<b>{c}</b>", ParagraphStyle(
                "th", fontName="Helvetica-Bold", fontSize=8.5,
                textColor=WHITE, leading=13
            )) for c in header_row]

            all_rows = [header_cells] + data_rows
            n_cols = len(header_row)
            col_w = usable_width / n_cols
            tbl = make_table(all_rows, col_widths=[col_w] * n_cols)
            flowables.append(tbl)
            flowables.append(Spacer(1, 6))
            continue

        # ── Bullet list
        m = re.match(r'^(\s*)[-*] (.+)', line)
        if m:
            indent = len(m.group(1))
            text = inline(m.group(2))
            style = ParagraphStyle(
                "bullet_i",
                parent=styles["bullet"],
                leftIndent=14 + indent * 8,
                bulletIndent=4 + indent * 8,
            )
            flowables.append(Paragraph(f"• {text}", style))
            i += 1
            continue

        # ── Numbered list
        m = re.match(r'^\d+\. (.+)', line)
        if m:
            text = inline(m.group(1))
            flowables.append(Paragraph(f"• {text}", styles["bullet"]))
            i += 1
            continue

        # ── Blank line
        if not line.strip():
            flowables.append(Spacer(1, 4))
            i += 1
            continue

        # ── Default: paragraph
        flowables.append(Paragraph(inline(line), styles["body"]))
        i += 1

    return flowables


# ── Cover page ────────────────────────────────────────────────────

def build_cover(styles):
    flowables = []
    usable_width = PAGE_W - 40*mm

    # Full-width banner block
    banner_data = [[
        Paragraph("ProAssess", styles["cover_title"]),
    ]]
    banner = Table(banner_data, colWidths=[usable_width])
    banner.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), INDIGO_DARK),
        ("TOPPADDING",    (0,0), (-1,-1), 28),
        ("BOTTOMPADDING", (0,0), (-1,-1), 14),
        ("LEFTPADDING",   (0,0), (-1,-1), 20),
        ("RIGHTPADDING",  (0,0), (-1,-1), 20),
    ]))

    subtitle_data = [[
        Paragraph("Full Project Documentation", styles["cover_subtitle"]),
    ]]
    subtitle = Table(subtitle_data, colWidths=[usable_width])
    subtitle.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), INDIGO),
        ("TOPPADDING",    (0,0), (-1,-1), 14),
        ("BOTTOMPADDING", (0,0), (-1,-1), 28),
        ("LEFTPADDING",   (0,0), (-1,-1), 20),
        ("RIGHTPADDING",  (0,0), (-1,-1), 20),
    ]))

    flowables.append(Spacer(1, 40*mm))
    flowables.append(banner)
    flowables.append(subtitle)
    flowables.append(Spacer(1, 12*mm))

    # Divider
    flowables.append(HRFlowable(width="100%", thickness=1, color=BORDER,
                                spaceAfter=10, spaceBefore=2))

    # Meta info grid
    meta_rows = [
        [
            Paragraph("<b>Platform</b>", ParagraphStyle("m", fontName="Helvetica-Bold",
                fontSize=9, textColor=SLATE_LIGHT)),
            Paragraph("ProAssess — Staff Proficiency Assessment Platform",
                ParagraphStyle("mv", fontName="Helvetica", fontSize=9, textColor=SLATE)),
        ],
        [
            Paragraph("<b>Stack</b>", ParagraphStyle("m", fontName="Helvetica-Bold",
                fontSize=9, textColor=SLATE_LIGHT)),
            Paragraph("FastAPI · PostgreSQL · Chroma · Next.js · GPT-4o",
                ParagraphStyle("mv", fontName="Helvetica", fontSize=9, textColor=SLATE)),
        ],
        [
            Paragraph("<b>Version</b>", ParagraphStyle("m", fontName="Helvetica-Bold",
                fontSize=9, textColor=SLATE_LIGHT)),
            Paragraph("1.0.0",
                ParagraphStyle("mv", fontName="Helvetica", fontSize=9, textColor=SLATE)),
        ],
        [
            Paragraph("<b>Classification</b>", ParagraphStyle("m", fontName="Helvetica-Bold",
                fontSize=9, textColor=SLATE_LIGHT)),
            Paragraph("Confidential — Internal Use Only",
                ParagraphStyle("mv", fontName="Helvetica", fontSize=9, textColor=SLATE)),
        ],
    ]
    meta_table = Table(meta_rows, colWidths=[45*mm, usable_width - 45*mm])
    meta_table.setStyle(TableStyle([
        ("TOPPADDING",    (0,0), (-1,-1), 7),
        ("BOTTOMPADDING", (0,0), (-1,-1), 7),
        ("LEFTPADDING",   (0,0), (-1,-1), 0),
        ("RIGHTPADDING",  (0,0), (-1,-1), 0),
        ("LINEBELOW",     (0,0), (-1,-2), 0.3, BORDER),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
    ]))

    flowables.append(meta_table)
    flowables.append(HRFlowable(width="100%", thickness=1, color=BORDER,
                                spaceAfter=16, spaceBefore=2))

    # Role badge row
    role_items = [
        ("Staff",         GREEN,  "Take assessments · View feedback"),
        ("Line Manager",  INDIGO, "Create · Deploy · Manage"),
        ("HR Admin",      AMBER,  "Knowledge base · Stats · Audit"),
        ("System Admin",  RED,    "Full platform access"),
    ]
    badge_cells = []
    for role, color, desc in role_items:
        cell = Table([
            [Paragraph(f"<b>{role}</b>", ParagraphStyle("rn",
                fontName="Helvetica-Bold", fontSize=9, textColor=WHITE))],
            [Paragraph(desc, ParagraphStyle("rd",
                fontName="Helvetica", fontSize=7.5, textColor=HexColor("#E2E8F0")))],
        ], colWidths=[(usable_width/4) - 3*mm])
        cell.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (-1,-1), color),
            ("TOPPADDING",    (0,0), (-1,-1), 8),
            ("BOTTOMPADDING", (0,0), (-1,-1), 8),
            ("LEFTPADDING",   (0,0), (-1,-1), 10),
            ("RIGHTPADDING",  (0,0), (-1,-1), 10),
            ("ROUNDEDCORNERS", [4,4,4,4]),
        ]))
        badge_cells.append(cell)

    badge_row = Table(
        [badge_cells],
        colWidths=[(usable_width/4)] * 4,
        hAlign="LEFT"
    )
    badge_row.setStyle(TableStyle([
        ("LEFTPADDING",  (0,0), (-1,-1), 2),
        ("RIGHTPADDING", (0,0), (-1,-1), 2),
    ]))
    flowables.append(badge_row)

    flowables.append(PageBreak())
    return flowables


# ── TOC page ──────────────────────────────────────────────────────

def build_toc_page(styles):
    flowables = []
    flowables.append(Spacer(1, 8*mm))
    flowables.append(Paragraph("Table of Contents", styles["toc_title"]))
    flowables.append(HRFlowable(width="100%", thickness=2, color=INDIGO,
                                spaceAfter=10, spaceBefore=0))

    toc_entries = [
        ("1.", "Project Overview"),
        ("2.", "System Architecture"),
        ("3.", "Technology Stack"),
        ("4.", "Infrastructure & Services"),
        ("5.", "Backend Deep Dive"),
        ("6.", "The RAG Pipeline"),
        ("7.", "Frontend Deep Dive"),
        ("8.", "Authentication & Roles"),
        ("9.", "Data Models"),
        ("10.", "API Reference"),
        ("11.", "Environment Variables"),
        ("12.", "Running the Project"),
        ("13.", "Database Migrations"),
        ("14.", "Project File Structure"),
        ("15.", "Known Limitations & Future Work"),
    ]

    rows = []
    for num, title in toc_entries:
        rows.append([
            Paragraph(f"<b>{num}</b>", ParagraphStyle("tn",
                fontName="Helvetica-Bold", fontSize=10, textColor=INDIGO)),
            Paragraph(title, ParagraphStyle("tt",
                fontName="Helvetica", fontSize=10, textColor=SLATE)),
            Paragraph("", ParagraphStyle("tp",
                fontName="Helvetica", fontSize=9, textColor=SLATE_LIGHT,
                alignment=TA_RIGHT)),
        ])

    toc_table = Table(rows, colWidths=[14*mm, usable_width - 28*mm, 14*mm])
    toc_table.setStyle(TableStyle([
        ("TOPPADDING",    (0,0), (-1,-1), 7),
        ("BOTTOMPADDING", (0,0), (-1,-1), 7),
        ("LEFTPADDING",   (0,0), (-1,-1), 6),
        ("RIGHTPADDING",  (0,0), (-1,-1), 6),
        ("ROWBACKGROUNDS", (0,0), (-1,-1), [WHITE, INDIGO_LIGHT]),
        ("LINEBELOW",      (0,0), (-1,-1), 0.3, BORDER),
        ("VALIGN",         (0,0), (-1,-1), "MIDDLE"),
        ("BOX",            (0,0), (-1,-1), 0.5, BORDER),
    ]))
    flowables.append(toc_table)
    flowables.append(PageBreak())
    return flowables


# ── Main build ────────────────────────────────────────────────────

def build_pdf(md_path: str, out_path: str):
    usable_width = PAGE_W - 40*mm

    doc = SimpleDocTemplate(
        out_path,
        pagesize=A4,
        leftMargin=20*mm,
        rightMargin=20*mm,
        topMargin=22*mm,
        bottomMargin=16*mm,
        title="ProAssess — Full Project Documentation",
        author="ProAssess Team",
        subject="Platform documentation",
        creator="ProAssess Doc Generator",
    )

    styles = make_styles()
    story = []

    # Cover
    story.extend(build_cover(styles))

    # TOC placeholder page
    story.extend(build_toc_page(styles))

    # Body — parse markdown
    with open(md_path, "r", encoding="utf-8") as f:
        md = f.read()

    # Strip the TOC block from the markdown (we rendered our own)
    md = re.sub(r'## Table of Contents\n[\s\S]*?(?=\n---)', '', md)

    body_flowables = parse_markdown(md, styles)
    story.extend(body_flowables)

    # Build
    page_cb = DocCanvas("ProAssess — Full Project Documentation")
    doc.build(story, onFirstPage=page_cb, onLaterPages=page_cb)
    print(f"\n✓ PDF written to: {out_path}\n")


if __name__ == "__main__":
    base = os.path.dirname(os.path.abspath(__file__))
    usable_width = PAGE_W - 40*mm
    build_pdf(
        md_path=os.path.join(base, "DOCUMENTATION.md"),
        out_path=os.path.join(base, "ProAssess_Documentation.pdf"),
    )
