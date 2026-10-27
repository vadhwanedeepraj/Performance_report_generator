"""
Innomatics Research Labs – Operations Report Generator
==============================================================
Real-time EdTech & Operations platform for generating learner performance & assessment reports.
Built with Streamlit + ReportLab for Innomatics Research Labs.
"""

import io
import os
import re
import json
import zipfile
import streamlit as st
import pandas as pd
from datetime import datetime
from report_engine import (
    build_single_report, build_all_reports,
    clean_label, to_numeric_rating, grade_for,
    compute_student_data,
)
from audit_logger import (
    log_audit_event, get_audit_log_bytes, get_audit_dataframe,
    get_deduplicated_audit_dataframe,
    get_session_id, clear_audit_log, AUDIT_FIELDS,
)
from constants import (
    GRADE_SCALE_PCT, grade_label_from_pct, MAX_CSV_SIZE_MB, MAX_CSV_ROWS,
    MAX_BATCH_WORKERS, MOCK_LABEL_SHORTLISTED, MOCK_LABEL_REVISION,
)

from generation_guard import check_duplicate, record_generation, get_all_history

# ─────────────────────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Innomatics Research Labs – Operations Report Generator",
    page_icon="🎓",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────────────────────
# ENV LOADER
# ─────────────────────────────────────────────────────────────
def load_env():
    if os.path.exists(".env"):
        with open(".env", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip(); v = v.strip().strip('"').strip("'")
                if k:
                    os.environ.setdefault(k, v)
load_env()

def get_groq_client(key):
    if not key:
        return None
    try:
        from groq import Groq
        return Groq(api_key=key)
    except Exception:
        return None

TEMPLATE_DIR = "templates"
os.makedirs(TEMPLATE_DIR, exist_ok=True)
RATING_OPTIONS = ["Poor", "Average", "Good", "Excellent"]

# ─────────────────────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────────────────────
def _ss(k, v):
    if k not in st.session_state:
        st.session_state[k] = v

_ss("df",            None)
_ss("logo_bytes",    None)
_ss("identity_cols", [])
_ss("pdf_name_col",  "")
_ss("written_cfg",   {})
_ss("written_name",  "Written Test")
_ss("wt_enabled",    False)
_ss("sections",      [])
_ss("perf_targets",  [])
_ss("show_perf",     True)
_ss("gen_results",   [])
_ss("last_zip",      None)
_ss("input_mode",    "upload")
_ss("intro_text",    "")
_ss("show_action_plan", True)
_ss("show_tech_action", True)
_ss("show_comm_action", True)
_ss("last_zip_ts",    "")   # timestamp captured at generation time, stable for downloads
_ss("merge_df_left",  None)   # first CSV in merge mode
_ss("merge_df_right", None)   # second CSV in merge mode
_ss("merged_df",      None)   # result of merge
_ss("force_regenerate", False) # user confirmed re-run of duplicate batch
_ss("gsheet_url",       "")
_ss("show_guide", False)       # onboarding helper card status
_ss("source_file",    "N/A")   # name of the uploaded CSV file for audit logging
_ss("session_id",    get_session_id())  # stable session ID for audit trail
# ── Marks / EdTech report card controls ──────────────────────
_ss("marks_pass_threshold",    75)      # % considered passing
_ss("marks_show_grade",        True)    # show grade badge + message
_ss("marks_show_recommendations", False) # show study tips
_ss("marks_show_analysis",     True)    # show strong vs focus panel
_ss("marks_show_rank",         True)    # show class rank if batch
_ss("marks_tone",              "Encouraging")   # feedback tone
_ss("marks_module_context",    "")
_ss("marks_benchmark_note", "<b><font color='#dc2626'>Important:</font></b> Your performance is evaluated relative to all candidates across all branches of Innomatics Research Labs who appeared for the DataAnalysis Job Fitment Assessment. The benchmarks shown below indicate the score required to be among the top 10% in each module.")
_ss("marks_show_benchmark_note", True)
      # optional course description for AI

# ── Dynamic Benchmarks & Footer ──────────────────────────────
_ss("benchmark_shortlist",     90)
_ss("benchmark_strong",        90)
_ss("benchmark_revision",      50)
_ss("footer_link_text",        "Schedule Mock Interview")
_ss("footer_link_url",         "https://example.com/schedule")
_ss("footer_deadline_text",    "Important: The mock interview scheduling link will remain active for 3 calendar days from the date of this report. Please ensure your booking is completed within this period.")
_ss("footer_mentor_text",      "If you have any questions, feel free to connect with your batch mentor before scheduling.")
_ss("enable_mock_interview",   True)
_ss("mi_eligibility_overrides", {})   # dict: student_key → True/False (manual override)
_ss("show_watermark",          False)
_ss("watermark_text",          "CONFIDENTIAL")
_ss("legal_notice",            "This report is system-generated for assessment and evaluation purposes only.")
# ── Audit Log Event Controls ──────────────────────────────
# Set of action strings that are ALLOWED to be written to audit_logs.csv.
# Users control this via the sidebar. Defaults to the two most useful events.
_ss("audit_log_events", {
    "Generated (Batch)",
    "Downloaded (Batch ZIP)",
    "Mock Interview Status Update",
})
# ── Table-wise Benchmark & Mock Interview Basis ───────────────
_ss("mi_benchmark_table",    "All Marks Tables (Combined)")
# ── Report Layout & Appearance Defaults ────────────────────────
_ss("report_page_size",      "A4")
_ss("report_orientation",    "Portrait")
_ss("report_margin_left",    40)
_ss("report_margin_right",   40)
_ss("intro_card_style",      "Callout (Accent Left Border)")
_ss("intro_bg_color",        "#F8FAFC")
_ss("intro_border_color",    "")  # empty string means defaults to primary_color
_ss("table_border_style",    "Full Grid")
_ss("table_border_color",    "#D1D5DB")
_ss("table_border_width",    0.4)


# ─────────────────────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────────────────────
def safe_filename(val):
    s = str(val).strip()
    # Strip trailing .0 from integer-like floats (e.g. 81010901925.0 → 81010901925)
    try:
        f = float(s)
        if f == int(f):
            s = str(int(f))
    except (ValueError, OverflowError):
        pass
    return re.sub(r'[^A-Za-z0-9_\-]', '_', s) or "report"


def _validate_csv(uploaded_file) -> tuple[bool, str]:
    """
    Validate an uploaded CSV file before processing.

    Returns (is_valid: bool, error_message: str).
    error_message is empty string when valid.
    """
    # File size guard
    size_mb = uploaded_file.size / (1024 * 1024)
    if size_mb > MAX_CSV_SIZE_MB:
        return False, (
            f"⚠️ File too large ({size_mb:.1f} MB). "
            f"Maximum allowed size is {MAX_CSV_SIZE_MB} MB. "
            "Please split into smaller batches."
        )

    # Empty file guard
    if uploaded_file.size == 0:
        return False, "⚠️ The uploaded file is empty. Please upload a valid CSV."

    # Quick row-count check (read headers + count lines without loading all data)
    try:
        uploaded_file.seek(0)
        import io as _io
        content = uploaded_file.read().decode("utf-8", errors="replace")
        uploaded_file.seek(0)
        line_count = content.count("\n")
        if line_count <= 1:
            return False, "⚠️ CSV has no data rows (only a header or is blank)."
        if line_count - 1 > MAX_CSV_ROWS:
            return False, (
                f"⚠️ CSV contains {line_count - 1:,} rows — this exceeds the "
                f"recommended limit of {MAX_CSV_ROWS:,}. "
                "Large batches may cause timeouts. Consider splitting into smaller files."
            )
    except Exception:
        pass   # Non-blocking — let pandas handle parse errors later

    return True, ""



def _should_log(action: str) -> bool:
    """Returns True only if this action type is enabled in Audit Log Settings."""
    allowed = st.session_state.get("audit_log_events", {
        "Generated (Batch)", "Downloaded (Batch ZIP)", "Mock Interview Status Update",
    })
    return action in allowed


# Wrap log_audit_event to respect user's event preferences from session state
# BUG-06 FIX: also auto-inject the current source_file so audit filter can work.
_original_log_audit_event = log_audit_event
def log_audit_event(action, *args, **kwargs):
    if _should_log(action):
        # Inject source_file from session state if not already provided
        if "source_file" not in kwargs:
            kwargs["source_file"] = st.session_state.get("source_file", "N/A")
        return _original_log_audit_event(action, *args, **kwargs)


def get_student_identity_vals(row, id_cols, fallback_name="N/A"):
    """
    Intelligently extracts Student Name, Enrollment ID, and dynamic metadata from identity columns.
    Checks column names first, and then validates based on value formats
    (purely numeric or scientific notation values are IDs, alphabetical strings are names)
    to dynamically correct for swapped columns in the uploaded data sheet.
    """
    sname = fallback_name
    eid = "N/A"
    if not id_cols:
        return sname, eid, "N/A"
        
    # Extract values for the selected identity columns
    vals = [str(row[c]).strip() for c in id_cols if c in row.index]
    
    # Filter out nulls/nan
    clean_vals = [v for v in vals if v.lower() not in ("nan", "none", "n/a", "")]
    
    if len(clean_vals) == 1:
        # Single value check
        v = clean_vals[0]
        if v.isdigit() or re.match(r'^\d+(\.\d+)?[eE]\+?\d+$', v):
            eid = v
        else:
            sname = v
    elif len(clean_vals) >= 2:
        # Multiple values: classify by format
        numeric_vals = []
        alpha_vals = []
        for v in clean_vals:
            # Check if scientific notation (e.g. 8.1E+10) or purely numeric
            if v.isdigit() or re.match(r'^\d+(\.\d+)?[eE]\+?\d+$', v):
                numeric_vals.append(v)
            else:
                alpha_vals.append(v)
                
        if alpha_vals:
            sname = alpha_vals[0]
        if numeric_vals:
            eid = numeric_vals[0]
            
        # Fallbacks if classification fails to separate them cleanly
        if sname == fallback_name and len(alpha_vals) == 0:
            first_val = clean_vals[0]
            if first_val != eid:
                sname = first_val
            elif len(clean_vals) > 1:
                sname = clean_vals[1]

    return sname, eid, "N/A"


def compute_grade(row, sections, _batch_df=None):
    """
    Compute an overall grade label and percentage score for a single student row.

    Handles three section types:
      - marks_table  : scored columns with a ``max`` cap (cfg col_cfg)
      - written_test : same column-level max logic as marks_table
      - rating_table : word/number ratings mapped to 1-4 scale, then scaled to 100

    Returns
    -------
    grade_label : str   e.g. "A+", "A", "B", "C", "D"
    score_pct   : float  0-100

    Grade thresholds and RATING_MAP come from constants.py (single source of truth).
    """
    import re as _re
    from constants import RATING_MAP, grade_label_from_pct

    total_scored, total_max = 0.0, 0.0

    for sec in sections:
        if not isinstance(sec, dict):
            continue
        if not sec.get("enabled", True):
            continue

        stype = sec.get("type", "")

        # ── marks_table / written_test ──────────────────────────────────────
        if stype in ("marks_table", "written_test"):
            col_cfg = sec.get("col_cfg", {}) or {}
            for col in sec.get("columns", []):
                if col not in row.index:
                    continue
                raw = str(row[col]).strip()
                if raw.lower() in ("nan", "na", "", "n/a", "none"):
                    continue
                try:
                    score = float(raw)
                except ValueError:
                    score = 0.0
                # Determine max from header pattern or col_cfg
                m = _re.search(r'out\s+of\s+([\d.]+)', col, _re.IGNORECASE)
                max_from_header = float(m.group(1)) if m else None
                cfg_c = col_cfg.get(col, {})
                max_s = (
                    float(cfg_c.get("max"))
                    if cfg_c.get("max")
                    else max_from_header
                )
                if max_s and max_s > 0:
                    total_scored += min(score, max_s)
                    total_max += max_s

        # ── rating_table ────────────────────────────────────────────────────
        elif stype == "rating_table":
            for col in sec.get("columns", []):
                if col not in row.index:
                    continue
                raw = str(row[col]).strip()
                if raw.lower() in ("nan", "na", "", "n/a", "none"):
                    continue
                # Try numeric first, then word map
                try:
                    val = float(raw)
                except ValueError:
                    val = float(RATING_MAP.get(raw.lower(), 0))
                # Clamp to 1-4 and convert to 0-100
                val = max(0.0, min(val, 4.0))
                total_scored += val
                total_max += 4.0

    if total_max <= 0:
        return "N/A", 0.0

    score_pct = round((total_scored / total_max) * 100.0, 1)

    # Use canonical grade scale from constants.py
    grade_label, _color, _verdict = grade_label_from_pct(score_pct)
    return grade_label, score_pct



def compute_mock_interview_eligibility(row, sections, batch_df, cfg):
    """
    Compute whether a student is eligible for Mock Interview scheduling.

    Logic mirrors report_engine.py render_marks_motivation():
      - If marks_table sections exist  →  use percentile vs shortlist_cut threshold.
      - If only rating_table sections  →  use average rating score percentage.
      - Returns (eligibility_label: str, is_shortlisted: bool, eval_score: float)
    """
    # BUG-18 FIX: move imports out of tight inner loops
    import re as _re

    shortlist_cut = cfg.get("benchmark_shortlist", 90)
    selected_tbl = cfg.get("mi_benchmark_table", "All Marks Tables (Combined)")
    eval_sections = sections
    if selected_tbl and selected_tbl != "All Marks Tables (Combined)":
        ts = [s for s in sections if isinstance(s, dict) and (s.get("title") == selected_tbl or s.get("name") == selected_tbl)]
        if ts:
            eval_sections = ts

    # ── Try marks_table path first ─────────────────────────────────────
    marks_items = []
    total_scored, total_max = 0.0, 0.0
    for sec in eval_sections:
        if not isinstance(sec, dict):
            continue
        if not sec.get("enabled", True) or sec.get("type") != "marks_table":
            continue
        col_cfg = sec.get("col_cfg", {}) or {}
        for col in sec.get("columns", []):
            if col not in row.index:
                continue
            raw = str(row[col]).strip()
            if raw.lower() in ("nan", "na", "", "n/a"):
                continue
            try:
                score = float(raw)
            except ValueError:
                score = 0.0
            m = _re.search(r'out\s+of\s+([\d.]+)', col, _re.IGNORECASE)
            max_from_header = float(m.group(1)) if m else None
            cfg_c = col_cfg.get(col, {})
            max_s = float(cfg_c.get("max")) if cfg_c.get("max") else max_from_header
            if max_s is None:
                max_s = 100.0
            if max_s and max_s > 0:
                total_scored += score
                total_max += max_s
                marks_items.append((score, max_s))

    if marks_items and total_max > 0:
        # Compute cohort-wide percentile (same as engine)
        if batch_df is not None and len(batch_df) > 1:
            try:
                cohort_totals = []
                for _, brow in batch_df.iterrows():
                    bs, bm = 0.0, 0.0
                    for sec in eval_sections:
                        if not isinstance(sec, dict):
                            continue
                        if not sec.get("enabled", True) or sec.get("type") != "marks_table":
                            continue
                        col_cfg = sec.get("col_cfg", {}) or {}
                        for col in sec.get("columns", []):
                            if col not in brow.index:
                                continue
                            raw2 = str(brow[col]).strip()
                            if raw2.lower() in ("nan", "na", "", "n/a"):
                                continue
                            try:
                                s2 = float(raw2)
                            except ValueError:
                                s2 = 0.0
                            m2 = _re.search(r'out\s+of\s+([\d.]+)', col, _re.IGNORECASE)
                            mh2 = float(m2.group(1)) if m2 else None
                            c2 = col_cfg.get(col, {})
                            mx2 = float(c2.get("max")) if c2.get("max") else mh2
                            if mx2 and mx2 > 0:
                                bs += s2
                                bm += mx2
                    cohort_totals.append(round((bs / bm) * 100, 1) if bm > 0 else 0.0)
                my_pct = round((total_scored / total_max) * 100, 1)
                # Tie-safe rank: count students who scored STRICTLY higher than this student.
                # Fixes a bug where sorted().index() gives tied students the same rank.
                rank = sum(1 for t in cohort_totals if t > my_pct) + 1
                eval_metric = round((1 - (rank - 1) / len(cohort_totals)) * 100, 1)
            except Exception:
                eval_metric = round((total_scored / total_max) * 100, 1)
        else:
            eval_metric = round((total_scored / total_max) * 100, 1)
    else:
        # Fallback: use rating_table average as percentage
        scores = []
        for sec in sections:
            if not isinstance(sec, dict):
                continue
            if not sec.get("enabled", True) or sec.get("type") != "rating_table":
                continue
            for col in sec.get("columns", []):
                if col in row.index:
                    v = to_numeric_rating(str(row[col]))
                    if v > 0:
                        scores.append(v)
        eval_metric = round((sum(scores) / len(scores) / 4.0) * 100, 1) if scores else 0.0

    is_shortlisted = eval_metric >= shortlist_cut
    label = MOCK_LABEL_SHORTLISTED if is_shortlisted else MOCK_LABEL_REVISION
    return label, is_shortlisted, eval_metric


# ─────────────────────────────────────────────────────────────
# CSS — Deepraj Analytics: Industrial Enterprise Design System
# ─────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800;900&family=Inter:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;600;700&family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap');

/* =========================================================
   DEEPRAJ ANALYTICS — INDUSTRIAL ENTERPRISE DESIGN SYSTEM
   White-background, sharp edges, precision engineering feel
   ========================================================= */

/* ─ Keyframe Animations ───────────────────────────────── */
@keyframes fadeInUp {
  from { opacity: 0; transform: translateY(16px); }
  to   { opacity: 1; transform: translateY(0); }
}
@keyframes pulse-dot {
  0%, 100% { opacity: 1; transform: scale(1); }
  50%       { opacity: 0.5; transform: scale(1.4); }
}
@keyframes scanline {
  0%   { transform: translateY(-100%); }
  100% { transform: translateY(100vh); }
}
@keyframes borderFlow {
  0%   { background-position: 0% 50%; }
  50%  { background-position: 100% 50%; }
  100% { background-position: 0% 50%; }
}
@keyframes slideRight {
  from { transform: translateX(-8px); opacity: 0; }
  to   { transform: translateX(0); opacity: 1; }
}
@keyframes countUp {
  from { opacity: 0; transform: translateY(6px); }
  to   { opacity: 1; transform: translateY(0); }
}

/* ─ Reset & Global Fonts ─────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; }
html, body, h1, h2, h3, h4, h5, h6, p, li, input, textarea, select {
  font-family: 'Outfit', 'Plus Jakarta Sans', 'Inter', -apple-system, sans-serif;
  font-size: 14.5px !important;
  -webkit-font-smoothing: antialiased !important;
  -moz-osx-font-smoothing: grayscale !important;
  text-rendering: optimizeLegibility !important;
}
[class^="stMarkdown"], [class^="stText"], [class^="stCaption"] {
  font-family: 'Outfit', 'Plus Jakarta Sans', 'Inter', sans-serif;
}

/* Base elements */
p, li, span, label, input, select, textarea {
  font-family: 'Outfit', 'Plus Jakarta Sans', 'Inter', sans-serif !important;
  font-size: 13.8px !important;
}
.material-icons,
.material-symbols-rounded,
[data-testid="stIconMaterial"],
span[class*="Icon"],
svg {
  font-family: 'Material Symbols Rounded', 'Material Icons', sans-serif !important;
}
code, pre, .mono {
  font-family: 'JetBrains Mono', 'Courier New', monospace !important;
}

/* ─ Design Tokens ─────────────────────────────────────── */
:root {
  /* Backgrounds */
  --bg:              #F4F6FA;
  --bg-card:         #FFFFFF;
  --bg-card-hover:   #FAFBFF;
  --sidebar-bg:      #FFFFFF;

  /* Industrial accent palette */
  --primary:         #1B3A6B;   /* deep navy */
  --primary-mid:     #1E4D99;
  --primary-light:   #2563EB;
  --primary-glow:    rgba(27, 58, 107, 0.12);
  --accent:          #E63946;   /* industrial red */
  --accent-bg:       #FFF5F5;
  --gold:            #C9A84C;   /* engineering gold */

  /* Text hierarchy */
  --text-main:       #0D1117;
  --text-body:       #1E293B;
  --text-sec:        #475569;
  --text-muted:      #94A3B8;
  --text-label:      #64748B;

  /* Status colors */
  --success:         #16A34A;
  --success-bg:      #F0FDF4;
  --warning:         #D97706;
  --warning-bg:      #FFFBEB;
  --danger:          #DC2626;
  --danger-bg:       #FEF2F2;
  --info:            #0284C7;
  --info-bg:         #F0F9FF;

  /* Borders */
  --border:          #DDE3EE;
  --border-strong:   #B8C4D9;
  --border-focus:    #1B3A6B;

  /* Shadows — layered depth (Slate/Navy ambient occlusion) */
  --shadow-xs:   0 1px 2px rgba(15, 23, 42, 0.03);
  --shadow-sm:   0 2px 8px rgba(15, 23, 42, 0.04), 0 1px 3px rgba(15, 23, 42, 0.02);
  --shadow-md:   0 8px 20px rgba(15, 23, 42, 0.06), 0 2px 6px rgba(15, 23, 42, 0.03);
  --shadow-lg:   0 16px 36px rgba(15, 23, 42, 0.08), 0 4px 12px rgba(15, 23, 42, 0.04);
  --shadow-xl:   0 24px 48px rgba(15, 23, 42, 0.10), 0 8px 24px rgba(15, 23, 42, 0.05);
  --shadow-navy: 0 8px 24px rgba(27, 58, 107, 0.15);
  --shadow-red:  0 8px 24px rgba(230, 57, 70, 0.16);

  /* Geometry — smoother corner profiles */
  --radius-xs:   5px;
  --radius-sm:   10px;
  --radius-md:   14px;
  --radius-lg:   20px;
  --radius-xl:   24px;
  --radius-full: 9999px;

  /* Motion — Out-Quintic ultra-smooth transition curve */
  --ease:        cubic-bezier(0.16, 1, 0.3, 1);
  --transition:  all 0.3s var(--ease);
  --transition-slow: all 0.5s var(--ease);
}

/* ─ Custom Scrollbar ─────────────────────────────────── */
::-webkit-scrollbar {
  width: 10px;
  height: 10px;
}
::-webkit-scrollbar-track {
  background: var(--bg) !important;
}
::-webkit-scrollbar-thumb {
  background: var(--border-strong) !important;
  border-radius: var(--radius-full) !important;
  border: 2.3px solid var(--bg) !important;
}
::-webkit-scrollbar-thumb:hover {
  background: var(--primary-mid) !important;
}

/* ─ App Background & Layout ────────────────────────────── */
.stApp, .main, [data-testid="stAppViewContainer"],
[data-testid="stMain"] {
  background: var(--bg) !important;
}
.main .block-container {
  padding: 1.6rem 2.2rem 3rem !important;
  max-width: 1340px !important;
  background: transparent !important;
  animation: fadeInUp 0.3s var(--ease) forwards;
}

/* ─ Hide Streamlit Chrome ──────────────────────────────── */
#MainMenu, footer, [data-testid="stDecoration"],
[data-testid="stStatusWidget"] { display: none !important; }

/* ─ Typography ─────────────────────────────────────────── */
.main p, .main li, .main .stMarkdown {
  color: var(--text-body);
  font-size: 0.94rem;
  line-height: 1.65;
  letter-spacing: 0.01em;
}
.main h1 {
  font-size: 2.05rem; font-weight: 800; color: var(--text-main);
  letter-spacing: -0.8px; line-height: 1.15;
}
.main h2 {
  font-size: 1.45rem; font-weight: 700; color: var(--text-main);
  letter-spacing: -0.4px; line-height: 1.3;
}
.main h3 {
  font-size: 1.15rem; font-weight: 600; color: var(--text-main);
  letter-spacing: -0.2px;
}
.main h4 {
  font-size: 1.00rem; font-weight: 600; color: var(--text-sec);
}

/* ─────────────────────────────────────────────────────────
   SIDEBAR — Industrial Control Panel
   ───────────────────────────────────────────────────────── */
section[data-testid="stSidebar"] {
  background: var(--sidebar-bg) !important;
  border-right: 2px solid var(--border) !important;
  min-width: 280px !important;
  box-shadow: 2px 0 20px rgba(0,0,0,0.05) !important;
}
section[data-testid="stSidebar"] > div { padding: 0 !important; }
section[data-testid="stSidebar"] [data-testid="stVerticalBlock"] {
  gap: 0.2rem !important;
}

/* Sidebar text */
section[data-testid="stSidebar"] label {
  color: var(--text-sec) !important;
  font-weight: 600 !important;
  font-size: 0.85rem !important;
  text-transform: uppercase !important;
  letter-spacing: 0.6px !important;
}
section[data-testid="stSidebar"] .stMarkdown p,
section[data-testid="stSidebar"] p {
  color: var(--text-sec) !important;
  font-size: 0.90rem !important;
  line-height: 1.6;
}
section[data-testid="stSidebar"] h2 {
  color: var(--text-main) !important;
  font-weight: 800 !important;
  font-size: 1.15rem !important;
}
section[data-testid="stSidebar"] h3 {
  color: var(--primary) !important;
  font-size: 0.8rem !important;
  font-weight: 700 !important;
  text-transform: uppercase;
  letter-spacing: 2px;
  padding: 0.7rem 1rem 0.3rem !important;
}
section[data-testid="stSidebar"] span { color: var(--text-sec) !important; }

/* Sidebar divider */
section[data-testid="stSidebar"] hr {
  border: none !important;
  border-top: 1px solid var(--border) !important;
  margin: 0.4rem 0.8rem !important;
}

/* Sidebar inputs */
section[data-testid="stSidebar"] input,
section[data-testid="stSidebar"] textarea {
  background: #F8FAFC !important;
  color: var(--text-main) !important;
  border: 1.5px solid var(--border) !important;
  border-radius: var(--radius-sm) !important;
  font-size: 0.98rem !important;
  transition: var(--transition) !important;
}
section[data-testid="stSidebar"] input:focus,
section[data-testid="stSidebar"] textarea:focus {
  border-color: var(--primary) !important;
  box-shadow: 0 0 0 3px var(--primary-glow) !important;
  background: #FFFFFF !important;
}
section[data-testid="stSidebar"] [data-baseweb="select"] > div {
  background: #F8FAFC !important;
  border: 1.5px solid var(--border) !important;
  border-radius: var(--radius-sm) !important;
  color: var(--text-main) !important;
}

/* Sidebar expanders */
section[data-testid="stSidebar"] [data-testid="stExpander"] {
  background: #FAFBFF !important;
  border: 1.5px solid var(--border) !important;
  border-radius: var(--radius-sm) !important;
  margin: 0.1rem 0.5rem 0.25rem !important;
  transition: var(--transition);
}
section[data-testid="stSidebar"] [data-testid="stExpander"]:hover {
  border-color: var(--primary) !important;
  box-shadow: var(--shadow-sm);
}
section[data-testid="stSidebar"] [data-testid="stExpander"] summary {
  padding: 0.45rem 0.7rem !important;
}
section[data-testid="stSidebar"] [data-testid="stExpander"] summary p {
  color: var(--text-main) !important;
  font-weight: 700 !important;
  font-size: 0.88rem !important;
}

/* Sidebar sliders */
section[data-testid="stSidebar"] [data-testid="stSlider"] > div > div > div {
  background: var(--primary) !important;
}
section[data-testid="stSidebar"] [data-testid="stSlider"] > div > div > div > div {
  background: var(--primary) !important;
  box-shadow: 0 0 0 4px var(--primary-glow) !important;
}

/* ─────────────────────────────────────────────────────────
   HERO BANNER — Industrial Command Center
   ───────────────────────────────────────────────────────── */
.hero {
  background: linear-gradient(135deg, #0D1B3E 0%, #1B3A6B 55%, #1E4D99 100%);
  padding: 2.4rem 3rem;
  border-radius: var(--radius-xl);
  margin-bottom: 1.6rem;
  border: 1px solid rgba(255,255,255,0.06);
  box-shadow: var(--shadow-xl), 0 0 0 1px rgba(27,58,107,0.1);
  position: relative;
  overflow: hidden;
}
/* Grid pattern overlay */
.hero::before {
  content: '';
  position: absolute; inset: 0;
  background-image:
    linear-gradient(rgba(255,255,255,0.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255,255,255,0.03) 1px, transparent 1px);
  background-size: 32px 32px;
  pointer-events: none;
}
/* Glow orb top-right */
.hero::after {
  content: '';
  position: absolute; top: -80px; right: -80px;
  width: 280px; height: 280px;
  background: radial-gradient(circle, rgba(37,99,235,0.25) 0%, transparent 65%);
  border-radius: 50%;
  pointer-events: none;
}
.hero h1 {
  font-size: 2.25rem !important;
  font-weight: 900 !important;
  color: #FFFFFF !important;
  letter-spacing: -1px !important;
  line-height: 1.12 !important;
  margin: 10px 0 8px !important;
  position: relative; z-index: 1;
}
.hero p {
  color: rgba(255,255,255,0.65) !important;
  font-size: 0.96rem !important;
  margin: 0 !important;
  position: relative; z-index: 1;
  font-weight: 400;
  line-height: 1.6;
}
/* Status chip row */
.chip-row { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; position: relative; z-index: 1; }
.chip {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 0.68rem; font-weight: 700;
  padding: 4px 11px;
  border-radius: var(--radius-full);
  letter-spacing: 0.4px;
  text-transform: uppercase;
  background: rgba(255,255,255,0.08);
  color: rgba(255,255,255,0.85);
  border: 1px solid rgba(255,255,255,0.14);
  backdrop-filter: blur(4px);
}
.chip-live {
  background: rgba(22,163,74,0.18);
  border-color: rgba(22,163,74,0.4);
  color: #86EFAC;
}
.chip-live::before {
  content: '';
  display: inline-block;
  width: 6px; height: 6px;
  background: #4ADE80;
  border-radius: 50%;
  animation: pulse-dot 1.5s ease-in-out infinite;
}
.chip-ai-on  { background:rgba(59,130,246,0.2); border-color:rgba(59,130,246,0.4); color:#93C5FD; }
.chip-ai-off { background:rgba(100,116,139,0.15); border-color:rgba(100,116,139,0.3); color:#94A3B8; }
.chip-gold   { background:rgba(201,168,76,0.18); border-color:rgba(201,168,76,0.35); color:#FCD34D; }

/* ─────────────────────────────────────────────────────────
   KPI STRIP — Real-Time Metrics
   ───────────────────────────────────────────────────────── */
.kpi-strip {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin: 0 0 1.4rem;
}
.kpi-card {
  flex: 1; min-width: 120px;
  background: var(--bg-card);
  border: 1.5px solid var(--border);
  border-radius: var(--radius-md);
  padding: 1rem 1.3rem;
  position: relative;
  overflow: hidden;
  box-shadow: var(--shadow-sm);
  transition: var(--transition);
  animation: countUp 0.4s var(--ease) forwards;
}
.kpi-card::before {
  content: '';
  position: absolute; top: 0; left: 0; right: 0;
  height: 3px;
  border-radius: var(--radius-md) var(--radius-md) 0 0;
}
.kpi-card.navy::before  { background: var(--primary); }
.kpi-card.red::before   { background: var(--accent); }
.kpi-card.green::before { background: var(--success); }
.kpi-card.gold::before  { background: var(--gold); }
.kpi-card.info::before  { background: var(--info); }
.kpi-card:hover {
  transform: translateY(-3px);
  box-shadow: var(--shadow-navy);
  border-color: var(--border-strong);
}
.kpi-val {
  font-size: 2.1rem; font-weight: 900;
  color: var(--text-main);
  line-height: 1; letter-spacing: -1.5px;
  font-family: 'JetBrains Mono', monospace !important;
}
.kpi-lbl {
  font-size: 0.68rem; font-weight: 700;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 1.4px;
  margin-top: 6px;
}
.kpi-sub {
  font-size: 0.75rem; color: var(--text-sec);
  margin-top: 3px;
}

/* Streamlit metric containers — industrial override */
div[data-testid="metric-container"] {
  background: var(--bg-card) !important;
  border: 1.5px solid var(--border) !important;
  border-radius: var(--radius-md) !important;
  padding: 1rem 1.3rem !important;
  box-shadow: var(--shadow-sm) !important;
  transition: var(--transition) !important;
  position: relative !important;
  overflow: hidden !important;
}
div[data-testid="metric-container"]::before {
  content: "";
  position: absolute; top: 0; left: 0; right: 0;
  height: 3px;
  background: linear-gradient(90deg, var(--primary), var(--primary-light));
  border-radius: var(--radius-md) var(--radius-md) 0 0;
}
div[data-testid="metric-container"]:hover {
  border-color: var(--border-strong);
  transform: translateY(-3px);
  box-shadow: var(--shadow-navy) !important;
}
div[data-testid="metric-container"] label {
  color: var(--text-muted) !important;
  font-size: 0.85rem !important;
  font-weight: 700 !important;
  text-transform: uppercase !important;
  letter-spacing: 1.4px !important;
}
div[data-testid="metric-container"] [data-testid="stMetricValue"] {
  color: var(--text-main) !important;
  font-weight: 900 !important;
  font-size: 2.3rem !important;
  letter-spacing: -0.8px !important;
  font-family: 'JetBrains Mono', monospace !important;
}

/* ─────────────────────────────────────────────────────────
   BUTTONS — Industrial precision
   ───────────────────────────────────────────────────────── */
.stButton > button {
  font-family: 'Outfit', 'Plus Jakarta Sans', 'Inter', sans-serif !important;
  background: var(--primary) !important;
  color: #FFFFFF !important;
  border: none !important;
  border-radius: var(--radius-sm) !important;
  font-weight: 700 !important;
  font-size: 1.02rem !important;
  padding: 0.68rem 1.8rem !important;
  transition: var(--transition) !important;
  box-shadow: var(--shadow-sm), inset 0 1px 0 rgba(255,255,255,0.08) !important;
  letter-spacing: 0.1px;
  cursor: pointer;
  position: relative;
  overflow: hidden;
}
.stButton > button::after {
  content: '';
  position: absolute; inset: 0;
  background: linear-gradient(180deg, rgba(255,255,255,0.06) 0%, transparent 100%);
  pointer-events: none;
}
.stButton > button:hover {
  background: linear-gradient(135deg, var(--primary-mid) 0%, var(--primary-light) 100%) !important;
  box-shadow: 0 8px 20px rgba(27, 58, 107, 0.22) !important;
  transform: translateY(-2px) !important;
}
.stButton > button:active {
  transform: translateY(0) !important;
  box-shadow: var(--shadow-xs) !important;
}

/* Secondary button */
.stButton > button[kind="secondary"] {
  background: var(--bg-card) !important;
  color: var(--text-body) !important;
  border: 1.5px solid var(--border) !important;
  box-shadow: var(--shadow-xs) !important;
}
.stButton > button[kind="secondary"]:hover {
  border-color: var(--primary) !important;
  color: var(--primary) !important;
  background: var(--primary-glow) !important;
  box-shadow: var(--shadow-sm) !important;
}

/* Download button — success green */
div[data-testid="stDownloadButton"] button {
  background: var(--success) !important;
  color: #FFFFFF !important;
  border: none !important;
  border-radius: var(--radius-sm) !important;
  font-weight: 700 !important;
  width: 100% !important;
  box-shadow: var(--shadow-sm) !important;
  transition: var(--transition) !important;
}
div[data-testid="stDownloadButton"] button:hover {
  background: #15803D !important;
  box-shadow: 0 4px 14px rgba(22,163,74,0.3) !important;
  transform: translateY(-1px) !important;
}

/* ─────────────────────────────────────────────────────────
   TABS — Industrial navigation bar
   ───────────────────────────────────────────────────────── */
.stTabs [data-baseweb="tab-list"] {
  background: var(--bg-card) !important;
  border-radius: var(--radius-md) !important;
  padding: 5px 6px !important;
  gap: 2px !important;
  border: 1.5px solid var(--border) !important;
  box-shadow: var(--shadow-sm);
}
.stTabs [data-baseweb="tab"] {
  border-radius: var(--radius-sm) !important;
  padding: 0.52rem 1.25rem !important;
  font-weight: 600 !important;
  font-size: 0.88rem !important;
  color: var(--text-sec) !important;
  background: transparent !important;
  border: none !important;
  transition: var(--transition) !important;
}
.stTabs [data-baseweb="tab"]:hover {
  color: var(--primary) !important;
  background: #EFF3FB !important;
}
.stTabs [aria-selected="true"] {
  background: linear-gradient(135deg, var(--primary) 0%, var(--primary-mid) 100%) !important;
  color: #FFFFFF !important;
  box-shadow: var(--shadow-navy) !important;
  font-weight: 700 !important;
  transform: translateY(-1px) !important;
}

/* ─────────────────────────────────────────────────────────
   INPUT FIELDS — Industrial precision inputs
   ───────────────────────────────────────────────────────── */
[data-testid="stTextInput"] input,
[data-testid="stTextArea"] textarea,
[data-testid="stNumberInput"] input {
  background: var(--bg-card) !important;
  color: var(--text-main) !important;
  border: 1.5px solid var(--border) !important;
  border-radius: var(--radius-sm) !important;
  font-size: 0.94rem !important;
  padding: 0.68rem 1.1rem !important;
  transition: var(--transition) !important;
  box-shadow: var(--shadow-xs);
  line-height: 1.5 !important;
}
[data-testid="stTextInput"] input:focus,
[data-testid="stTextArea"] textarea:focus,
[data-testid="stNumberInput"] input:focus {
  border-color: var(--primary) !important;
  box-shadow: 0 0 0 4px rgba(37, 99, 235, 0.15) !important;
  outline: none !important;
  background: #FAFBFF !important;
}
[data-testid="stTextInput"] input::placeholder,
[data-testid="stTextArea"] textarea::placeholder {
  color: var(--text-muted) !important;
  font-size: 0.98rem !important;
}

/* Selectbox */
[data-baseweb="select"] > div {
  background: var(--bg-card) !important;
  border: 1.5px solid var(--border) !important;
  border-radius: var(--radius-sm) !important;
  transition: var(--transition) !important;
}
[data-baseweb="select"] > div:focus-within {
  border-color: var(--primary) !important;
  box-shadow: 0 0 0 4px rgba(37, 99, 235, 0.15) !important;
}

/* ─────────────────────────────────────────────────────────
   EXPANDERS — Industrial accordion panels
   ───────────────────────────────────────────────────────── */
[data-testid="stExpander"] {
  background: var(--bg-card) !important;
  border: 1.5px solid var(--border) !important;
  border-radius: var(--radius-md) !important;
  box-shadow: var(--shadow-xs);
  transition: var(--transition);
  overflow: hidden;
}
[data-testid="stExpander"]:hover {
  border-color: var(--border-strong);
  box-shadow: var(--shadow-sm);
}
[data-testid="stExpander"] summary {
  padding: 0.8rem 1.1rem !important;
  border-radius: var(--radius-md) !important;
}
[data-testid="stExpander"] summary p {
  color: var(--text-main) !important;
  font-weight: 700 !important;
  font-size: 1.02rem !important;
}

/* ─────────────────────────────────────────────────────────
   DATAFRAMES & TABLES — Industrial grid
   ───────────────────────────────────────────────────────── */
[data-testid="stDataFrame"],
[data-testid="stDataEditor"] {
  border-radius: var(--radius-md) !important;
  overflow: hidden !important;
  border: 1.5px solid var(--border) !important;
  box-shadow: var(--shadow-sm);
}

/* ─────────────────────────────────────────────────────────
   ALERTS — Industrial severity badges
   ───────────────────────────────────────────────────────── */
[data-testid="stAlert"] {
  border-radius: var(--radius-md) !important;
  border-left-width: 4px !important;
  box-shadow: var(--shadow-xs);
  font-size: 0.92rem !important;
  font-weight: 500 !important;
  line-height: 1.65 !important;
}

/* ─────────────────────────────────────────────────────────
   PROGRESS BAR
   ───────────────────────────────────────────────────────── */
.stProgress { border-radius: var(--radius-full) !important; }
.stProgress > div > div {
  background: linear-gradient(90deg, var(--primary), var(--primary-light)) !important;
  border-radius: var(--radius-full) !important;
  transition: width 0.5s var(--ease) !important;
}

/* ─────────────────────────────────────────────────────────
   MISC — Tags, Toggles, Dividers
   ───────────────────────────────────────────────────────── */
[data-baseweb="tag"] {
  background: #EFF3FB !important;
  border: 1px solid #C3D0EB !important;
  border-radius: 6px !important;
}
[data-baseweb="tag"] span { color: var(--primary) !important; font-weight: 700 !important; }

[data-testid="stToggle"] > label > div[data-checked="true"] {
  background: var(--primary) !important;
}

hr {
  border: none !important;
  border-top: 1.5px solid var(--border) !important;
  margin: 1.2rem 0 !important;
}

/* ─────────────────────────────────────────────────────────
   FILE UPLOADER — Drop zone
   ───────────────────────────────────────────────────────── */
[data-testid="stFileUploader"] > div {
  border: 2px dashed var(--border-strong) !important;
  border-radius: var(--radius-md) !important;
  background: #F8FAFC !important;
  transition: var(--transition);
}
[data-testid="stFileUploader"] > div:hover {
  border-color: var(--primary) !important;
  background: #EFF3FB !important;
}

/* ─────────────────────────────────────────────────────────
   CUSTOM INDUSTRIAL COMPONENTS
   ───────────────────────────────────────────────────────── */

/* Section divider with label */
.sec-label {
  display: flex; align-items: center; gap: 10px;
  margin: 1.6rem 0 0.9rem;
}
.sec-label-line {
  flex: 1; height: 1.5px;
  background: linear-gradient(90deg, var(--border-strong), transparent);
}
.sec-label-text {
  font-size: 0.68rem; font-weight: 800;
  color: var(--text-label);
  text-transform: uppercase; letter-spacing: 2.2px;
  white-space: nowrap;
}

/* Status indicator strip */
.status-strip {
  display: flex; align-items: center; gap: 16px;
  padding: 0.6rem 1rem;
  background: var(--bg-card);
  border: 1.5px solid var(--border);
  border-radius: var(--radius-sm);
  margin-bottom: 1rem;
  box-shadow: var(--shadow-xs);
}
.status-dot {
  width: 8px; height: 8px;
  border-radius: 50%;
  display: inline-block; flex-shrink: 0;
}
.status-dot.live  { background: #4ADE80; animation: pulse-dot 1.5s ease-in-out infinite; }
.status-dot.warn  { background: #FCD34D; }
.status-dot.dead  { background: #94A3B8; }
.status-item {
  display: flex; align-items: center; gap: 6px;
  font-size: 0.82rem; color: var(--text-sec); font-weight: 600;
}

/* Filename preview badge */
.fname-preview {
  background: #EFF3FB;
  border: 1.5px solid #C3D0EB;
  border-radius: var(--radius-xs);
  padding: 0.45rem 0.95rem;
  font-size: 0.82rem;
  color: var(--primary);
  font-weight: 700;
  margin-top: 0.4rem;
  font-family: 'JetBrains Mono', 'Courier New', monospace !important;
  letter-spacing: 0.2px;
}

/* Success / Error badges */
.badge-ok  { color: var(--success); font-weight: 800; }
.badge-err { color: var(--danger);  font-weight: 800; }
.badge-warn { color: var(--warning); font-weight: 800; }

/* Info panel */
.info-panel {
  background: var(--info-bg);
  border: 1.5px solid rgba(2,132,199,0.25);
  border-left: 4px solid var(--info);
  border-radius: var(--radius-sm);
  padding: 1rem 1.2rem;
  font-size: 0.9rem;
  color: #0C4A6E;
  line-height: 1.7;
  margin: 0.5rem 0;
}
.warn-panel {
  background: var(--warning-bg);
  border: 1.5px solid rgba(217,119,6,0.25);
  border-left: 4px solid var(--warning);
  border-radius: var(--radius-sm);
  padding: 1rem 1.2rem;
  font-size: 0.9rem;
  color: #78350F;
  line-height: 1.7;
  margin: 0.5rem 0;
}

/* ─────────────────────────────────────────────────────────
   GUIDE / ONBOARDING
   ───────────────────────────────────────────────────────── */
.guide-box {
  background: #EFF3FB !important;
  border: 1.5px solid #C3D0EB !important;
  border-left: 4px solid var(--primary) !important;
  border-radius: var(--radius-lg) !important;
  padding: 1.6rem 2rem !important;
  margin: 0.5rem 0 1.5rem !important;
  box-shadow: var(--shadow-sm) !important;
  animation: fadeInUp 0.3s var(--ease) forwards;
}
.guide-title {
  color: var(--primary) !important;
  font-weight: 800 !important;
  font-size: 1.1rem !important;
  margin-bottom: 1.2rem !important;
  display: flex !important;
  align-items: center !important;
  gap: 8px !important;
}
.guide-steps-grid { display: flex; flex-direction: column; gap: 1.2rem; }
.guide-step {
  border-bottom: 1px solid rgba(195,208,235,0.5);
  padding-bottom: 1rem;
}
.guide-step:last-child { border-bottom: none; padding-bottom: 0; }
.guide-step-title {
  font-weight: 700 !important; color: var(--text-main) !important;
  font-size: 0.97rem !important; margin-bottom: 0.4rem !important;
  display: flex; align-items: center; gap: 8px;
}
.guide-step-title .step-num {
  background: var(--primary) !important; color: #FFFFFF !important;
  font-size: 0.68rem !important; font-weight: 800 !important;
  padding: 2px 9px !important; border-radius: var(--radius-full) !important;
  text-transform: uppercase; letter-spacing: 0.5px;
}
.guide-step-desc {
  color: var(--text-sec) !important;
  font-size: 0.9rem !important; line-height: 1.72 !important;
}
.guide-step-desc ul { margin-top: 0.4rem !important; padding-left: 1.2rem !important; }
.guide-step-desc li { color: var(--text-sec) !important; margin-bottom: 0.3rem !important; }
.guide-step-desc code {
  background: rgba(27,58,107,0.08) !important; color: var(--primary) !important;
  padding: 1px 5px !important; border-radius: 4px !important;
  font-family: 'JetBrains Mono', monospace !important;
  font-size: 0.76rem !important;
}

/* ─────────────────────────────────────────────────────────
   SCROLLBAR — precision thin
   ───────────────────────────────────────────────────────── */
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: #F1F5F9; border-radius: 99px; }
::-webkit-scrollbar-thumb { background: #C3D0EB; border-radius: 99px; }
::-webkit-scrollbar-thumb:hover { background: var(--primary); }

/* ─────────────────────────────────────────────────────────
   CAPTIONS / COLOR PICKER
   ───────────────────────────────────────────────────────── */
.stCaption, .stCaption p {
  color: var(--text-muted) !important;
  font-size: 0.83rem !important;
}
section[data-testid="stSidebar"] [data-testid="stColorPicker"] > div > div {
  border-radius: var(--radius-sm) !important;
  border: 1.5px solid var(--border) !important;
}

/* Responsive */
@media (max-width: 768px) {
  .main .block-container { padding: 1rem 1.1rem 2rem !important; }
  .hero { padding: 1.6rem 1.8rem !important; }
  .hero h1 { font-size: 1.5rem !important; }
  .kpi-strip { gap: 8px !important; }
  .kpi-card  { min-width: 100px !important; }
}

/* ─────────────────────────────────────────────────────────
   HERO BANNER
   ───────────────────────────────────────────────────────── */
.hero {
  background: #FFFFFF;
  border: 1.5px solid #DDE3EE;
  border-radius: 12px;
  padding: 2rem 2.4rem 1.6rem;
  margin-bottom: 1.2rem;
  position: relative;
  overflow: hidden;
  box-shadow: 0 2px 12px rgba(27,58,107,0.07), 0 1px 3px rgba(0,0,0,0.04);
}
.hero::before {
  content: "";
  position: absolute; inset: 0;
  background:
    linear-gradient(135deg, rgba(27,58,107,0.03) 0%, transparent 60%),
    repeating-linear-gradient(
      90deg,
      transparent, transparent 39px,
      rgba(27,58,107,0.04) 39px, rgba(27,58,107,0.04) 40px
    ),
    repeating-linear-gradient(
      0deg,
      transparent, transparent 39px,
      rgba(27,58,107,0.04) 39px, rgba(27,58,107,0.04) 40px
    );
  pointer-events: none;
}
.hero h1 {
  font-size: 1.75rem !important;
  font-weight: 800 !important;
  color: #0D1117 !important;
  letter-spacing: -0.8px !important;
  margin: 0.5rem 0 0.35rem !important;
  line-height: 1.2 !important;
  position: relative;
}
.hero p {
  font-size: 0.9rem !important;
  color: #475569 !important;
  margin: 0 !important;
  position: relative;
}

/* ─ Chip / Badge Row ─ */
.chip-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 0.7rem;
}
.chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 10px;
  border-radius: 99px;
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.3px;
  background: #F1F5F9;
  color: #334155;
  border: 1px solid #DDE3EE;
}
.chip-live {
  background: rgba(22,163,74,0.12);
  color: #15803D;
  border-color: rgba(22,163,74,0.35);
}
.chip-gold {
  background: rgba(201,168,76,0.13);
  color: #92400E;
  border-color: rgba(201,168,76,0.4);
}
.chip-ai-on {
  background: rgba(99,102,241,0.12);
  color: #4338CA;
  border-color: rgba(99,102,241,0.35);
}
.chip-ai-off {
  background: #F1F5F9;
  color: #64748B;
  border-color: #DDE3EE;
}

/* ─────────────────────────────────────────────────────────
   KPI STRIP
   ───────────────────────────────────────────────────────── */
.kpi-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 1.4rem;
  animation: fadeInUp 0.4s var(--ease);
}
.kpi-card {
  flex: 1;
  min-width: 130px;
  background: #FFFFFF;
  border: 1.5px solid #DDE3EE;
  border-radius: 10px;
  padding: 1rem 1.2rem 0.85rem;
  box-shadow: 0 1px 6px rgba(0,0,0,0.04);
  position: relative;
  overflow: hidden;
  transition: transform 0.18s ease, box-shadow 0.18s ease;
}
.kpi-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 16px rgba(27,58,107,0.12);
}
.kpi-card::before {
  content: "";
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 3px;
  border-radius: 10px 10px 0 0;
}
.kpi-card.navy::before  { background: #1B3A6B; }
.kpi-card.green::before { background: #16A34A; }
.kpi-card.gold::before  { background: #C9A84C; }
.kpi-card.red::before   { background: #B91C1C; }
.kpi-val {
  font-size: 1.45rem;
  font-weight: 800;
  color: #0D1117;
  letter-spacing: -0.5px;
  line-height: 1.1;
}
.kpi-lbl {
  font-size: 0.62rem;
  font-weight: 800;
  color: #64748B;
  text-transform: uppercase;
  letter-spacing: 1.2px;
  margin-top: 4px;
}
.kpi-sub {
  font-size: 0.7rem;
  color: #94A3B8;
  margin-top: 3px;
}
</style>

""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────────────────────
with st.sidebar:
    # Compute active dataset stats
    has_data = st.session_state.df is not None
    row_count = len(st.session_state.df) if has_data else 0
    ds_status = f"🟢 Connected ({row_count} records)" if has_data else "⚪ Idle (Awaiting data)"
    ds_file = st.session_state.source_file if st.session_state.source_file != "N/A" else "None"

    # Build optional file row separately to avoid nested f-string issues
    file_row_html = (
        f'<div style="font-size:0.68rem;color:#475569;font-family:monospace;'
        f'overflow:hidden;white-space:nowrap;text-overflow:ellipsis;">'
        f'<strong style="color:#0D1117;">File:</strong> {ds_file}</div>'
    ) if has_data else ""

    if os.path.exists("logo/innomatics_logo.png"):
        st.image("logo/innomatics_logo.png", use_container_width=True)

    st.markdown(f"""
    <div style="padding:0.6rem 0.6rem 0.8rem;border-bottom:1.5px solid #DDE3EE;margin-bottom:0.6rem;">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;">
        <div style="width:34px;height:34px;background:linear-gradient(135deg,#B91C1C,#DC2626);
                    border-radius:8px;display:flex;align-items:center;justify-content:center;
                    font-size:1.1rem;box-shadow:0 2px 8px rgba(185,28,28,0.25);flex-shrink:0;">🎓</div>
        <div>
          <div style="font-size:0.85rem;font-weight:800;color:#0D1117;letter-spacing:-0.3px;line-height:1.2;">
            Innomatics Operations
          </div>
          <div style="font-family:monospace;font-size:0.58rem;color:#B91C1C;font-weight:700;
                      letter-spacing:0.6px;text-transform:uppercase;margin-top:2px;">
            REPORT & DISPATCH MANAGER
          </div>
        </div>
      </div>
      <div style="display:flex;flex-direction:column;gap:4px;background:#F8FAFC;
                  border:1px solid #DDE3EE;padding:8px 10px;border-radius:4px;margin-top:8px;">
        <div style="display:flex;align-items:center;justify-content:space-between;">
          <span style="font-size:0.63rem;font-weight:700;color:#475569;text-transform:uppercase;letter-spacing:0.5px;">SYS STATE</span>
          <span style="font-size:0.6rem;padding:2px 7px;font-weight:800;border-radius:4px;
                       background:rgba(22,163,74,0.15);border:1px solid rgba(22,163,74,0.4);
                       color:#16A34A;display:inline-flex;align-items:center;gap:4px;">
            <span style="width:5px;height:5px;background:#4ADE80;border-radius:50%;
                         display:inline-block;animation:pulse-dot 1.5s ease-in-out infinite;"></span> LIVE
          </span>
        </div>
        <div style="font-size:0.67rem;color:#475569;font-family:monospace;word-break:break-all;">
          <strong style="color:#0D1117;">Session:</strong> {st.session_state.session_id[:12]}...
        </div>
        <div style="font-size:0.67rem;color:#475569;font-family:monospace;">
          <strong style="color:#0D1117;">Data:</strong> {ds_status}
        </div>
        {file_row_html}
      </div>
    </div>
    """, unsafe_allow_html=True)

    with st.expander("🏫 Institute & Branding", expanded=True):
        company_name    = st.text_input("Institute / Academy Name", "Innomatics Research Labs", key="sb_company")
        report_title_sb = st.text_input("Report / Assessment Title", "MOCK INTERVIEW EVALUATION REPORT", key="sb_title")
        
        st.markdown("---")
        intro_text = st.text_area(
            "Report Introduction",
            value=st.session_state.intro_text,
            height=80,
            placeholder="This learner performance report was prepared by {company_name}.",
            key="sb_intro",
        )
        st.session_state.intro_text = intro_text

        st.markdown("---")
        st.caption("🎨 Brand Colors")
        c1, c2, c3 = st.columns(3)
        with c1:
            primary_color   = st.color_picker("Primary", "#B91C1C", key="sb_pc")
        with c2:
            secondary_color = st.color_picker("Header", "#1E293B", key="sb_sc")
        with c3:
            accent_color    = st.color_picker("Accent", "#F8FAFC", key="sb_ac")

    with st.expander("📊 Grading & Assessment Benchmarks", expanded=False):
        st.caption("Set learner classification thresholds for this cohort")
        marks_pass_threshold = st.slider(
            "Passing Score Threshold (%)", 30, 90,
            value=st.session_state.marks_pass_threshold, step=5, key="sb_pass_thresh"
        )
        st.session_state.marks_pass_threshold = marks_pass_threshold

        st.session_state.benchmark_shortlist = st.slider(
            "Shortlist for Live Interview — Minimum Percentile (%)", 50, 100, 
            value=st.session_state.benchmark_shortlist, step=5, key="sb_bm_short",
            help=(
                "Students are shortlisted if their cohort percentile is ≥ this value. "
                "Example: setting 80 means a student must score higher than at least 80% "
                "of the cohort (i.e., top 20%) to be shortlisted for a live interview."
            )
        )
        st.session_state.benchmark_strong = st.slider(
            "Concept Mastery Benchmark (Top X%)", 50, 100, 
            value=st.session_state.benchmark_strong, step=5, key="sb_bm_strong"
        )
        st.session_state.benchmark_revision = st.slider(
            "Revision Required (Below X%)", 10, 90, 
            value=st.session_state.benchmark_revision, step=5, key="sb_bm_rev"
        )

        # ── Table-wise Mock Interview Basis ──
        marks_tbl_names = [
            s.get("title") or s.get("name")
            for s in st.session_state.sections
            if isinstance(s, dict) and s.get("type") == "marks_table" and s.get("enabled", True)
        ]
        seen_tbls = set()
        uniq_tbl_names = [x for x in marks_tbl_names if x and not (x in seen_tbls or seen_tbls.add(x))]
        tbl_options = ["All Marks Tables (Combined)"] + uniq_tbl_names
        curr_tbl_sel = st.session_state.get("mi_benchmark_table", "All Marks Tables (Combined)")
        sel_tbl_idx = tbl_options.index(curr_tbl_sel) if curr_tbl_sel in tbl_options else 0

        st.session_state.mi_benchmark_table = st.selectbox(
            "Mock Interview Shortlist Basis Table",
            tbl_options,
            index=sel_tbl_idx,
            key="sb_mi_bm_table",
            help=(
                "Choose which marks table is used to calculate student percentiles and determine "
                "mock interview eligibility. 'All Marks Tables (Combined)' evaluates students on total marks across all tables. "
                "Or select an individual table (e.g. 'Python Programming' or 'MySQL') to evaluate eligibility specifically on that table."
            )
        )

        st.markdown("---")
        marks_tone = st.selectbox(
            "AI Feedback Tone", ["Encouraging", "Professional", "Strict", "CTO & Mentor"],
            index=["Encouraging", "Professional", "Strict", "CTO & Mentor"].index(st.session_state.marks_tone) if st.session_state.marks_tone in ["Encouraging", "Professional", "Strict", "CTO & Mentor"] else 0,
            key="sb_tone"
        )
        st.session_state.marks_tone = marks_tone

        marks_module_context = st.text_input(
            "Course / Module Context (for AI feedback)",
            value=st.session_state.marks_module_context,
            placeholder="e.g. Data Science Bootcamp, Full Stack Development",
            key="sb_module_ctx"
        )
        st.session_state.marks_module_context = marks_module_context

        st.markdown("---")
        marks_show_benchmark_note = st.toggle(
            "Include Benchmark Notice in PDF",
            value=st.session_state.marks_show_benchmark_note,
            key="sb_show_bm_note",
            help="Show or hide the candidate relative benchmarks note in the PDF report."
        )
        st.session_state.marks_show_benchmark_note = marks_show_benchmark_note
        
        if st.session_state.marks_show_benchmark_note:
            marks_benchmark_note = st.text_area(
                "Benchmark Performance Note",
                value=st.session_state.marks_benchmark_note,
                height=100,
                key="sb_bm_note",
                help="This notice appears above the module-wise performance benchmarks table."
            )
            st.session_state.marks_benchmark_note = marks_benchmark_note

    with st.expander("🚀 Next Steps & Learner Actions", expanded=False):
        enable_mock_interview = st.toggle("Enable Live Interview Scheduling", value=st.session_state.enable_mock_interview, key="sb_mock_int")
        st.session_state.enable_mock_interview = enable_mock_interview
        
        st.session_state.footer_deadline_text = st.text_area(
            "Scheduling Deadline Notice", value=st.session_state.footer_deadline_text, height=60, key="sb_ft_deadline"
        )
        st.session_state.footer_mentor_text = st.text_area(
            "Mentor / Counselor Message", value=st.session_state.footer_mentor_text, height=60, key="sb_ft_mentor"
        )
        st.session_state.footer_link_text = st.text_input("CTA Button Text", value=st.session_state.footer_link_text, key="sb_ft_ltxt")
        st.session_state.footer_link_url = st.text_input("CTA Link URL", value=st.session_state.footer_link_url, key="sb_ft_lurl")

    with st.expander("🤖 AI Feedback Engine", expanded=False):
        enable_ai = st.toggle("Enable Groq AI Feedback", value=False, key="sb_ai")
        env_key   = os.environ.get("GROQ_API_KEY", "").strip()
        api_key   = st.text_input("Groq API Key", type="password", value=env_key, placeholder="gsk_...", key="sb_apikey")
        ai_model  = st.selectbox("AI Model", ["llama-3.3-70b-versatile","llama-3.1-8b-instant","mixtral-8x7b-32768"], key="sb_model")

    with st.expander("💾 Report Templates", expanded=False):
        saved_templates = [f[:-5] for f in os.listdir(TEMPLATE_DIR) if f.endswith(".json")]
        if saved_templates:
            sel_t = st.selectbox("Saved", ["— select —"] + saved_templates, key="sb_sel_t")
            tc1, tc2 = st.columns(2)
            with tc1:
                if st.button("📂 Load", key="sb_load_t") and sel_t != "— select —":
                    with open(f"{TEMPLATE_DIR}/{sel_t}.json", encoding="utf-8") as fh:
                        t = json.load(fh)
                    for k in ("identity_cols","pdf_name_col","written_cfg","written_name",
                              "wt_enabled","sections","perf_targets","show_perf",
                              "intro_text","show_tech_action","show_comm_action",
                              "enable_mock_interview", "marks_benchmark_note", "marks_show_benchmark_note"):
                        if k in t: st.session_state[k] = t[k]
                    st.success(f"Loaded: {sel_t}")
                    st.rerun()
            with tc2:
                if st.button("🗑️ Del", key="sb_del_t") and sel_t != "— select —":
                    os.remove(f"{TEMPLATE_DIR}/{sel_t}.json")
                    st.warning("Deleted.")
                    st.rerun()
        else:
            st.caption("No templates yet.")

        st.markdown("---")
        tname = st.text_input("Name", "My Template", key="sb_tname")
        if st.button("💾 Save", key="sb_save_t"):
            payload = {k: st.session_state[k] for k in
                       ("identity_cols","pdf_name_col","written_cfg","written_name",
                        "wt_enabled","sections","perf_targets","show_perf",
                        "intro_text","show_tech_action","show_comm_action",
                        "enable_mock_interview", "marks_benchmark_note", "marks_show_benchmark_note") if k in st.session_state}
            payload["saved_at"] = datetime.now().isoformat()
            safe = re.sub(r'[^A-Za-z0-9_\- ]', '', tname).strip() or "template"
            with open(f"{TEMPLATE_DIR}/{safe}.json", "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            st.success(f"Saved: {safe}")

    with st.expander("🖼️ Institute Logo", expanded=False):
        logo_up = st.file_uploader("Upload Institute Logo", type=["png","jpg","jpeg"], key="sb_logo")
        if logo_up:
            st.session_state.logo_bytes = logo_up.read()
            st.image(io.BytesIO(st.session_state.logo_bytes), width=120)
        else:
            for _lp in ["logo/innomatics_logo.png","logo/logo.png","logo/company_logo.png"]:
                if os.path.exists(_lp):
                    st.caption(f"🖼️ Using: `{_lp}`")
                    st.image(_lp, width=120)
                    break
            else:
                st.caption("No logo found — upload one above.")

    with st.expander("📋 Audit Log Settings", expanded=False):
        st.caption("Choose which events are written to the audit log CSV. Unticked events are silently skipped — no log row is created.")

        _audit_opts = {
            "Generated (Batch)":          "📄 Report Generation (per student)",
            "Downloaded (Batch ZIP)":     "📦 Batch ZIP Download (per student)",
            "Downloaded (Preview)":       "🔎 Preview PDF Download",
            "Downloaded (Manual)":        "✏️ Manual Entry Download",
            "Downloaded (History)":       "🗂️ History Tab Re-download",
            "Mock Interview Status Update": "📅 Mock Interview Booking Updates",
        }

        new_audit_events = set()
        for _key, _label in _audit_opts.items():
            _checked = _key in st.session_state.audit_log_events
            if st.checkbox(_label, value=_checked, key=f"audit_ev_{_key.replace(' ','_')}"):
                new_audit_events.add(_key)
        st.session_state.audit_log_events = new_audit_events

        _active = len(new_audit_events)
        if _active == 0:
            st.warning("⚠️ No events selected — nothing will be logged.")
        else:
            st.caption(f"✅ **{_active} of {len(_audit_opts)}** event types will be logged.")

    with st.expander("🔐 Security & Compliance", expanded=False):
        st.caption("PDF Watermark & Legal Notice Controls")
        show_wm = st.toggle("Enable PDF Watermark", value=st.session_state.show_watermark, key="sb_show_wm")
        st.session_state.show_watermark = show_wm
        
        if show_wm:
            wm_text = st.text_input("Watermark Text", value=st.session_state.watermark_text, key="sb_wm_txt")
            st.session_state.watermark_text = wm_text
            
        legal_text = st.text_area("Legal / Disclaimer Notice", value=st.session_state.legal_notice, height=70, key="sb_legal_txt")
        st.session_state.legal_notice = legal_text

    with st.expander("📐 Report Layout & Appearance", expanded=False):
        st.caption("Page Geometry, Identity Card Style & Table Aesthetics")

        # ── Page Geometry ─────────────────────────────────────────
        st.markdown("**Page Geometry**")
        _ps_opts = ["A4", "Letter", "Legal"]
        _curr_ps = st.session_state.report_page_size
        _ps_idx = _ps_opts.index(_curr_ps) if _curr_ps in _ps_opts else 0
        st.session_state.report_page_size = st.selectbox(
            "Page Size", _ps_opts, index=_ps_idx, key="sb_page_size",
            help="PDF paper dimensions. Default: A4 (matches standard certificate format)."
        )

        _ori_opts = ["Portrait", "Landscape"]
        _curr_ori = st.session_state.report_orientation
        _ori_idx = _ori_opts.index(_curr_ori) if _curr_ori in _ori_opts else 0
        st.session_state.report_orientation = st.selectbox(
            "Orientation", _ori_opts, index=_ori_idx, key="sb_orientation",
            help="Page layout orientation. Default: Portrait."
        )

        _cm1, _cm2 = st.columns(2)
        with _cm1:
            st.session_state.report_margin_left = st.slider(
                "Left Margin (pt)", 20, 60,
                value=int(st.session_state.report_margin_left), step=2, key="sb_margin_left"
            )
        with _cm2:
            st.session_state.report_margin_right = st.slider(
                "Right Margin (pt)", 20, 60,
                value=int(st.session_state.report_margin_right), step=2, key="sb_margin_right"
            )

        st.markdown("---")
        # ── Intro / Student Greeting Card Style ───────────────────
        st.markdown("**Intro & Identity Card Style**")
        _ic_opts = ["Callout (Accent Left Border)", "Banner (Top Header Box)", "Clean (Minimal)"]
        _curr_ic = st.session_state.intro_card_style
        _ic_idx = _ic_opts.index(_curr_ic) if _curr_ic in _ic_opts else 0
        st.session_state.intro_card_style = st.selectbox(
            "Card Style", _ic_opts, index=_ic_idx, key="sb_intro_card_style",
            help="'Callout' gives a thick accent border on the left. 'Banner' adds a top border. 'Clean' removes border padding."
        )

        _cc1, _cc2 = st.columns(2)
        with _cc1:
            st.session_state.intro_bg_color = st.color_picker(
                "Card Background", value=st.session_state.intro_bg_color, key="sb_intro_bg"
            )
        with _cc2:
            _default_border = primary_color if not st.session_state.intro_border_color else st.session_state.intro_border_color
            st.session_state.intro_border_color = st.color_picker(
                "Card Accent Border", value=_default_border, key="sb_intro_border"
            )

        st.markdown("---")
        # ── Table Border Style Variants ───────────────────────────
        st.markdown("**Table & Grid Aesthetics**")
        _tb_opts = ["Full Grid", "Horizontal Lines Only", "Boxed Border", "Subtle Border", "Minimal (No Grid)"]
        _curr_tb = st.session_state.table_border_style
        _tb_idx = _tb_opts.index(_curr_tb) if _curr_tb in _tb_opts else 0
        st.session_state.table_border_style = st.selectbox(
            "Border Style", _tb_opts, index=_tb_idx, key="sb_tbl_border_style",
            help="Customise table borders across all score and assessment tables."
        )

        _cb1, _cb2 = st.columns(2)
        with _cb1:
            st.session_state.table_border_color = st.color_picker(
                "Border Line Color", value=st.session_state.table_border_color, key="sb_tbl_bcolor"
            )
        with _cb2:
            st.session_state.table_border_width = st.slider(
                "Border Width (pt)", 0.2, 1.5,
                value=float(st.session_state.table_border_width), step=0.1, key="sb_tbl_bwidth"
            )





# ─────────────────────────────────────────────────────────────
# HERO & KPI STRIP — Real-Time Production Workspace
# ─────────────────────────────────────────────────────────────
# Gather stats for the KPI strip
has_data_kpi = st.session_state.df is not None
student_count_kpi = len(st.session_state.df) if has_data_kpi else 0
sections_count_kpi = len([s for s in st.session_state.sections if s.get("enabled", True)])
ai_mode_lbl = "AI FEEDBACK: ACTIVE" if enable_ai else "AI FEEDBACK: OFF"
ai_class = "chip-ai-on" if enable_ai else "chip-ai-off"

# Render Hero Banner with Innomatics Operations & Dispatch Branding
if os.path.exists("logo/innomatics_logo.png"):
    st.image("logo/innomatics_logo.png", width=260)

st.markdown(f"""
<div class="hero">
  <h1>Innomatics Research Labs – Operations & Dispatch Report Manager</h1>
  <p>Automated Student Assessment Reports, Real-Time Cohort Analytics & Bulk Email Delivery &nbsp;·&nbsp; <strong style="color: #B91C1C;">Innomatics Research Labs</strong></p>
</div>
""", unsafe_allow_html=True)

# Render Industrial KPI Strip
st.markdown(f"""
<div class="kpi-strip">
  <div class="kpi-card navy">
    <div class="kpi-val">{student_count_kpi}</div>
    <div class="kpi-lbl">LOADED RECORDS</div>
    <div class="kpi-sub">Active learner cohort</div>
  </div>
  <div class="kpi-card green">
    <div class="kpi-val">{sections_count_kpi}</div>
    <div class="kpi-lbl">ACTIVE SECTIONS</div>
    <div class="kpi-sub">Report modules enabled</div>
  </div>
  <div class="kpi-card gold">
    <div class="kpi-val">{"ENABLED" if st.session_state.wt_enabled else "DISABLED"}</div>
    <div class="kpi-lbl">WRITTEN EXAM</div>
    <div class="kpi-sub">Theory marks processing</div>
  </div>
  <div class="kpi-card red">
    <div class="kpi-val">{"ACTIVE" if st.session_state.enable_mock_interview else "OFF"}</div>
    <div class="kpi-lbl">MOCK SCHEDULE</div>
    <div class="kpi-sub">Booking integration</div>
  </div>
</div>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────
# ONBOARDING GUIDE
# ─────────────────────────────────────────────────────────────
g_col1, g_col2 = st.columns([7, 3])
with g_col1:
    st.caption("💡 First time here? Toggle the Onboarding Guide to learn how to generate reports in 3 easy steps.")
with g_col2:
    show_guide = st.toggle("📖 Onboarding Guide", value=st.session_state.show_guide, key="toggle_guide")
    st.session_state.show_guide = show_guide

if st.session_state.show_guide:
    st.markdown("""
    <div class="guide-box">
      <div class="guide-title">
        🚀 Onboarding Guide — Master Operations & Dispatch Report Manager in 3 Steps
      </div>
      <div class="guide-steps-grid">
        <div class="guide-step">
          <div class="guide-step-title">
            <span class="step-num">Step 1</span> 📁 Prep & Source Learner Data
          </div>
          <div class="guide-step-desc">
            Choose your reporting mode on the <strong>Learner Data</strong> workspace:
            <ul>
              <li><strong>Option A: Generate an Interview Feedback Report only</strong> — Upload your Mock Interview file. Verify that the columns contain skill ratings (using standard scale terms: <code>Poor</code>, <code>Average</code>, <code>Good</code>, <code>Excellent</code>).</li>
              <li><strong>Option B: Generate an Assessment Marks Report only</strong> — Upload your cohort marksheets. Ensure the columns contain numerical scores for individual subjects.</li>
              <li><strong>Option C: Generate a Combined Assessment & Feedback Report</strong> — Upload <em>both</em> CSV/Excel datasets and merge them instantly on a common key (e.g. Email or Enrollment ID) in the **Merge Two Datasets** panel.</li>
            </ul>
          </div>
        </div>
        <div class="guide-step">
          <div class="guide-step-title">
            <span class="step-num">Step 2</span> 🎨 Build Your Report Template
          </div>
          <div class="guide-step-desc">
            Navigate to the <strong>Report Builder</strong> tab to layout custom PDF modules:
            <ul>
              <li>Use **Add Rating Table** to display Mock Interview skill ratings.</li>
              <li>Enable **Action / Improvement Plans** (Technical & Communication) to print qualitative mentorship suggestions.</li>
              <li>Add a **Marks Module** for subject marks to trigger passing thresholds, cohort grade distributions, and class rankings.</li>
            </ul>
          </div>
        </div>
        <div class="guide-step">
          <div class="guide-step-title">
            <span class="step-num">Step 3</span> ⚡ Validate, Generate & Export
          </div>
          <div class="guide-step-desc">
            Head to the <strong>Generate Reports</strong> tab. Review individual profiles, validate data integrity, run cohort generation (with optional AI feedback), and instantly download the compiled student reports as a single structured <strong>ZIP file</strong>.
          </div>
        </div>
      </div>
    </div>
    """, unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
# TABS
# ─────────────────────────────────────────────────────────────
tab_data, tab_design, tab_gen, tab_cohort, tab_hist, tab_audit = st.tabs([
    "📁 Step 1: Data Sources & Merger",
    "🎨 Step 2: Report Designer & Presets",
    "⚡ Step 3: Batch PDF Generation",
    "📈 Step 4: Cohort Analytics",
    "🗂️ Step 5: Generation History",
    "🔍 Step 6: Audit & Compliance",
])

# ══════════════════════════════════════════════════════════════
# TAB 1 — DATA SOURCE
# ══════════════════════════════════════════════════════════════
with tab_data:
    st.markdown("## 📁 Learner Data Source")

    b1, b2, b3, b4 = st.columns(4)
    with b1:
        if st.button("📤 Batch Upload — CSV / Excel",
                     type="primary" if st.session_state.input_mode == "upload" else "secondary",
                     key="btn_mode_up"):
            st.session_state.input_mode = "upload"; st.rerun()
    with b2:
        if st.button("🟢 Google Sheet Link",
                     type="primary" if st.session_state.input_mode == "gsheet" else "secondary",
                     key="btn_mode_gsheet"):
            st.session_state.input_mode = "gsheet"; st.rerun()
    with b3:
        if st.button("✏️ Manual Entry — Single Learner",
                     type="primary" if st.session_state.input_mode == "manual" else "secondary",
                     key="btn_mode_man"):
            st.session_state.input_mode = "manual"; st.rerun()
    with b4:
        if st.button("🔗 Merge Two Datasets",
                     type="primary" if st.session_state.input_mode == "merge" else "secondary",
                     key="btn_mode_merge"):
            st.session_state.input_mode = "merge"; st.rerun()

    st.markdown("<br/>", unsafe_allow_html=True)

    # ── UPLOAD ──────────────────────────────────────────────
    if st.session_state.input_mode == "upload":
        up = st.file_uploader("Upload CSV or Excel", type=["csv","xlsx","xls"], key="up_file")
        if up is not None:
            # ── CSV Validation (size, empty-file, row count) ─────────────
            if up.name.lower().endswith(".csv"):
                ok, err_msg = _validate_csv(up)
                if not ok:
                    st.error(err_msg)
                    st.stop()

            # Only reload the DataFrame when the uploaded file actually changes
            # (identified by name + byte-size). Without this guard, every widget
            # interaction re-runs _load() and overwrites the session data.
            _file_key = f"{up.name}_{up.size}"
            if st.session_state.get("_last_file_key") != _file_key:
                def _load(name, data):
                    buf = io.BytesIO(data)
                    df = pd.read_csv(buf) if name.lower().endswith(".csv") else pd.read_excel(buf)
                    # Drop rows where ALL cells are empty/NaN (trailing blank rows
                    # from Excel/Google Sheets exports inflate the row count)
                    df = df.dropna(how="all").reset_index(drop=True)
                    return df

                raw = _load(up.name, up.getvalue())
                raw.columns = [str(c).strip() for c in raw.columns]
                st.session_state.source_file    = up.name
                st.session_state.force_regenerate = False
                st.session_state["_last_file_key"] = _file_key
                from generation_guard import invalidate_cache
                invalidate_cache()
                st.session_state.df = raw.copy()
            # Always read cols from session state (works for both fresh load and cached load)
            cols = st.session_state.df.columns.tolist()


            st.markdown("### ✏️ Learner Records — Review & Edit")
            edited = st.data_editor(st.session_state.df, num_rows="dynamic", key="data_ed")
            st.session_state.df = edited

            t1, t2, t3 = st.columns(3)
            with t1:
                dup_col = st.selectbox("Find duplicates by:", cols, key="dup_col")
            with t2:
                st.markdown("<br/>", unsafe_allow_html=True)
                if st.button("🔍 Check Duplicates", key="btn_dups"):
                    d = edited[edited.duplicated(subset=[dup_col], keep=False)]
                    if len(d):
                        st.warning(f"⚠️ {len(d)} duplicate rows on **{dup_col}**")
                        st.dataframe(d)
                    else:
                        st.success("No duplicates found!")
            with t3:
                st.markdown("<br/>", unsafe_allow_html=True)
                st.download_button("⬇️ Download Edited CSV", edited.to_csv(index=False).encode("utf-8"),
                                   "edited_data.csv", "text/csv", key="btn_dl_edit")

            st.info("⬆️ Go to the **🎨 Report Builder** tab to configure your report sections.")
        else:
            st.info("👆 Upload a cohort CSV or Excel file to begin generating learner reports.")
            st.markdown("<br/>", unsafe_allow_html=True)
            
            cs1, cs2 = st.columns(2)
            with cs1:
                if st.button("⚡ Load Demo Cohort Data", key="btn_load_demo", width='stretch'):
                    # Load pristine demo data
                    demo_data = {
                        "Student Name": ["Arjun Mehta", "Sophia Sen", "Kabir Kapoor", "Ananya Rao", "Rohan Sharma"],
                        "Enrollment ID": ["INN-2026-001", "INN-2026-002", "INN-2026-003", "INN-2026-004", "INN-2026-005"],
                        "Batch / Cohort": ["Data Science Core", "Data Science Core", "Full Stack Core", "Full Stack Core", "Data Science Core"],
                        "Evaluator Name": ["Deepraj Vadhwane", "Deepraj Vadhwane", "Deepraj Vadhwane", "Deepraj Vadhwane", "Deepraj Vadhwane"],
                        "Python Programming": ["Excellent", "Good", "Average", "Excellent", "Poor"],
                        "SQL Database Skill": ["Excellent", "Average", "Good", "Good", "Average"],
                        "Machine Learning Concepts": ["Good", "Average", "Good", "Excellent", "Poor"],
                        "Communication & Articulation": ["Excellent", "Good", "Excellent", "Average", "Good"],
                        "Theory Assessment (%)": [88, 72, 65, 94, 48],
                        "Hands-on Lab (%)": [92, 78, 70, 96, 52],
                        "Final Project Score (%)": [90, 80, 75, 95, 50],
                        "Technical Remarks": [
                            "Outstanding developer mindset. Strong foundations in data pipelines and optimization.",
                            "Demonstrated solid debugging skills. Needs practice on complex database joins.",
                            "Good core concepts. Encouraged to spend more hours building raw frontend code.",
                            "Masterful understanding of advanced algorithms. Excellent class participation.",
                            "Required to review core programming loops and database normalization basics."
                        ],
                        "Communication Remarks": [
                            "Articulates complex designs with ease. Natural team lead.",
                            "Clear speaker, rushes explanation sometimes. Practice explaining under timed conditions.",
                            "Highly engaging speaker. Excellent visual storytelling and whiteboard presentation.",
                            "Clear, concise, and structured. Ready for senior stakeholder mock reviews.",
                            "Needs to structure explanations. Practice the STAR framework for behavioral mocks."
                        ]
                    }
                    demo_df = pd.DataFrame(demo_data)
                    st.session_state.df = demo_df
                    st.session_state.identity_cols = ["Student Name", "Enrollment ID", "Batch / Cohort", "Evaluator Name"]
                    st.session_state.pdf_name_col = "Student Name"
                    
                    # Pre-configure beautiful builder sections
                    st.session_state.sections = [
                        {
                            "name": "💻 Core Technical Skills",
                            "type": "rating_table",
                            "enabled": True,
                            "columns": ["Python Programming", "SQL Database Skill", "Machine Learning Concepts"]
                        },
                        {
                            "name": "🗣️ Communication Skills",
                            "type": "rating_table",
                            "enabled": True,
                            "columns": ["Communication & Articulation"]
                        },
                        {
                            "name": "📝 Exam Performance",
                            "type": "rating_table",
                            "enabled": True,
                            "columns": ["Theory Assessment (%)", "Hands-on Lab (%)", "Final Project Score (%)"]
                        },
                        {
                            "name": "💡 Technical Action Plan",
                            "type": "text_field",
                            "enabled": True,
                            "columns": ["Technical Remarks"]
                        },
                        {
                            "name": "💬 Communication Action Plan",
                            "type": "text_field",
                            "enabled": True,
                            "columns": ["Communication Remarks"]
                        }
                    ]
                    st.session_state.show_tech_action = True
                    st.session_state.show_comm_action = True
                    st.success("🎉 Sample cohort data (5 students) and report structures loaded successfully! Go to 'Report Builder' or 'Generate Reports' next.")
                    st.rerun()
                    
            with cs2:
                # Prepare a beautiful starter CSV structure to download
                dl_demo_data = {
                    "Student Name": ["Arjun Mehta"],
                    "Enrollment ID": ["INN-2026-001"],
                    "Batch / Cohort": ["Data Science Core"],
                    "Evaluator Name": ["Deepraj Vadhwane"],
                    "Python Programming": ["Excellent"],
                    "SQL Database Skill": ["Excellent"],
                    "Machine Learning Concepts": ["Good"],
                    "Communication & Articulation": ["Excellent"],
                    "Theory Assessment (%)": [88],
                    "Hands-on Lab (%)": [92],
                    "Final Project Score (%)": [90],
                    "Technical Remarks": ["Outstanding developer mindset. Strong foundations in pipelines."],
                    "Communication Remarks": ["Articulates complex designs with ease. Natural team lead."]
                }
                dl_df = pd.DataFrame(dl_demo_data)
                csv_bytes = dl_df.to_csv(index=False).encode('utf-8')
                st.download_button(
                "⬇️ Download Starter Template CSV",
                data=csv_bytes,
                file_name="deepraj_analytics_template.csv",
                mime="text/csv",
                key="btn_dl_template",
                width='stretch'
            )

    # ── GOOGLE SHEET ──────────────────────────────────────────
    elif st.session_state.input_mode == "gsheet":
        st.markdown("### 🟢 Google Sheet Link Integration")
        st.info("""
        **Frictionless Import:** Paste the Google Sheet URL below.
        
        ⚠️ **Important requirement:** Your sheet sharing setting must be set to **"Anyone with the link can view"** so the generator can fetch the records.
        """)
        
        sheet_url = st.text_input(
            "Google Sheet URL",
            value=st.session_state.gsheet_url,
            placeholder="https://docs.google.com/spreadsheets/d/.../edit?usp=sharing",
            key="input_gsheet_url"
        )
        st.session_state.gsheet_url = sheet_url
        
        if st.button("⚡ Load Google Sheet Data", type="primary", key="btn_load_gsheet"):
            if not sheet_url.strip():
                st.error("Please enter a valid Google Sheets URL.")
            else:
                with st.spinner("Fetching data from Google Sheets..."):
                    try:
                        id_match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", sheet_url)
                        if not id_match:
                            st.error("Invalid Google Sheet URL format. Make sure it contains '/spreadsheets/d/SPREADSHEET_ID'")
                        else:
                            sheet_id = id_match.group(1)
                            gid_match = re.search(r"gid=([0-9]+)", sheet_url)
                            if gid_match:
                                csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid_match.group(1)}"
                            else:
                                csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
                            
                            raw = pd.read_csv(csv_url)
                            # Drop fully-blank rows (common in Google Sheets exports)
                            raw = raw.dropna(how="all").reset_index(drop=True)
                            raw.columns = [str(c).strip() for c in raw.columns]
                            st.session_state.source_file = f"GSheet: {sheet_id}"
                            st.session_state.df = raw.copy()
                            st.success(f"🎉 Successfully loaded **{len(raw)}** records from Google Sheets!")
                            st.rerun()
                    except Exception as e:
                        st.error(f"❌ Failed to fetch Google Sheet data: {e}")
                        st.info("💡 Double check that your Google Sheet is set to 'Anyone with the link can view'.")

        if st.session_state.df is not None:
            raw = st.session_state.df
            cols = raw.columns.tolist()
            
            st.markdown(f"""
            <div class="stat-grid">
              <div class="stat-card"><div class="val">{len(raw)}</div><div class="lbl">Learners</div></div>
              <div class="stat-card"><div class="val">{len(cols)}</div><div class="lbl">Data Fields</div></div>
              <div class="stat-card"><div class="val">{"ON" if enable_ai else "OFF"}</div><div class="lbl">AI Feedback</div></div>
              <div class="stat-card"><div class="val">{len(st.session_state.sections)}</div><div class="lbl">Report Sections</div></div>
            </div>
            """, unsafe_allow_html=True)

            st.markdown("### ✏️ Learner Records — Review & Edit")
            edited = st.data_editor(st.session_state.df, num_rows="dynamic", key="data_ed_gsheet")
            st.session_state.df = edited

            t1, t2, t3 = st.columns(3)
            with t1:
                dup_col = st.selectbox("Find duplicates by:", cols, key="dup_col_gsheet")
            with t2:
                st.markdown("<br/>", unsafe_allow_html=True)
                if st.button("🔍 Check Duplicates", key="btn_dups_gsheet"):
                    d = edited[edited.duplicated(subset=[dup_col], keep=False)]
                    if len(d):
                        st.warning(f"⚠️ {len(d)} duplicate rows on **{dup_col}**")
                        st.dataframe(d)
                    else:
                        st.success("No duplicates found!")
            with t3:
                st.markdown("<br/>", unsafe_allow_html=True)
                st.download_button("⬇️ Download Edited CSV", edited.to_csv(index=False).encode("utf-8"),
                                   "edited_data.csv", "text/csv", key="btn_dl_edit_gsheet")

            st.info("⬆️ Go to the **🎨 Report Builder** tab to configure your report sections.")

    # ── MERGE TWO CSVs ────────────────────────────────────────
    elif st.session_state.input_mode == "merge":
        st.markdown("### 🔗 Merge Two Learner Datasets")
        st.info("""
        Upload two datasets (e.g. **Assessment Results CSV** + **Module Marks CSV**).  
        Choose a common key column (Enrollment ID, Email, etc.) from each file — only matching learners will appear in the merged dataset.
        """)

        @st.cache_data(show_spinner=False)
        def _load_any(name, data):
            buf = io.BytesIO(data)
            return pd.read_csv(buf) if name.lower().endswith(".csv") else pd.read_excel(buf)

        mc1, mc2 = st.columns(2)

        with mc1:
            st.markdown("#### 📄 File 1 — Primary Assessment Dataset")
            st.caption("e.g. Mock Interview / Skill Evaluation CSV (ratings, feedback)")
            up_left = st.file_uploader("Upload File 1", type=["csv","xlsx","xls"], key="merge_up_left")
            if up_left:
                if up_left.size > 10 * 1024 * 1024:
                    st.warning("⚠️ **Large File Warning:** File 1 is larger than 10MB.")
                df_left = _load_any(up_left.name, up_left.getvalue())
                df_left.columns = [str(c).strip() for c in df_left.columns]
                st.session_state.merge_df_left = df_left
                st.success(f"✅ {len(df_left)} rows · {len(df_left.columns)} columns")
                st.dataframe(df_left.head(3), width='stretch')

        with mc2:
            st.markdown("#### 📄 File 2 — Module Scores Dataset")
            st.caption("e.g. Module Marks / Course Assessment CSV")
            up_right = st.file_uploader("Upload File 2", type=["csv","xlsx","xls"], key="merge_up_right")
            if up_right:
                if up_right.size > 10 * 1024 * 1024:
                    st.warning("⚠️ **Large File Warning:** File 2 is larger than 10MB.")
                df_right = _load_any(up_right.name, up_right.getvalue())
                df_right.columns = [str(c).strip() for c in df_right.columns]
                st.session_state.merge_df_right = df_right
                st.success(f"✅ {len(df_right)} rows · {len(df_right.columns)} columns")
                st.dataframe(df_right.head(3), width='stretch')

        df_l = st.session_state.merge_df_left
        df_r = st.session_state.merge_df_right

        if df_l is not None and df_r is not None:
            st.markdown("---")
            st.markdown("#### 🔑 Join Configuration")

            jc1, jc2, jc3 = st.columns([2, 2, 1])
            with jc1:
                key_left = st.selectbox(
                    "Join key from File 1",
                    df_l.columns.tolist(),
                    index=next((i for i, c in enumerate(df_l.columns) if any(k in c.lower() for k in ["enroll","email","id"])), 0),
                    key="merge_key_left"
                )
            with jc2:
                key_right = st.selectbox(
                    "Join key from File 2",
                    df_r.columns.tolist(),
                    index=next((i for i, c in enumerate(df_r.columns) if any(k in c.lower() for k in ["enroll","email","id"])), 0),
                    key="merge_key_right"
                )
            with jc3:
                join_type = st.selectbox(
                    "Join Type",
                    ["inner", "left", "right"],
                    key="merge_join_type",
                    help="inner = only students present in BOTH files; left = all from File 1"
                )

            if st.button("🔗 Merge Now", type="primary", key="btn_do_merge"):
                try:
                    # Rename key in right df to match left if different
                    df_r_renamed = df_r.copy()
                    if key_left != key_right:
                        df_r_renamed = df_r_renamed.rename(columns={key_right: key_left})

                    # Drop overlapping non-key columns from right (add _marks suffix)
                    overlap = [c for c in df_r_renamed.columns if c in df_l.columns and c != key_left]
                    df_r_renamed = df_r_renamed.rename(columns={c: f"{c}_marks" for c in overlap})

                    merged = pd.merge(df_l, df_r_renamed, on=key_left, how=join_type)
                    merged.columns = [str(c).strip() for c in merged.columns]

                    st.session_state.merged_df = merged
                    st.session_state.df = merged.copy()  # Make it the active dataset
                    st.session_state.source_file = f"Merged: {up_left.name} + {up_right.name}"
                    st.success(f"✅ Merged! **{len(merged)} students** matched · **{len(merged.columns)} columns** total")
                except Exception as e:
                    st.error(f"❌ Merge failed: {e}")

            if st.session_state.merged_df is not None:
                merged = st.session_state.merged_df
                left_only = len(df_l) - len(merged) if join_type == "inner" else 0
                right_only = len(df_r) - len(merged) if join_type == "inner" else 0

                st.markdown(f"""
                <div class="stat-grid">
                  <div class="stat-card"><div class="val">{len(merged)}</div><div class="lbl">Matched Learners</div></div>
                  <div class="stat-card"><div class="val">{len(merged.columns)}</div><div class="lbl">Total Fields</div></div>
                  <div class="stat-card"><div class="val">{left_only}</div><div class="lbl">Unmatched (File 1)</div></div>
                  <div class="stat-card"><div class="val">{right_only}</div><div class="lbl">Unmatched (File 2)</div></div>
                </div>
                """, unsafe_allow_html=True)

                st.markdown("#### 👁️ Merged Data Preview")
                st.dataframe(merged, width='stretch', height=280)

                dl_col1, dl_col2 = st.columns(2)
                with dl_col1:
                    st.download_button(
                        "⬇️ Download Merged CSV",
                        merged.to_csv(index=False).encode("utf-8"),
                        "merged_data.csv", "text/csv", key="merge_dl"
                    )
                with dl_col2:
                    st.info("✅ Merged dataset is now active — go to **🎨 Report Builder** to configure report sections.")
        else:
            st.info("👆 Upload both learner dataset files above to configure the merge.")

    # ── MANUAL ENTRY ──────────────────────────────────────
    else:
        st.markdown("### ✏️ Manual Entry — Single Learner Report")
        man_secs = st.session_state.sections if st.session_state.sections else [
            {"name":"Technical Skill Evaluation","type":"rating_table","enabled":True,
             "columns":["Concept 1","Concept 2","Concept 3"]},
            {"name":"Instructor Feedback","type":"text_field","enabled":True,"columns":["Remarks"]},
        ]
        man_id = st.session_state.identity_cols or \
                 ["Student Name","Enrollment ID","Batch / Cohort","Evaluator Name"]

        with st.form("manual_form", clear_on_submit=False):
            vals = {}
            st.markdown("#### 👤 Learner Information")
            ma, mb = st.columns(2)
            for i, col in enumerate(man_id):
                with (ma if i % 2 == 0 else mb):
                    vals[col] = st.text_input(clean_label(col), key=f"mf_{i}")

            for si, sec in enumerate(man_secs):
                if not sec.get("enabled", True):
                    continue
                st.markdown(f"#### {sec['name']}")
                sa, sb = st.columns(2)
                cols_def = sec.get("columns", [])
                if sec["type"] == "rating_table":
                    for i, col in enumerate(cols_def):
                        with (sa if i % 2 == 0 else sb):
                            vals[col] = st.selectbox(clean_label(col), RATING_OPTIONS, key=f"mr_{si}_{i}")
                elif sec["type"] == "text_field":
                    for i, col in enumerate(cols_def):
                        vals[col] = st.text_area(clean_label(col), height=70, key=f"mt_{si}_{i}")

            sub = st.form_submit_button("🚀 Generate Learner Report")

        if sub:
            row_s = pd.Series(vals)
            # BUG-07 FIX: mcfg was missing all benchmark/footer/marks threshold keys.
            # This caused manual-entry reports to always use default values regardless
            # of what the user set in the sidebar.
            mcfg = {
                "company_name": company_name, "report_title": report_title_sb,
                "primary_color": primary_color, "secondary_color": secondary_color,
                "accent_color": accent_color,
                "identity_columns": man_id,
                "sections": man_secs,
                "show_performance_summary": st.session_state.show_perf,
                "enable_mock_interview": st.session_state.enable_mock_interview,
                "performance_sections": [s["name"] for s in man_secs if s.get("type")=="rating_table"],
                "intro_text": st.session_state.intro_text,
                "show_tech_action": st.session_state.show_tech_action,
                "show_comm_action": st.session_state.show_comm_action,
                # ── Marks / EdTech report card settings (was missing, BUG-07) ────
                "marks_pass_threshold":       st.session_state.marks_pass_threshold,
                "marks_tone":                 st.session_state.marks_tone,
                "marks_module_context":       st.session_state.marks_module_context,
                "marks_show_grade":           st.session_state.marks_show_grade,
                "marks_show_recommendations": st.session_state.marks_show_recommendations,
                "marks_show_analysis":        st.session_state.marks_show_analysis,
                "marks_show_rank":            st.session_state.marks_show_rank,
                "marks_benchmark_note":       st.session_state.marks_benchmark_note,
                "marks_show_benchmark_note":  st.session_state.marks_show_benchmark_note,
                # ── Dynamic Benchmarks & Footer (was missing, BUG-07) ─────────────
                "benchmark_shortlist":        st.session_state.benchmark_shortlist,
                "benchmark_strong":           st.session_state.benchmark_strong,
                "benchmark_revision":         st.session_state.benchmark_revision,
                "footer_link_text":           st.session_state.footer_link_text,
                "footer_link_url":            st.session_state.footer_link_url,
                "footer_deadline_text":        st.session_state.footer_deadline_text,
                "footer_mentor_text":         st.session_state.footer_mentor_text,
                "show_watermark":             st.session_state.show_watermark,
                "watermark_text":             st.session_state.watermark_text,
                "legal_notice":               st.session_state.legal_notice,
                # ── Table-wise Shortlisting & Theme Aesthetics ────────────
                "mi_benchmark_table":         st.session_state.mi_benchmark_table,
                "page_size":                  st.session_state.report_page_size,
                "orientation":                st.session_state.report_orientation.lower(),
                "margin_left":                st.session_state.report_margin_left,
                "margin_right":               st.session_state.report_margin_right,
                "intro_card_style":           {"Callout (Accent Left Border)": "callout", "Banner (Top Header Box)": "banner", "Clean (Minimal)": "clean"}.get(st.session_state.intro_card_style, "callout"),
                "intro_bg_color":             st.session_state.intro_bg_color,
                "intro_border_color":         st.session_state.intro_border_color if st.session_state.intro_border_color else primary_color,
                "table_border_style":         {"Full Grid": "grid", "Horizontal Lines Only": "horizontal_only", "Boxed Border": "boxed", "Subtle Border": "subtle", "Minimal (No Grid)": "minimal"}.get(st.session_state.table_border_style, "grid"),
                "table_border_color":         st.session_state.table_border_color,
                "table_border_width":         st.session_state.table_border_width,
            }
            with st.spinner("Generating PDF..."):
                try:
                    gc = get_groq_client(api_key) if enable_ai else None
                    st.session_state.source_file = "Manual Entry"
                    pdf_b = build_single_report(row_s, mcfg, gc, st.session_state.logo_bytes)
                    nm = vals.get("Student Name", "report").replace(" ", "_")
                    st.success(f"Report generated for **{vals.get('Student Name','')}**")
                    dl_clicked = st.download_button("📥 Download PDF", pdf_b, f"{nm}.pdf", "application/pdf", key="man_dl")
                    if dl_clicked:
                        sn = vals.get(man_id[0], "N/A") if len(man_id) > 0 else "N/A"
                        en = vals.get(man_id[1], "N/A") if len(man_id) > 1 else "N/A"
                        log_audit_event("Downloaded (Manual)", report_title_sb, sn, en, f"MANUAL_{nm}")
                    st.balloons()
                except Exception as e:
                    st.error(f"❌ {e}")


# ══════════════════════════════════════════════════════════════
# TAB 2 — REPORT DESIGNER
# ══════════════════════════════════════════════════════════════
with tab_design:
    st.markdown("## 🎨 Report Builder")

    if st.session_state.df is None:
        st.info("📁 Upload learner data in the **📁 Learner Data** tab first.")
    else:
        df_d     = st.session_state.df
        all_cols = df_d.columns.tolist()

        # ── IDENTITY ────────────────────────────────────────
        st.markdown("### 👤 Learner Identity Fields")
        id_default = [c for c in st.session_state.identity_cols if c in all_cols] or \
                     [c for c in all_cols if any(k in c.lower()
                      for k in ["name","enroll","id","batch","interviewer"])][:5]
        identity_cols = st.multiselect("Select learner identity columns:", all_cols, default=id_default, key="d_id_cols")
        st.session_state.identity_cols = identity_cols

        # ── PDF NAMING ───────────────────────────────────────
        st.markdown("---")
        st.markdown("### 🏷️ Report PDF Naming")
        cur_name_col = st.session_state.pdf_name_col if st.session_state.pdf_name_col in all_cols else all_cols[0]
        pdf_name_col = st.selectbox("Column for PDF filename:", all_cols,
                                     index=all_cols.index(cur_name_col), key="d_pdf_col")
        st.session_state.pdf_name_col = pdf_name_col
        sample_vals  = df_d[pdf_name_col].astype(str).head(5).tolist()
        preview_str  = "  |  ".join([f"{safe_filename(v)}.pdf" for v in sample_vals])
        st.markdown(f'<div class="fname-preview">📄 Preview: {preview_str}</div>', unsafe_allow_html=True)

        # ── WRITTEN TEST ─────────────────────────────────────
        st.markdown("---")
        st.markdown("### ✍️ Written / Online Assessment Section")
        wt_on = st.toggle("Include Written Assessment", value=st.session_state.wt_enabled, key="d_wt_on")
        st.session_state.wt_enabled = wt_on

        if wt_on:
            wt_title = st.text_input("Section title", value=st.session_state.written_name, key="d_wt_title")
            st.session_state.written_name = wt_title
            avail_wt = [c for c in all_cols if c not in identity_cols]
            wt_sel   = st.multiselect("Select columns:", avail_wt,
                                       default=[c for c in st.session_state.written_cfg if c in avail_wt],
                                       key="d_wt_cols")
            new_wt = {}
            if wt_sel:
                st.markdown("**Column configuration:**")
                for col in wt_sel:
                    old = st.session_state.written_cfg.get(col, {})
                    wa, wb, wc = st.columns([3, 1, 1])
                    with wa:
                        lbl = st.text_input("Label", old.get("label", clean_label(col)), key=f"wl_{col}")
                    with wb:
                        mn  = st.number_input("Min req.", 0, value=int(old.get("min_req") or 0), key=f"wn_{col}")
                    with wc:
                        mx  = st.number_input("Max", 0, value=int(old.get("max") or 0), key=f"wx_{col}")
                    new_wt[col] = {"label": lbl, "min_req": mn or None, "max": mx or None}
            st.session_state.written_cfg = new_wt
        else:
            st.session_state.written_cfg = {}

        # ── EVALUATION SECTIONS ───────────────────────────────
        st.markdown("---")
        st.markdown("### 📊 Assessment Sections")
        
        # ⚡ AUTO-CONFIGURE SMART BUTTON
        if st.button("⚡ Auto-Detect & Configure Sections from CSV", type="primary", key="btn_auto_cfg"):
            new_sections = []
            avail = all_cols.copy()
            
            # Detect Tech Rating
            tech_cols = [c for c in avail if "technical rating" in c.lower() or "rating" in c.lower()]
            if tech_cols:
                new_sections.append({"name": "Technical Evaluation", "type": "rating_table", "columns": tech_cols, "enabled": True})
                
            # Detect Tech Actions
            tech_act_cols = [c for c in avail if "action plan" in c.lower() and ("tech" in c.lower() or "mysql" in c.lower())]
            if tech_act_cols:
                new_sections.append({"name": "Technical Action Plan", "type": "action_plan_grid", "columns": tech_act_cols, "enabled": True})
                st.session_state.show_tech_action = True

            # Detect Soft Skills Actions
            soft_act_cols = [c for c in avail if "action plan" in c.lower() and ("soft" in c.lower() or "comm" in c.lower())]
            if soft_act_cols:
                new_sections.append({"name": "Communication Action Plan", "type": "action_plan_grid", "columns": soft_act_cols, "enabled": True})
                st.session_state.show_comm_action = True
                
            # Detect Remarks
            remark_cols = [c for c in avail if "remark" in c.lower() or "feedback" in c.lower()]
            if remark_cols:
                new_sections.append({"name": "Instructor Remarks", "type": "text_field", "columns": remark_cols, "enabled": True})

            if new_sections:
                st.session_state.sections = new_sections
                st.success(f"✅ Auto-configured {len(new_sections)} sections successfully!")
                st.rerun()
            else:
                st.warning("⚠️ Could not auto-detect standard columns. Please configure manually.")

        st.markdown("<br/>", unsafe_allow_html=True)
        sa1, sa2, sa3, sa4 = st.columns(4)
        with sa1:
            if st.button("➕ Add Rating Table", key="btn_add_rt"):
                st.session_state.sections.append({
                    "name": f"Section {len(st.session_state.sections)+1}",
                    "type": "rating_table", "columns": [], "enabled": True,
                })
                st.rerun()
        with sa2:
            if st.button("➕ Add Action Plan", key="btn_add_ap"):
                st.session_state.show_tech_action = True
                st.session_state.show_comm_action = True
                st.session_state.sections.append({
                    "name": "Action Plan Grid", "type": "action_plan_grid",
                    "columns": [], "enabled": True,
                })
                st.rerun()
        with sa3:
            if st.button("➕ Add Marks", key="btn_add_mt"):
                count = len([s for s in st.session_state.sections if s.get("type") == "marks_table"]) + 1
                st.session_state.sections.append({
                    "name": f"Marks Table {count}",
                    "title": "Module-wise Performance" if count == 1 else f"Module-wise Performance {count}",
                    "type": "marks_table", "columns": [], "enabled": True,
                })
                st.rerun()
        with sa4:
            if st.button("🔄 Clear All", key="btn_clr"):
                st.session_state.sections = []; st.rerun()

        avail = [c for c in all_cols
                 if c not in identity_cols and c not in st.session_state.written_cfg]
        del_idx = None

        for idx, sec in enumerate(st.session_state.sections):
            stype   = sec.get("type", "rating_table")
            icon    = "✅" if sec.get("enabled", True) else "⏸️"
            s_label = {"rating_table":"📊 Rating", "text_field":"💬 Text",
                       "action_plan_grid":"📋 Action Grid", "marks_table":"📝 Marks"}.get(stype, stype)

            with st.expander(f"{icon} {sec.get('name','Section')} [{s_label}]", expanded=True):
                e1, e2, e3, e4 = st.columns([3, 1.4, 0.8, 0.6])
                with e1:
                    sn = st.text_input("Name", sec.get("name",""), key=f"sn_{idx}")
                with e2:
                    st_type = st.selectbox("Type",
                                           ["rating_table","text_field","action_plan_grid","marks_table"],
                                           index=["rating_table","text_field","action_plan_grid","marks_table"].index(stype)
                                                 if stype in ["rating_table","text_field","action_plan_grid","marks_table"] else 0,
                                           key=f"st_{idx}")
                with e3:
                    se = st.toggle("On", value=sec.get("enabled",True), key=f"se_{idx}")
                with e4:
                    if st.button("🗑️", key=f"del_{idx}"):
                        del_idx = idx

                # Dynamic PDF Header Title input
                if st_type == "marks_table":
                    default_title = sec.get("title") or (
                        "Module-wise Performance" if sec.get("name", "").startswith("Marks Table")
                        else sec.get("name", "Module-wise Performance")
                    )
                    st_title = st.text_input(
                        "Table Heading / Title (Displayed on PDF)",
                        value=default_title,
                        key=f"stitle_{idx}",
                        help="Enter the title heading that will appear above this marks table in the generated PDF report (e.g. 'Module-wise Performance', 'Theory Assessment', 'Practical Assessment')."
                    )
                else:
                    st_title = sec.get("title", sn)

                sc_default = [c for c in sec.get("columns",[]) if c in avail]
                sc_cols    = st.multiselect("Columns:", avail, default=sc_default, key=f"sc_{idx}")

                col_cfg = sec.get("col_cfg", {})
                if st_type == "marks_table" and sc_cols:
                    st.markdown("**Column Configuration (Marks Table):**")
                    new_col_cfg = {}
                    for col in sc_cols:
                        old = col_cfg.get(col, {})
                        
                        import re as _r
                        old_label = col.split("[")[-1].replace("]", "").strip() if "[" in col else col
                        old_label = _r.sub(r'\(?\s*out\s+of\s+[\d.]+\s*\)?', '', old_label, flags=_r.IGNORECASE).strip().strip("()")
                        
                        m = _r.search(r'out\s+of\s+([\d.]+)', col, _r.IGNORECASE)
                        max_from_header = int(float(m.group(1))) if m else 100
                        
                        ca, cb = st.columns([3, 1])
                        with ca:
                            lbl = st.text_input("Label", old.get("label", old_label), key=f"ml_{idx}_{col}")
                        with cb:
                            mx = st.number_input("Max Marks", 1, value=int(old.get("max") or max_from_header), key=f"mm_{idx}_{col}")
                        new_col_cfg[col] = {"label": lbl, "max": mx}
                    col_cfg = new_col_cfg

                updated = {"name": sn, "title": st_title, "type": st_type, "columns": sc_cols, "enabled": se, "col_cfg": col_cfg}
                st.session_state.sections[idx] = updated

        if del_idx is not None:
            st.session_state.sections.pop(del_idx); st.rerun()

        # ── MODULAR REPORT INSIGHTS & ACTION PLAN CONTROLS ─────────────────
        st.markdown("---")
        st.markdown("### 🧩 Additional Report Modules & Insights Controls")
        st.caption("Configure optional enterprise report panels to tailor the report layout for your specific use case.")

        col_m1, col_m2 = st.columns(2)
        with col_m1:
            st.markdown("##### 📈 Performance Overview Panel")
            st.session_state.show_perf = st.toggle("Include Performance Overview Card", value=st.session_state.show_perf, key="d_show_perf")
            st.session_state.marks_show_rank = st.toggle("Show Cohort Rank & Percentile", value=st.session_state.get("marks_show_rank", True), key="d_show_rank")
            st.session_state.marks_show_grade = st.toggle("Show Overall Grade Badge", value=st.session_state.get("marks_show_grade", True), key="d_show_grade")
            st.session_state.show_honors_certificate = st.toggle("Page 2 Honors Certificate (Grade A/A+)", value=st.session_state.get("show_honors_certificate", False), key="d_show_cert")

        with col_m2:
            st.markdown("##### 🎯 Personalised Action Plan")
            st.session_state.show_action_plan = st.toggle("Include Personalised Action Plan Section", value=st.session_state.get("show_action_plan", True), key="d_master_ap")
            if st.session_state.show_action_plan:
                st.session_state.show_tech_action = st.toggle("  └ Technical Improvement Focus", value=st.session_state.get("show_tech_action", True), key="d_tech_ap")
                st.session_state.show_comm_action = st.toggle("  └ Soft-Skills Improvement Focus", value=st.session_state.get("show_comm_action", True), key="d_comm_ap")
            else:
                st.session_state.show_tech_action = False
                st.session_state.show_comm_action = False
            st.session_state.marks_show_analysis = st.toggle("Subject Strength vs Focus Analysis", value=st.session_state.get("marks_show_analysis", True), key="d_show_ana")

        final_sections = [s for s in st.session_state.sections if s.get("enabled")]
        if final_sections or st.session_state.wt_enabled:
            wt_count  = 1 if st.session_state.wt_enabled and st.session_state.written_cfg else 0
            sec_count = len(final_sections)
            ap_active = "Active" if st.session_state.show_action_plan else "OFF (Disabled)"
            st.success(f"✅ Designer Status: **{wt_count} Written Test** + **{sec_count} Sections** | "
                       f"Performance Panel: **{'ON' if st.session_state.show_perf else 'OFF'}** | "
                       f"Action Plan: **{ap_active}** | "
                       f"PDF Names from: **{st.session_state.pdf_name_col}**")

        # ── LIVE PREVIEW ──────────────────────────────
        st.markdown("---")
        st.markdown("### 👁️ Live Report Preview")
        st.caption("Generate a real-time preview of the learner report card for the first record — no tab switching needed.")
        if st.button("👁️ Generate Live Preview", type="primary", key="btn_live_prev"):
            if st.session_state.df is None:
                st.warning("⚠️ Upload data in the **Data Source** tab first.")
            else:
                with st.spinner("Generating preview..."):
                    try:
                        import base64
                        _prev_df = st.session_state.df   

                        all_sections = []
                        if st.session_state.wt_enabled and st.session_state.written_cfg:
                            all_sections.append({
                                "name": st.session_state.written_name,
                                "type": "written_test",
                                "columns": st.session_state.written_cfg,
                                "enabled": True,
                            })
                        all_sections += st.session_state.sections

                        preview_cfg = {
                            "company_name":       company_name,
                            "report_title":       report_title_sb,
                            "primary_color":      primary_color,
                            "secondary_color":    secondary_color,
                            "accent_color":       accent_color,
                            "identity_columns":   st.session_state.identity_cols,
                            "sections":           all_sections,
                            "show_performance_summary": st.session_state.show_perf,
                            "enable_mock_interview": st.session_state.enable_mock_interview,
                            "intro_text":         st.session_state.intro_text,
                            "show_action_plan":   st.session_state.get("show_action_plan", True),
                            "show_tech_action":   st.session_state.show_tech_action,
                            "show_comm_action":   st.session_state.show_comm_action,
                            "show_honors_certificate": st.session_state.get("show_honors_certificate", False),
                            "marks_pass_threshold":       st.session_state.marks_pass_threshold,
                            "marks_tone":                 st.session_state.marks_tone,
                            "marks_module_context":       st.session_state.marks_module_context,
                            "marks_show_grade":           st.session_state.marks_show_grade,
                            "marks_show_recommendations": st.session_state.marks_show_recommendations,
                            "marks_show_analysis":        st.session_state.marks_show_analysis,
                            "marks_show_rank":            st.session_state.marks_show_rank,
                            "marks_benchmark_note":       st.session_state.marks_benchmark_note,
                            "marks_show_benchmark_note":  st.session_state.marks_show_benchmark_note,
                            "benchmark_shortlist":        st.session_state.benchmark_shortlist,
                            "benchmark_strong":           st.session_state.benchmark_strong,
                            "benchmark_revision":         st.session_state.benchmark_revision,
                            "footer_link_text":           st.session_state.footer_link_text,
                            "footer_link_url":            st.session_state.footer_link_url,
                            "footer_deadline_text":       st.session_state.footer_deadline_text,
                            "footer_mentor_text":         st.session_state.footer_mentor_text,
                            "show_watermark":             st.session_state.show_watermark,
                            "watermark_text":             st.session_state.watermark_text,
                            "legal_notice":               st.session_state.legal_notice,
                            # ── Table-wise Shortlisting & Theme Aesthetics ────────────
                            "mi_benchmark_table":         st.session_state.mi_benchmark_table,
                            "page_size":                  st.session_state.report_page_size,
                            "orientation":                st.session_state.report_orientation.lower(),
                            "margin_left":                st.session_state.report_margin_left,
                            "margin_right":               st.session_state.report_margin_right,
                            "intro_card_style":           {"Callout (Accent Left Border)": "callout", "Banner (Top Header Box)": "banner", "Clean (Minimal)": "clean"}.get(st.session_state.intro_card_style, "callout"),
                            "intro_bg_color":             st.session_state.intro_bg_color,
                            "intro_border_color":         st.session_state.intro_border_color if st.session_state.intro_border_color else primary_color,
                            "table_border_style":         {"Full Grid": "grid", "Horizontal Lines Only": "horizontal_only", "Boxed Border": "boxed", "Subtle Border": "subtle", "Minimal (No Grid)": "minimal"}.get(st.session_state.table_border_style, "grid"),
                            "table_border_color":         st.session_state.table_border_color,
                            "table_border_width":         st.session_state.table_border_width,
                            "_batch_df":    _prev_df,   # ✅ full pandas DataFrame
                        }

                        first_row = _prev_df.iloc[0]
                        gc = get_groq_client(api_key) if enable_ai else None
                        pdf_bytes = build_single_report(first_row, preview_cfg, gc, st.session_state.logo_bytes)

                        base64_pdf = base64.b64encode(pdf_bytes).decode("utf-8")
                        pdf_display = (
                            f'<iframe src="data:application/pdf;base64,{base64_pdf}" '
                            f'width="100%" height="900" type="application/pdf"></iframe>'
                        )
                        st.markdown(pdf_display, unsafe_allow_html=True)

                    except Exception as e:
                        import traceback
                        st.error(f"❌ Preview Error: {e}")
                        st.code(traceback.format_exc(), language="python")


# ══════════════════════════════════════════════════════════════
# TAB 3 — GENERATE
# ══════════════════════════════════════════════════════════════
with tab_gen:
    st.markdown("## ⚡ Generate Learner Reports")

    if st.session_state.df is None:
        st.info("📁 Upload learner data in the **📁 Learner Data** tab first.")
    else:
        gen_df   = st.session_state.df
        all_cols = gen_df.columns.tolist()

        all_sections = []
        if st.session_state.wt_enabled and st.session_state.written_cfg:
            all_sections.append({
                "name": st.session_state.written_name,
                "type": "written_test",
                "columns": st.session_state.written_cfg,
                "enabled": True,
            })
        all_sections += st.session_state.sections

        cfg = {
            "company_name":       company_name,
            "report_title":       report_title_sb,
            "primary_color":      primary_color,
            "secondary_color":    secondary_color,
            "accent_color":       accent_color,
            "identity_columns":   st.session_state.identity_cols,
            "sections":           all_sections,
            "show_performance_summary": st.session_state.show_perf,
            "enable_mock_interview": st.session_state.enable_mock_interview,
            "intro_text":         st.session_state.intro_text,
            "show_action_plan":   st.session_state.get("show_action_plan", True),
            "show_tech_action":   st.session_state.show_tech_action,
            "show_comm_action":   st.session_state.show_comm_action,
            "show_honors_certificate": st.session_state.get("show_honors_certificate", False),
            # ── Marks / EdTech report card settings ────────────────
            "marks_pass_threshold":       st.session_state.marks_pass_threshold,
            "marks_tone":                 st.session_state.marks_tone,
            "marks_module_context":       st.session_state.marks_module_context,
            "marks_show_grade":           st.session_state.marks_show_grade,
            "marks_show_recommendations": st.session_state.marks_show_recommendations,
            "marks_show_analysis":        st.session_state.marks_show_analysis,
            "marks_show_rank":            st.session_state.marks_show_rank,
            "marks_benchmark_note":       st.session_state.marks_benchmark_note,
            "marks_show_benchmark_note":  st.session_state.marks_show_benchmark_note,
            
            # ── Dynamic Benchmarks & Footer ────────────────────────
            "benchmark_shortlist":        st.session_state.benchmark_shortlist,
            "benchmark_strong":           st.session_state.benchmark_strong,
            "benchmark_revision":         st.session_state.benchmark_revision,
            "footer_link_text":           st.session_state.footer_link_text,
            "footer_link_url":            st.session_state.footer_link_url,
            "footer_deadline_text":       st.session_state.footer_deadline_text,
            "footer_mentor_text":         st.session_state.footer_mentor_text,
            "show_watermark":             st.session_state.show_watermark,
            "watermark_text":             st.session_state.watermark_text,
            "legal_notice":               st.session_state.legal_notice,
            # ── Table-wise Shortlisting & Theme Aesthetics ────────────
            "mi_benchmark_table":         st.session_state.mi_benchmark_table,
            "page_size":                  st.session_state.report_page_size,
            "orientation":                st.session_state.report_orientation.lower(),
            "margin_left":                st.session_state.report_margin_left,
            "margin_right":               st.session_state.report_margin_right,
            "intro_card_style":           {"Callout (Accent Left Border)": "callout", "Banner (Top Header Box)": "banner", "Clean (Minimal)": "clean"}.get(st.session_state.intro_card_style, "callout"),
            "intro_bg_color":             st.session_state.intro_bg_color,
            "intro_border_color":         st.session_state.intro_border_color if st.session_state.intro_border_color else primary_color,
            "table_border_style":         {"Full Grid": "grid", "Horizontal Lines Only": "horizontal_only", "Boxed Border": "boxed", "Subtle Border": "subtle", "Minimal (No Grid)": "minimal"}.get(st.session_state.table_border_style, "grid"),
            "table_border_color":         st.session_state.table_border_color,
            "table_border_width":         st.session_state.table_border_width,
            "_batch_df":    gen_df,   # full batch for rank/percentile
        }

        pdf_col  = st.session_state.pdf_name_col if st.session_state.pdf_name_col in all_cols else all_cols[0]
        # Prioritise exact "student name" match before partial-match fallback
        _name_candidates = [c for c in all_cols if c.lower() == "student name"] or \
                           [c for c in all_cols if "student" in c.lower() and "name" in c.lower()] or \
                           [c for c in all_cols if "name" in c.lower()]
        name_col = _name_candidates[0] if _name_candidates else all_cols[0]

        ic1, ic2, ic3 = st.columns(3)
        ic1.metric("Total Learners",  len(gen_df))
        ic2.metric("Report Sections",  len([s for s in all_sections if s.get("enabled",True)]))
        ic3.metric("AI Feedback",   "ON" if enable_ai else "OFF")

        enabled_names = " | ".join(
            s["name"] for s in all_sections if s.get("enabled",True)
        ) or "⚠️ None — configure in 🎨 Report Designer"
        st.info(f"**Sections:** {enabled_names}  |  **PDF names from:** `{pdf_col}`")

        if not all_sections:
            st.warning("⚠️ No sections configured. Visit 🎨 Report Designer first.")

        # ── PREVIEW ─────────────────────────────────────────
        with st.expander("👁️ Preview One Report", expanded=False):
            students = gen_df[name_col].astype(str).tolist()
            sel_s    = st.selectbox("Select student:", students, key="g_prev_sel")
            p_idx    = students.index(sel_s)

            if st.button("🔎 Generate Preview", key="g_prev_btn"):
                with st.spinner("Building preview PDF..."):
                    try:
                        gc = get_groq_client(api_key) if enable_ai else None
                        pb = build_single_report(gen_df.iloc[p_idx], cfg, gc, st.session_state.logo_bytes)
                        fname_val = str(gen_df.iloc[p_idx].get(pdf_col, sel_s))
                        st.success(f"✅ Preview ready for **{sel_s}**")
                        dl_prev = st.download_button("⬇️ Download Preview PDF", pb,
                                           f"{safe_filename(fname_val)}.pdf",
                                           "application/pdf", key="g_prev_dl")
                        if dl_prev:
                            sn, en, extra_notes = get_student_identity_vals(gen_df.iloc[p_idx], st.session_state.identity_cols, fallback_name=sel_s)
                            log_audit_event("Downloaded (Preview)", report_title_sb, sn, en, f"PREV_{safe_filename(fname_val)}")
                    except Exception as e:
                        st.error(f"❌ {e}")

        st.markdown("---")
        st.markdown(f"### 🚀 Generate All **{len(gen_df)}** Learner Report Cards")

        # ────────────────────────────────────────────────────────────────────
        # ── Mock Interview Eligibility Override Panel ─────────────────────
        # Cap at 50 rows to prevent UI freeze with large datasets
        _MI_OVERRIDE_LIMIT = 50
        if st.session_state.enable_mock_interview:
            with st.expander("🎯 Mock Interview Eligibility — Manual Override", expanded=False):
                if len(gen_df) > _MI_OVERRIDE_LIMIT:
                    st.warning(
                        f"⚠️ Manual override is disabled for large batches (**{len(gen_df)} students**). "
                        f"Eligibility will be **auto-computed** from each student’s score. "
                        f"To use overrides, filter your dataset to ≤{_MI_OVERRIDE_LIMIT} rows."
                    )
                else:
                    st.caption(
                        "By default, eligibility is **auto-computed** from each student's score vs. the "
                        f"shortlist threshold ({st.session_state.benchmark_shortlist}%). "
                        "Use the toggles below to **manually override** any student's eligibility before generating."
                    )

                    _mi_override_cols = st.columns([3, 2, 1])
                    _mi_override_cols[0].markdown("**Student**")
                    _mi_override_cols[1].markdown("**Auto-computed Eligibility**")
                    _mi_override_cols[2].markdown("**Override: Shortlisted?**")

                    _new_overrides = {}
                    for _oi, (_, _orow) in enumerate(gen_df.iterrows()):
                        _oname_raw = _orow.get(name_col, "")
                        _oname = str(_oname_raw).strip() if str(_oname_raw).lower() not in ("nan", "none", "") else f"Row_{_oi}"
                        _okey  = f"{_oi}__{_oname}"

                        _auto_label, _auto_short, _auto_score = compute_mock_interview_eligibility(
                            _orow, all_sections,
                            gen_df if cfg.get("enable_mock_interview", True) else None, cfg
                        )
                        _auto_chip = "✅ Shortlisted" if _auto_short else f"❌ {MOCK_LABEL_REVISION}"

                        _cur_override = st.session_state.mi_eligibility_overrides.get(_okey, _auto_short)

                        _rc1, _rc2, _rc3 = st.columns([3, 2, 1])
                        with _rc1:
                            st.markdown(f"`{_oname}`")
                        with _rc2:
                            st.markdown(
                                f"<span style='color:{'#16a34a' if _auto_short else '#dc2626'};font-size:0.82rem;'>"
                                f"{_auto_chip} ({_auto_score}%)</span>",
                                unsafe_allow_html=True
                            )
                        with _rc3:
                            _toggled = st.toggle(
                                "Shortlisted", value=_cur_override,
                                key=f"mi_override_{_oi}", label_visibility="collapsed",
                            )
                            _new_overrides[_okey] = _toggled

                    st.session_state.mi_eligibility_overrides = _new_overrides

                    _shortlisted_count = sum(1 for v in _new_overrides.values() if v)
                    _total_count = len(_new_overrides)
                    st.markdown(
                        f"<div style='margin-top:0.6rem;padding:0.5rem 1rem;"
                        f"background:#EFF6FF;border-radius:8px;border:1px solid #BFDBFE;'"
                        f"><b style='color:#1D4ED8;'>📊 Summary:</b> "
                        f"<b>{_shortlisted_count}</b> of <b>{_total_count}</b> students "
                        f"will be marked as <b>{MOCK_LABEL_SHORTLISTED}</b> for Mock Interview.</div>",
                        unsafe_allow_html=True
                    )

                    if st.button("🔄 Reset All to Auto-computed", key="btn_mi_reset_overrides", type="secondary"):
                        st.session_state.mi_eligibility_overrides = {}
                        st.rerun()

        # ── Duplicate detection (informational only — never blocks generation) ──
        is_dup, dup_record = check_duplicate(gen_df, report_title_sb)
        if is_dup:
            st.info(
                f"ℹ️ This dataset was previously generated on "
                f"`{dup_record['generated_at']}` for **{dup_record['student_count']}** students. "
                f"You can generate again freely — reports will be overwritten."
            )

        # ── PDF Preview (first student) ────────────────────────────────────
        with st.expander("👁️ Preview First Student PDF — Validate Layout Before Full Batch", expanded=False):
            st.caption(
                "Generates a single PDF for the **first student in the dataset** instantly. "
                "Use this to validate your section layout and branding before committing to the full batch."
            )
            if st.button("👁️ Generate Preview PDF", key="btn_preview_first", type="secondary"):
                try:
                    _prev_row = gen_df.iloc[0]
                    _prev_gc  = get_groq_client(api_key) if enable_ai else None
                    with st.spinner("Generating preview PDF..."):
                        _prev_bytes = build_single_report(_prev_row, cfg, _prev_gc, st.session_state.logo_bytes)
                    _prev_name = str(_prev_row.get(name_col, "Preview")).strip()
                    st.success(f"✅ Preview generated for **{_prev_name}**")
                    st.download_button(
                        label=f"⬇️ Download Preview — {_prev_name}.pdf",
                        data=_prev_bytes,
                        file_name=f"PREVIEW_{safe_filename(_prev_name)}.pdf",
                        mime="application/pdf",
                        key="btn_dl_preview_first",
                    )
                except Exception as _prev_e:
                    st.error(f"❌ Preview failed: {_prev_e}")
                    import traceback
                    st.code(traceback.format_exc())

        # Guard: require at least one enabled section before allowing generation
        _has_sections = bool(all_sections)
        go = st.button("🚀 Generate All Reports", key="g_gen_btn", type="primary",
                       disabled=not _has_sections)

        if go:
            gc       = get_groq_client(api_key) if enable_ai else None
            prog     = st.progress(0)
            status   = st.empty()
            live_tbl = st.empty()
            results  = []
            zip_buf  = io.BytesIO()

            # ── Helper to extract a field from a row by keyword ──────────────
            def _row_pick(r, *keywords):
                for kw in keywords:
                    for col in r.index:
                        if kw in str(col).lower():
                            v = str(r[col]).strip()
                            if v.lower() not in ("nan", "none", ""):
                                return v
                return "N/A"

            id_cols = st.session_state.identity_cols

            # ── Pre-compute metadata for every row (fast, no PDF generation) ──
            row_meta = []  # list of dicts with per-row identity/eligibility data
            rows_list = list(gen_df.iterrows())
            for ri, (_, row) in enumerate(rows_list):
                stuname = str(row.get(name_col, f"Row_{ri}")).strip()
                pdf_val = str(row.get(pdf_col,  f"report_{ri}")).strip()
                fname   = f"{safe_filename(pdf_val)}.pdf"
                grade_l, score = compute_grade(row, all_sections, [])
                _sname_a, _eid_a, _ = get_student_identity_vals(row, id_cols, fallback_name=stuname)
                _email_a  = _row_pick(row, "email", "mail")
                _batch_a  = _row_pick(row, "batch")
                _branch_a = _row_pick(row, "branch", "location", "centre", "center")
                _mi_label, _is_shortlisted, _mi_score = compute_mock_interview_eligibility(
                    row, all_sections,
                    gen_df if cfg.get("enable_mock_interview", True) else None, cfg
                )
                _okey_gen = f"{ri}__{stuname}"
                _overrides = st.session_state.get("mi_eligibility_overrides", {})
                if _okey_gen in _overrides:
                    _is_shortlisted = _overrides[_okey_gen]
                    _mi_label = MOCK_LABEL_SHORTLISTED if _is_shortlisted else MOCK_LABEL_REVISION
                _mi_schedule = (
                    "Scheduled" if (cfg.get("enable_mock_interview", True) and _is_shortlisted)
                    else "Not Scheduled"
                )
                row_meta.append({
                    "ri": ri, "row": row, "stuname": stuname, "fname": fname,
                    "grade_l": grade_l, "score": score,
                    "_sname_a": _sname_a, "_eid_a": _eid_a,
                    "_email_a": _email_a, "_batch_a": _batch_a, "_branch_a": _branch_a,
                    "_mi_label": _mi_label, "_is_shortlisted": _is_shortlisted,
                    "_mi_schedule": _mi_schedule,
                })

            # ── Parallel PDF generation ──────────────────────────────────────
            from concurrent.futures import ThreadPoolExecutor, as_completed

            _logo_bytes = st.session_state.get("logo_bytes", None)

            def _build_pdf(meta):
                """Worker: build one PDF, return (meta, bytes_or_none, error_or_none)."""
                try:
                    pdf_b = build_single_report(meta["row"], cfg, gc, _logo_bytes)
                    return meta, pdf_b, None
                except Exception as exc:
                    return meta, None, exc

            pdf_results = {}   # ri → (pdf_bytes | None, error | None)
            total = len(row_meta)

            with ThreadPoolExecutor(max_workers=MAX_BATCH_WORKERS) as executor:
                futures = {executor.submit(_build_pdf, m): m for m in row_meta}
                done_count = 0
                for future in as_completed(futures):
                    meta, pdf_b, err = future.result()
                    pdf_results[meta["ri"]] = (pdf_b, err)
                    done_count += 1
                    pct = int(done_count / total * 100)
                    prog.progress(pct)
                    status.markdown(f"⚡ Processing **{done_count}/{total}** reports — {pct}% complete")

            # ── Assemble ZIP in row order + collect results ──────────────────
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for meta in row_meta:
                    ri    = meta["ri"]
                    fname = meta["fname"]
                    pdf_b, err = pdf_results.get(ri, (None, Exception("Unknown error")))
                    t_elapsed = "parallel"

                    if pdf_b is not None:
                        zf.writestr(fname, pdf_b)
                        results.append({
                            "Status":"✅", "Student":meta["_sname_a"], "File":fname,
                            "Grade":meta["grade_l"], "Score":meta["score"], "Time(s)":t_elapsed,
                            "Result":"Success", "_pdf":pdf_b, "_fname":fname,
                            "_email":meta["_email_a"], "_batch":meta["_batch_a"],
                            "_branch":meta["_branch_a"], "_eid":meta["_eid_a"],
                            "_mi_label":meta["_mi_label"],
                            "_is_shortlisted":meta["_is_shortlisted"],
                            "Mock Interview":meta["_mi_schedule"],
                        })
                        log_audit_event(
                            "Generated (Batch)", report_title_sb,
                            meta["_sname_a"], meta["_eid_a"],
                            report_id=fname, status="Success",
                            email=meta["_email_a"], batch=meta["_batch_a"],
                            branch=meta["_branch_a"],
                            session_id=st.session_state.session_id,
                            **{"Mock Interview Eligibility": meta["_mi_label"],
                               "Mock Interview Scheduling": meta["_mi_schedule"]},
                        )
                    else:
                        err_str = str(err)[:200]
                        zf.writestr(f"ERROR_{safe_filename(meta['fname'])}.txt", str(err))
                        results.append({
                            "Status":"❌", "Student":meta["_sname_a"], "File":fname,
                            "Grade":"—", "Score":"—", "Time(s)":"—",
                            "Result":f"Failed: {err_str}", "_pdf":None, "_fname":None,
                            "_email":meta["_email_a"], "_batch":meta["_batch_a"],
                            "_branch":meta["_branch_a"], "_eid":meta["_eid_a"],
                            "_mi_label":meta["_mi_label"],
                            "_is_shortlisted":meta["_is_shortlisted"],
                            "Mock Interview":meta["_mi_schedule"],
                        })
                        log_audit_event(
                            "Generated (Batch)", report_title_sb,
                            meta["_sname_a"], meta["_eid_a"],
                            report_id=fname, status="Failed",
                            email=meta["_email_a"], batch=meta["_batch_a"],
                            branch=meta["_branch_a"],
                            session_id=st.session_state.session_id,
                            **{"Mock Interview Eligibility": meta["_mi_label"],
                               "Mock Interview Scheduling": "Not Scheduled (Generation Failed)"},
                        )

            # ── Live results table ───────────────────────────────────────────
            live_df = pd.DataFrame([{k: v for k, v in r.items() if not k.startswith("_")} for r in results])
            live_tbl.dataframe(live_df)

            zip_buf.seek(0)
            st.session_state.last_zip    = zip_buf.read()
            st.session_state.gen_results = results
            st.session_state.last_zip_ts = datetime.now().strftime("%Y%m%d_%H%M")
            st.session_state.force_regenerate = False   # reset flag after successful run
            prog.progress(100)
            status.empty()
            live_tbl.empty()

            ok  = sum(1 for r in results if r["Result"] == "Success")
            err = len(results) - ok
            # Record this generation in the history file
            record_generation(gen_df, report_title_sb, len(results), ok)
            st.success(f"🎉 Done! **{ok}/{len(results)}** learner report cards generated successfully."
                       + (f"{err} failed — see details below." if err else ""))
            if ok > 0:
                st.balloons()
            st.dataframe(pd.DataFrame([{k:v for k,v in r.items() if not k.startswith("_")} for r in results]))

            # ── Failed Reports Summary ────────────────────────────────────────
            failed_results = [r for r in results if r["Result"] != "Success"]
            if failed_results:
                with st.expander(f"❌ Failed Reports — {len(failed_results)} student(s) had errors", expanded=True):
                    st.caption(
                        "These PDFs could not be generated. Check the error messages below. "
                        "Common causes: missing score columns, invalid data types, or AI timeout."
                    )
                    fail_df = pd.DataFrame([{
                        "Student":   r["Student"],
                        "File":      r["File"],
                        "Error":     r["Result"].replace("Failed: ", "")[:300],
                    } for r in failed_results])
                    st.dataframe(fail_df, width='stretch')
                    st.warning(
                        f"💡 **Tip:** Fix the data for these {len(failed_results)} student(s) "
                        "and re-run with 'Force Regenerate' checked — existing successful reports "
                        "will be preserved in the ZIP."
                    )


        if st.session_state.last_zip:
            ts_dl = st.session_state.last_zip_ts or datetime.now().strftime("%Y%m%d_%H%M")
            c_dl_zip, c_email_nav = st.columns([1, 1])
            with c_dl_zip:
                dl_zip = st.download_button(
                    f"📥 Download All Reports ZIP — {company_name} ({ts_dl})",
                    data=st.session_state.last_zip,
                    file_name=f"{company_name.replace(' ','_')}_Reports_{ts_dl}.zip",
                    mime="application/zip",
                    key="g_dl_zip",
                    width='stretch',
                )
            with c_email_nav:
                st.page_link(
                    "pages/2_Email_Dispatcher.py",
                    label="📧 Send via Email Dispatcher",
                    icon="📧",
                    width='stretch',
                )
            if dl_zip:
                # Log one download row per student (not one aggregate row)
                for _r in st.session_state.gen_results:
                    if _r.get("Result") == "Success":
                        log_audit_event(
                            "Downloaded (Batch ZIP)", report_title_sb,
                            _r.get("Student", "N/A"), _r.get("_eid", "N/A"),
                            report_id=_r.get("_fname", "N/A"),
                            status="Downloaded",
                            email=_r.get("_email", "N/A"),
                            batch=_r.get("_batch", "N/A"),
                            branch=_r.get("_branch", "N/A"),
                            session_id=st.session_state.session_id,
                        )

            audit_bytes = get_audit_log_bytes()
            if audit_bytes:
                st.download_button(
                    "📥 Download Audit Logs CSV",
                    data=audit_bytes,
                    file_name=f"Audit_Logs_{report_title_sb.replace(' ','_')}_{ts_dl}.csv",
                    mime="text/csv",
                    key="g_dl_audit",
                )

            # Dynamically logged mock interview activations automatically inside batch loop - no manual button needed!
            pass

        # ── Generation History expander ──────────────────────────
        history = get_all_history()
        if history:
            with st.expander(f"🗓️ Generation History — {len(history)} run(s)", expanded=False):
                hist_df = pd.DataFrame(history)[["generated_at","report_title","student_count","ok_count","hash"]]
                hist_df.columns = ["Generated At", "Report Title", "Students", "Success", "Batch Hash"]
                st.dataframe(hist_df, width='stretch')
                if st.button("🗑️ Clear Generation History", key="btn_clr_hist"):
                    import os as _os
                    if _os.path.exists("generation_history.json"):
                        _os.remove("generation_history.json")
                    st.success("History cleared.")
                    st.rerun()


# ══════════════════════════════════════════════════════════════
# TAB 4 — COHORT ANALYTICS
# ══════════════════════════════════════════════════════════════
with tab_cohort:
    st.markdown("## 📈 Cohort Analytics Dashboard")

    if st.session_state.df is None:
        st.info("📁 Upload learner data in the **📁 Learner Data** tab first.")
    else:
        cohort_df = st.session_state.df.copy()
        
        # ── Step 1: Detect primary column mappings ───────────────────
        cols = cohort_df.columns.tolist()
        
        # Dynamic search for Student Name column
        name_candidates = [c for c in cols if c.lower() == "student name"] or \
                           [c for c in cols if "student" in c.lower() and "name" in c.lower()] or \
                           [c for c in cols if "name" in c.lower()]
        name_col = name_candidates[0] if name_candidates else cols[0]

        # Dynamic search for Batch / Cohort column
        batch_candidates = [c for c in cols if c.lower() in ("batch", "cohort", "batch no", "batch no.")] or \
                            [c for c in cols if "batch" in c.lower() or "cohort" in c.lower()]
        batch_col = batch_candidates[0] if batch_candidates else (st.session_state.identity_cols[2] if len(st.session_state.identity_cols) > 2 and st.session_state.identity_cols[2] in cols else None)

        # ── Step 2: Compute overall metrics for each student ─────────
        all_sections = []
        if st.session_state.wt_enabled and st.session_state.written_cfg:
            all_sections.append({
                "name": st.session_state.written_name,
                "type": "written_test",
                "columns": st.session_state.written_cfg,
                "enabled": True,
            })
        all_sections += st.session_state.sections

        computed_list = []
        for ri, row in cohort_df.iterrows():
            # Standard rating averages
            scores = []
            for sec in all_sections:
                if sec.get("type") != "rating_table":
                    continue
                for col in sec.get("columns", []):
                    if col in row.index:
                        scores.append(to_numeric_rating(str(row[col])))
            
            # Marks scores — BUG-16 FIX: use weighted total/max to match engine's
            # _compute_class_stats, which uses (total_scored / total_max) not per-column avg.
            marks_total_scored = 0.0
            marks_total_max    = 0.0
            for sec in all_sections:
                if sec.get("type") != "marks_table":
                    continue
                for col in sec.get("columns", []):
                    if col in row.index:
                        try:
                            val = float(row[col])
                            m = re.search(r'out\s+of\s+([\d.]+)', col, re.IGNORECASE)
                            max_s = float(m.group(1)) if m else 100.0
                            marks_total_scored += val
                            marks_total_max    += max_s
                        except Exception:
                            pass
            
            # Unified performance percentage (0 to 100)
            if marks_total_max > 0:
                overall_pct = (marks_total_scored / marks_total_max) * 100
            elif scores:
                overall_pct = (sum(scores) / len(scores)) / 4.0 * 100
            else:
                overall_pct = 0.0

            # Grade & Verdict
            def get_grade_and_verdict(sc_pct):
                sc = sc_pct / 25.0  # map 0-100 to 0-4
                if sc >= 3.5: return "A+", "Pass — Outstanding", "#059669"
                if sc >= 3.0: return "A",  "Pass — Proficient", "#16a34a"
                if sc >= 2.5: return "B",  "Average — Needs Effort", "#d97706"
                if sc >= 1.5: return "C",  "Poor — Serious Gaps", "#ea580c"
                return               "D",  "Critical Gap — Action Now", "#dc2626"
            
            grade_val, verdict_val, color_val = get_grade_and_verdict(overall_pct)
            
            computed_list.append({
                "Student": str(row.get(name_col, f"Learner_{ri}")),
                "Batch": str(row.get(batch_col, "Standard Cohort")) if batch_col else "Standard Cohort",
                "Score (%)": round(overall_pct, 1),
                "Grade": grade_val,
                "Verdict": verdict_val,
                "Color": color_val,
                "RowIndex": ri
            })
        
        comp_df = pd.DataFrame(computed_list)

        # ── Step 3: Interactive Dashboard Filters ────────────────────
        st.markdown("### 🔍 Interactive Filters")
        
        fc1, fc2, fc3 = st.columns([1.5, 1.5, 2])
        
        with fc1:
            batches = ["All Batches"] + sorted(comp_df["Batch"].unique().tolist())
            sel_batch = st.selectbox("Cohort / Batch Group", batches, index=0, key="co_sel_batch")
            
        with fc2:
            grade_tiers = ["All Grades", "Mastery (A/A+)", "Passing (B)", "Focus Required (C/D)"]
            sel_tier = st.selectbox("Performance Tier", grade_tiers, index=0, key="co_sel_tier")
            
        with fc3:
            search_query = st.text_input("🔍 Search Candidate Name", placeholder="Type name to filter...", key="co_search")

        # Apply filters
        filtered_df = comp_df.copy()
        if sel_batch != "All Batches":
            filtered_df = filtered_df[filtered_df["Batch"] == sel_batch]
            
        if sel_tier == "Mastery (A/A+)":
            filtered_df = filtered_df[filtered_df["Grade"].isin(["A+", "A"])]
        elif sel_tier == "Passing (B)":
            filtered_df = filtered_df[filtered_df["Grade"] == "B"]
        elif sel_tier == "Focus Required (C/D)":
            filtered_df = filtered_df[filtered_df["Grade"].isin(["C", "D"])]
            
        if search_query.strip():
            filtered_df = filtered_df[filtered_df["Student"].str.lower().str.contains(search_query.lower())]

        # ── Step 4: Metric Overview Cards ────────────────────────────
        st.markdown("<br/>", unsafe_allow_html=True)
        
        total_students = len(filtered_df)
        if total_students > 0:
            avg_score = round(filtered_df["Score (%)"].mean(), 1)
            pass_threshold = st.session_state.marks_pass_threshold
            passing_students = len(filtered_df[filtered_df["Score (%)"] >= pass_threshold])
            pass_rate = round((passing_students / total_students) * 100, 1)
            mastery_students = len(filtered_df[filtered_df["Grade"].isin(["A+", "A"])])
            mastery_rate = round((mastery_students / total_students) * 100, 1)
        else:
            avg_score, pass_rate, mastery_rate = 0.0, 0.0, 0.0

        st.markdown(f"""
        <div class="stat-grid">
          <div class="stat-card">
            <div class="val">{total_students}</div>
            <div class="lbl">Filtered Cohort Size</div>
          </div>
          <div class="stat-card">
            <div class="val">{avg_score}%</div>
            <div class="lbl">Average Performance</div>
          </div>
          <div class="stat-card">
            <div class="val" style="color:#059669">{pass_rate}%</div>
            <div class="lbl">Passing Rate (Threshold: {st.session_state.marks_pass_threshold}%)</div>
          </div>
          <div class="stat-card">
            <div class="val" style="color:#2563EB">{mastery_rate}%</div>
            <div class="lbl">Mastery Level (Grade A/A+)</div>
          </div>
        </div>
        """, unsafe_allow_html=True)
        st.markdown("<br/>", unsafe_allow_html=True)

        if total_students == 0:
            st.warning("⚠️ No students match the active filter criteria. Try resetting the filters.")
        else:
            # ── Step 5: Cohort Dashboard Visual Charts Grid ──────────────
            cc1, cc2 = st.columns(2)
            
            with cc1:
                st.markdown("#### 📊 Grade Distribution")
                grade_counts = filtered_df["Grade"].value_counts().reindex(["A+", "A", "B", "C", "D"]).fillna(0)
                st.bar_chart(grade_counts, color="#2563EB")
                
            with cc2:
                st.markdown("#### 📈 Average Ratings / Marks per Topic")
                # BUG-09 FIX: initialize topic_averages before the inner block so it is always
                # defined when the AI analysis button references it below (even if 0 students match).
                topic_averages = {}
                
                for sec in all_sections:
                    if sec.get("type") in ("rating_table", "marks_table"):
                        for col in sec.get("columns", []):
                            if col in cohort_df.columns:
                                # BUG-08 FIX: use .loc with the actual DataFrame index instead of
                                # .iloc with RowIndex positional values — prevents IndexError when
                                # the source DataFrame has a non-default integer index.
                                student_indices = filtered_df["RowIndex"].tolist()
                                data_slice = cohort_df.loc[
                                    cohort_df.index.intersection(student_indices), col
                                ]
                                
                                if sec.get("type") == "rating_table":
                                    numeric_vals = data_slice.apply(lambda x: to_numeric_rating(str(x)))
                                    mean_val = (numeric_vals.mean() / 4.0) * 100 if len(numeric_vals) > 0 else 0
                                else:
                                    numeric_vals = pd.to_numeric(data_slice, errors="coerce").fillna(0)
                                    m = re.search(r'out\s+of\s+([\d.]+)', col, re.IGNORECASE)
                                    max_s = float(m.group(1)) if m else 100.0
                                    mean_val = (numeric_vals.mean() / max_s) * 100 if max_s > 0 else 0
                                
                                label = clean_label(col)
                                topic_averages[label] = round(mean_val, 1)
                
                if topic_averages:
                    topic_df = pd.Series(topic_averages)
                    st.bar_chart(topic_df, color="#16A34A")
                else:
                    st.caption("No topics or assessment columns configured yet in Report Designer.")

            # ── Step 6: Cohort Student Roster ────────────────────────────
            st.markdown("---")
            st.markdown("### 📋 Student Cohort Performance Roster")
            
            display_cols = ["Student", "Batch", "Score (%)", "Grade", "Verdict"]
            st.dataframe(filtered_df[display_cols], width='stretch', height=280)

            # ── Step 7: AI Batch Reviewer & Mentorship Insights ───────────
            st.markdown("---")
            st.markdown("### 🤖 AI Cohort Mentorship Insights")
            st.caption("Generate an aggregated mentor summary analyzing the entire batch to identify systemic strengths, skill gaps, and recommendations.")
            
            enable_ai = st.session_state.get("sb_ai", False)
            if not enable_ai:
                st.info("💡 Enable the **Groq AI Feedback** engine in the sidebar and enter your API key to generate cohort takeaways.")
            else:
                if st.button("🤖 Generate Cohort AI Takeaways", key="btn_cohort_ai", type="primary"):
                    with st.spinner("AI is analyzing cohort performance..."):
                        try:
                            cohort_summary_text = f"Batch Group: {sel_batch}\nTotal Students: {total_students}\nAverage Score: {avg_score}%\nPassing Rate: {pass_rate}%\n"
                            
                            top_performers = filtered_df.sort_values(by="Score (%)", ascending=False).head(3)
                            bottom_performers = filtered_df.sort_values(by="Score (%)", ascending=True).head(3)
                            
                            cohort_summary_text += "\nTop Performing Candidates:\n"
                            for _, r in top_performers.iterrows():
                                cohort_summary_text += f"- {r['Student']}: {r['Score (%)']}% (Grade {r['Grade']})\n"
                                
                            cohort_summary_text += "\nNeeds Mentorship Candidates:\n"
                            for _, r in bottom_performers.iterrows():
                                cohort_summary_text += f"- {r['Student']}: {r['Score (%)']}% (Grade {r['Grade']})\n"

                            cohort_summary_text += "\nModule Average Mastery Levels:\n"
                            for topic, avg in topic_averages.items():
                                cohort_summary_text += f"- {topic}: {avg}% mastery\n"
                                
                            prompt = f"""
                            You are a senior training head and expert EdTech mentor.
                            Analyze the following cohort performance summary data:
                            
                            {cohort_summary_text}
                            
                            Please write a structured Batch Evaluation Report with exactly the following three sections using clear, professional, and encouraging language:
                            
                            ### 🏆 Cohort Highlights & Strengths
                            - Detail 2-3 areas where the batch is excelling, citing specific modules.
                            
                            ### ⚠️ Systemic Foundational Gaps
                            - Highlight 1-2 key topics where student performance falls behind, suggesting potential teaching adjustments.
                            
                            ### 🎯 Actionable Class-wide Mentorship Recommendations
                            - Provide a list of practical steps (e.g. review bootcamps, structured code labs, communication mock rounds) to get all students past the passing threshold of {pass_threshold}%.
                            """
                            
                            client = get_groq_client(st.session_state.get("sb_apikey", ""))
                            if client:
                                model_name = st.session_state.get("sb_model", "llama-3.3-70b-versatile")
                                response = client.chat.completions.create(
                                    messages=[{"role": "user", "content": prompt}],
                                    model=model_name,
                                    temperature=0.4,
                                )
                                st.markdown("#### 📝 Cohort Mentor Analysis")
                                st.markdown(response.choices[0].message.content)
                            else:
                                st.error("Groq API client is not configured correctly.")
                        except Exception as e:
                            st.error(f"Failed to generate Cohort AI Takeaways: {e}")


# ══════════════════════════════════════════════════════════════
# TAB 5 — HISTORY
# ══════════════════════════════════════════════════════════════
with tab_hist:
    st.markdown("## 🗂️ Results & Generation History")

    results = st.session_state.gen_results
    if not results:
        st.info("No learner reports generated yet. Use the **⚡ Generate Reports** tab to get started.")
    else:
        ok_c  = sum(1 for r in results if r["Result"] == "Success")
        err_c = len(results) - ok_c

        st.markdown(f"""
        <div class="stat-grid">
          <div class="stat-card"><div class="val">{len(results)}</div><div class="lbl">Reports Generated</div></div>
          <div class="stat-card"><div class="val" style="color:#059669">{ok_c}</div><div class="lbl">Successful</div></div>
          <div class="stat-card"><div class="val" style="color:#dc2626">{err_c}</div><div class="lbl">Failed</div></div>
        </div>
        """, unsafe_allow_html=True)

        sq = st.text_input("🔍 Search by learner name or report file:", placeholder="Type to filter...", key="h_search")
        filtered = [r for r in results
                    if not sq.strip() or sq.lower() in r["Student"].lower() or sq.lower() in r["File"].lower()]
        st.markdown(f"**{len(filtered)}** records")

        if filtered:
            tbl = pd.DataFrame([{k:v for k,v in r.items() if not k.startswith("_")} for r in filtered])
            st.dataframe(tbl, height=280)

            st.markdown("### 📥 Download Individual Learner Reports")
            for ri, r in enumerate(filtered):
                if r.get("_pdf") is not None:
                    ha, hb = st.columns([5, 2])
                    with ha:
                        badge = (f'<span class="badge-ok">✅ {r["Grade"]} ({r["Score"]})</span>'
                                 if r["Result"]=="Success"
                                 else '<span class="badge-err">❌ Failed</span>')
                        st.markdown(f"**{r['Student']}** — `{r['File']}` — {badge}", unsafe_allow_html=True)
                    with hb:
                        dl_hist = st.download_button("⬇️ PDF", data=r["_pdf"],
                                           file_name=r["_fname"], mime="application/pdf",
                                           key=f"h_dl_{ri}")
                        if dl_hist:
                            log_audit_event("Downloaded (History)", report_title_sb, r["Student"], "N/A", r["_fname"])

            grades = [r["Grade"] for r in results if r["Grade"] not in ("—","N/A") and r["Result"]=="Success"]
            if grades:
                st.markdown("---")
                st.markdown("### 📊 Cohort Grade Distribution")
                st.bar_chart(pd.Series(grades).value_counts())

            st.markdown("---")
            st.download_button(
                "📋 Export Results CSV",
                tbl.to_csv(index=False).encode("utf-8"),
                f"results_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                "text/csv", key="h_export_csv",
            )
        else:
            st.warning("No records match your search.")


# ══════════════════════════════════════════════════════════════
# TAB 6 — AUDIT LOG & ACCOUNTABILITY
# ══════════════════════════════════════════════════════════════
with tab_audit:
    st.markdown("## 🔍 Audit & Accountability Dashboard")
    st.caption("Live security logs, data download accountability tracker, and mock interview status pipeline.")

    # Read audit DataFrame — deduplicated to suppress repeated Generated/Downloaded rows
    audit_master_df = get_deduplicated_audit_dataframe()


    # ── Source File Filtering — Refresh Logs on Upload ─────────────────────
    curr_file = st.session_state.get("source_file", "N/A")
    
    col_hdr1, col_hdr2, col_hdr3 = st.columns([2, 1, 1])
    with col_hdr1:
        st.markdown(f"📂 Current Cohort Dataset: **{curr_file}**")
    with col_hdr2:
        show_all = st.checkbox("Show all historical logs", value=False, key="audit_show_all_logs")
    with col_hdr3:
        if st.button("🗑️ Clear Audit History", key="audit_top_clear_btn", type="secondary", help="Permanently delete all historical logs to start fresh with a clean slate!"):
            clear_audit_log()
            st.success("✅ Audit logs successfully cleared!")
            st.rerun()
        
    # BUG-06 FIX: "Source File" is now written to the audit CSV (was blocked before).
    # Filter by that column when a dataset is loaded, so the audit view is scoped
    # to events related to the current file only.
    if not show_all and curr_file != "N/A":
        if "Source File" in audit_master_df.columns:
            audit_df = audit_master_df[audit_master_df["Source File"] == curr_file]
        else:
            # Legacy logs without Source File column — show all
            audit_df = audit_master_df
    else:
        audit_df = audit_master_df

    if audit_df.empty:
        st.info("💡 **No active records found for the currently active dataset.** Generate reports first, or check 'Show all historical logs' above to view past logs.")
    else:
        # 1. Summary metric cards
        total_logs = len(audit_df)
        
        # Exclude N/A for unique student count
        valid_students = audit_df[audit_df["Student Name"] != "N/A"]
        
        # Determine unique students robustly
        all_unique_names = set()
        for idx, r in valid_students.iterrows():
            sn = str(r["Student Name"]).strip()
            ei = str(r["Enrollment ID"]).strip()
            if sn != "N/A" and not (sn.isdigit() or re.match(r'^\d+(\.\d+)?[eE]\+?\d+$', sn)):
                all_unique_names.add(sn)
            if ei != "N/A" and not (ei.isdigit() or re.match(r'^\d+(\.\d+)?[eE]\+?\d+$', ei)):
                all_unique_names.add(ei)
        if not all_unique_names:
            all_unique_names = set(valid_students["Student Name"].unique())
            if "N/A" in all_unique_names:
                all_unique_names.remove("N/A")
                
        unique_students = len(all_unique_names)
        
        generated_count = len(audit_df[audit_df["Action"].str.contains("Generated", na=False)])
        downloaded_count = len(audit_df[audit_df["Action"].str.contains("Downloaded", na=False)])
        
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.metric("Total Events Logged", f"{total_logs}")
        with c2:
            st.metric("Unique Students Tracked", f"{unique_students}")
        with c3:
            st.metric("Reports Generated", f"{generated_count}")
        with c4:
            st.metric("Reports Downloaded", f"{downloaded_count}")

        st.markdown("---")

        # 2. Charts section (Timeline and Action Breakdown)
        ch_col1, ch_col2 = st.columns(2)
        
        with ch_col1:
            st.markdown("### 📅 Daily Activity Timeline")
            try:
                # Group by day and count events
                chart_df = audit_df.copy()
                chart_df["Date"] = pd.to_datetime(chart_df["Timestamp"]).dt.date
                timeline_data = chart_df.groupby(["Date", "Action"]).size().unstack(fill_value=0)
                st.bar_chart(timeline_data, width='stretch')
            except Exception as e:
                st.caption(f"Waiting for more data to generate timeline: {e}")
                
        with ch_col2:
            st.markdown("### 📊 Event Breakdown")
            try:
                action_counts = audit_df["Action"].value_counts()
                st.bar_chart(action_counts, color="#8b5cf6", width='stretch')
            except Exception as e:
                st.caption(f"Waiting for more data: {e}")

        st.markdown("---")

        # 3. Delivery Status Table (Alice & Riya Pipeline style)
        if not valid_students.empty:
            st.markdown("### 📋 Student Delivery Status Pipeline")
            st.caption("Student-by-student overview tracking progress from generation to interview booking status.")
            
            pipeline_data = []
            
            # Smart deduplication: Group and associate all events belonging to the student name
            for name in sorted(all_unique_names):
                student_events = valid_students[
                    (valid_students["Student Name"] == name) |
                    (valid_students["Enrollment ID"] == name)
                ]
                
                # Determine the best numeric/scientific Enrollment ID for this student
                best_eid = "N/A"
                for idx, r in student_events.iterrows():
                    for col in ["Student Name", "Enrollment ID"]:
                        val = str(r[col]).strip()
                        if val != "N/A" and val != name:
                            if val.isdigit() or re.match(r'^\d+(\.\d+)?[eE]\+?\d+$', val):
                                best_eid = val
                                break
                    if best_eid != "N/A":
                        break
                        
                if best_eid == "N/A":
                    # Fallback to any non-name and non-N/A value
                    for idx, r in student_events.iterrows():
                        for col in ["Student Name", "Enrollment ID"]:
                            val = str(r[col]).strip()
                            if val != "N/A" and val != name:
                                best_eid = val
                                break
                        if best_eid != "N/A":
                            break
                            
                # Sort student events by Timestamp
                group_sorted = student_events.sort_values(by="Timestamp")
                
                gen_events = group_sorted[group_sorted["Action"].str.contains("Generated", na=False)]
                dl_events  = group_sorted[group_sorted["Action"].str.contains("Downloaded", na=False)]
                # Manual booking-form updates take highest priority
                mi_update_events = group_sorted[
                    group_sorted["Action"] == "Mock Interview Status Update"
                ]

                # Report generation status
                if not gen_events.empty:
                    last_gen = gen_events.iloc[-1]
                    gen_status = "✅ Generated" if last_gen["Status"] == "Success" else "❌ Failed"
                else:
                    gen_status = "⏳ Not Yet"

                dl_status = "✅ Downloaded" if not dl_events.empty else "⏳ Not Yet"

                # Mock Interview eligibility & scheduling status
                mi_eligibility = "Unknown"
                mi_status = "Unknown"
                # Priority 1: a manual booking form update always overrides computed status
                if not mi_update_events.empty:
                    last_mi = mi_update_events.iloc[-1]
                    mi_eligibility = MOCK_LABEL_SHORTLISTED
                    mi_status = f"✅ {last_mi['Status']}"

                # Priority 2: read stored columns written by batch generation
                elif not gen_events.empty and "Mock Interview Eligibility" in gen_events.columns:
                    last_gen_row = gen_events.iloc[-1]
                    stored_elg = str(last_gen_row.get("Mock Interview Eligibility", "")).strip()
                    mi_eligibility = stored_elg if stored_elg not in ("", "N/A", "nan") else "Unknown"

                    if not st.session_state.get("enable_mock_interview", True):
                        mi_status = "🚫 Feature Disabled"
                    elif mi_eligibility == MOCK_LABEL_SHORTLISTED:
                        mi_status = "✅ Scheduled"
                    else:
                        mi_status = f"❌ Not Scheduled ({MOCK_LABEL_REVISION})"

                # Priority 3: legacy logs without stored column
                else:
                    mi_eligibility = "Unknown (regenerate to compute)"
                    if not st.session_state.get("enable_mock_interview", True):
                        mi_status = "🚫 Feature Disabled"
                    elif gen_status.startswith("✅"):
                        mi_status = "⏳ Pending (regenerate reports to compute)"
                if group_sorted.empty:
                    last_act = "N/A"
                else:
                    last_act = group_sorted.iloc[-1]["Timestamp"]

                pipeline_data.append({
                    "Student":                    name,
                    "Enrollment ID":              best_eid,
                    "Report Status":              gen_status,
                    "Download Status":            dl_status,
                    "Mock Interview Eligibility": mi_eligibility,
                    "Interview Scheduling":       mi_status,
                    "Last Activity":              last_act,
                })
                
            pipeline_df = pd.DataFrame(pipeline_data)
            
            # Allow searching pipeline
            p_search = st.text_input("🔍 Filter Delivery Pipeline:", placeholder="Search by student name or enrollment ID...", key="pipeline_search")
            if p_search.strip():
                p_query = p_search.lower()
                pipeline_df = pipeline_df[
                    pipeline_df["Student"].str.lower().str.contains(p_query, na=False) |
                    pipeline_df["Enrollment ID"].str.lower().str.contains(p_query, na=False)
                ]
                
            st.dataframe(pipeline_df, width='stretch', height=280)
            
            # Export Delivery Status (strip emojis to prevent Windows/Excel encoding corruption)
            clean_export_df = pipeline_df.copy()
            for col in ["Report Status", "Download Status", "Mock Interview Eligibility", "Interview Scheduling"]:
                if col in clean_export_df.columns:
                    clean_export_df[col] = (
                        clean_export_df[col]
                        .astype(str)
                        .str.replace("✅ ", "", regex=False)
                        .str.replace("❌ ", "", regex=False)
                        .str.replace("⏳ ", "", regex=False)
                        .str.replace("🚫 ", "", regex=False)
                    )

            
            st.download_button(
                "📋 Export Delivery Status Report CSV",
                clean_export_df.to_csv(index=False).encode("utf-8"),
                f"delivery_status_report_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                "text/csv",
                key="dl_delivery_report",
            )
            
            st.markdown("---")
            
            # Manage and confirm bookings
            st.markdown("### 📅 Confirm & Manage Mock Interview Bookings")
            st.caption("Confirm bookings received from students or mark completed mock rounds to maintain precise accountability.")
            
            student_options = sorted(pipeline_df["Student"].unique().tolist()) if not pipeline_df.empty else []
            
            if not student_options:
                st.info("ℹ️ **No active candidates in the pipeline yet.** Generate reports first to update mock interview bookings.")
            else:
                with st.form("mi_update_form", clear_on_submit=True):
                    u_col1, u_col2 = st.columns(2)
                    with u_col1:
                        sel_student = st.selectbox("Select Student", student_options, key="mi_sel_student")
                    with u_col2:
                        sel_mi_status = st.selectbox(
                            "New Booking Status",
                            ["Scheduled", "Completed", "No Show / Cancelled", "Link Sent (Pending booking)"],
                            key="mi_sel_status"
                        )
                        
                    submit_update = st.form_submit_button("💾 Save Status Update", type="primary")
                    
                if submit_update:
                    st_row = pipeline_df[pipeline_df["Student"] == sel_student]
                    st_eid = st_row.iloc[0]["Enrollment ID"] if not st_row.empty else "N/A"
                    
                    # Fetch supplementary details from last known log of this student
                    st_original = valid_students[valid_students["Student Name"] == sel_student]
                    st_email = st_original.iloc[-1]["Email"] if not st_original.empty else "N/A"
                    st_batch = st_original.iloc[-1]["Batch"] if not st_original.empty else "N/A"
                    st_branch = st_original.iloc[-1]["Branch"] if not st_original.empty else "N/A"
                    st_rep_title = st_original.iloc[-1]["Report Title"] if not st_original.empty else "Technical Evaluation Report"
                    st_rep_id = st_original.iloc[-1]["Report ID"] if not st_original.empty else "N/A"
                    
                    log_audit_event(
                        action="Mock Interview Status Update",
                        report_title=st_rep_title,
                        student_name=sel_student,
                        enrollment_id=st_eid,
                        report_id=st_rep_id,
                        status=sel_mi_status,
                        email=st_email,
                        batch=st_batch,
                        branch=st_branch,
                        session_id=st.session_state.session_id,
                    )
                    st.success(f"🎉 Booking status successfully updated to **{sel_mi_status}** for student **{sel_student}**!")
                    st.rerun()
            
            st.markdown("---")

        # 4. Searchable Raw Audit Log
        st.markdown("### 🗄️ Full Searchable Audit Logs")
        
        # Filters row
        f_c1, f_c2, f_c3 = st.columns([1, 1, 2])
        with f_c1:
            actions_list = ["All"] + sorted(audit_df["Action"].dropna().unique().tolist())
            sel_action = st.selectbox("Action Filter", actions_list, key="audit_filter_action")
        with f_c2:
            status_list = ["All"] + sorted(audit_df["Status"].dropna().unique().tolist())
            sel_status = st.selectbox("Status Filter", status_list, key="audit_filter_status")
        with f_c3:
            search_query = st.text_input("Search Logs", placeholder="Search Student, Batch, Branch, or Log ID...", key="audit_search_query")
            
        # Apply filters
        filtered_df = audit_df.copy()
        if sel_action != "All":
            filtered_df = filtered_df[filtered_df["Action"] == sel_action]
        if sel_status != "All":
            filtered_df = filtered_df[filtered_df["Status"] == sel_status]
        if search_query.strip():
            sq = search_query.lower()
            conditions = []
            for col in ["Student Name", "Enrollment ID", "Batch", "Branch", "Log ID", "Action", "Status", "Notes", "Source File", "Delivery Channel"]:
                if col in filtered_df.columns:
                    conditions.append(filtered_df[col].astype(str).str.lower().str.contains(sq, na=False))
            if conditions:
                import functools
                import operator
                filtered_df = filtered_df[functools.reduce(operator.or_, conditions)]
            
        st.markdown(f"Displaying **{len(filtered_df)}** log entries")
        
        # Column selection (fully dynamic!)
        all_avail_cols = audit_df.columns.tolist()
        default_show = [c for c in ["Timestamp", "Action", "Status", "Student Name", "Enrollment ID", "Batch", "Branch"] if c in all_avail_cols]
        if not default_show:
            default_show = all_avail_cols[:7]
            
        show_cols = st.multiselect(
            "Select Columns to Display",
            options=all_avail_cols,
            default=default_show,
            key="audit_cols_select"
        )
        if not show_cols:
            show_cols = [c for c in ["Timestamp", "Action", "Status", "Student Name"] if c in all_avail_cols] or all_avail_cols[:3]
            
        st.dataframe(filtered_df[show_cols], width='stretch', height=350)
        
        # 5. Export and Clear logs buttons
        ex_col1, ex_col2 = st.columns(2)
        with ex_col1:
            st.download_button(
                "📋 Export Filtered Audit Logs CSV",
                filtered_df.to_csv(index=False).encode("utf-8"),
                f"filtered_audit_logs_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                "text/csv",
                key="audit_export_filtered",
            )
        with ex_col2:
            st.warning("⚠️ **Danger Zone**")
            clear_confirm = st.checkbox("I confirm that I want to permanently delete all local audit log files", key="audit_clear_confirm")
            if st.button("🗑️ Clear All Audit Logs", type="secondary", disabled=not clear_confirm, key="audit_clear_btn"):
                clear_audit_log()
                st.success("✅ Audit logs successfully cleared!")
                st.rerun()


# ─────────────────────────────────────────────────────────────
# FOOTER
# ─────────────────────────────────────────────────────────────
st.markdown("""
<div style='text-align:center; color: var(--text-sec); font-size:.75rem; margin-top:3rem;
            padding:1.4rem 0 0.8rem; border-top:1.5px solid var(--border);'>
    <span style="font-size:1rem; vertical-align:middle; margin-right:4px;">🎓</span>
    <strong style="color: var(--primary); font-family: 'Plus Jakarta Sans', sans-serif;">Innomatics Research Labs – Operations & Dispatch Report Manager</strong>
    &nbsp;&middot;&nbsp; Automated Student Assessment Reports & Bulk Dispatch
    &nbsp;&middot;&nbsp; Official Operations Portal
</div>
""", unsafe_allow_html=True)