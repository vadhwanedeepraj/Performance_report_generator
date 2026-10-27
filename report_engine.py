import io
import re
import os
import zipfile
import hashlib
import time
import json
import threading
from datetime import date
from constants import (
    RATING_MAP, GRADE_SCALE_0_4, AI_CACHE_DIR, AI_CACHE_TTL_DAYS,
    MOCK_PDF_LABEL_SHORTLISTED, MOCK_PDF_LABEL_REVISION,
)

import pandas as pd
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table,
    Image, TableStyle, KeepTogether, HRFlowable,
    PageBreak, FrameBreak
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch, mm
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT, TA_JUSTIFY
from reportlab.lib.utils import ImageReader
from reportlab.platypus.flowables import Flowable
from report_theme import get_page_geometry, build_intro_card_style, apply_table_border_style

# ──────────────────────────────────────────────────────────────────
# SCORING ENGINE  (constants imported from constants.py)
# ──────────────────────────────────────────────────────────────────

os.makedirs(AI_CACHE_DIR, exist_ok=True)

# Purge stale AI cache files older than TTL (runs once at import time)
def _purge_stale_ai_cache():
    try:
        cutoff = time.time() - AI_CACHE_TTL_DAYS * 86_400
        for fname in os.listdir(AI_CACHE_DIR):
            fpath = os.path.join(AI_CACHE_DIR, fname)
            if fname.endswith(".txt") and os.path.getmtime(fpath) < cutoff:
                os.remove(fpath)
    except Exception:
        pass

_purge_stale_ai_cache()


def grade_for(score: float):
    """Map a 0-4 score to (grade_label, reportlab_color). Uses GRADE_SCALE_0_4 from constants."""
    for threshold, label, hex_color, _verdict in GRADE_SCALE_0_4:
        if score >= threshold:
            return label, colors.HexColor(hex_color)
    return "D", colors.HexColor("#dc2626")


def generate_ai_with_retry(groq_client, prompt, system_prompt, model, max_retries=3):
    hash_input = json.dumps({"sys": system_prompt, "prompt": prompt, "model": model}, sort_keys=True)
    # SHA-256 (replaces MD5 — consistent with generation_guard.py)
    cache_key  = hashlib.sha256(hash_input.encode()).hexdigest()[:32]
    cache_file = os.path.join(AI_CACHE_DIR, f"{cache_key}.txt")
    if os.path.exists(cache_file):
        try:
            with open(cache_file, encoding="utf-8") as f:
                return f.read()
        except Exception:
            pass
    last_error = None
    for attempt in range(max_retries):
        try:
            response = groq_client.chat.completions.create(
                messages=[{"role": "system", "content": system_prompt},
                          {"role": "user",   "content": prompt}],
                model=model, temperature=0.2, max_tokens=900,
            )
            ai_text = response.choices[0].message.content
            with open(cache_file, "w", encoding="utf-8") as f:
                f.write(ai_text)
            return ai_text
        except Exception as e:
            last_error = e
            time.sleep(2 ** attempt)
    raise Exception(f"AI Generation failed: {str(last_error)}")


def clean_label(col: str) -> str:
    if "[" in col and "]" in col:
        return col.split("[")[-1].replace("]", "").strip()
    return col.strip()


def clean_display_val(val) -> str:
    """Convert a cell value to a human-readable string.

    Strips the trailing '.0' that pandas adds when an integer column is
    read as float (e.g. Enrolment ID 81010901925.0 → '81010901925',
    Batch # 455.0 → '455').  Non-numeric and genuinely fractional values
    are returned as-is.
    """
    s = str(val).strip()
    # Handle pandas float representation of integers (e.g. '81010901925.0')
    try:
        f = float(s)
        if f == int(f):            # whole number stored as float
            return str(int(f))
    except (ValueError, OverflowError):
        pass
    return s


def to_numeric_rating(value: str) -> float:
    clean_val = str(value).strip()
    if not clean_val or clean_val.lower() in ("nan", "none", "n/a", "na", "-"):
        return 0.0
    try:
        num = float(clean_val)
        if 0.0 <= num <= 4.0:
            return num
    except ValueError:
        pass
    return float(RATING_MAP.get(clean_val.lower(), 0.0))


def rl_safe(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def safe_paragraph(text: str, style) -> Paragraph:
    """Safely build a Paragraph. If XML parsing crashes (e.g. from invalid tags in user inputs),
    clean all tags and fallback to a plain text paragraph."""
    try:
        return Paragraph(str(text), style)
    except Exception:
        # Strip all HTML-like tags to produce plain text
        clean = re.sub(r'<[^>]*>', '', str(text))
        # Re-escape XML entities for ReportLab safety
        clean = clean.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return Paragraph(clean, style)


def md_to_rl(text: str) -> str:
    if not text:
        return ""
    # Escape special XML characters for ReportLab Paragraph safety
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    
    # Restore allowed/safe ReportLab XML tags
    text = text.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")
    text = text.replace("&lt;i&gt;", "<i>").replace("&lt;/i&gt;", "</i>")
    text = text.replace("&lt;u&gt;", "<u>").replace("&lt;/u&gt;", "</u>")
    text = text.replace("&lt;/font&gt;", "</font>")
    text = text.replace("&lt;br/&gt;", "<br/>").replace("&lt;br&gt;", "<br/>")
    text = re.sub(r'&lt;font\s+color\s*=\s*([\'"]?)(#[a-fA-F0-9]{6}|[a-zA-Z]+)\1\s*&gt;', r'<font color="\2">', text)

    # Compile Markdown styles to ReportLab tags
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.*?)\*',     r'<i>\1</i>', text)
    text = re.sub(r'(?m)^- ', '<br/>&nbsp;&nbsp;&bull;&nbsp;', text)
    text = text.replace("\n- ", "<br/>&nbsp;&nbsp;&bull;&nbsp;")
    text = re.sub(r'\n#+\s*(.*)', lambda m: f'<br/><b>{m.group(1)}</b>', text)
    text = text.replace("\n", "<br/>")
    return text


# ──────────────────────────────────────────────────────────────────
# CUSTOM FLOWABLES
# ──────────────────────────────────────────────────────────────────

class ColorBar(Flowable):
    """A full-width colored horizontal bar — used as section dividers."""
    def __init__(self, width, height=3, color="#1B3A6B", radius=2):
        super().__init__()
        self.bar_width  = width
        self.bar_height = height
        self.color      = colors.HexColor(color)
        self.radius     = radius
        self.width  = width
        self.height = height + 2

    def draw(self):
        self.canv.setFillColor(self.color)
        self.canv.roundRect(0, 1, self.bar_width, self.bar_height,
                            self.radius, fill=1, stroke=0)


class ScoreCircle(Flowable):
    """Draws a filled circle badge with a score inside."""
    def __init__(self, score, max_score=4.0, size=60,
                 fill_color="#1B3A6B", text_color="#FFFFFF"):
        super().__init__()
        self.score       = score
        self.max_score   = max_score
        self.size        = size
        self.fill_color  = colors.HexColor(fill_color)
        self.text_color  = colors.HexColor(text_color)
        self.width  = size
        self.height = size

    def draw(self):
        c = self.canv
        r = self.size / 2
        c.setFillColor(self.fill_color)
        c.circle(r, r, r, fill=1, stroke=0)
        c.setFillColor(self.text_color)
        c.setFont("Helvetica-Bold", 13)
        label = f"{self.score:.2f}"
        c.drawCentredString(r, r + 4, label)
        c.setFont("Helvetica", 7)
        c.drawCentredString(r, r - 8, f"out of {self.max_score:.1f}")


class GradeBadge(Flowable):
    """A pill-shaped grade badge."""
    def __init__(self, grade, verdict, v_color, width=200, height=36):
        super().__init__()
        self.grade   = grade
        self.verdict = verdict
        self.v_color = colors.HexColor(v_color)
        self.width   = width
        self.height  = height

    def draw(self):
        c = self.canv
        c.setFillColor(self.v_color)
        c.roundRect(0, 0, self.width, self.height, 8, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 12)
        c.drawCentredString(self.width / 2, self.height / 2 - 4,
                            f"{self.grade}  ·  {self.verdict}")


class RatingBar(Flowable):
    """A horizontal progress bar for skill scores with text drawn internally."""
    def __init__(self, score, max_score=4.0, width=120, height=12, color="#1B3A6B"):
        super().__init__()
        self.score     = score
        self.max_score = max_score
        self.bar_width = width
        self.color     = colors.HexColor(color)
        self.width     = width
        self.height    = height + 4

    def draw(self):
        c  = self.canv
        bw = self.bar_width
        bh = 12
        # Background track
        c.setFillColor(colors.HexColor("#E2E8F0"))
        c.roundRect(0, 0, bw, bh, 4, fill=1, stroke=0)
        # Fill
        fill_w = int((self.score / self.max_score) * bw)
        if fill_w > 0:
            c.setFillColor(self.color)
            c.roundRect(0, 0, fill_w, bh, 4, fill=1, stroke=0)
            
            # Draw score text inside the filled bar
            c.setFillColor(colors.white)
            c.setFont("Helvetica-Bold", 7.5)
            # Center the text inside the filled portion.
            if fill_w > 20: # Make sure there is enough space to draw inside
                c.drawRightString(fill_w - 4, 3.5, f"{self.score:.1f}")
            else:
                c.setFillColor(colors.HexColor("#334155"))
                c.drawString(fill_w + 4, 3.5, f"{self.score:.1f}")


# ──────────────────────────────────────────────────────────────────
# DATA PARSING & MATH
# ──────────────────────────────────────────────────────────────────

