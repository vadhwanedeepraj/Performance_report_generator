import io
import re
import os
import zipfile
import hashlib
import time
import json
from datetime import date
from constants import (
    RATING_MAP, GRADE_SCALE_0_4, AI_CACHE_DIR, AI_CACHE_TTL_DAYS,
    MOCK_PDF_LABEL_SHORTLISTED, MOCK_PDF_LABEL_REVISION,
)
from report_theme import (
    get_page_geometry, build_styles as build_theme_styles,
    build_corporate_table_style, build_accent_table_style,
    resolve_font_family,
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

# ──────────────────────────────────────────────────────────────────
# SCORING ENGINE & AI CACHING
# ──────────────────────────────────────────────────────────────────

os.makedirs(AI_CACHE_DIR, exist_ok=True)

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
    """Convert a cell value to a clean string, stripping pandas float .0 if integer."""
    s = str(val).strip()
    try:
        f = float(s)
        if f == int(f):
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
    """Safely build a Paragraph. If XML parsing crashes, strip unclosed tags and fallback to plain text."""
    try:
        return Paragraph(str(text), style)
    except Exception:
        clean = re.sub(r'<[^>]*>', '', str(text))
        clean = clean.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return Paragraph(clean, style)


def md_to_rl(text: str) -> str:
    if not text:
        return ""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")
    text = text.replace("&lt;i&gt;", "<i>").replace("&lt;/i&gt;", "</i>")
    text = text.replace("&lt;u&gt;", "<u>").replace("&lt;/u&gt;", "</u>")
    text = text.replace("&lt;/font&gt;", "</font>")
    text = text.replace("&lt;br/&gt;", "<br/>").replace("&lt;br&gt;", "<br/>")
    text = re.sub(r'&lt;font\s+color\s*=\s*([\'"]?)(#[a-fA-F0-9]{6}|[a-zA-Z]+)\1\s*&gt;', r'<font color="\2">', text)

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


class RatingBar(Flowable):
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
        c.setFillColor(colors.HexColor("#E2E8F0"))
        c.roundRect(0, 0, bw, bh, 4, fill=1, stroke=0)
        fill_w = int((self.score / self.max_score) * bw)
        if fill_w > 0:
            c.setFillColor(self.color)
            c.roundRect(0, 0, fill_w, bh, 4, fill=1, stroke=0)
            c.setFillColor(colors.white)
            c.setFont("Helvetica-Bold", 7.5)
            if fill_w > 20:
                c.drawRightString(fill_w - 4, 3.5, f"{self.score:.1f}")
            else:
                c.setFillColor(colors.HexColor("#334155"))
                c.drawString(fill_w + 4, 3.5, f"{self.score:.1f}")


class MarksBar(Flowable):
    def __init__(self, pct, width=120, height=12, color="#1B3A6B"):
        super().__init__()
        self.pct       = max(0.0, min(pct, 100.0))
        self.bar_width = width
        if self.pct >= 75:
            self.color = colors.HexColor("#16a34a")
        elif self.pct >= 40:
            self.color = colors.HexColor("#ea580c")
        else:
            self.color = colors.HexColor("#dc2626")
        self.width     = width
        self.height    = height + 4

    def draw(self):
        c  = self.canv
        bw = self.bar_width
        bh = 14
        c.setFillColor(colors.HexColor("#E5E7EB"))
        c.roundRect(0, 0, bw, bh, 4, fill=1, stroke=0)
        fw = int((self.pct / 100.0) * bw)
        if fw > 0:
            c.setFillColor(self.color)
            c.roundRect(0, 0, fw, bh, 4, fill=1, stroke=0)
        if fw > 28:
            c.setFillColor(colors.white)
            c.setFont("Helvetica-Bold", 7)
            c.drawRightString(fw - 4, 3.5, f"{self.pct:.0f}%")
        else:
            c.setFillColor(colors.HexColor("#334155"))
            c.setFont("Helvetica-Bold", 7)
            c.drawString(fw + 4, 3.5, f"{self.pct:.0f}%")


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

    total_marks_scored = 0.0
    total_marks_max    = 0.0

    for sec in all_sections:
        if isinstance(sec, dict) and not sec.get("enabled", True):
            continue
        stype = sec.get("type", "") if isinstance(sec, dict) else ""
        sname = sec.get("name", "") if isinstance(sec, dict) else ""

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
                        if score <= 2:
                            clean_c = clean_label(col)
                            if is_comm:
                                comm_actions.append(clean_c)
                            else:
                                tech_actions.append(clean_c)

        elif stype == "written_test":
            col_data = sec.get("columns", {})
            col_items = col_data.items() if isinstance(col_data, dict) else [(c, {}) for c in col_data]
            for col, meta in col_items:
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
            items, ts, tm = _parse_marks_cols(row, sec)
            total_marks_scored += ts
            total_marks_max    += tm
            for it in items:
                if it.get("pct") is not None and it["pct"] < 75:
                    lbl = it.get("label", "Module")
                    tech_actions.append(f"{lbl} (Scored {it['score']}/{it['max']})")

    tech_avg = sum(tech_scores) / len(tech_scores) if tech_scores else 0.0
    comm_avg = sum(comm_scores) / len(comm_scores) if comm_scores else 0.0

    if has_written:
        written_norm = (written_score / written_max) * 4 if written_max > 0 else 0.0
    else:
        written_norm = 0.0

    if tech_scores and total_marks_max > 0:
        marks_norm = (total_marks_scored / total_marks_max) * 4.0
        overall = (tech_avg + marks_norm) / 2.0
    elif tech_scores:
        overall = tech_avg
    elif total_marks_max > 0:
        overall = (total_marks_scored / total_marks_max) * 4.0
    else:
        overall = 0.0

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

    id_cols = cfg.get("identity_columns") or cfg.get("identity_cols", [])
    sname = ""
    for c in id_cols:
        if "name" in c.lower() and c in row.index:
            sname = str(row[c]).strip()
            break
    if not sname:
        for c in row.index:
            if "name" in c.lower():
                sname = str(row[c]).strip()
                break
    if not sname:
        sname = "Learner"

    return {
        "_sname":         sname,
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
        "marks_scored":   total_marks_scored,
        "marks_max":      total_marks_max,
        "skills":         skill_list,
        "strengths":      strengths,
        "weaknesses":     weaknesses,
        "action_plan":    action_plan,
        "remarks":        remarks,
        "tech_actions":   tech_actions,
        "comm_actions":   comm_actions,
        "rank":           rank,
        "total_students": total_students,
        "_row":           row,
    }


def _parse_marks_cols(row, sec):
    items = []
    total_scored, total_max = 0.0, 0.0

    if isinstance(sec, list):
        cols    = sec
        col_cfg = {}
    elif isinstance(sec, dict):
        cols    = sec.get("columns", [])
        col_cfg = sec.get("col_cfg", {}) or {}
    else:
        return items, total_scored, total_max
    
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
        old_label = re.sub(r'\(?\s*out\s+of\s+[\d.]+\s*\)?', '', old_label, flags=re.IGNORECASE).strip().strip("()")
        
        cfg_for_col = col_cfg.get(col, {})
        label = cfg_for_col.get("label", old_label)
        
        m = re.search(r'out\s+of\s+([\d.]+)', col, re.IGNORECASE)
        max_from_header = float(m.group(1)) if m else None
        
        max_s = float(cfg_for_col.get("max")) if cfg_for_col.get("max") else max_from_header
        if max_s is None:
            max_s = 100.0

        pct = round((score / max_s) * 100, 1) if (max_s and max_s > 0) else None
        if max_s and max_s > 0:
            total_scored += score
            total_max    += max_s

        items.append({"label": label, "score": score, "max": max_s, "pct": pct, "col": col})

    return items, total_scored, total_max


def _compute_class_stats(row, cfg):
    import numpy as np

    batch_df = cfg.get("_batch_df")
    if batch_df is None or len(batch_df) < 2:
        return None, None, None, None, None

    marks_sections = [
        s for s in cfg.get("sections", [])
        if isinstance(s, dict)
        and s.get("type") == "marks_table"
        and s.get("enabled", True)
    ]
    if not marks_sections:
        return None, None, None, None, None

    total_students = len(batch_df)

    def _row_total(r):
        scored, mx = 0.0, 0.0
        for sec in marks_sections:
            _, ts, tm = _parse_marks_cols(r, sec)
            scored += ts; mx += tm
        return round((scored / mx) * 100, 2) if mx > 0 else 0.0

    all_totals = [_row_total(r) for _, r in batch_df.iterrows()]
    my_total   = _row_total(row)

    rank       = sum(1 for t in all_totals if t > my_total) + 1
    total_pct  = round((1 - (rank - 1) / total_students) * 100, 1)
    class_avg  = round(sum(all_totals) / total_students, 1)

    module_pcts = {}
    module_benchmarks = {}
    for sec in marks_sections:
        col_cfg = sec.get("col_cfg", {})
        for col in sec.get("columns", []):
            old_label = col.split("[")[-1].replace("]", "").strip() if "[" in col else col
            old_label = re.sub(r'\(?\s*out\s+of\s+[\d.]+\s*\)?', '', old_label, flags=re.IGNORECASE).strip().strip("()")
            
            cfg_for_col = col_cfg.get(col, {})
            label = cfg_for_col.get("label", old_label)
            if not label:
                continue

            m = re.search(r'out\s+of\s+([\d.]+)', col, re.IGNORECASE)
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
            
            my_col_pct = _col_pct(row)
            col_rank   = sum(1 for p in col_scores_pct if p > my_col_pct) + 1
            module_pcts[label] = round((1 - (col_rank - 1) / total_students) * 100, 1)
            
            p90_th = cfg.get("benchmark_strong", 90)
            p50_th = cfg.get("benchmark_revision", 50)
            p90 = np.percentile(col_raw_scores, p90_th)
            p50 = np.percentile(col_raw_scores, p50_th)
            module_benchmarks[label] = {"P90": round(p90, 1), "P50": round(p50, 1)}

    return rank, total_pct, class_avg, module_pcts, module_benchmarks


# ──────────────────────────────────────────────────────────────────
# CANVAS DRAWING: HEADER, FOOTER, WATERMARK
# ──────────────────────────────────────────────────────────────────

_LOGO_BYTES = None

def _draw_watermark(canvas, text, page_w, page_h):
    canvas.saveState()
    canvas.setFont("Helvetica-Bold", 54)
    canvas.setFillColor(colors.HexColor("#E2E8F0"))
    canvas.setFillAlpha(0.12)
    canvas.translate(page_w / 2, page_h / 2)
    canvas.rotate(45)
    canvas.drawCentredString(0, 0, text)
    canvas.restoreState()


def _draw_header_footer(canvas, doc, cfg, page_w, page_h, content_w, logo_bytes=None, is_first_page=True):
    canvas.saveState()

    margin_left = float(cfg.get("margin_left", 40))
    margin_right = float(cfg.get("margin_right", 40))
    pc = cfg.get("primary_color", "#B91C1C")
    gold = cfg.get("gold_accent", "#C9A84C")
    report_title = cfg.get("report_title", "Technical Evaluation Report")
    show_header = cfg.get("show_header", True)
    show_footer = cfg.get("show_footer", True)

    # Watermark
    if cfg.get("show_watermark", False) or cfg.get("watermark_text"):
        wm_txt = cfg.get("watermark_text", "CONFIDENTIAL")
        _draw_watermark(canvas, wm_txt, page_w, page_h)

    # ── Header ─────────────────────────────────────────────
    if show_header:
        resolved_logo = logo_bytes or _LOGO_BYTES
        if not resolved_logo and cfg.get("logo_base64"):
            try:
                import base64
                resolved_logo = base64.b64decode(cfg["logo_base64"])
            except Exception:
                pass
        max_lw = float(cfg.get("logo_width", 260))
        max_lh = float(cfg.get("logo_height", 70))
        logo_align = str(cfg.get("logo_align", "center")).lower().strip()

        # Render Logo
        if resolved_logo and cfg.get("show_logo", True):
            try:
                reader = ImageReader(io.BytesIO(resolved_logo))
                lw, lh = reader.getSize()
                scale = min(max_lw / lw, max_lh / lh)
                disp_w = lw * scale
                disp_h = lh * scale

                if logo_align == "left":
                    x = margin_left
                elif logo_align == "right":
                    x = page_w - margin_right - disp_w
                else: # center
                    x = (page_w - disp_w) / 2
                
                y = page_h - 12 - disp_h
                canvas.drawImage(reader, x, y, disp_w, disp_h, preserveAspectRatio=True, mask='auto')
            except Exception:
                pass

        # Report Title
        canvas.setFillColor(colors.HexColor(pc))
        font_bold = resolve_font_family(cfg.get("font_family"))[1]
        font_reg = resolve_font_family(cfg.get("font_family"))[0]

        title_sz = float(cfg.get("title_font_size", 13))
        canvas.setFont(font_bold, title_sz)
        canvas.drawCentredString(page_w / 2, page_h - 93, report_title)

        # Date
        if cfg.get("show_date", True):
            date_fmt = cfg.get("date_format", "%d %B %Y")
            try:
                d_str = date.today().strftime(date_fmt)
            except Exception:
                d_str = date.today().strftime("%d %B %Y")
            canvas.setFont(font_reg, 8)
            canvas.setFillColor(colors.HexColor("#334155"))
            canvas.drawCentredString(page_w / 2, page_h - 106, d_str)

        # Accent Header Line
        canvas.setStrokeColor(colors.HexColor(gold))
        canvas.setLineWidth(1.2)
        canvas.line(margin_left, page_h - 114, page_w - margin_right, page_h - 114)

    # ── Footer ─────────────────────────────────────────────
    if show_footer:
        # Legal / Disclaimer Notice
        canvas.setFillColor(colors.HexColor("#94A3B8"))
        f_sz = float(cfg.get("footer_font_size", 6.5))
        canvas.setFont(resolve_font_family(cfg.get("font_family"))[0], f_sz)
        footer_text = cfg.get("legal_notice", "This report is system-generated for assessment and evaluation purposes only.")
        canvas.drawCentredString(page_w / 2, 16, footer_text)

        # Page Number
        if cfg.get("show_page_numbers", True):
            p_fmt = cfg.get("page_number_format", "Page {page}")
            p_str = p_fmt.replace("{page}", str(doc.page))
            canvas.setFont(resolve_font_family(cfg.get("font_family"))[0], f_sz + 0.5)
            canvas.drawCentredString(page_w / 2, 26, p_str)

        # Bottom separator rule
        canvas.setStrokeColor(colors.HexColor(cfg.get("table_border_color", "#D1D5DB")))
        canvas.setLineWidth(0.4)
        canvas.line(margin_left, 34, page_w - margin_right, 34)

    canvas.restoreState()


# ──────────────────────────────────────────────────────────────────
# RENDER SECTIONS
# ──────────────────────────────────────────────────────────────────

def _section_title(text, styles, pc, content_w):
    inner = safe_paragraph(f"<b>{text}</b>", styles["section_head"])
    bar_t = Table([[inner]], colWidths=[content_w])
    bar_t.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
        ("LINEBEFORE",   (0, 0), (0, -1),  4, colors.HexColor(pc)),
        ("TOPPADDING",   (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 3),
        ("LEFTPADDING",  (0, 0), (-1, -1), 8),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return [Spacer(1, 4), bar_t, Spacer(1, 2)]


def render_identity(story, row, cfg, styles, pdata, content_w):
    pc       = cfg.get("primary_color", "#B91C1C")
    id_cols  = cfg.get("identity_columns") or cfg.get("identity_cols", [])
    intro    = cfg.get("intro_text", "")
    company  = cfg.get("company_name", "")

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
        story.append(safe_paragraph(resolved_intro, styles["intro"]))
        story.append(Spacer(1, 2))

    td = []
    for col in id_cols:
        if col in row.index:
            val = clean_display_val(row[col])
            if val.lower() not in ("nan", "na", ""):
                td.append([
                    safe_paragraph(f"<b>{rl_safe(col)}</b>", styles["cell_bold"]),
                    safe_paragraph(rl_safe(val), styles["cell"]),
                ])

    if td:
        story.extend(_section_title("Candidate Details", styles, pc, content_w))
        t = Table(td, colWidths=[content_w * 0.30, content_w * 0.70])
        t.setStyle(build_accent_table_style(cfg))
        story.append(t)
        story.append(Spacer(1, cfg.get("section_spacing", 6)))


def render_written(story, pdata, cfg, styles, content_w):
    if not pdata.get("has_written"):
        return

    pc = cfg.get("primary_color", "#B91C1C")
    story.extend(_section_title("Written Test Performance", styles, pc, content_w))

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

    t = Table(td, colWidths=[content_w * 0.33, content_w * 0.33, content_w * 0.34])
    t.setStyle(build_corporate_table_style(cfg))
    story.append(t)
    story.append(Spacer(1, cfg.get("section_spacing", 6)))


def render_marks_table(story, row, section, cfg, styles, content_w):
    pc   = cfg.get("primary_color", "#B91C1C")
    cols = section.get("columns", [])
    if not cols:
        return

    items, total_scored, total_max = _parse_marks_cols(row, section)
    if not items:
        return

    _, _, _, _, module_benchmarks = _compute_class_stats(row, cfg)
    
    table_title = section.get("title") or section.get("name") or "Module-wise Performance"
    story.extend(_section_title(table_title, styles, pc, content_w))
    
    show_note = cfg.get("marks_show_benchmark_note", True)
    raw_note  = cfg.get("marks_benchmark_note", "")
    if show_note and raw_note:
        note_txt = md_to_rl(raw_note)
        story.append(safe_paragraph(note_txt, styles["body"]))
        story.append(Spacer(1, 3))

    td = [["Module", "Your Score", "Readiness Status", "Benchmark\nScore"]]

    for it in items:
        score = it["score"]
        max_s = it["max"]
        label = it["label"]
        
        benchmark_data = module_benchmarks.get(label) if module_benchmarks else None
        if benchmark_data:
            benchmark = benchmark_data["P90"]
            p50_benchmark = benchmark_data["P50"]
        else:
            benchmark = (max_s * 0.9) if max_s else 0
            p50_benchmark = (max_s * 0.5) if max_s else 0
        
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
        bar = MarksBar(pct, width=content_w * 0.25, height=12, color=pc)
        
        sc_txt = str(int(score)) if score == int(score) else str(score)
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

    cw = [content_w * 0.23, content_w * 0.32, content_w * 0.25, content_w * 0.20]
    t  = Table(td, colWidths=cw)
    t.setStyle(build_corporate_table_style(cfg))
    story.append(t)
    story.append(Spacer(1, cfg.get("section_spacing", 4)))


def render_marks_motivation(story, row, cfg, styles, content_w):
    pc = cfg.get("primary_color", "#B91C1C")
    gold = cfg.get("gold_accent", "#C9A84C")

    all_items = []
    total_scored, total_max = 0.0, 0.0
    marks_table_count = 0
    for sec in cfg.get("sections", []):
        if not isinstance(sec, dict):
            continue
        if not sec.get("enabled", True) or sec.get("type") != "marks_table":
            continue
        marks_table_count += 1
        items, ts, tm = _parse_marks_cols(row, sec)
        all_items.extend(items)
        total_scored += ts
        total_max    += tm

    if marks_table_count == 0 or not all_items or total_max == 0:
        return

    rank, total_percentile, class_avg, module_pcts, module_benchmarks = _compute_class_stats(row, cfg)
    eval_metric = total_percentile if (total_percentile is not None) else round((total_scored / total_max) * 100, 1)

    pass_threshold = cfg.get("marks_pass_threshold", 75)
    top_cut   = max(pass_threshold + 15, 90)
    upper_cut = pass_threshold
    lower_cut = max(pass_threshold - 25, 25)

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

    # Check for manual eligibility overrides from Step 4
    overrides = cfg.get("mi_eligibility_overrides", {})
    if overrides:
        for k, ov in overrides.items():
            name_part = k.split("__", 1)[-1].strip() if "__" in k else k.strip()
            for col in row.index:
                if str(row[col]).strip().lower() == name_part.lower():
                    is_shortlisted = bool(ov)
                    break

    enable_mock = cfg.get("enable_mock_interview", True)

    story.extend(_section_title("Overall Performance", styles, pc, content_w))
    
    td_overall = [
        [safe_paragraph("<b>Overall Score</b>", styles["cell_bold"]), safe_paragraph(f"{total_scored:.0f} / {total_max:.0f}", styles["cell"])],
        [safe_paragraph("<b>Your Standing</b>", styles["cell_bold"]), safe_paragraph(standing, styles["cell"])],
    ]
    if enable_mock:
        td_overall.append(
            [safe_paragraph("<b>Mock Interview Eligibility</b>", styles["cell_bold"]),
             safe_paragraph(MOCK_PDF_LABEL_SHORTLISTED if is_shortlisted else MOCK_PDF_LABEL_REVISION, styles["cell"])]
        )
    t_overall = Table(td_overall, colWidths=[content_w * 0.35, content_w * 0.65])
    t_overall.setStyle(build_accent_table_style(cfg))
    story.append(t_overall)
    story.append(Spacer(1, 3))

    story.extend(_section_title("Overall Readiness Summary", styles, pc, content_w))
    story.append(safe_paragraph(f"<b>{standing}</b>", styles["body_bold"]))
    story.append(Spacer(1, 2))
    story.append(safe_paragraph(summary_text, styles["body"]))
    story.append(Spacer(1, 3))

    # Next steps block
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
        if enable_mock:
            story.append(safe_paragraph("• Once you feel prepared, connect with your batch mentor to discuss readiness for mock interviews.", styles["body"]))
        else:
            story.append(safe_paragraph("• Connect with your batch mentor to review your performance and clarify any doubts regarding your learning path.", styles["body"]))
    story.append(Spacer(1, cfg.get("section_spacing", 4)))


def render_marks_analysis(story, row, cfg, styles, content_w):
    if not cfg.get("marks_show_analysis", True):
        return

    pc = cfg.get("primary_color", "#B91C1C")
    all_items = []
    for sec in cfg.get("sections", []):
        if not isinstance(sec, dict) or not sec.get("enabled", True) or sec.get("type") != "marks_table":
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

    story.extend(_section_title("Performance Insights", styles, pc, content_w))

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

    str_paras = _make_bullets(strong, "▲", "#16a34a")
    wk_paras  = _make_bullets(focus,  "▼", "#dc2626")

    col_data = [[
        safe_paragraph("<b>STRONG PROFICIENCY (≥75%)</b>", styles["section_sub"]),
        safe_paragraph("<b>NEEDS FOCUS (&lt;50%)</b>", styles["section_sub"]),
    ]]
    for i in range(max(len(str_paras), len(wk_paras))):
        l = str_paras[i] if i < len(str_paras) else safe_paragraph("", styles["body"])
        r = wk_paras[i]  if i < len(wk_paras)  else safe_paragraph("", styles["body"])
        col_data.append([l, r])

    col_w = (content_w - 8) / 2
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
        ("LINEAFTER",     (0,0), (0,-1),  1, colors.HexColor(cfg.get("table_border_color", "#D1D5DB"))),
        ("BOX",           (0,0), (-1,-1), 1, colors.HexColor(cfg.get("table_border_color", "#D1D5DB"))),
    ]))
    story.append(t)

    if avg:
        story.append(Spacer(1, 3))
        story.append(safe_paragraph("<b>Revision Recommended (50–74%):</b>", styles["body_bold"]))
        for it in avg:
            story.append(safe_paragraph(
                f'<font color="#ea580c">●</font>  <b>{rl_safe(it["label"])}</b>'
                f'  <font color="#94A3B8">({it["pct"]:.0f}%)</font>',
                styles["bullet"]
            ))

    story.append(Spacer(1, cfg.get("section_spacing", 4)))


def render_overview(story, pdata, cfg, styles, content_w):
    if not cfg.get("show_performance_summary", True):
        return

    pc      = cfg.get("primary_color", "#B91C1C")
    overall = pdata["overall"]
    grade   = pdata["grade"]
    verdict = pdata["verdict"]
    v_color = pdata["v_color"]

    marks_scored = 0.0
    marks_max    = 0.0
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

    if not has_ratings and not has_marks_sections:
        return

    block = []
    block.extend(_section_title("Performance Overview", styles, pc, content_w))

    if not has_ratings and has_marks_sections and marks_max > 0:
        pct = (marks_scored / marks_max) * 100
        from constants import grade_label_from_pct
        grade_l, v_color_m, verdict_m = grade_label_from_pct(pct)
        cols_data = [["Overall Score", "Grade", "Verdict"]]
        cols_data.append([
            safe_paragraph(f"<b>{marks_scored:.0f} / {marks_max:.0f} ({pct:.1f}%)</b>", styles["cell_bold"]),
            safe_paragraph(f"<font color='{v_color_m}'><b>{grade_l}</b></font>", styles["cell_color"]),
            safe_paragraph(f"<font color='{v_color_m}'><b>{verdict_m}</b></font>", styles["cell_color"]),
        ])
        t = Table(cols_data, colWidths=[content_w / 3] * 3)
    elif has_ratings and has_marks_sections and marks_max > 0:
        pct = (marks_scored / marks_max) * 100
        from constants import grade_label_from_pct
        grade_l, v_color_m, verdict_m = grade_label_from_pct(pct)
        cols_data = [["Technical Rating", "Marks Score", "Grade", "Verdict"]]
        cols_data.append([
            safe_paragraph(f"<b>{overall:.2f} / 4.00</b>", styles["cell_bold"]),
            safe_paragraph(f"<b>{marks_scored:.0f} / {marks_max:.0f} ({pct:.1f}%)</b>", styles["cell_bold"]),
            safe_paragraph(f"<font color='{v_color_m}'><b>{grade_l}</b></font>", styles["cell_color"]),
            safe_paragraph(f"<font color='{v_color_m}'><b>{verdict_m}</b></font>", styles["cell_color"]),
        ])
        t = Table(cols_data, colWidths=[content_w * 0.25, content_w * 0.35, content_w * 0.20, content_w * 0.20])
    else:
        cols_data = [["Technical Score", "Grade", "Verdict"]]
        cols_data.append([
            safe_paragraph(f"<b>{overall:.2f} / 4.00</b>", styles["cell_bold"]),
            safe_paragraph(f"<font color='{v_color}'><b>{grade}</b></font>", styles["cell_color"]),
            safe_paragraph(f"<font color='{v_color}'><b>{verdict}</b></font>", styles["cell_color"]),
        ])
        t = Table(cols_data, colWidths=[content_w / 3] * 3)
    t.setStyle(build_corporate_table_style(cfg))
    block.append(t)
    block.append(Spacer(1, 3))
    story.append(KeepTogether(block))