def compute_student_data(row, cfg, rank=None, total_students=None):
    all_sections = cfg.get("sections", [])

    tech_scores  = []
    comm_scores  = []
    has_written  = False
    written_score = 0.0
    written_max   = 100.0

    skill_list   = []
    action_plan  = ""
    remarks      = ""
    tech_actions = []
    comm_actions = []

    for sec in all_sections:
        if not sec.get("enabled", True):
            continue
        stype = sec.get("type", "")
        sname = sec.get("name", "")

        if stype == "rating_table":
            is_comm = any(k in sname.lower()
                          for k in ["communication", "soft skill", "behavioral"])
            for col in sec.get("columns", []):
                if col in row.index:
                    val   = str(row[col]).strip()
                    score = to_numeric_rating(val)
                    if score > 0:
                        if is_comm:
                            comm_scores.append(score)
                        else:
                            tech_scores.append(score)
                        skill_list.append({
                            "name":    clean_label(col),
                            "score":   score,
                            "val":     val,
                            "is_comm": is_comm,
                        })
                        # Scores <= 2 (Poor or Average) are flagged for improvement
                        if score <= 2:
                            clean_c = clean_label(col)
                            if is_comm:
                                comm_actions.append(clean_c)
                            else:
                                tech_actions.append(clean_c)

        elif stype == "written_test":
            for col, meta in (sec.get("columns", {}).items()
                              if isinstance(sec.get("columns"), dict)
                              else [(c, {}) for c in sec.get("columns", [])]):
                if col in row.index:
                    try:
                        written_score = float(row[col])
                        m = meta if isinstance(meta, dict) else {}
                        written_max   = float(m.get("max", 100) or 100)
                        has_written   = True
                        break
                    except Exception:
                        pass

        elif stype == "text_field":
            for col in sec.get("columns", []):
                if col in row.index:
                    val = str(row[col]).strip()
                    if val.lower() not in ("nan", "na", "", "n/a"):
                        if "action" in col.lower() or "roadmap" in col.lower() or "plan" in col.lower():
                            action_plan = val
                        elif any(k in col.lower() for k in ["remark", "feedback", "comment"]):
                            remarks = val

        elif stype == "action_plan_grid":
            is_comm = any(k in sname.lower()
                          for k in ["communication", "soft skill", "behavioral"])
            for col in sec.get("columns", []):
                if col in row.index:
                    val = str(row[col]).strip()
                    val_l = val.lower()
                    if val_l and val_l not in ("nan", "no", "false", "0", "none", "n/a", "not needed", ""):
                        clean_c = clean_label(col)
                        if is_comm:
                            comm_actions.append(clean_c)
                        else:
                            tech_actions.append(clean_c)

        elif stype == "marks_table":
            items, _, _ = _parse_marks_cols(row, sec)
            for it in items:
                # Flag modules scoring below 75% for focus in action plan
                if it.get("pct") is not None and it["pct"] < 75:
                    lbl = it.get("label", "Module")
                    tech_actions.append(f"{lbl} (Scored {it['score']}/{it['max']})")

    # ── Weighted overall score ────────────────────────────────────
    tech_avg = sum(tech_scores) / len(tech_scores) if tech_scores else 0
    comm_avg = sum(comm_scores) / len(comm_scores) if comm_scores else 0

    if has_written:
        written_norm = (written_score / written_max) * 4 if written_max > 0 else 0
    else:
        written_norm = 0.0

    # Set overall score to exactly match the Technical average
    overall = tech_avg

    def get_gv(sc):
        if sc >= 3.5: return "A+ (Excellent)", "Pass — Outstanding",       "#059669"
        if sc >= 3.0: return "A  (Good)",      "Pass — Proficient",        "#16a34a"
        if sc >= 2.5: return "B  (Average)",   "Average — Needs Effort",   "#d97706"
        if sc >= 1.5: return "C  (Below Avg)", "Poor — Serious Gaps",      "#ea580c"
        return              "D  (Critical)",   "Critical Gap — Action Now", "#dc2626"

    grade, verdict, v_color = get_gv(overall)

    strengths  = sorted([s for s in skill_list if s["score"] >= 3.5],
                         key=lambda x: x["score"], reverse=True)[:4]
    weaknesses = sorted([s for s in skill_list if s["score"] <= 2.5],
                         key=lambda x: x["score"])[:4]

    return {
        "overall":        overall,
        "tech_avg":       tech_avg,
        "comm_avg":       comm_avg,
        "grade":          grade,
        "verdict":        verdict,
        "v_color":        v_color,
        "has_written":    has_written,
        "written_score":  written_score,
        "written_max":    written_max,
        "written_norm":   written_norm if has_written else None,
        "skills":         skill_list,
        "strengths":      strengths,
        "weaknesses":     weaknesses,
        "action_plan":    action_plan,
        "remarks":        remarks,
        "tech_actions":   tech_actions,
        "comm_actions":   comm_actions,
        "rank":           rank,
        "total_students": total_students,
        "_row":           row,   # stored for marks-based overview re-computation
    }


# ──────────────────────────────────────────────────────────────────
# STYLES
# ──────────────────────────────────────────────────────────────────

# PAGE_W / PAGE_H are module-level fallbacks for flowables that don't
# have access to a per-call cfg (custom flowables, section helpers, etc.).
# build_single_report computes the real content_w via get_page_geometry(cfg).
PAGE_W  = A4[0] - 80   # usable width inside default 40px L+R margins
PAGE_H  = A4[1]

# Corporate palette — Innomatics brand
C_PRIMARY = "#B91C1C"  # Innomatics primary red brand colour
C_NAVY    = C_PRIMARY  # Backward-compatible alias for primary brand colour
C_GOLD   = "#C9A84C"   # gold accent
C_SLATE  = "#334155"   # body text
C_LIGHT  = "#F1F5F9"   # light bg
C_BORDER = "#CBD5E1"   # table borders
C_WHITE  = "#FFFFFF"
C_HEADER = "#1E293B"   # dark charcoal for table headers (avoids all-red)
C_GREEN  = "#16a34a"   # strong proficiency
C_ORANGE = "#ea580c"   # revision recommended
C_RED    = "#dc2626"   # foundational gap


def build_styles(pc=C_NAVY):
    base = getSampleStyleSheet()
    def ps(name, **kw):
        return ParagraphStyle(name, parent=base["Normal"], **kw)

    return {
        # ── Document title
        "doc_title": ps("DocTitle",
            fontName="Helvetica-Bold", fontSize=16, alignment=TA_CENTER,
            textColor=colors.white, spaceAfter=2, leading=19),

        "doc_subtitle": ps("DocSub",
            fontName="Helvetica", fontSize=8.5, alignment=TA_CENTER,
            textColor=colors.HexColor("#B8CCEA"), spaceAfter=0),

        "center": ps("CenterText",
            fontName="Helvetica", fontSize=9.5, alignment=TA_CENTER,
            textColor=colors.HexColor(C_SLATE), leading=13),

        # ── Section headings
        "section_head": ps("SecHead",
            fontName="Helvetica-Bold", fontSize=10, textColor=colors.HexColor(pc),
            spaceBefore=8, spaceAfter=3, leading=13,
            borderPad=0),

        "section_sub": ps("SecSub",
            fontName="Helvetica-Bold", fontSize=8,
            textColor=colors.HexColor(C_SLATE),
            spaceBefore=6, spaceAfter=2, leading=10),

        # ── Body text
        "body": ps("Body",
            fontName="Helvetica", fontSize=8,
            textColor=colors.HexColor(C_SLATE),
            leading=11, spaceAfter=2),

        "body_italic": ps("BodyI",
            fontName="Helvetica-Oblique", fontSize=8,
            textColor=colors.HexColor("#64748B"),
            leading=11, spaceAfter=2),

        "body_bold": ps("BodyB",
            fontName="Helvetica-Bold", fontSize=8,
            textColor=colors.HexColor(C_SLATE),
            leading=11, spaceAfter=1),

        # ── Table cells
        "cell": ps("Cell",
            fontName="Helvetica", fontSize=7.5,
            textColor=colors.HexColor(C_SLATE), leading=10),

        "cell_bold": ps("CellB",
            fontName="Helvetica-Bold", fontSize=7.5,
            textColor=colors.HexColor("#0F172A"), leading=10),

        "cell_header": ps("CellH",
            fontName="Helvetica-Bold", fontSize=8,
            textColor=colors.white, leading=10),

        "cell_color": ps("CellC",
            fontName="Helvetica-Bold", fontSize=8,
            leading=10),

        # ── Bullet item
        "bullet": ps("Bullet",
            fontName="Helvetica", fontSize=8,
            textColor=colors.HexColor(C_SLATE),
            leftIndent=12, firstLineIndent=-9,
            leading=11, spaceAfter=2),

        "bullet_bold": ps("BulletB",
            fontName="Helvetica-Bold", fontSize=8,
            textColor=colors.HexColor(C_NAVY),
            leftIndent=12, firstLineIndent=-9,
            leading=11, spaceAfter=2),

        # ── Intro paragraph
        "intro": ps("Intro",
            fontName="Helvetica-Oblique", fontSize=8,
            textColor=colors.HexColor("#475569"),
            leading=11, spaceAfter=3,
            leftIndent=10, rightIndent=10),

        # ── Rank badge text
        "rank_text": ps("Rank",
            fontName="Helvetica-Bold", fontSize=14,
            textColor=colors.HexColor(C_GOLD),
            alignment=TA_CENTER, leading=17),

        "footer_text": ps("Footer",
            fontName="Helvetica", fontSize=6.5,
            textColor=colors.HexColor("#94A3B8"),
            alignment=TA_CENTER, leading=8),
    }


# ──────────────────────────────────────────────────────────────────
# TABLE STYLE HELPERS
# ──────────────────────────────────────────────────────────────────

def corporate_table_style(header_bg=C_HEADER, stripe_bg="#F8FAFC", cfg=None):
    """Premium table: primary-colour header, white text, alternating stripes.

    When cfg is supplied, the table border style is read from
    cfg['table_border_style'] (default: 'grid' — identical to previous behaviour).
    """
    ts = TableStyle([
        # Header row with primary colour fill
        ("BACKGROUND",    (0, 0), (-1, 0),  colors.HexColor(header_bg)),
        ("TEXTCOLOR",     (0, 0), (-1, 0),  colors.white),
        ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, 0),  8.5),
        ("TOPPADDING",    (0, 0), (-1, 0),  6),
        ("BOTTOMPADDING", (0, 0), (-1, 0),  6),
        # Body rows
        ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",      (0, 1), (-1, -1), 8),
        ("TOPPADDING",    (0, 1), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN",         (0, 0), (-1, -1), "LEFT"),
        # Header underline accent
        ("LINEBELOW",     (0, 0), (-1, 0),  1.5, colors.HexColor(header_bg)),
        # Alternating stripes
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor(stripe_bg)]),
    ])
    # Apply border style from cfg (defaults to "grid" — same as before)
    effective_cfg = cfg or {}
    return apply_table_border_style(ts, effective_cfg, header_bg)


def accent_table_style(accent=C_GOLD):
    """Premium 2-col info table: subtle alternating rows."""
    return TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), colors.white),
        ("FONTNAME",      (0, 0), (0, -1),  "Helvetica-Bold"),
        ("FONTNAME",      (1, 0), (1, -1),  "Helvetica"),
        ("FONTSIZE",      (0, 0), (-1, -1), 8.5),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("GRID",          (0, 0), (-1, -1), 0.4, colors.HexColor("#D1D5DB")),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1),
         [colors.white, colors.HexColor("#F8FAFC")]),
    ])


# ──────────────────────────────────────────────────────────────────
# HEADER CANVAS — decorative page header background
# ──────────────────────────────────────────────────────────────────