def render_skill_breakdown(story, pdata, cfg, styles, content_w, section=None):
    pc     = cfg.get("primary_color", "#B91C1C")
    skills = pdata.get("skills", [])
    if not skills:
        return

    sec_title = section.get("title") or section.get("name") if section else "Mock Interview Skill Breakdown"
    story.extend(_section_title(sec_title, styles, pc, content_w))

    COLOR_MAP = {
        4: "#059669", 3: "#16a34a", 2: "#d97706", 1: "#ea580c", 0: "#dc2626",
    }

    td = [["Technical Area", "Performance Bar", "Level"]]
    for s in skills:
        sc  = s["score"]
        val = s["val"].capitalize()
        c   = COLOR_MAP.get(int(sc), "#dc2626")
        td.append([
            safe_paragraph(rl_safe(s["name"]), styles["cell"]),
            RatingBar(sc, max_score=4.0, width=content_w * 0.35, height=12, color=c),
            safe_paragraph(f"<font color='{c}'><b>{rl_safe(val)}</b></font>", styles["cell_color"]),
        ])

    t = Table(td, colWidths=[content_w * 0.43, content_w * 0.38, content_w * 0.19])
    t.setStyle(build_corporate_table_style(cfg))
    story.append(t)
    story.append(Spacer(1, cfg.get("section_spacing", 4)))