# Module-level logo cache — used only as a single-user fallback (disk logo).
# Per-call logo bytes are now passed through closures to avoid thread-safety issues.
_LOGO_BYTES = None

def _draw_watermark(canvas, text="CONFIDENTIAL"):
    """Draws a professional diagonal watermark across the page."""
    canvas.saveState()
    W, H = A4
    canvas.setFont("Helvetica-Bold", 54)
    canvas.setFillColor(colors.HexColor("#E2E8F0"))
    canvas.setFillAlpha(0.12)
    canvas.translate(W / 2, H / 2)
    canvas.rotate(45)
    canvas.drawCentredString(0, 0, text)
    canvas.restoreState()


def _draw_first_page_header(canvas, doc, pc, report_title, logo_bytes=None, watermark_text=None, legal_notice=None):
    """Certificate-style first page: large logo, bold title, clean layout.
    BUG-02 FIX: logo_bytes is now passed explicitly (not read from global)
    so each build_single_report call is isolated and thread-safe.
    """
    canvas.saveState()
    W, H = A4

    # Watermark
    if watermark_text:
        _draw_watermark(canvas, watermark_text)

    # ── Large logo – prominently centred at top
    resolved = logo_bytes or _LOGO_BYTES   # fallback to disk-cached logo
    if resolved:
        try:
            from reportlab.lib.utils import ImageReader
            reader = ImageReader(io.BytesIO(resolved))
            lw, lh = reader.getSize()
            MAX_W, MAX_H = 280, 75
            scale  = min(MAX_W / lw, MAX_H / lh)
            disp_w = lw * scale
            disp_h = lh * scale
            x = (W - disp_w) / 2
            y = H - 10 - disp_h
            canvas.drawImage(reader, x, y, disp_w, disp_h,
                             preserveAspectRatio=True, mask='auto')
        except Exception:
            pass

    # ── Report title — bold, in primary brand colour, centred
    canvas.setFillColor(colors.HexColor(pc))
    canvas.setFont("Helvetica-Bold", 13)
    canvas.drawCentredString(W / 2, H - 95, report_title)

    # ── Date — smaller, dark slate, centred below title
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#334155"))
    canvas.drawCentredString(W / 2, H - 108, date.today().strftime("%d %B %Y"))

    # ── Thin accent line below date (gold — contrasts with title)
    canvas.setStrokeColor(colors.HexColor(C_GOLD))
    canvas.setLineWidth(1.2)
    canvas.line(45, H - 115, W - 45, H - 115)

    # ── Footer: page number + confidential / legal notice
    canvas.setFillColor(colors.HexColor("#94A3B8"))
    canvas.setFont("Helvetica", 6.5)
    
    footer_text = legal_notice or "This report is system-generated for assessment and evaluation purposes only."
    canvas.drawCentredString(W / 2, 18, footer_text)

    canvas.setFont("Helvetica", 7)
    canvas.drawCentredString(W / 2, 28, f"Page {doc.page}")

    # Bottom separator line
    canvas.setStrokeColor(colors.HexColor(C_BORDER))
    canvas.setLineWidth(0.4)
    canvas.line(45, 36, W - 45, 36)
    canvas.restoreState()


def _draw_later_page_header(canvas, doc, pc, report_title, logo_bytes=None, watermark_text=None, legal_notice=None):
    """Later pages — matching minimal footer and elegant logo header.
    BUG-02 FIX: logo_bytes is now passed explicitly (not read from global).
    """
    canvas.saveState()
    W, H = A4

    # Watermark
    if watermark_text:
        _draw_watermark(canvas, watermark_text)

    # ── Logo – centered at top (matching first page size exactly)
    resolved = logo_bytes or _LOGO_BYTES   # fallback to disk-cached logo
    if resolved:
        try:
            from reportlab.lib.utils import ImageReader
            reader = ImageReader(io.BytesIO(resolved))
            lw, lh = reader.getSize()
            MAX_W, MAX_H = 280, 75
            scale  = min(MAX_W / lw, MAX_H / lh)
            disp_w = lw * scale
            disp_h = lh * scale
            x = (W - disp_w) / 2
            y = H - 10 - disp_h
            canvas.drawImage(reader, x, y, disp_w, disp_h,
                             preserveAspectRatio=True, mask='auto')
        except Exception:
            pass

    # ── Report title — bold, in primary brand colour, centred
    canvas.setFillColor(colors.HexColor(pc))
    canvas.setFont("Helvetica-Bold", 13)
    canvas.drawCentredString(W / 2, H - 95, report_title)

    # ── Date — smaller, dark slate, centred below title
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#334155"))
    canvas.drawCentredString(W / 2, H - 108, date.today().strftime("%d %B %Y"))

    # ── Thin accent line below date (gold — contrasts with title)
    canvas.setStrokeColor(colors.HexColor(C_GOLD))
    canvas.setLineWidth(1.2)
    canvas.line(45, H - 115, W - 45, H - 115)

    # Disclaimer / Legal notice
    canvas.setFillColor(colors.HexColor("#94A3B8"))
    canvas.setFont("Helvetica", 6.5)
    
    footer_text = legal_notice or "This report is system-generated for assessment and evaluation purposes only."
    canvas.drawCentredString(W / 2, 18, footer_text)

    canvas.setFont("Helvetica", 7)
    canvas.drawCentredString(W / 2, 28, f"Page {doc.page}")

    # Bottom separator
    canvas.setStrokeColor(colors.HexColor(C_BORDER))
    canvas.setLineWidth(0.4)
    canvas.line(45, 36, W - 45, 36)
    canvas.restoreState()


# ──────────────────────────────────────────────────────────────────
# RENDER BLOCKS
# ──────────────────────────────────────────────────────────────────

def _section_title(text, styles, pc=C_NAVY):
    """Premium section heading with thick left accent bar."""
    inner = safe_paragraph(f"<b>{text}</b>", styles["section_head"])
    bar_t = Table(
        [[inner]],
        colWidths=[PAGE_W],
    )
    bar_t.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
        ("LINEBEFORE",   (0, 0), (0, -1),  4, colors.HexColor(pc)),
        ("TOPPADDING",   (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 3),
        ("LEFTPADDING",  (0, 0), (-1, -1), 8),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return [Spacer(1, 4), bar_t, Spacer(1, 2)]


# BUG-19 FIX: render_logo was dead code — logo is rendered inside canvas header callbacks.
# Function removed.


def render_identity(story, row, cfg, styles, pdata):
    """Student profile card — compact one-page version."""
    pc       = cfg.get("primary_color", C_NAVY)
    id_cols  = cfg.get("identity_columns", [])
    intro    = cfg.get("intro_text", "")
    company  = cfg.get("company_name", "")

    # ── Greeting line
    student_name = ""
    for c in id_cols:
        if "name" in c.lower() and c in row.index:
            student_name = str(row[c]).strip()
            break
    if student_name:
        story.append(Spacer(1, 1))
        story.append(safe_paragraph(f"Dear <b>{rl_safe(student_name)}</b>,", styles["body_bold"]))
        story.append(Spacer(1, 1))

    if intro:
        resolved_intro = md_to_rl(intro.replace("{company_name}", company))
        intro_card_style = cfg.get("intro_card_style", "clean")
        if intro_card_style in ("callout", "banner"):
            # Wrap intro text in a styled card using report_theme
            content_w = cfg.get("_content_w", PAGE_W)
            intro_tbl = Table(
                [[safe_paragraph(resolved_intro, styles["intro"])]],
                colWidths=[content_w],
            )
            intro_tbl.setStyle(build_intro_card_style(cfg))
            story.append(intro_tbl)
        else:
            story.append(safe_paragraph(resolved_intro, styles["intro"]))
        story.append(Spacer(1, 2))

    # ── Identity table — no header row, just label:value pairs like the reference
    td = []
    for col in id_cols:
        if col in row.index:
            val = clean_display_val(row[col])
            if val.lower() not in ("nan", "na", ""):
                td.append([
                    safe_paragraph(f"<b>{rl_safe(col)}</b>", styles["cell_bold"]),
                    safe_paragraph(rl_safe(val),  styles["cell"]),
                ])

    if td:
        story.extend(_section_title("Candidate Details", styles, pc))
        t = Table(td, colWidths=[PAGE_W * 0.30, PAGE_W * 0.70])
        t.setStyle(accent_table_style(C_GOLD))
        story.append(t)
        story.append(Spacer(1, 6))


class MarksBar(Flowable):
    """Colour-coded horizontal progress bar (green/orange/red)."""
    def __init__(self, pct, width=120, height=12, color="#1B3A6B"):
        super().__init__()
        self.pct       = max(0.0, min(pct, 100.0))
        self.bar_width = width
        # Auto colour-code based on percentage
        if self.pct >= 75:
            self.color = colors.HexColor("#16a34a")   # green
        elif self.pct >= 40:
            self.color = colors.HexColor("#ea580c")   # orange
        else:
            self.color = colors.HexColor("#dc2626")   # red
        self.width     = width
        self.height    = height + 4

    def draw(self):
        c  = self.canv
        bw = self.bar_width
        bh = 14
        # Background track
        c.setFillColor(colors.HexColor("#E5E7EB"))
        c.roundRect(0, 0, bw, bh, 4, fill=1, stroke=0)
        # Fill
        fw = int((self.pct / 100.0) * bw)
        if fw > 0:
            c.setFillColor(self.color)
            c.roundRect(0, 0, fw, bh, 4, fill=1, stroke=0)
        # Percentage label
        if fw > 28:
            c.setFillColor(colors.white)
            c.setFont("Helvetica-Bold", 7)
            c.drawRightString(fw - 4, 3.5, f"{self.pct:.0f}%")
        else:
            c.setFillColor(colors.HexColor("#334155"))
            c.setFont("Helvetica-Bold", 7)
            c.drawString(fw + 4, 3.5, f"{self.pct:.0f}%")


def _parse_marks_cols(row, sec):
    """Parse all mark columns and return list of dicts + totals.
    
    `sec` can be:
      - a section dict  {"columns": [...], "col_cfg": {...}, ...}
      - or a plain list of column names (legacy fallback)
    """
    import re as _r
    items = []
    total_scored, total_max = 0.0, 0.0

    # ── Normalise sec so it is always a dict ─────────────────
    if isinstance(sec, list):
        # Legacy: bare list of column names passed directly
        cols    = sec
        col_cfg = {}
    elif isinstance(sec, dict):
        cols    = sec.get("columns", [])
        col_cfg = sec.get("col_cfg", {}) or {}
    else:
        return items, total_scored, total_max   # unknown type — skip
    
    # col_cfg must be a dict; reset if corrupted
    if not isinstance(col_cfg, dict):
        col_cfg = {}
    for col in cols:
        if col not in row.index:
            continue
        raw = str(row[col]).strip()
        if raw.lower() in ("nan", "na", "", "n/a"):
            continue
        try:
            score = float(raw)
        except ValueError:
            score = 0.0

        old_label = col.split("[")[-1].replace("]", "").strip() if "[" in col else col
        old_label = _r.sub(r'\(?\s*out\s+of\s+[\d.]+\s*\)?', '', old_label, flags=_r.IGNORECASE).strip().strip("()")
        
        cfg_for_col = col_cfg.get(col, {})
        label = cfg_for_col.get("label", old_label)
        
        m = _r.search(r'out\s+of\s+([\d.]+)', col, _r.IGNORECASE)
        max_from_header = float(m.group(1)) if m else None
        
        max_s = float(cfg_for_col.get("max")) if cfg_for_col.get("max") else max_from_header
        # If still None (no col_cfg set and no 'out of N' in header), default to 100
        if max_s is None:
            max_s = 100.0

        pct = round((score / max_s) * 100, 1) if (max_s and max_s > 0) else None
        if max_s and max_s > 0:
            total_scored += score
            total_max    += max_s

        items.append({"label": label, "score": score, "max": max_s, "pct": pct, "col": col})

    return items, total_scored, total_max


def render_marks_table(story, row, section, cfg, styles):
    """Subject-wise marks table matching the user's reference image."""
    pc        = cfg.get("primary_color", C_NAVY)
    cols      = section.get("columns", [])
    if not cols:
        return

    items, total_scored, total_max = _parse_marks_cols(row, section)
    if not items:
        return

    # Compute class stats to get benchmarks
    _, _, _, _, module_benchmarks, table_benchmarks = _compute_class_stats(row, cfg)
    
    # Dynamic Section Title — customizable via UI/template, falls back to section name or default
    table_title = section.get("title") or section.get("name") or "Module-wise Performance"
    sec_name = section.get("name") or table_title
    story.extend(_section_title(table_title, styles, pc))
    
    default_note = (
        "<b><font color='#dc2626'>Important:</font></b> Your performance is evaluated relative to all candidates "
        "across all branches of Innomatics Research Labs who appeared for the DataAnalysis Job Fitment Assessment. "
        "The benchmarks shown below indicate the score required to be among the top 10% in each module."
    )
    raw_note = cfg.get("marks_benchmark_note", default_note)
    # BUG-14 FIX: respect the marks_show_benchmark_note toggle
    show_note = cfg.get("marks_show_benchmark_note", True)
    if show_note and raw_note:
        note_txt = md_to_rl(raw_note)
        story.append(safe_paragraph(note_txt, styles["body"]))
        story.append(Spacer(1, 3))

    td = [["Module", "Your Score", "Readiness Status", "Benchmark\nScore"]]
    has_max = any(it["max"] is not None for it in items)
    p90_th = float(cfg.get("benchmark_strong", 90))
    p50_th = float(cfg.get("benchmark_revision", 50))

    for it in items:
        score = it["score"]
        max_s = it["max"]
        label = it["label"]
        
        benchmark_data = None
        if module_benchmarks:
            benchmark_data = (
                module_benchmarks.get(f"{table_title}::{label}") or
                module_benchmarks.get(f"{sec_name}::{label}") or
                module_benchmarks.get(label) or
                module_benchmarks.get(it.get("col"))
            )

        if benchmark_data:
            benchmark = benchmark_data["P90"]
            p50_benchmark = benchmark_data["P50"]
        else:
            # Fallback if no benchmark available (e.g. no batch data)
            benchmark = round(max_s * (p90_th / 100.0), 1) if max_s else 0
            p50_benchmark = round(max_s * (p50_th / 100.0), 1) if max_s else 0
        
        # Determine Readiness Status + colour
        if score >= benchmark:
            st_label = "Strong Proficiency"
            st_color = "#16a34a"
        elif score >= p50_benchmark:
            st_label = "Revision Recommended"
            st_color = "#ea580c"
        else:
            st_label = "Foundational Gap"
            st_color = "#dc2626"

        pct = (score / max_s * 100) if max_s and max_s > 0 else 0
        bar = MarksBar(pct, width=PAGE_W * 0.25, height=12, color=pc)
        
        sc_txt = str(int(score)) if score == int(score) else str(score)
        # BUG-10 FIX: max_s may be None — int(None) raises TypeError
        if max_s is None:
            mx_txt = "—"
        elif max_s == int(max_s):
            mx_txt = str(int(max_s))
        else:
            mx_txt = f"{max_s:.1f}"
        
        score_cell = [
            safe_paragraph(f"<font size='7.5'><b>{sc_txt}</b> / {mx_txt}</font>", styles["cell"]),
            Spacer(1, 2),
            bar,
        ]
        
        bm_txt = str(int(benchmark)) if benchmark == int(benchmark) else f"{benchmark:.1f}"

        td.append([
            safe_paragraph(f"<b>{rl_safe(label)}</b>", styles["cell_bold"]),
            score_cell,
            safe_paragraph(f"<font color='{st_color}'><b>{st_label}</b></font>", styles["cell"]),
            safe_paragraph(f"<b>{bm_txt}</b>", styles["cell"]),
        ])

    # ── Table-wise Total & Benchmark Row ──────────────────────
    tbl_bm = None
    if table_benchmarks:
        tbl_bm = table_benchmarks.get(table_title) or table_benchmarks.get(sec_name)

    if tbl_bm:
        tbl_p90 = tbl_bm["P90"]
        tbl_p50 = tbl_bm["P50"]
    else:
        tbl_p90 = round(total_max * (p90_th / 100.0), 1) if total_max else 0
        tbl_p50 = round(total_max * (p50_th / 100.0), 1) if total_max else 0

    if total_scored >= tbl_p90:
        tbl_st_label = "Strong Proficiency"
        tbl_st_color = "#16a34a"
    elif total_scored >= tbl_p50:
        tbl_st_label = "Revision Recommended"
        tbl_st_color = "#ea580c"
    else:
        tbl_st_label = "Foundational Gap"
        tbl_st_color = "#dc2626"

    tbl_bm_txt = str(int(tbl_p90)) if tbl_p90 == int(tbl_p90) else f"{tbl_p90:.1f}"
    tbl_sc_txt = str(int(total_scored)) if total_scored == int(total_scored) else f"{total_scored:.1f}"
    tbl_mx_txt = str(int(total_max)) if total_max == int(total_max) else f"{total_max:.1f}"

    td.append([
        safe_paragraph("<b>Table Total</b>", styles["cell_bold"]),
        safe_paragraph(f"<font size='8'><b>{tbl_sc_txt}</b> / {tbl_mx_txt}</font>", styles["cell_bold"]),
        safe_paragraph(f"<font color='{tbl_st_color}'><b>{tbl_st_label}</b></font>", styles["cell_bold"]),
        safe_paragraph(f"<b>{tbl_bm_txt}</b>", styles["cell_bold"]),
    ])

    cw = [PAGE_W*0.22, PAGE_W*0.33, PAGE_W*0.25, PAGE_W*0.20]
    t  = Table(td, colWidths=cw)
    ts = corporate_table_style(pc)
    ts.add("ALIGN",  (0, 0), (-1, -1), "LEFT")
    ts.add("VALIGN", (0, 0), (-1, -1), "MIDDLE")
    ts.add("LEFTPADDING", (0, 0), (-1, -1), 6)
    ts.add("RIGHTPADDING", (0, 0), (-1, -1), 6)
    ts.add("LINEABOVE",  (0, -1), (-1, -1), 1.2, colors.HexColor(pc))
    ts.add("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#F1F5F9"))
    t.setStyle(ts)
    story.append(t)
    story.append(Spacer(1, 4))