def render_action_plan(story, pdata, cfg, styles, groq_client=None, content_w=None):
    if not cfg.get("show_action_plan", True):
        return

    if content_w is None:
        content_w = float(cfg.get("content_width", 515))

    pc = cfg.get("primary_color", "#B91C1C")
    show_tech = cfg.get("show_tech_action", True)
    show_comm = cfg.get("show_comm_action", True)

    tech_list = pdata.get("tech_actions", []) if show_tech else []
    comm_list = pdata.get("comm_actions", []) if show_comm else []
    gen_plan  = pdata.get("action_plan", "")

    if not tech_list and not comm_list and not gen_plan:
        return

    story.extend(_section_title("Personalised Action Plan", styles, pc, content_w))

    if gen_plan and not tech_list and not comm_list:
        inner = [
            safe_paragraph("<b>Recommended Action Items & Growth Plan</b>", styles["section_sub"]),
            Spacer(1, 4),
            safe_paragraph(md_to_rl(gen_plan), styles["body"])
        ]
        t = Table([[inner]], colWidths=[content_w])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), colors.HexColor("#EEF2FF")),
            ("TOPPADDING", (0,0), (-1,-1), 6),
            ("BOTTOMPADDING", (0,0), (-1,-1), 6),
            ("LEFTPADDING", (0,0), (-1,-1), 10),
            ("RIGHTPADDING", (0,0), (-1,-1), 10),
            ("BOX", (0,0), (-1,-1), 0.5, colors.HexColor(cfg.get("table_border_color", "#D1D5DB"))),
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
            
        col_width = (content_w - 10) / 2 if (show_tech and tech_list and show_comm and comm_list) else content_w
        t = Table([[inner]], colWidths=[col_width])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), colors.HexColor(bg_c)),
            ("TOPPADDING", (0,0), (-1,-1), 4),
            ("BOTTOMPADDING", (0,0), (-1,-1), 5),
            ("LEFTPADDING", (0,0), (-1,-1), 10),
            ("RIGHTPADDING", (0,0), (-1,-1), 10),
            ("BOX", (0,0), (-1,-1), 0.5, colors.HexColor(cfg.get("table_border_color", "#D1D5DB"))),
            ("LINEBEFORE", (0,0), (0,-1), 2.5, colors.HexColor(icon_c)),
        ]))
        return t

    boxes = []
    if show_tech and tech_list:
        boxes.append(_render_bullet_box("Technical Improvement Focus", tech_list, pc, "#EEF2FF", "technical"))
    if show_comm and comm_list:
        boxes.append(_render_bullet_box("Soft-Skills Improvement Focus", comm_list, cfg.get("gold_accent", "#C9A84C"), "#FFFBEB", "communication/behavioral"))

    if len(boxes) == 2:
        wt = Table([[boxes[0], boxes[1]]], colWidths=[(content_w-10)/2, (content_w-10)/2])
        wt.setStyle(TableStyle([
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("LEFTPADDING", (0,0), (-1,-1), 0),
            ("RIGHTPADDING", (0,0), (-1,-1), 0),
            ("TOPPADDING", (0,0), (-1,-1), 0),
            ("BOTTOMPADDING", (0,0), (-1,-1), 0),
        ]))
        story.append(wt)
    elif len(boxes) == 1:
        wt = Table([[boxes[0]]], colWidths=[content_w])
        wt.setStyle(TableStyle([
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("LEFTPADDING", (0,0), (-1,-1), 0),
            ("RIGHTPADDING", (0,0), (-1,-1), 0),
            ("TOPPADDING", (0,0), (-1,-1), 0),
            ("BOTTOMPADDING", (0,0), (-1,-1), 0),
        ]))
        story.append(wt)

    story.append(Spacer(1, cfg.get("section_spacing", 4)))