def render_marks_analysis(story, row, cfg, styles):
    """Side-by-side panel: Strong Subjects (≥75%) vs Needs Focus (<50%).
    BUG-01 FIX: function body was empty — no story output was ever produced.
    """
    if not cfg.get("marks_show_analysis", True):
        return

    pc = cfg.get("primary_color", C_NAVY)
    all_items = []
    for sec in cfg.get("sections", []):
        if not isinstance(sec, dict):
            continue
        if not sec.get("enabled", True) or sec.get("type") != "marks_table":
            continue
        items, _, _ = _parse_marks_cols(row, sec)
        all_items.extend(items)

    valid_items = [it for it in all_items if it["pct"] is not None]
    if not valid_items or len(valid_items) < 2:
        return

    strong = [it for it in valid_items if it["pct"] >= 75]
    focus  = [it for it in valid_items if it["pct"] < 50]
    avg    = [it for it in valid_items if 50 <= it["pct"] < 75]

    if not strong and not focus and not avg:
        return

    story.extend(_section_title("Performance Insights", styles, pc))

    def _make_bullets(items, icon, color):
        if not items:
            return [safe_paragraph("<i>None identified.</i>", styles["body_italic"])]
        return [
            safe_paragraph(
                f'<font color="{color}">{icon}</font>  <b>{rl_safe(it["label"])}</b>'
                f'  <font color="#94A3B8">({it["pct"]:.0f}%)</font>',
                styles["bullet_bold"]
            ) for it in items
        ]

    str_paras = _make_bullets(strong, "▲", C_GREEN)
    wk_paras  = _make_bullets(focus,  "▼", C_RED)

    col_data = [[
        safe_paragraph("<b>STRONG PROFICIENCY (≥75%)</b>", styles["section_sub"]),
        safe_paragraph("<b>NEEDS FOCUS (&lt;50%)</b>", styles["section_sub"]),
    ]]
    for i in range(max(len(str_paras), len(wk_paras))):
        l = str_paras[i] if i < len(str_paras) else safe_paragraph("", styles["body"])
        r = wk_paras[i]  if i < len(wk_paras)  else safe_paragraph("", styles["body"])
        col_data.append([l, r])

    col_w = (PAGE_W - 8) / 2
    t = Table(col_data, colWidths=[col_w, col_w])
    t.setStyle(TableStyle([
        ("VALIGN",        (0,0), (-1,-1), "TOP"),
        ("TOPPADDING",    (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 3),
        ("LEFTPADDING",   (0,0), (-1,-1), 7),
        ("RIGHTPADDING",  (0,0), (-1,-1), 7),
        ("BACKGROUND",    (0,0), (0,0),   colors.HexColor("#ECFDF5")),
        ("BACKGROUND",    (1,0), (1,0),   colors.HexColor("#FEF2F2")),
        ("BACKGROUND",    (0,1), (0,-1),  colors.HexColor("#F0FDF4")),
        ("BACKGROUND",    (1,1), (1,-1),  colors.HexColor("#FFF5F5")),
        ("LINEAFTER",     (0,0), (0,-1),  1, colors.HexColor(C_BORDER)),
        ("BOX",           (0,0), (-1,-1), 1, colors.HexColor(C_BORDER)),
    ]))
    story.append(t)

    # Revision Recommended row (50–75%)
    if avg:
        story.append(Spacer(1, 3))
        story.append(safe_paragraph("<b>Revision Recommended (50–74%):</b>", styles["body_bold"]))
        for it in avg:
            story.append(safe_paragraph(
                f'<font color="{C_ORANGE}">●</font>  <b>{rl_safe(it["label"])}</b>'
                f'  <font color="#94A3B8">({it["pct"]:.0f}%)</font>',
                styles["bullet"]
            ))

    story.append(Spacer(1, 3))

_cohort_cache_lock = threading.Lock()

def precompute_cohort_cache(batch_df, cfg):
    """
    Precompute invariant class-level distributions and percentiles for the entire batch.
    Takes O(N) time once, enabling subsequent per-student lookups to run in O(1) time
    rather than scanning the entire DataFrame O(N) times repeatedly.
    """
    marks_sections = [
        s for s in cfg.get("sections", [])
        if isinstance(s, dict) and s.get("type") == "marks_table" and s.get("enabled", True)
    ]
    if not marks_sections or batch_df is None or len(batch_df) < 2:
        return None

    import re as _r
    import numpy as np

    total_students = len(batch_df)
    p90_th = float(cfg.get("benchmark_strong", 90))
    p50_th = float(cfg.get("benchmark_revision", 50))

    parsed_sections = []
    for sec in marks_sections:
        sec_title = sec.get("title") or sec.get("name") or "Module-wise Performance"
        sec_name  = sec.get("name") or sec_title

        batch_scores = []
        for _, brow in batch_df.iterrows():
            _, ts, tm = _parse_marks_cols(brow, sec)
            batch_scores.append(ts)

        sec_p90 = np.percentile(batch_scores, p90_th) if batch_scores else 0.0
        sec_p50 = np.percentile(batch_scores, p50_th) if batch_scores else 0.0
        sec_avg = round(sum(batch_scores) / total_students, 1) if total_students > 0 else 0.0

        col_data = []
        col_cfg = sec.get("col_cfg", {})
        for col in sec.get("columns", []):
            old_label = col.split("[")[-1].replace("]", "").strip() if "[" in col else col
            old_label = _r.sub(r'\(?\s*out\s+of\s+[\d.]+\s*\)?', '', old_label, flags=_r.IGNORECASE).strip().strip("()")

            cfg_for_col = col_cfg.get(col, {})
            label = cfg_for_col.get("label", old_label)
            if not label:
                continue

            m = _r.search(r'out\s+of\s+([\d.]+)', col, _r.IGNORECASE)
            max_from_header = float(m.group(1)) if m else None
            max_s = float(cfg_for_col.get("max")) if cfg_for_col.get("max") else max_from_header
            if not max_s or max_s <= 0:
                continue

            def _col_score(r, c=col):
                raw = str(r[c]).strip() if c in r.index else "nan"
                if raw.lower() in ("nan", "na", "", "n/a"):
                    return 0.0
                try:
                    return float(raw)
                except ValueError:
                    return 0.0

            def _col_pct(r, c=col, mx=max_s):
                sc = _col_score(r, c)
                return round((sc / mx) * 100, 2)

            col_raw_scores = [_col_score(r) for _, r in batch_df.iterrows()]
            col_scores_pct = [_col_pct(r) for _, r in batch_df.iterrows()]

            mod_p90 = np.percentile(col_raw_scores, p90_th)
            mod_p50 = np.percentile(col_raw_scores, p50_th)
            bm_val = {"P90": round(float(mod_p90), 1), "P50": round(float(mod_p50), 1)}

            col_data.append({
                "col": col, "label": label, "max_s": max_s,
                "col_scores_pct": col_scores_pct, "bm_val": bm_val,
                "sec_title": sec_title, "sec_name": sec_name,
            })

        parsed_sections.append({
            "sec": sec, "sec_title": sec_title, "sec_name": sec_name,
            "batch_scores": batch_scores,
            "sec_p90": round(float(sec_p90), 1),
            "sec_p50": round(float(sec_p50), 1),
            "sec_avg": sec_avg,
            "col_data": col_data,
        })

    def _row_total(r):
        scored, mx = 0.0, 0.0
        for sec in marks_sections:
            _, ts, tm = _parse_marks_cols(r, sec)
            scored += ts; mx += tm
        return round((scored / mx) * 100, 2) if mx > 0 else 0.0

    all_totals = [_row_total(r) for _, r in batch_df.iterrows()]
    combined_class_avg = round(sum(all_totals) / total_students, 1)

    return {
        "total_students": total_students,
        "parsed_sections": parsed_sections,
        "all_totals": all_totals,
        "combined_class_avg": combined_class_avg,
        "marks_sections": marks_sections,
        "selected_tbl": cfg.get("mi_benchmark_table", "All Marks Tables (Combined)"),
    }


def _compute_class_stats(row, cfg):
    """
    Compute class-relative stats from the batch DataFrame with high-performance caching.
    Returns: (rank, total_percentile, class_avg, module_pcts, module_benchmarks, table_benchmarks)
      - rank             : int   — student rank (1 = highest scorer)
      - total_percentile : float — percentile based on TOTAL marks across all modules (or selected table)
      - class_avg        : float — class average percentage
      - module_pcts      : dict  — {label: percentile} per subject column
      - module_benchmarks: dict  — {(sec_title, label): {"P90": ..., "P50": ...}} + fallback {label: ...}
      - table_benchmarks : dict  — {sec_title: {"P90": ..., "P50": ..., "rank": ..., "percentile": ..., "score": ..., "max": ..., "class_avg": ...}}
    Returns (None, None, None, None, None, None) if no batch data.
    """
    batch_df = cfg.get("_batch_df")
    if batch_df is None or len(batch_df) < 2:
        return None, None, None, None, None, None

    cohort_cache = cfg.get("_cohort_cache")
    if cohort_cache is None:
        with _cohort_cache_lock:
            cohort_cache = cfg.get("_cohort_cache")
            if cohort_cache is None:
                cohort_cache = precompute_cohort_cache(batch_df, cfg)
                cfg["_cohort_cache"] = cohort_cache

    if not cohort_cache:
        return None, None, None, None, None, None

    # Row memoization: avoids re-computing when called multiple times per report
    row_key = row.name if hasattr(row, 'name') and row.name is not None else tuple(str(v) for v in row.values)
    row_cache = cfg.setdefault("_row_stats_cache", {})
    if row_key in row_cache:
        return row_cache[row_key]

    total_students = cohort_cache["total_students"]
    parsed_sections = cohort_cache["parsed_sections"]

    table_benchmarks = {}
    for ps in parsed_sections:
        sec = ps["sec"]
        _, my_sec_scored, my_sec_max = _parse_marks_cols(row, sec)
        batch_scores = ps["batch_scores"]
        sec_rank = sum(1 for s in batch_scores if s > my_sec_scored) + 1
        sec_pct  = round((1 - (sec_rank - 1) / total_students) * 100, 1)

        tbl_data = {
            "P90": ps["sec_p90"],
            "P50": ps["sec_p50"],
            "rank": sec_rank,
            "percentile": sec_pct,
            "score": my_sec_scored,
            "max": my_sec_max,
            "class_avg": ps["sec_avg"],
            "title": ps["sec_title"],
            "name": ps["sec_name"],
        }
        table_benchmarks[ps["sec_title"]] = tbl_data
        table_benchmarks[ps["sec_name"]]  = tbl_data

    # Combined or Selected-Table Overall Ranking
    selected_tbl = cohort_cache["selected_tbl"]
    if selected_tbl and selected_tbl != "All Marks Tables (Combined)" and selected_tbl in table_benchmarks:
        st_data   = table_benchmarks[selected_tbl]
        rank      = st_data["rank"]
        total_pct = st_data["percentile"]
        class_avg = st_data["class_avg"]
    else:
        scored, mx = 0.0, 0.0
        for sec in cohort_cache["marks_sections"]:
            _, ts, tm = _parse_marks_cols(row, sec)
            scored += ts; mx += tm
        my_total   = round((scored / mx) * 100, 2) if mx > 0 else 0.0
        all_totals = cohort_cache["all_totals"]
        rank       = sum(1 for t in all_totals if t > my_total) + 1
        total_pct  = round((1 - (rank - 1) / total_students) * 100, 1)
        class_avg  = cohort_cache["combined_class_avg"]

    # Per-module percentiles
    module_pcts = {}
    module_benchmarks = {}
    for ps in parsed_sections:
        for cd in ps["col_data"]:
            col = cd["col"]
            label = cd["label"]
            max_s = cd["max_s"]

            raw = str(row[col]).strip() if col in row.index else "nan"
            if raw.lower() in ("nan", "na", "", "n/a"):
                sc = 0.0
            else:
                try:
                    sc = float(raw)
                except ValueError:
                    sc = 0.0

            my_col_pct = round((sc / max_s) * 100, 2)
            col_scores_pct = cd["col_scores_pct"]
            col_rank   = sum(1 for p in col_scores_pct if p > my_col_pct) + 1

            pct_val = round((1 - (col_rank - 1) / total_students) * 100, 1)
            bm_val = cd["bm_val"]
            sec_title = cd["sec_title"]
            sec_name = cd["sec_name"]

            module_pcts[label] = pct_val
            module_pcts[f"{sec_title}::{label}"] = pct_val
            module_benchmarks[f"{sec_title}::{label}"] = bm_val
            module_benchmarks[f"{sec_name}::{label}"]  = bm_val
            module_benchmarks[label] = bm_val
            module_benchmarks[col]   = bm_val

    result = (rank, total_pct, class_avg, module_pcts, module_benchmarks, table_benchmarks)
    row_cache[row_key] = result
    return result


def render_marks_motivation(story, row, cfg, styles, groq_client=None):
    """
    Renders Overall Performance, Overall Readiness Summary, 
    Strengths and Improvement Areas, and Next Steps based on the user's reference image.
    """
    pc = cfg.get("primary_color", C_NAVY)

    # ── Collect marks from marks_table sections only ───────────────
    all_items = []
    total_scored, total_max = 0.0, 0.0
    marks_table_count = 0
    marks_sections = [
        s for s in cfg.get("sections", [])
        if isinstance(s, dict) and s.get("enabled", True) and s.get("type") == "marks_table"
    ]
    for sec in marks_sections:
        marks_table_count += 1
        items, ts, tm = _parse_marks_cols(row, sec)
        sec_title = sec.get("title") or sec.get("name") or "Module-wise Performance"
        sec_name  = sec.get("name") or sec_title
        for it in items:
            it["sec_title"] = sec_title
            it["sec_name"]  = sec_name
        all_items.extend(items)
        total_scored += ts
        total_max    += tm

    if marks_table_count == 0:
        return

    if not all_items or total_max == 0:
        return

    # ── Class statistics (6-value return) ─────────────────────────
    rank, total_percentile, class_avg, module_pcts, module_benchmarks, table_benchmarks = _compute_class_stats(row, cfg)
    
    selected_tbl = cfg.get("mi_benchmark_table", "All Marks Tables (Combined)")
    is_table_specific = (
        selected_tbl and 
        selected_tbl != "All Marks Tables (Combined)" and 
        table_benchmarks and 
        selected_tbl in table_benchmarks
    )

    if is_table_specific:
        st_info = table_benchmarks[selected_tbl]
        tbl_scored = st_info["score"]
        tbl_max = st_info["max"]
        eval_metric = total_percentile if (total_percentile is not None) else (round((tbl_scored / tbl_max) * 100, 1) if tbl_max > 0 else 0.0)
    else:
        # Use percentile if available, fallback to raw % if not.
        eval_metric = total_percentile if (total_percentile is not None) else round((total_scored / total_max) * 100, 1)

    # BUG-03 FIX: use the user-configured pass threshold for standing labels
    # instead of hardcoded 90/75/50 breakpoints.
    pass_threshold = cfg.get("marks_pass_threshold", 75)
    top_cut   = max(pass_threshold + 15, 90)   # "top performers" band
    upper_cut = pass_threshold                  # passing band
    lower_cut = max(pass_threshold - 25, 25)    # below-passing band

    if eval_metric >= top_cut:
        standing = "Top Performers"
        summary_text = "Your overall performance places you among the strongest candidates in this assessment cohort. You demonstrate a high level of conceptual clarity and applied skills, indicating strong readiness for mock interviews and subsequent hiring evaluations."
    elif eval_metric >= upper_cut:
        standing = f"Above Passing Threshold ({pass_threshold}%)"
        summary_text = "Your overall performance places you among the stronger candidates in this assessment cohort. You demonstrate solid conceptual clarity and applied skills, indicating good readiness for mock interviews and subsequent hiring evaluations."
    elif eval_metric >= lower_cut:
        standing = f"Below Passing Threshold ({pass_threshold}%)"
        summary_text = "Your performance indicates areas of strength as well as opportunities for improvement. Based on your module-wise evaluation, we recommend focusing on the following areas to strengthen your interview readiness."
    else:
        standing = "Needs Significant Improvement"
        summary_text = "Your performance indicates areas of strength as well as opportunities for improvement. Based on your module-wise evaluation, we recommend focusing on the following areas to strengthen your interview readiness."

    shortlist_cut = cfg.get("benchmark_shortlist", 90)
    is_shortlisted = eval_metric >= shortlist_cut

    # ═══════════════════════════════════════════════════════════════
    # SECTION 3 — OVERALL PERFORMANCE
    # ═══════════════════════════════════════════════════════════════
    story.extend(_section_title("Overall Performance", styles, pc))
    
    enable_mock = cfg.get("enable_mock_interview", True)
    
    td_overall = [
        [safe_paragraph("<b>Overall Score</b>", styles["cell_bold"]), safe_paragraph(f"{total_scored:.0f} / {total_max:.0f}", styles["cell"])],
    ]
    if is_table_specific:
        td_overall.append([
            safe_paragraph(f"<b>{rl_safe(selected_tbl)} Score</b>", styles["cell_bold"]),
            safe_paragraph(f"{tbl_scored:.0f} / {tbl_max:.0f} (Percentile: {total_percentile:.1f}%)", styles["cell"])
        ])
    td_overall.append(
        [safe_paragraph("<b>Your Standing</b>", styles["cell_bold"]), safe_paragraph(standing, styles["cell"])]
    )
    if enable_mock:
        elig_label = MOCK_PDF_LABEL_SHORTLISTED if is_shortlisted else MOCK_PDF_LABEL_REVISION
        basis_note = f" (Based on {selected_tbl})" if is_table_specific else ""
        td_overall.append(
            [safe_paragraph(f"<b>Mock Interview Eligibility{basis_note}</b>", styles["cell_bold"]), safe_paragraph(elig_label, styles["cell"])]
        )
    t_overall = Table(td_overall, colWidths=[PAGE_W*0.35, PAGE_W*0.65])
    t_overall.setStyle(accent_table_style(C_GOLD))
    story.append(t_overall)
    story.append(Spacer(1, 3))

    # ═══════════════════════════════════════════════════════════════
    # SECTION 4 — OVERALL READINESS SUMMARY
    # ═══════════════════════════════════════════════════════════════
    story.extend(_section_title("Overall Readiness Summary", styles, pc))
    story.append(safe_paragraph(f"<b>{standing}</b>", styles["body_bold"]))
    story.append(Spacer(1, 2))
    story.append(safe_paragraph(summary_text, styles["body"]))
    story.append(Spacer(1, 3))

    # ═══════════════════════════════════════════════════════════════
    # SECTION 5 — STRENGTHS AND IMPROVEMENT AREAS
    # ═══════════════════════════════════════════════════════════════
    strong = []
    revision = []
    gaps = []

    p90_th = float(cfg.get("benchmark_strong", 90))
    p50_th = float(cfg.get("benchmark_revision", 50))

    for it in all_items:
        score = it["score"]
        label = it["label"]
        max_s = it["max"]
        sec_title = it.get("sec_title", "")
        sec_name  = it.get("sec_name", "")
        
        benchmark_data = None
        if module_benchmarks:
            benchmark_data = (
                module_benchmarks.get(f"{sec_title}::{label}") or
                module_benchmarks.get(f"{sec_name}::{label}") or
                module_benchmarks.get(label) or
                module_benchmarks.get(it.get("col"))
            )

        if benchmark_data:
            benchmark = benchmark_data["P90"]
            p50_benchmark = benchmark_data["P50"]
        else:
            benchmark = round(max_s * (p90_th / 100.0), 1) if max_s else 0
            p50_benchmark = round(max_s * (p50_th / 100.0), 1) if max_s else 0
        
        display_label = f"{label} ({sec_title})" if len(marks_sections) > 1 and sec_title else label
        if score >= benchmark:
            strong.append(display_label)
        elif score >= p50_benchmark:
            revision.append(display_label)
        else:
            gaps.append(display_label)

    if gaps:
        story.append(safe_paragraph(f"• <b>Foundational Gaps:</b> {', '.join(gaps)}", styles["body"]))
    if revision:
        story.append(safe_paragraph(f"• <b>Revision Recommended:</b> {', '.join(revision)}", styles["body"]))
    if strong:
        story.append(safe_paragraph(f"• <b>Strong Proficiency:</b> {', '.join(strong)}", styles["body"]))
        
    story.append(Spacer(1, 4))

    # ═══════════════════════════════════════════════════════════════
    # SECTION 6 — NEXT STEPS
    # ═══════════════════════════════════════════════════════════════
    story.append(safe_paragraph("<b>Next Steps:</b>", styles["body_bold"]))
    story.append(Spacer(1, 2))
    if enable_mock and is_shortlisted:
        story.append(safe_paragraph("• You may proceed to schedule your mock interview using the link below.", styles["body"]))
        
        ft_deadline = cfg.get("footer_deadline_text", "").strip()
        if ft_deadline:
            story.append(safe_paragraph(f"• {ft_deadline}", styles["body"]))
            
        ft_mentor = cfg.get("footer_mentor_text", "").strip()
        if ft_mentor:
            story.append(safe_paragraph(f"• {ft_mentor}", styles["body"]))
            
        ft_url = cfg.get("footer_link_url", "")
        ft_txt = cfg.get("footer_link_text", "Schedule Mock Interview")
        if ft_url:
            story.append(safe_paragraph(f"• <font color='blue'><u><a href='{ft_url}'>{ft_txt}</a></u></font>", styles["body"]))
    else:
        if gaps:
            story.append(safe_paragraph("• Modules marked as Foundational Gap indicate core concepts that require structured relearning. Begin by revisiting fundamentals and practicing from the basics in these areas.", styles["body"]))
        if revision:
            story.append(safe_paragraph("• Modules marked as Revision Recommended reflect partial understanding. Use focused revision and practice to strengthen accuracy and confidence in these areas.", styles["body"]))
        if not gaps and not revision:
            story.append(safe_paragraph("• Continue strengthening your understanding across all modules and maintain consistent practice.", styles["body"]))

        if enable_mock:
            story.append(safe_paragraph("• Once you feel prepared, connect with your batch mentor to discuss readiness for mock interviews.", styles["body"]))
        else:
            story.append(safe_paragraph("• Connect with your batch mentor to review your performance and clarify any doubts regarding your learning path.", styles["body"]))
        
    story.append(Spacer(1, 4))



def render_overview(story, pdata, cfg, styles):
    """Performance overview — adapts to marks-only or rating-only or mixed data."""
    if not cfg.get("show_performance_summary", True):
        return

    pc      = cfg.get("primary_color", C_NAVY)
    overall = pdata["overall"]
    grade   = pdata["grade"]
    verdict = pdata["verdict"]
    v_color = pdata["v_color"]

    # ── Compute marks-based summary if no rating skills exist ──────
    marks_scored = 0.0
    marks_max    = 0.0

    # We re-compute from cfg sections directly using row stored in pdata if present
    row_ref = pdata.get("_row")
    if row_ref is not None:
        for sec in cfg.get("sections", []):
            if not isinstance(sec, dict) or not sec.get("enabled", True) or sec.get("type") != "marks_table":
                continue
            _, ts, tm = _parse_marks_cols(row_ref, sec)
            marks_scored += ts
            marks_max    += tm

    has_marks_sections = any(
        isinstance(s, dict) and s.get("type") == "marks_table" and s.get("enabled", True)
        for s in cfg.get("sections", [])
    )
    has_ratings = bool(pdata.get("skills"))

    # Skip only when there are no rating skills AND no marks data.
    if not has_ratings and not has_marks_sections:
        return

    block = []
    block.extend(_section_title("Performance Overview", styles, pc))

    if not has_ratings and has_marks_sections and marks_max > 0:
        # Marks-only mode
        pct = (marks_scored / marks_max) * 100
        from constants import grade_label_from_pct
        grade_l, v_color_m, verdict_m = grade_label_from_pct(pct)
        cols_data = [["Overall Score", "Grade", "Verdict"]]
        cols_data.append([
            safe_paragraph(f"<b>{marks_scored:.0f} / {marks_max:.0f} ({pct:.1f}%)</b>", styles["cell_bold"]),
            safe_paragraph(f"<font color='{v_color_m}'><b>{grade_l}</b></font>", styles["cell_color"]),
            safe_paragraph(f"<font color='{v_color_m}'><b>{verdict_m}</b></font>", styles["cell_color"]),
        ])
    else:
        # Rating-based / mixed mode
        cols_data = [["Technical Score", "Grade", "Verdict"]]
        cols_data.append([
            safe_paragraph(f"<b>{overall:.2f} / 4.00</b>", styles["cell_bold"]),
            safe_paragraph(f"<font color='{v_color}'><b>{grade}</b></font>", styles["cell_color"]),
            safe_paragraph(f"<font color='{v_color}'><b>{verdict}</b></font>", styles["cell_color"]),
        ])

    t = Table(cols_data, colWidths=[PAGE_W / 3] * 3)
    t.setStyle(corporate_table_style(pc))
    block.append(t)
    block.append(Spacer(1, 3))

    story.append(KeepTogether(block))


def render_skill_breakdown(story, pdata, cfg, styles):
    """Compact skill-by-skill breakdown — minimal padding for one-page fit."""
    pc     = cfg.get("primary_color", C_NAVY)
    skills = pdata["skills"]
    if not skills:
        return

    story.extend(_section_title("Mock Interview Skill Breakdown", styles, pc))

    COLOR_MAP = {
        4: "#059669",
        3: "#16a34a",
        2: "#d97706",
        1: "#ea580c",
        0: "#dc2626",
    }

    td = [["Technical Area", "Performance Bar", "Level"]]
    for s in skills:
        sc  = s["score"]
        val = s["val"].capitalize()
        c   = COLOR_MAP.get(int(sc), "#dc2626")
        td.append([
            safe_paragraph(rl_safe(s["name"]), styles["cell"]),
            RatingBar(sc, max_score=4.0, width=PAGE_W*0.35, height=12, color=c),
            safe_paragraph(f"<font color='{c}'><b>{rl_safe(val)}</b></font>",
                      styles["cell_color"]),
        ])

    # Compact table style — smaller padding
    compact_ts = corporate_table_style(pc)
    compact_ts.add("TOPPADDING",    (0,0), (-1,-1), 3)
    compact_ts.add("BOTTOMPADDING",(0,0), (-1,-1), 3)
    compact_ts.add("FONTSIZE",      (0,1), (-1,-1), 7.5)

    t = Table(td, colWidths=[PAGE_W*0.43, PAGE_W*0.38, PAGE_W*0.19])
    t.setStyle(compact_ts)
    story.append(t)
    story.append(Spacer(1, 3))


def render_written(story, pdata, cfg, styles):
    if not pdata["has_written"]:
        return

    pc = cfg.get("primary_color", C_NAVY)
    story.extend(_section_title("Written Test Performance", styles, pc))

    wn  = pdata["written_norm"]
    ws  = pdata["written_score"]
    wm  = pdata["written_max"]
    pct = round((ws / wm) * 100, 1) if wm > 0 else 0

    if wn >= 3.5: lvl, c = "Excellent", "#059669"
    elif wn >= 3.0: lvl, c = "Good",    "#16a34a"
    elif wn >= 2.5: lvl, c = "Average", "#d97706"
    elif wn >= 1.5: lvl, c = "Below Average", "#ea580c"
    else:            lvl, c = "Critical Gap",  "#dc2626"

    td = [
        ["Written Score", "Percentage", "Level"],
        [
            safe_paragraph(f"<b>{ws} / {wm}</b>", styles["cell_bold"]),
            safe_paragraph(f"<b>{pct}%</b>",       styles["cell_bold"]),
            safe_paragraph(f"<font color='{c}'><b>{lvl}</b></font>", styles["cell_color"]),
        ],
    ]

    t = Table(td, colWidths=[PAGE_W * 0.33, PAGE_W * 0.33, PAGE_W * 0.34])
    t.setStyle(corporate_table_style(pc))
    story.append(t)
    story.append(Spacer(1, 3))


def render_highlights(story, pdata, cfg, styles):
    """Compact Strengths & Improvements — side-by-side, max 2 items each."""
    pc = cfg.get("primary_color", C_NAVY)

    strengths  = pdata["strengths"][:2]   # cap at 2 for one-page
    weaknesses = pdata["weaknesses"][:2]

    # Only render if there is something to show
    if not strengths and not weaknesses:
        return

    story.extend(_section_title("Strengths & Improvement Areas", styles, pc))

    def _mk(items, icon, color):
        if not items:
            return [safe_paragraph("<i>None identified.</i>", styles["body_italic"])]
        return [
            safe_paragraph(
                f'<font color="{color}">{icon}</font>  <b>{rl_safe(s["name"])}</b>'
                f'  <font color="#94A3B8">({s["val"].capitalize()})</font>',
                styles["bullet_bold"]
            ) for s in items
        ]

    str_paras = _mk(strengths,  "▲", "#059669")
    wk_paras  = _mk(weaknesses, "▼", "#dc2626")

    col_data = [[safe_paragraph("<b>STRENGTHS</b>", styles["section_sub"]),
                 safe_paragraph("<b>AREAS TO IMPROVE</b>", styles["section_sub"])]]
    for i in range(max(len(str_paras), len(wk_paras))):
        l = str_paras[i] if i < len(str_paras) else safe_paragraph("", styles["body"])
        r = wk_paras[i]  if i < len(wk_paras)  else safe_paragraph("", styles["body"])
        col_data.append([l, r])

    col_w = (PAGE_W - 8) / 2
    t = Table(col_data, colWidths=[col_w, col_w])
    t.setStyle(TableStyle([
        ("VALIGN",        (0,0), (-1,-1), "TOP"),
        ("TOPPADDING",    (0,0), (-1,-1), 4),
        ("BOTTOMPADDING",(0,0), (-1,-1), 3),
        ("LEFTPADDING",   (0,0), (-1,-1), 7),
        ("RIGHTPADDING",  (0,0), (-1,-1), 7),
        ("BACKGROUND",    (0,0), (0,0),   colors.HexColor("#ECFDF5")),
        ("BACKGROUND",    (1,0), (1,0),   colors.HexColor("#FEF2F2")),
        ("BACKGROUND",    (0,1), (0,-1),  colors.HexColor("#F0FDF4")),
        ("BACKGROUND",    (1,1), (1,-1),  colors.HexColor("#FFF5F5")),
        ("LINEAFTER",     (0,0), (0,-1),  1, colors.HexColor(C_BORDER)),
        ("BOX",           (0,0), (-1,-1), 1, colors.HexColor(C_BORDER)),
    ]))
    story.append(t)
    story.append(Spacer(1, 3))


def render_action_plan(story, pdata, cfg, styles, groq_client=None):
    """
    Action Plans (Complete Assessment) — Ultra-concise bullet lists in side-by-side shaded cards.
    If groq_client is passed, applies AI polishing automatically.
    """
    # ── Master Toggle Check ──────────────────────────────────────
    if not cfg.get("show_action_plan", True):
        return

    pc = cfg.get("primary_color", C_NAVY)

    show_tech = cfg.get("show_tech_action", True)
    show_comm = cfg.get("show_comm_action", True)

    tech_list = pdata.get("tech_actions", []) if show_tech else []
    comm_list = pdata.get("comm_actions", []) if show_comm else []
    gen_plan  = pdata.get("action_plan", "")

    if not tech_list and not comm_list and not gen_plan:
        return

    story.extend(_section_title("Personalised Action Plan", styles, pc))

    if gen_plan and not tech_list and not comm_list:
        inner = [
            safe_paragraph("<b>Recommended Action Items & Growth Plan</b>", styles["section_sub"]),
            Spacer(1, 4),
            safe_paragraph(md_to_rl(gen_plan), styles["body"])
        ]
        t = Table([[inner]], colWidths=[PAGE_W])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), colors.HexColor("#EEF2FF")),
            ("TOPPADDING", (0,0), (-1,-1), 6),
            ("BOTTOMPADDING", (0,0), (-1,-1), 6),
            ("LEFTPADDING", (0,0), (-1,-1), 10),
            ("RIGHTPADDING", (0,0), (-1,-1), 10),
            ("BOX", (0,0), (-1,-1), 0.5, colors.HexColor(C_BORDER)),
            ("LINEBEFORE", (0,0), (0,-1), 2.5, colors.HexColor(pc)),
        ]))
        story.append(t)
        story.append(Spacer(1, 3))
        return

    def _render_bullet_box(title, items, icon_c, bg_c, domain="technical"):
        if not items: return None
        
        display_items = items
        if groq_client:
            sys_prompt = "You are an elite career coach. Be extremely concise. Generate quick bullet points."
            raw_str = chr(10).join(items)
            prompt = f"Convert these {domain} flagged improvement areas into 2 to 3 ultra-concise, actionable bullet points:\n{raw_str}\nReturn ONLY the bullet points starting with '-'. No intro text, no closing text."
            try:
                res = groq_client.chat.completions.create(
                    model=cfg.get("ai_model", "llama-3.3-70b-versatile"),
                    messages=[
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.3
                )
                txt = res.choices[0].message.content
                display_items = [x.lstrip("-").strip() for x in txt.split("\n") if x.strip()]
            except Exception:
                pass
                
        inner = [safe_paragraph(f"<b>{title}</b>", styles["section_sub"]), Spacer(1, 4)]
        for item in display_items:
            inner.append(safe_paragraph(f'<font color="{colors.HexColor(icon_c)}">◆</font>  {rl_safe(item)}', styles["bullet"]))
            
        col_width = (PAGE_W - 10) / 2 if (show_tech and tech_list and show_comm and comm_list) else PAGE_W
        t = Table([[inner]], colWidths=[col_width])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), colors.HexColor(bg_c)),
            ("TOPPADDING", (0,0), (-1,-1), 4),
            ("BOTTOMPADDING", (0,0), (-1,-1), 5),
            ("LEFTPADDING", (0,0), (-1,-1), 10),
            ("RIGHTPADDING", (0,0), (-1,-1), 10),
            ("BOX", (0,0), (-1,-1), 0.5, colors.HexColor(C_BORDER)),
            ("LINEBEFORE", (0,0), (0,-1), 2.5, colors.HexColor(icon_c)),
        ]))
        return t

    boxes = []
    if show_tech and tech_list:
        boxes.append(_render_bullet_box("Technical Improvement Focus", tech_list, C_NAVY, "#EEF2FF", "technical"))
    if show_comm and comm_list:
        boxes.append(_render_bullet_box("Soft-Skills Improvement Focus", comm_list, C_GOLD, "#FFFBEB", "communication/behavioral"))

    if len(boxes) == 2:
        wt = Table([[boxes[0], boxes[1]]], colWidths=[(PAGE_W-10)/2, (PAGE_W-10)/2])
        wt.setStyle(TableStyle([
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("LEFTPADDING", (0,0), (-1,-1), 0),
            ("RIGHTPADDING", (0,0), (-1,-1), 0),
            ("TOPPADDING", (0,0), (-1,-1), 0),
            ("BOTTOMPADDING", (0,0), (-1,-1), 0),
        ]))
        story.append(wt)
    elif len(boxes) == 1:
        # BUG-11 FIX: never mutate ReportLab's private _argW attribute directly.
        # Rebuild the single box as a full-width wrapper table instead.
        wt = Table([[boxes[0]]], colWidths=[PAGE_W])
        wt.setStyle(TableStyle([
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("LEFTPADDING", (0,0), (-1,-1), 0),
            ("RIGHTPADDING", (0,0), (-1,-1), 0),
            ("TOPPADDING", (0,0), (-1,-1), 0),
            ("BOTTOMPADDING", (0,0), (-1,-1), 0),
        ]))
        story.append(wt)

    story.append(Spacer(1, 3))


def render_remarks(story, pdata, cfg, styles):
    """Compact instructor remarks block."""
    remarks = pdata.get("remarks", "")
    if not remarks or remarks.lower() in ("nan", "na", "n/a", ""):
        return

    pc = cfg.get("primary_color", C_NAVY)
    story.extend(_section_title("Instructor Remarks", styles, pc))

    rd = [[safe_paragraph(md_to_rl(remarks), styles["body"])]]
    rt = Table(rd, colWidths=[PAGE_W])
    rt.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), colors.HexColor("#F8FAFC")),
        ("TOPPADDING",    (0,0), (-1,-1), 2),
        ("BOTTOMPADDING",(0,0), (-1,-1), 2),
        ("LEFTPADDING",   (0,0), (-1,-1), 8),
        ("RIGHTPADDING",  (0,0), (-1,-1), 8),
        ("LINEBEFORE",    (0,0), (0,-1),  2.5, colors.HexColor(C_GOLD)),
        ("BOX",           (0,0), (-1,-1), 0.4, colors.HexColor(C_BORDER)),
    ]))
    story.append(rt)
    story.append(Spacer(1, 3))


def render_footer_declaration(story, cfg, styles):
    """Footer Notes matching the reference image."""
    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", thickness=0.3,
                             color=colors.HexColor(C_BORDER),
                             spaceAfter=2, spaceBefore=0))