def render_remarks(story, pdata, cfg, styles, content_w, section=None):
    remarks = pdata.get("remarks", "")
    if not remarks or remarks.lower() in ("nan", "na", "n/a", ""):
        return

    pc = cfg.get("primary_color", "#B91C1C")
    sec_title = section.get("title") or section.get("name") if section else "Instructor Remarks"
    story.extend(_section_title(sec_title, styles, pc, content_w))

    rd = [[safe_paragraph(md_to_rl(remarks), styles["body"])]]
    rt = Table(rd, colWidths=[content_w])
    rt.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), colors.HexColor("#F8FAFC")),
        ("TOPPADDING",    (0,0), (-1,-1), 2),
        ("BOTTOMPADDING", (0,0), (-1,-1), 2),
        ("LEFTPADDING",   (0,0), (-1,-1), 8),
        ("RIGHTPADDING",  (0,0), (-1,-1), 8),
        ("LINEBEFORE",    (0,0), (0,-1),  2.5, colors.HexColor(cfg.get("gold_accent", "#C9A84C"))),
        ("BOX",           (0,0), (-1,-1), 0.4, colors.HexColor(cfg.get("table_border_color", "#D1D5DB"))),
    ]))
    story.append(rt)
    story.append(Spacer(1, cfg.get("section_spacing", 4)))