def render_honors_certificate(story, pdata, cfg, styles):
    """Optional Page 2 Certificate of Distinction for high achievers (Grade A / A+)."""
    if not cfg.get("show_honors_certificate", False):
        return
    
    grade = str(pdata.get("grade", ""))
    overall = pdata.get("overall", 0.0)
    if "A" not in grade and overall < 3.0:
        return

    pc = cfg.get("primary_color", C_NAVY)
    company = cfg.get("company_name", "Innomatics Research Labs")
    stuname = pdata.get("_sname", "Learner")

    story.append(PageBreak())
    story.append(Spacer(1, 40))
    story.append(safe_paragraph(f"<font color='{pc}' size='22'><b>CERTIFICATE OF DISTINCTION</b></font>", styles["center"]))
    story.append(Spacer(1, 10))
    story.append(safe_paragraph("<i>This Certificate of Outstanding Performance is proudly awarded to</i>", styles["center"]))
    story.append(Spacer(1, 15))
    story.append(safe_paragraph(f"<font size='18'><b>{rl_safe(stuname)}</b></font>", styles["center"]))
    story.append(Spacer(1, 15))
    story.append(safe_paragraph(f"For demonstrating exceptional proficiency and scoring top honors (<b>Grade {grade}</b>) in the <b>{rl_safe(cfg.get('report_title', 'Assessment'))}</b>.", styles["center"]))
    story.append(Spacer(1, 25))
    
    cert_table = Table([[
        safe_paragraph(f"<b>Issued by:</b> {rl_safe(company)}", styles["cell"]),
        safe_paragraph("<b>Status:</b> Verified Excellence", styles["cell"])
    ]], colWidths=[(PAGE_W-20)/2, (PAGE_W-20)/2])
    cert_table.setStyle(TableStyle([
        ("BOX", (0,0), (-1,-1), 1, colors.HexColor(pc)),
        ("PADDING", (0,0), (-1,-1), 10),
        ("BACKGROUND", (0,0), (-1,-1), colors.HexColor("#F8FAFC")),
    ]))
    story.append(cert_table)


# ──────────────────────────────────────────────────────────────────
# MAIN EXPORT
# ──────────────────────────────────────────────────────────────────

def build_single_report(row, cfg: dict, groq_client=None,
                         logo_bytes=None,
                         rank=None, total_students=None,
                         report_id=None) -> bytes:
    global _LOGO_BYTES

    # BUG-02 FIX: resolve logo bytes locally and pass via closure instead of
    # relying on the shared global — makes each call thread-safe.
    _resolved_logo = logo_bytes
    if not _resolved_logo:
        # Try disk fallback and update the module-level cache
        if not _LOGO_BYTES:
            for lp in ["logo/innomatics_logo.png", "logo/logo.png", "logo/company_logo.png"]:
                if os.path.exists(lp):
                    try:
                        with open(lp, "rb") as f:
                            _LOGO_BYTES = f.read()
                        break
                    except Exception:
                        pass
        _resolved_logo = _LOGO_BYTES

    pc           = cfg.get("primary_color", C_NAVY)
    company      = cfg.get("company_name", "Company")
    report_title = cfg.get("report_title", "Technical Evaluation Report")
    styles       = build_styles(pc)

    # Extract watermark & legal notice options
    watermark_text = cfg.get("watermark_text", None)
    if not watermark_text and cfg.get("show_watermark", False):
        watermark_text = "CONFIDENTIAL"
    legal_notice = cfg.get("legal_notice", None)

    # ── Page geometry (report_theme.py) ─────────────────────────────────────
    # Defaults reproduce the previous hardcoded A4-portrait behaviour exactly.
    margin_left  = float(cfg.get("margin_left",  40))
    margin_right = float(cfg.get("margin_right", 40))
    _pw, _ph, content_w, final_pagesize = get_page_geometry(cfg)
    # Store content_w in cfg so render helpers (render_identity intro card) can read it
    cfg["_content_w"] = content_w

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=final_pagesize,
        rightMargin=margin_right, leftMargin=margin_left,
        topMargin=122,    # space for decorative centered header band
        bottomMargin=32,
    )

    # Capture resolved parameters in closure so each report call is independent
    def first_page(canvas, doc):
        _draw_first_page_header(canvas, doc, pc, report_title, _resolved_logo, watermark_text, legal_notice)

    def later_pages(canvas, doc):
        _draw_later_page_header(canvas, doc, pc, report_title, _resolved_logo, watermark_text, legal_notice)

    story = []
    pdata = compute_student_data(row, cfg, rank=rank, total_students=total_students)

    # ── Detect what kind of data this report contains ───────────
    has_marks   = any(s.get("type") == "marks_table" and s.get("enabled", True)
                      for s in cfg.get("sections", []))
    has_ratings = bool(pdata.get("skills"))

    # ── Report layout ───────────────────────────────────────────
    render_identity(story, row, cfg, styles, pdata)
    render_written(story, pdata, cfg, styles)

    # Marks sections — table + insight blocks
    for sec in cfg.get("sections", []):
        if not isinstance(sec, dict):
            continue
        if sec.get("enabled", True) and sec.get("type") == "marks_table":
            render_marks_table(story, row, sec, cfg, styles)

    if has_marks:
        render_marks_motivation(story, row, cfg, styles)   # grade badge + message
        render_marks_analysis(story, row, cfg, styles)     # strong vs focus subjects

    # Rating-based sections (skip when not present)
    if has_ratings:
        render_skill_breakdown(story, pdata, cfg, styles)

    # Personalised Action Plan (renders for all report types whenever action items or toggles exist)
    render_action_plan(story, pdata, cfg, styles, groq_client)

    render_overview(story, pdata, cfg, styles)
    render_remarks(story, pdata, cfg, styles)
    render_footer_declaration(story, cfg, styles)
    render_honors_certificate(story, pdata, cfg, styles)

    doc.build(story, onFirstPage=first_page, onLaterPages=later_pages)
    buf.seek(0)
    return buf.read()


def build_all_reports(
    df: pd.DataFrame,
    cfg: dict,
    groq_client=None,
    logo_bytes: bytes = None,
    id_column: str = "Enrollement ID",
    progress_callback=None,
) -> bytes:
    """Batch export — computes rank before generating if enabled."""
    import re as _re
    import datetime as dt

    show_rank = cfg.get("show_rank", False)
    total     = len(df)

    # Pre-compute ranks if needed
    ranks = [None] * total
    if show_rank:
        # BUG-05 FIX: removed circular self-import (from report_engine import ...)
        # compute_student_data is already in scope in this module.
        import numpy as np
        scores = []
        for _, row in df.iterrows():
            pd_tmp = compute_student_data(row, cfg)
            scores.append(pd_tmp["overall"])
        # Rank 1 = highest score
        order  = sorted(range(total), key=lambda i: scores[i], reverse=True)
        rank_map = {orig: rank + 1 for rank, orig in enumerate(order)}
        ranks    = [rank_map[i] for i in range(total)]

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, (_, row) in enumerate(df.iterrows()):
            try:
                raw_id  = clean_display_val(row[id_column] if id_column in row.index
                              else f"student_{i}")
                safe_id = _re.sub(r'[^A-Za-z0-9_\-]', '_', raw_id)
                pdf_b   = build_single_report(
                    row, cfg, groq_client, logo_bytes,
                    rank=ranks[i], total_students=total,
                    report_id=f"BATCH_{safe_id}_{dt.datetime.now().strftime('%Y%m%d%H%M')}"
                )
                zf.writestr(f"{safe_id}_report.pdf", pdf_b)
            except Exception as e:
                name = str(row.iloc[0]) if len(row) > 0 else f"row_{i}"
                zf.writestr(f"ERROR_{i}_{name}.txt",
                             f"Report generation failed:\n{e}")
            if progress_callback:
                progress_callback(i + 1, total)

    zip_buf.seek(0)
    return zip_buf.read()