def render_footer_declaration(story, cfg, styles):
    story.append(Spacer(1, 4))
    story.append(HRFlowable(width="100%", thickness=0.3,
                             color=colors.HexColor(cfg.get("table_border_color", "#D1D5DB")),
                             spaceAfter=2, spaceBefore=0))


def render_honors_certificate(story, pdata, cfg, styles, content_w):
    if not cfg.get("show_honors_certificate", False):
        return
    
    grade = str(pdata.get("grade", ""))
    overall = pdata.get("overall", 0.0)
    if "A" not in grade and overall < 3.0:
        return

    pc = cfg.get("primary_color", "#B91C1C")
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
    ]], colWidths=[(content_w-20)/2, (content_w-20)/2])
    cert_table.setStyle(TableStyle([
        ("BOX", (0,0), (-1,-1), 1, colors.HexColor(pc)),
        ("PADDING", (0,0), (-1,-1), 10),
        ("BACKGROUND", (0,0), (-1,-1), colors.HexColor("#F8FAFC")),
    ]))
    story.append(cert_table)


# ──────────────────────────────────────────────────────────────────
# MAIN EXPORT ENGINE
# ──────────────────────────────────────────────────────────────────

def build_single_report(row, cfg: dict, groq_client=None,
                         logo_bytes=None,
                         rank=None, total_students=None,
                         report_id=None) -> bytes:
    global _LOGO_BYTES

    _resolved_logo = logo_bytes
    if not _resolved_logo and cfg.get("logo_base64"):
        try:
            import base64
            _resolved_logo = base64.b64decode(cfg["logo_base64"])
        except Exception:
            pass
    if not _resolved_logo:
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

    # Dynamic Page Geometry
    page_w, page_h, content_w, final_pagesize = get_page_geometry(cfg)
    cfg["content_width"] = content_w

    m_top = float(cfg.get("margin_top", 115))
    m_bottom = float(cfg.get("margin_bottom", 40))
    m_left = float(cfg.get("margin_left", 40))
    m_right = float(cfg.get("margin_right", 40))

    styles = build_theme_styles(cfg)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=final_pagesize,
        rightMargin=m_right, leftMargin=m_left,
        topMargin=m_top,
        bottomMargin=m_bottom,
    )

    def on_page(canvas, document):
        _draw_header_footer(canvas, document, cfg, page_w, page_h, content_w, _resolved_logo, is_first_page=(document.page == 1))

    story = []
    pdata = compute_student_data(row, cfg, rank=rank, total_students=total_students)

    # Detect if any sections explicitly handle identity card
    configured_sections = cfg.get("sections", [])
    has_identity_sec = any(s.get("type") in ("identity_card", "identity") for s in configured_sections if isinstance(s, dict))
    has_written_sec = any(s.get("type") == "written_test" for s in configured_sections if isinstance(s, dict))
    has_marks_sec = any(s.get("type") == "marks_table" for s in configured_sections if isinstance(s, dict))
    has_ratings = bool(pdata.get("skills"))

    # 1. Identity section (if not in configured sections, render as standard top block)
    if not has_identity_sec:
        render_identity(story, row, cfg, styles, pdata, content_w)

    # 2. Written test (if not in configured sections)
    if not has_written_sec and pdata.get("has_written"):
        render_written(story, pdata, cfg, styles, content_w)

    # 3. Dynamic Section Execution Pipeline
    rendered_action_plan = False
    rendered_overview = False

    for sec in configured_sections:
        if not isinstance(sec, dict) or not sec.get("enabled", True):
            continue

        # Start section on new page if configured
        if sec.get("page_break_before", False) and story:
            story.append(PageBreak())

        stype = sec.get("type", "")

        if stype in ("identity_card", "identity"):
            render_identity(story, row, cfg, styles, pdata, content_w)
        elif stype == "written_test":
            render_written(story, pdata, cfg, styles, content_w)
        elif stype == "marks_table":
            render_marks_table(story, row, sec, cfg, styles, content_w)
        elif stype == "marks_motivation":
            render_marks_motivation(story, row, cfg, styles, content_w)
        elif stype == "marks_analysis":
            render_marks_analysis(story, row, cfg, styles, content_w)
        elif stype == "rating_table":
            render_skill_breakdown(story, pdata, cfg, styles, content_w, section=sec)
        elif stype == "action_plan_grid":
            render_action_plan(story, pdata, cfg, styles, groq_client, content_w)
            rendered_action_plan = True
        elif stype == "performance_overview":
            render_overview(story, pdata, cfg, styles, content_w)
            rendered_overview = True
        elif stype == "text_field":
            render_remarks(story, pdata, cfg, styles, content_w, section=sec)
        elif stype == "honors_certificate":
            render_honors_certificate(story, pdata, cfg, styles, content_w)

    # Fallbacks for sections not explicitly listed in legacy section list
    if has_marks_sec and not any(s.get("type") == "marks_motivation" for s in configured_sections if isinstance(s, dict)):
        render_marks_motivation(story, row, cfg, styles, content_w)
        render_marks_analysis(story, row, cfg, styles, content_w)

    if has_ratings and not any(s.get("type") == "rating_table" for s in configured_sections if isinstance(s, dict)):
        render_skill_breakdown(story, pdata, cfg, styles, content_w)

    if not rendered_action_plan:
        render_action_plan(story, pdata, cfg, styles, groq_client, content_w)

    if not rendered_overview:
        render_overview(story, pdata, cfg, styles, content_w)

    if not any(s.get("type") == "text_field" for s in configured_sections if isinstance(s, dict)):
        render_remarks(story, pdata, cfg, styles, content_w)

    render_footer_declaration(story, cfg, styles)

    if not any(s.get("type") == "honors_certificate" for s in configured_sections if isinstance(s, dict)):
        render_honors_certificate(story, pdata, cfg, styles, content_w)

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
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
    import datetime as dt

    show_rank = cfg.get("show_rank", False) or cfg.get("marks_show_rank", False)
    total     = len(df)

    ranks = [None] * total
    if show_rank:
        scores = []
        for _, row in df.iterrows():
            pd_tmp = compute_student_data(row, cfg)
            scores.append(pd_tmp["overall"])
        order  = sorted(range(total), key=lambda i: scores[i], reverse=True)
        rank_map = {orig: rank + 1 for rank, orig in enumerate(order)}
        ranks    = [rank_map[i] for i in range(total)]

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, (_, row) in enumerate(df.iterrows()):
            try:
                raw_id  = clean_display_val(row[id_column] if id_column in row.index else f"student_{i}")
                safe_id = re.sub(r'[^A-Za-z0-9_\-]', '_', raw_id)
                pdf_b   = build_single_report(
                    row, cfg, groq_client, logo_bytes,
                    rank=ranks[i], total_students=total,
                    report_id=f"BATCH_{safe_id}_{dt.datetime.now().strftime('%Y%m%d%H%M')}"
                )
                zf.writestr(f"{safe_id}_report.pdf", pdf_b)
            except Exception as e:
                name = str(row.iloc[0]) if len(row) > 0 else f"row_{i}"
                zf.writestr(f"ERROR_{i}_{name}.txt", f"Report generation failed:\n{e}")
            if progress_callback:
                progress_callback(i + 1, total)

    zip_buf.seek(0)
    return zip_buf.read()
