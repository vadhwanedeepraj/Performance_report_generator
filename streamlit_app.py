"""
Innomatics Research Labs – Operations Report Generator & Designer
================================================================
Enterprise EdTech & Operations platform for generating learner performance & assessment reports.
Built with Streamlit + ReportLab.
"""

import io
import os
import re
import json
import base64
import zipfile
import streamlit as st
import pandas as pd
from datetime import datetime

from config_manager import (
    DEFAULT_REPORT_CONFIG, THEME_PRESETS,
    validate_and_normalize_config, list_templates,
    load_template, save_template, duplicate_template,
    delete_template, export_template_json, import_template_json,
)
from report_theme import (
    get_page_geometry, build_styles,
    build_corporate_table_style, build_accent_table_style,
)
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
# LOAD EXTERNAL CSS
# ─────────────────────────────────────────────────────────────
def load_css():
    if os.path.exists("style.css"):
        with open("style.css", encoding="utf-8") as f:
            st.markdown(f"<style>{f.read()}</style>", unsafe_allow_html=True)

load_css()

# ─────────────────────────────────────────────────────────────
# ENV LOADER & GROQ CLIENT
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

# ─────────────────────────────────────────────────────────────
# SESSION STATE INITIALIZATION
# ─────────────────────────────────────────────────────────────
def _ss(k, v):
    if k not in st.session_state:
        st.session_state[k] = v

_ss("df",                None)
_ss("logo_bytes",        None)
_ss("gen_results",       [])
_ss("last_zip",          None)
_ss("input_mode",        "upload")
_ss("last_zip_ts",        "")
_ss("merge_df_left",     None)
_ss("merge_df_right",    None)
_ss("merged_df",         None)
_ss("force_regenerate",  False)
_ss("gsheet_url",        "")
_ss("show_guide",        False)
_ss("source_file",       "N/A")
_ss("session_id",        get_session_id())
_ss("mi_eligibility_overrides", {})
_ss("audit_log_events",  {
    "Generated (Batch)",
    "Downloaded (Batch ZIP)",
    "Mock Interview Status Update",
})

# Initialize report configuration in session state
if "report_cfg" not in st.session_state:
    st.session_state.report_cfg = json.loads(json.dumps(DEFAULT_REPORT_CONFIG))
else:
    st.session_state.report_cfg = validate_and_normalize_config(st.session_state.report_cfg)

cfg_active = st.session_state.report_cfg
_ss("identity_cols", cfg_active.get("identity_columns", ["Enrolment ID", "Full Name", "Batch #"]))
_ss("pdf_name_col",  cfg_active.get("pdf_name_col", "Enrolment ID"))
_ss("sections",      cfg_active.get("sections", []))
_ss("written_cfg",   cfg_active.get("written_cfg", {}))
_ss("written_name",  cfg_active.get("written_name", "Written Test"))
_ss("wt_enabled",    cfg_active.get("wt_enabled", False))
_ss("show_perf",     cfg_active.get("show_perf", True))
_ss("intro_text",    cfg_active.get("intro_text", ""))
_ss("show_action_plan", cfg_active.get("show_action_plan", True))
_ss("show_tech_action", cfg_active.get("show_tech_action", True))
_ss("show_comm_action", cfg_active.get("show_comm_action", True))
_ss("marks_pass_threshold", cfg_active.get("marks_pass_threshold", 75))
_ss("marks_show_grade", cfg_active.get("marks_show_grade", True))
_ss("marks_show_analysis", cfg_active.get("marks_show_analysis", True))
_ss("marks_show_rank", cfg_active.get("marks_show_rank", True))
_ss("marks_tone", cfg_active.get("marks_tone", "Encouraging"))
_ss("marks_module_context", cfg_active.get("marks_module_context", ""))
_ss("marks_benchmark_note", cfg_active.get("marks_benchmark_note", ""))
_ss("marks_show_benchmark_note", cfg_active.get("marks_show_benchmark_note", True))
_ss("benchmark_shortlist", cfg_active.get("benchmark_shortlist", 90))
_ss("benchmark_strong", cfg_active.get("benchmark_strong", 90))
_ss("benchmark_revision", cfg_active.get("benchmark_revision", 50))
_ss("footer_link_text", cfg_active.get("footer_link_text", "Schedule Mock Interview"))
_ss("footer_link_url", cfg_active.get("footer_link_url", "https://example.com/schedule"))
_ss("footer_deadline_text", cfg_active.get("footer_deadline_text", ""))
_ss("footer_mentor_text", cfg_active.get("footer_mentor_text", ""))
_ss("enable_mock_interview", cfg_active.get("enable_mock_interview", True))
_ss("show_watermark", cfg_active.get("show_watermark", False))
_ss("watermark_text", cfg_active.get("watermark_text", "CONFIDENTIAL"))
_ss("legal_notice", cfg_active.get("legal_notice", "This report is system-generated for assessment and evaluation purposes only."))
_ss("show_honors_certificate", cfg_active.get("show_honors_certificate", False))

# ─────────────────────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────────────────────
def safe_filename(val):
    s = str(val).strip()
    try:
        f = float(s)
        if f == int(f):
            s = str(int(f))
    except (ValueError, OverflowError):
        pass
    return re.sub(r'[^A-Za-z0-9_\-]', '_', s) or "report"


def _validate_csv(uploaded_file) -> tuple[bool, str]:
    size_mb = uploaded_file.size / (1024 * 1024)
    if size_mb > MAX_CSV_SIZE_MB:
        return False, f"⚠️ File too large ({size_mb:.1f} MB). Maximum allowed size is {MAX_CSV_SIZE_MB} MB."
    if uploaded_file.size == 0:
        return False, "⚠️ The uploaded file is empty. Please upload a valid CSV."
    try:
        uploaded_file.seek(0)
        content = uploaded_file.read().decode("utf-8", errors="replace")
        uploaded_file.seek(0)
        line_count = content.count("\n")
        if line_count <= 1:
            return False, "⚠️ CSV has no data rows (only a header or is blank)."
        if line_count - 1 > MAX_CSV_ROWS:
            return False, f"⚠️ CSV contains {line_count - 1:,} rows — exceeds recommended limit of {MAX_CSV_ROWS:,}."
    except Exception:
        pass
    return True, ""


def _should_log(action: str) -> bool:
    allowed = st.session_state.get("audit_log_events", {
        "Generated (Batch)", "Downloaded (Batch ZIP)", "Downloaded (Preview)", "Mock Interview Status Update",
    })
    return action in allowed


_original_log_audit_event = log_audit_event
def log_audit_event(action, *args, **kwargs):
    if _should_log(action):
        if "source_file" not in kwargs:
            kwargs["source_file"] = st.session_state.get("source_file", "N/A")
        return _original_log_audit_event(action, *args, **kwargs)


def get_student_identity_vals(row, id_cols, fallback_name="N/A"):
    sname = fallback_name
    eid = "N/A"
    if not id_cols:
        return sname, eid, "N/A"
    vals = [str(row[c]).strip() for c in id_cols if c in row.index]
    clean_vals = [v for v in vals if v.lower() not in ("nan", "none", "n/a", "")]
    if len(clean_vals) == 1:
        v = clean_vals[0]
        if v.isdigit() or re.match(r'^\d+(\.\d+)?[eE]\+?\d+$', v):
            eid = v
        else:
            sname = v
    elif len(clean_vals) >= 2:
        numeric_vals = []
        alpha_vals = []
        for v in clean_vals:
            if v.isdigit() or re.match(r'^\d+(\.\d+)?[eE]\+?\d+$', v):
                numeric_vals.append(v)
            else:
                alpha_vals.append(v)
        if alpha_vals:
            sname = alpha_vals[0]
        if numeric_vals:
            eid = numeric_vals[0]
        if sname == fallback_name and len(alpha_vals) == 0:
            first_val = clean_vals[0]
            if first_val != eid:
                sname = first_val
            elif len(clean_vals) > 1:
                sname = clean_vals[1]
    return sname, eid, "N/A"


def compute_grade(row, sections, _batch_df=None):
    total_scored, total_max = 0.0, 0.0
    for sec in sections:
        if not isinstance(sec, dict) or not sec.get("enabled", True):
            continue
        stype = sec.get("type", "")
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
                m = re.search(r'out\s+of\s+([\d.]+)', col, re.IGNORECASE)
                max_from_header = float(m.group(1)) if m else None
                cfg_c = col_cfg.get(col, {})
                max_s = float(cfg_c.get("max")) if cfg_c.get("max") else max_from_header
                if max_s and max_s > 0:
                    total_scored += min(score, max_s)
                    total_max += max_s
        elif stype == "rating_table":
            for col in sec.get("columns", []):
                if col not in row.index:
                    continue
                raw = str(row[col]).strip()
                if raw.lower() in ("nan", "na", "", "n/a", "none"):
                    continue
                try:
                    val = float(raw)
                except ValueError:
                    val = float(RATING_MAP.get(raw.lower(), 0))
                val = max(0.0, min(val, 4.0))
                total_scored += val
                total_max += 4.0

    if total_max <= 0:
        return "N/A", 0.0
    score_pct = round((total_scored / total_max) * 100.0, 1)
    grade_label, _color, _verdict = grade_label_from_pct(score_pct)
    return grade_label, score_pct


def compute_mock_interview_eligibility(row, sections, batch_df, cfg):
    shortlist_cut = cfg.get("benchmark_shortlist", 90)
    marks_items = []
    total_scored, total_max = 0.0, 0.0
    for sec in sections:
        if not isinstance(sec, dict) or not sec.get("enabled", True) or sec.get("type") != "marks_table":
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
            m = re.search(r'out\s+of\s+([\d.]+)', col, re.IGNORECASE)
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
        if batch_df is not None and len(batch_df) > 1:
            try:
                cohort_totals = []
                for _, brow in batch_df.iterrows():
                    bs, bm = 0.0, 0.0
                    for sec in sections:
                        if not isinstance(sec, dict) or not sec.get("enabled", True) or sec.get("type") != "marks_table":
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
                            m2 = re.search(r'out\s+of\s+([\d.]+)', col, re.IGNORECASE)
                            mh2 = float(m2.group(1)) if m2 else None
                            c2 = col_cfg.get(col, {})
                            mx2 = float(c2.get("max")) if c2.get("max") else mh2
                            if mx2 and mx2 > 0:
                                bs += s2
                                bm += mx2
                    cohort_totals.append(round((bs / bm) * 100, 1) if bm > 0 else 0.0)
                my_pct = round((total_scored / total_max) * 100, 1)
                rank = sum(1 for t in cohort_totals if t > my_pct) + 1
                eval_metric = round((1 - (rank - 1) / len(cohort_totals)) * 100, 1)
            except Exception:
                eval_metric = round((total_scored / total_max) * 100, 1)
        else:
            eval_metric = round((total_scored / total_max) * 100, 1)
    else:
        scores = []
        for sec in sections:
            if not isinstance(sec, dict) or not sec.get("enabled", True) or sec.get("type") != "rating_table":
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


def apply_loaded_template(loaded_cfg: dict):
    norm_cfg = validate_and_normalize_config(loaded_cfg)
    st.session_state.report_cfg = norm_cfg
    st.session_state.sections = json.loads(json.dumps(norm_cfg.get("sections", [])))
    st.session_state.identity_cols = list(norm_cfg.get("identity_columns", []))
    st.session_state.pdf_name_col = norm_cfg.get("pdf_name_col", "Enrolment ID")
    st.session_state.intro_text = norm_cfg.get("intro_text", "")
    st.session_state.show_perf = norm_cfg.get("show_performance_summary", True)
    st.session_state.show_action_plan = norm_cfg.get("show_action_plan", True)
    st.session_state.show_tech_action = norm_cfg.get("show_tech_action", True)
    st.session_state.show_comm_action = norm_cfg.get("show_comm_action", True)
    st.session_state.show_honors_certificate = norm_cfg.get("show_honors_certificate", False)
    st.session_state.marks_pass_threshold = norm_cfg.get("marks_pass_threshold", 75)
    st.session_state.marks_show_grade = norm_cfg.get("marks_show_grade", True)
    st.session_state.marks_show_analysis = norm_cfg.get("marks_show_analysis", True)
    st.session_state.marks_show_rank = norm_cfg.get("marks_show_rank", True)

    if norm_cfg.get("logo_base64"):
        try:
            st.session_state.logo_bytes = base64.b64decode(norm_cfg["logo_base64"])
        except Exception:
            pass

    # Invalidate widget cache keys so widgets load freshly from report_cfg
    for k in list(st.session_state.keys()):
        if any(k.startswith(pfx) for pfx in ("cfg_", "cp_", "s3_", "d_")):
            del st.session_state[k]


def get_current_full_config(df_ref=None) -> dict:
    """
    Assembles a complete, validated configuration dictionary combining session state
    formatting variables, layout settings, and active sections.
    """
    cfg = dict(st.session_state.report_cfg)

    if st.session_state.get("logo_bytes"):
        cfg["logo_base64"] = base64.b64encode(st.session_state.logo_bytes).decode("utf-8")

    # Reconstruct sections list with written assessment if enabled
    all_sections = []
    if st.session_state.get("wt_enabled") and st.session_state.get("written_cfg"):
        all_sections.append({
            "name": st.session_state.get("written_name", "Written Test"),
            "title": st.session_state.get("written_name", "Written Test"),
            "type": "written_test",
            "columns": st.session_state.get("written_cfg", {}),
            "enabled": True,
            "page_break_before": False,
        })
    all_sections += st.session_state.get("sections", [])
    cfg["sections"] = all_sections

    cfg["identity_columns"] = list(st.session_state.get("identity_cols", cfg.get("identity_columns", [])))
    cfg["identity_cols"] = cfg["identity_columns"]
    cfg["pdf_name_col"] = st.session_state.get("pdf_name_col", cfg.get("pdf_name_col", "Enrolment ID"))

    # Synchronize modular controls from session state or cfg
    modular_keys = [
        ("show_performance_summary", "show_perf"),
        ("show_perf", "show_perf"),
        ("show_action_plan", "show_action_plan"),
        ("show_tech_action", "show_tech_action"),
        ("show_comm_action", "show_comm_action"),
        ("show_honors_certificate", "show_honors_certificate"),
        ("marks_pass_threshold", "marks_pass_threshold"),
        ("marks_show_grade", "marks_show_grade"),
        ("marks_show_analysis", "marks_show_analysis"),
        ("marks_show_rank", "marks_show_rank"),
        ("marks_tone", "marks_tone"),
        ("marks_module_context", "marks_module_context"),
        ("marks_benchmark_note", "marks_benchmark_note"),
        ("marks_show_benchmark_note", "marks_show_benchmark_note"),
        ("benchmark_shortlist", "benchmark_shortlist"),
        ("benchmark_strong", "benchmark_strong"),
        ("benchmark_revision", "benchmark_revision"),
        ("enable_mock_interview", "enable_mock_interview"),
    ]
    for key, ss_key in modular_keys:
        if ss_key in st.session_state:
            cfg[key] = st.session_state[ss_key]

    # For intro_text, legal_notice, watermark, footer links, prioritize report_cfg if set
    for key, widget_key in [
        ("intro_text", "cfg_intro"),
        ("legal_notice", "cfg_legal_notice"),
        ("show_watermark", "cfg_show_wm"),
        ("watermark_text", "cfg_wm_text"),
        ("footer_link_text", "cfg_ftr_link_txt"),
        ("footer_link_url", "cfg_ftr_link_url"),
        ("footer_deadline_text", "cfg_ftr_deadline"),
        ("footer_mentor_text", "cfg_ftr_mentor"),
    ]:
        if widget_key in st.session_state:
            cfg[key] = st.session_state[widget_key]
        elif key in st.session_state.report_cfg:
            cfg[key] = st.session_state.report_cfg[key]
        elif key in st.session_state:
            cfg[key] = st.session_state[key]

    if st.session_state.get("mi_eligibility_overrides"):
        cfg["mi_eligibility_overrides"] = dict(st.session_state.mi_eligibility_overrides)

    if st.session_state.get("logo_bytes"):
        import base64
        cfg["logo_base64"] = base64.b64encode(st.session_state.logo_bytes).decode("utf-8")

    if df_ref is not None:
        cfg["_batch_df"] = df_ref

    return validate_and_normalize_config(cfg)


# ─────────────────────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────────────────────
with st.sidebar:
    has_data = st.session_state.df is not None
    row_count = len(st.session_state.df) if has_data else 0
    ds_status = f"🟢 Connected ({row_count} records)" if has_data else "⚪ Idle (Awaiting data)"
    ds_file = st.session_state.source_file if st.session_state.source_file != "N/A" else "None"

    if st.session_state.get("logo_bytes"):
        st.image(io.BytesIO(st.session_state.logo_bytes), use_container_width=True)
    elif os.path.exists("logo/innomatics_logo.png"):
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
            REPORT GENERATOR & DESIGNER
          </div>
        </div>
      </div>
      <div style="display:flex;flex-direction:column;gap:4px;background:#F8FAFC;
                  border:1px solid #DDE3EE;padding:8px 10px;border-radius:4px;margin-top:8px;">
        <div style="font-size:0.67rem;color:#475569;font-family:monospace;">
          <strong style="color:#0D1117;">Data:</strong> {ds_status}
        </div>
        <div style="font-size:0.68rem;color:#475569;font-family:monospace;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;">
          <strong style="color:#0D1117;">File:</strong> {ds_file}
        </div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    with st.expander("⚡ Quick Template Switcher", expanded=True):
        saved_tpls = list_templates()
        if saved_tpls:
            cur_tpl = st.selectbox("Active Template", saved_tpls, index=0, key="sb_quick_tpl")
            if st.button("📂 Load Selected Template", key="sb_quick_load", type="primary", use_container_width=True):
                ok, loaded_cfg, msg = load_template(cur_tpl)
                if ok:
                    apply_loaded_template(loaded_cfg)
                    st.success(f"Loaded: {cur_tpl}")
                    st.rerun()
                else:
                    st.error(msg)
        else:
            st.caption("No templates saved yet. Design and save one in Step 3.")

    with st.expander("🖼️ Institute Logo", expanded=False):
        sb_logo_up = st.file_uploader("Upload Institute Logo", type=["png","jpg","jpeg"], key="sb_logo")
        if sb_logo_up:
            st.session_state.logo_bytes = sb_logo_up.read()
            st.caption("✅ Custom Logo Active")
            st.image(io.BytesIO(st.session_state.logo_bytes), width=130)
            if st.button("🔄 Reset to Default Logo", key="btn_reset_logo_sb"):
                st.session_state.logo_bytes = None
                st.rerun()
        else:
            if st.session_state.get("logo_bytes"):
                st.caption("✅ Custom Logo Active")
                st.image(io.BytesIO(st.session_state.logo_bytes), width=130)
                if st.button("🔄 Reset to Default Logo", key="btn_reset_logo_sb2"):
                    st.session_state.logo_bytes = None
                    st.rerun()
            else:
                for _lp in ["logo/innomatics_logo.png","logo/logo.png","logo/company_logo.png"]:
                    if os.path.exists(_lp):
                        st.caption(f"🖼️ Using default: `{_lp}`")
                        st.image(_lp, width=130)
                        break
                else:
                    st.caption("No logo found — upload one above.")

    with st.expander("🤖 AI Feedback Engine", expanded=False):
        enable_ai = st.toggle("Enable Groq AI Feedback", value=False, key="sb_ai")
        env_key   = os.environ.get("GROQ_API_KEY", "").strip()
        api_key   = st.text_input("Groq API Key", type="password", value=env_key, placeholder="gsk_...", key="sb_apikey")
        ai_model  = st.selectbox("AI Model", ["llama-3.3-70b-versatile","llama-3.1-8b-instant","mixtral-8x7b-32768"], key="sb_model")

    with st.expander("📋 Audit Log Controls", expanded=False):
        st.caption("Select events recorded in audit_logs.csv:")
        _audit_opts = {
            "Generated (Batch)":          "📄 Report Generation",
            "Downloaded (Batch ZIP)":     "📦 Batch ZIP Download",
            "Downloaded (Preview)":       "🔎 Preview PDF Download",
            "Mock Interview Status Update": "📅 Interview Bookings",
        }
        new_audit_events = set()
        for _key, _label in _audit_opts.items():
            _checked = _key in st.session_state.audit_log_events
            if st.checkbox(_label, value=_checked, key=f"audit_ev_{_key.replace(' ','_')}"):
                new_audit_events.add(_key)
        st.session_state.audit_log_events = new_audit_events


# ─────────────────────────────────────────────────────────────
# HERO & KPI STRIP
# ─────────────────────────────────────────────────────────────
has_data_kpi = st.session_state.df is not None
student_count_kpi = len(st.session_state.df) if has_data_kpi else 0
sections_count_kpi = len([s for s in st.session_state.sections if s.get("enabled", True)])
ai_mode_lbl = "AI FEEDBACK: ACTIVE" if enable_ai else "AI FEEDBACK: OFF"
ai_class = "chip-ai-on" if enable_ai else "chip-ai-off"

st.markdown(f"""
<div class="hero-strip">
  <div class="hero-header">
    <div class="hero-title-group">
      <div class="hero-title">Innomatics Operations &amp; Report Management</div>
      <div class="hero-sub">
        Automated Performance Cards &middot; Section Builder &middot; Custom Report Designer &middot; Bulk Delivery
      </div>
    </div>
    <div class="hero-badges">
      <span class="chip-status chip-live">
        <span class="pulse-dot"></span> SYSTEM ONLINE
      </span>
      <span class="chip-status {ai_class}">{ai_mode_lbl}</span>
    </div>
  </div>
  <div class="kpi-strip">
    <div class="kpi-cell">
      <div class="kpi-val">{student_count_kpi}</div>
      <div class="kpi-lbl">Active Cohort Records</div>
    </div>
    <div class="kpi-cell">
      <div class="kpi-val">{sections_count_kpi}</div>
      <div class="kpi-lbl">Enabled Report Sections</div>
    </div>
    <div class="kpi-cell">
      <div class="kpi-val">{len(list_templates())}</div>
      <div class="kpi-lbl">Saved Templates</div>
    </div>
    <div class="kpi-cell">
      <div class="kpi-val">{st.session_state.report_cfg.get('page_size', 'A4')}</div>
      <div class="kpi-lbl">Current Page Format</div>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────
# ONBOARDING GUIDE TOGGLE
# ─────────────────────────────────────────────────────────────
g_col1, g_col2 = st.columns([7, 3])
with g_col1:
    st.caption("💡 Follow the familiar step-by-step workflow: Data Sources → Section Builder → Custom Designer → Batch PDF Generation.")
with g_col2:
    show_guide = st.toggle("📖 Quick Start Guide", value=st.session_state.show_guide, key="toggle_guide")
    st.session_state.show_guide = show_guide

if show_guide:
    st.markdown("""
    <div class="guide-box">
      <h4>🚀 4-Step Production Workflow:</h4>
      <ol>
        <li><strong>Step 1: Data Sources &amp; Merger</strong> — Upload CSV/Excel, import from Google Sheets, or load Demo Cohort Data.</li>
        <li><strong>Step 2: Section Builder &amp; Column Mapping</strong> — Familiar column mapper to assign candidate identity fields, PDF filename column, and evaluation sections.</li>
        <li><strong>Step 3: Report Customization &amp; Designer</strong> — Comprehensive styling studio to customize page geometry, margins, typography, branding colors, section reordering, page breaks, and templates.</li>
        <li><strong>Step 4: Batch PDF Generation</strong> — Preview candidate PDFs and generate the full cohort ZIP package.</li>
      </ol>
    </div>
    """, unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
# MAIN WORKSPACE TABS — 7-STEP FAMILIAR WORKFLOW
# ─────────────────────────────────────────────────────────────
tab_data, tab_builder, tab_design, tab_gen, tab_cohort, tab_hist, tab_audit = st.tabs([
    "📁 Step 1: Data Sources & Merger",
    "🛠️ Step 2: Section Builder & Column Mapping",
    "🎨 Step 3: Report Customization & Designer",
    "⚡ Step 4: Batch PDF Generation",
    "📈 Step 5: Cohort Analytics",
    "🗂️ Step 6: Generation History",
    "🔍 Step 7: Audit & Compliance",
])

# ══════════════════════════════════════════════════════════════
# TAB 1 — STEP 1: DATA SOURCES & MERGER (UNCHANGED)
# ══════════════════════════════════════════════════════════════
with tab_data:
    st.markdown("## 📁 Step 1: Learner Data Source & Merger")

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

    # ── UPLOAD
    if st.session_state.input_mode == "upload":
        up = st.file_uploader("Upload CSV or Excel", type=["csv","xlsx","xls"], key="up_file")
        if up is not None:
            if up.name.lower().endswith(".csv"):
                ok, err_msg = _validate_csv(up)
                if not ok:
                    st.error(err_msg)
                    st.stop()

            _file_key = f"{up.name}_{up.size}"
            if st.session_state.get("_last_file_key") != _file_key:
                def _load(name, data):
                    buf = io.BytesIO(data)
                    df = pd.read_csv(buf) if name.lower().endswith(".csv") else pd.read_excel(buf)
                    return df.dropna(how="all").reset_index(drop=True)

                raw = _load(up.name, up.getvalue())
                raw.columns = [str(c).strip() for c in raw.columns]
                st.session_state.source_file = up.name
                st.session_state.force_regenerate = False
                st.session_state["_last_file_key"] = _file_key
                st.session_state.df = raw.copy()

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

            st.info("⬆️ Data loaded! Proceed to **🛠️ Step 2: Section Builder** to map columns or configure sections.")
        else:
            st.info("👆 Upload a cohort CSV or Excel file, or click below to test with sample data.")
            cs1, cs2 = st.columns(2)
            with cs1:
                if st.button("⚡ Load Demo Cohort Data", key="btn_load_demo", use_container_width=True):
                    demo_data = {
                        "Student Name": ["Arjun Mehta", "Sophia Sen", "Kabir Kapoor", "Ananya Rao", "Rohan Sharma"],
                        "Enrollment ID": ["INN-2026-001", "INN-2026-002", "INN-2026-003", "INN-2026-004", "INN-2026-005"],
                        "Batch #": ["Data Science Core", "Data Science Core", "Full Stack Core", "Full Stack Core", "Data Science Core"],
                        "Python Programming": ["Excellent", "Good", "Average", "Excellent", "Poor"],
                        "SQL Database Skill": ["Excellent", "Average", "Good", "Good", "Average"],
                        "Machine Learning Concepts": ["Good", "Average", "Good", "Excellent", "Poor"],
                        "Communication & Soft Skills": ["Excellent", "Good", "Excellent", "Average", "Good"],
                        "Python Prog (out of 100)": [92, 78, 65, 96, 48],
                        "EDA Foundation (out of 100)": [88, 72, 70, 94, 52],
                        "MySQL and PowerBI (out of 100)": [95, 80, 75, 92, 50],
                        "Aptitude (out of 100)": [90, 85, 68, 98, 60],
                        "Remarks": [
                            "Outstanding developer mindset. Strong foundations in data pipelines and optimization.",
                            "Demonstrated solid debugging skills. Needs practice on complex database joins.",
                            "Good core concepts. Encouraged to spend more hours building raw code.",
                            "Masterful understanding of advanced algorithms. Excellent class participation.",
                            "Required to review core programming loops and database normalization basics."
                        ]
                    }
                    st.session_state.df = pd.DataFrame(demo_data)
                    st.session_state.source_file = "demo_cohort_data.csv"
                    st.session_state.identity_cols = ["Enrollment ID", "Student Name", "Batch #"]
                    st.session_state.pdf_name_col = "Enrollment ID"
                    st.success("✅ Demo cohort data loaded successfully!")
                    st.rerun()

    # ── GOOGLE SHEET
    elif st.session_state.input_mode == "gsheet":
        st.markdown("### 🟢 Connect Google Sheet")
        gurl = st.text_input("Enter Google Sheet Share Link:", value=st.session_state.gsheet_url, key="gsheet_in")
        if st.button("Connect & Import Sheet", type="primary"):
            if "docs.google.com/spreadsheets" in gurl:
                try:
                    match = re.search(r"/d/([a-zA-Z0-9-_]+)", gurl)
                    if match:
                        sid = match.group(1)
                        export_url = f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv"
                        df_g = pd.read_csv(export_url)
                        st.session_state.df = df_g
                        st.session_state.source_file = "Google_Sheet_Import.csv"
                        st.success(f"✅ Imported {len(df_g)} rows from Google Sheet!")
                        st.rerun()
                except Exception as ge:
                    st.error(f"Failed to import: {ge}")
            else:
                st.warning("Please provide a valid Google Sheet URL.")

    # ── MANUAL ENTRY
    elif st.session_state.input_mode == "manual":
        st.markdown("### ✏️ Single Learner Evaluation Form")
        with st.form("manual_entry_form"):
            m_name = st.text_input("Candidate Name", "Rahul Sharma")
            m_id = st.text_input("Enrollment ID", "INN-2026-999")
            m_batch = st.text_input("Batch #", "Batch 455")
            m_p1 = st.number_input("Python Score (out of 100)", 0, 100, 85)
            m_p2 = st.number_input("EDA Score (out of 100)", 0, 100, 78)
            m_comm = st.selectbox("Communication Rating", ["Poor", "Average", "Good", "Excellent"], index=2)
            m_tech = st.selectbox("Technical Rating", ["Poor", "Average", "Good", "Excellent"], index=3)
            m_remarks = st.text_area("Instructor Remarks", "Candidate shows solid analytical skills.")
            submitted = st.form_submit_button("Create Learner Record", type="primary")
            if submitted:
                row_dict = {
                    "Student Name": [m_name],
                    "Enrollment ID": [m_id],
                    "Batch #": [m_batch],
                    "Python Programming": [m_tech],
                    "Communication & Soft Skills": [m_comm],
                    "Python Prog (out of 100)": [m_p1],
                    "EDA Foundation (out of 100)": [m_p2],
                    "Remarks": [m_remarks]
                }
                st.session_state.df = pd.DataFrame(row_dict)
                st.session_state.source_file = "manual_entry.csv"
                st.session_state.identity_cols = ["Enrollment ID", "Student Name", "Batch #"]
                st.session_state.pdf_name_col = "Enrollment ID"
                st.success("Record created! Proceed to Step 2.")
                st.rerun()

    # ── DATASET MERGER
    elif st.session_state.input_mode == "merge":
        st.markdown("### 🔗 Merge Two Datasets")
        m_c1, m_c2 = st.columns(2)
        with m_c1:
            up1 = st.file_uploader("Upload First CSV/Excel (e.g. Theory Exam)", key="m_up1")
            if up1:
                st.session_state.merge_df_left = pd.read_csv(up1) if up1.name.endswith(".csv") else pd.read_excel(up1)
                st.caption(f"Loaded: {len(st.session_state.merge_df_left)} rows")
        with m_c2:
            up2 = st.file_uploader("Upload Second CSV/Excel (e.g. Mock Interview)", key="m_up2")
            if up2:
                st.session_state.merge_df_right = pd.read_csv(up2) if up2.name.endswith(".csv") else pd.read_excel(up2)
                st.caption(f"Loaded: {len(st.session_state.merge_df_right)} rows")

        if st.session_state.merge_df_left is not None and st.session_state.merge_df_right is not None:
            common_cols = list(set(st.session_state.merge_df_left.columns).intersection(set(st.session_state.merge_df_right.columns)))
            if common_cols:
                join_col = st.selectbox("Select Key Column to Join On:", common_cols)
                if st.button("🔗 Merge Datasets Now", type="primary"):
                    merged = pd.merge(st.session_state.merge_df_left, st.session_state.merge_df_right, on=join_col, how="inner")
                    st.session_state.df = merged
                    st.session_state.source_file = f"merged_{join_col}.csv"
                    st.success(f"Successfully merged {len(merged)} records on '{join_col}'!")
                    st.rerun()
            else:
                st.warning("No common column headers found between both datasets.")


# ══════════════════════════════════════════════════════════════
# TAB 2 — STEP 2: SECTION BUILDER & COLUMN MAPPING (PRESERVED FAMILIAR WORKFLOW)
# ══════════════════════════════════════════════════════════════
with tab_builder:
    st.markdown("## 🛠️ Step 2: Section Builder & Column Mapping")
    st.caption("Map uploaded CSV columns to candidate identity fields, assessment sections, and evaluation tables.")

    if st.session_state.df is None:
        st.info("📁 Upload learner data in **📁 Step 1: Data Sources & Merger** first.")
    else:
        df_d     = st.session_state.df
        all_cols = df_d.columns.tolist()

        # ── IDENTITY
        st.markdown("### 👤 Learner Identity Fields")
        id_default = [c for c in st.session_state.identity_cols if c in all_cols] or \
                     [c for c in all_cols if any(k in c.lower() for k in ["name","enroll","id","batch","interviewer"])][:5]
        identity_cols = st.multiselect("Select learner identity columns:", all_cols, default=id_default, key="d_id_cols")
        st.session_state.identity_cols = identity_cols
        st.session_state.report_cfg["identity_columns"] = identity_cols

        # ── PDF NAMING
        st.markdown("---")
        st.markdown("### 🏷️ Report PDF Naming")
        cur_name_col = st.session_state.pdf_name_col if st.session_state.pdf_name_col in all_cols else all_cols[0]
        pdf_name_col = st.selectbox("Column for PDF filename:", all_cols,
                                     index=all_cols.index(cur_name_col), key="d_pdf_col")
        st.session_state.pdf_name_col = pdf_name_col
        st.session_state.report_cfg["pdf_name_col"] = pdf_name_col
        sample_vals  = df_d[pdf_name_col].astype(str).head(5).tolist()
        preview_str  = "  |  ".join([f"{safe_filename(v)}.pdf" for v in sample_vals])
        st.markdown(f'<div class="fname-preview">📄 Preview: {preview_str}</div>', unsafe_allow_html=True)

        # ── WRITTEN TEST
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

        # ── EVALUATION SECTIONS
        st.markdown("---")
        st.markdown("### 📊 Assessment Sections")

        # ⚡ AUTO-CONFIGURE SMART BUTTON
        if st.button("⚡ Auto-Detect & Configure Sections from CSV", type="primary", key="btn_auto_cfg"):
            new_sections = []
            avail = all_cols.copy()

            # Detect Tech Rating
            tech_cols = [c for c in avail if "technical rating" in c.lower() or "rating" in c.lower() or "skill" in c.lower()]
            if tech_cols:
                new_sections.append({"name": "Technical Evaluation", "type": "rating_table", "columns": tech_cols, "enabled": True})

            # Detect Marks columns
            marks_cols = [c for c in avail if "out of" in c.lower() or "marks" in c.lower() or "score" in c.lower() or "%" in c.lower()]
            if marks_cols:
                new_sections.append({"name": "Module-wise Performance", "type": "marks_table", "columns": marks_cols, "enabled": True})

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
            remark_cols = [c for c in avail if "remark" in c.lower() or "feedback" in c.lower() or "comment" in c.lower()]
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
                    "type": "rating_table", "columns": [], "enabled": True, "page_break_before": False
                })
                st.rerun()
        with sa2:
            if st.button("➕ Add Action Plan", key="btn_add_ap"):
                st.session_state.show_tech_action = True
                st.session_state.show_comm_action = True
                st.session_state.sections.append({
                    "name": "Action Plan Grid", "type": "action_plan_grid",
                    "columns": [], "enabled": True, "page_break_before": False
                })
                st.rerun()
        with sa3:
            if st.button("➕ Add Marks Table", key="btn_add_mt"):
                count = len([s for s in st.session_state.sections if s.get("type") == "marks_table"]) + 1
                st.session_state.sections.append({
                    "name": f"Marks Table {count}",
                    "title": "Module-wise Performance" if count == 1 else f"Module-wise Performance {count}",
                    "type": "marks_table", "columns": [], "enabled": True, "page_break_before": False
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

            with st.expander(f"{icon} {sec.get('name','Section')} [{s_label}]", expanded=(idx < 2)):
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

                default_title = sec.get("title") or (
                    "Module-wise Performance" if sec.get("name", "").startswith("Marks Table")
                    else sec.get("name", "Module-wise Performance")
                )
                st_title = st.text_input("Table Heading / Title (Displayed on PDF)", value=default_title, key=f"stitle_{idx}")

                sc_default = [c for c in sec.get("columns",[]) if c in avail]
                sc_cols    = st.multiselect("Columns:", avail, default=sc_default, key=f"sc_{idx}")

                col_cfg = sec.get("col_cfg", {})
                if st_type == "marks_table" and sc_cols:
                    st.markdown("**Column Configuration (Marks Table):**")
                    new_col_cfg = {}
                    for col in sc_cols:
                        old = col_cfg.get(col, {})
                        old_label = col.split("[")[-1].replace("]", "").strip() if "[" in col else col
                        old_label = re.sub(r'\(?\s*out\s+of\s+[\d.]+\s*\)?', '', old_label, flags=re.IGNORECASE).strip().strip("()")
                        m = re.search(r'out\s+of\s+([\d.]+)', col, re.IGNORECASE)
                        max_from_header = int(float(m.group(1))) if m else 100
                        ca, cb = st.columns([3, 1])
                        with ca:
                            lbl = st.text_input("Label", old.get("label", old_label), key=f"ml_{idx}_{col}")
                        with cb:
                            mx = st.number_input("Max Marks", 1, value=int(old.get("max") or max_from_header), key=f"mm_{idx}_{col}")
                        new_col_cfg[col] = {"label": lbl, "max": mx}
                    col_cfg = new_col_cfg

                updated = {
                    "id": sec.get("id", f"sec_{idx+1}"),
                    "name": sn, "title": st_title, "type": st_type,
                    "columns": sc_cols, "enabled": se, "col_cfg": col_cfg,
                    "page_break_before": sec.get("page_break_before", False)
                }
                st.session_state.sections[idx] = updated

        if del_idx is not None:
            st.session_state.sections.pop(del_idx); st.rerun()

        # ── MODULAR REPORT INSIGHTS & ACTION PLAN CONTROLS
        st.markdown("---")
        st.markdown("### 🧩 Additional Report Modules & Insights Controls")
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

        st.info("💡 Next: Proceed to **🎨 Step 3: Report Customization & Designer** to adjust page setup, fonts, margins, colors, and live preview!")


# ══════════════════════════════════════════════════════════════
# TAB 3 — STEP 3: CONSOLIDATED REPORT CUSTOMIZATION & DESIGNER
# ══════════════════════════════════════════════════════════════
with tab_design:
    st.markdown("## 🎨 Step 3: Report Customization & Designer Studio")
    st.caption("Consolidated design center: customize page geometry, margins, fonts, branding colors, section ordering, page breaks, table borders, and templates.")

    des_tab_layout, des_tab_brand, des_tab_type, des_tab_sec, des_tab_hf, des_tab_table, des_tab_tpl = st.tabs([
        "📐 Page Setup & Margins",
        "🎨 Branding & Colors",
        "🔤 Typography & Hierarchy",
        "📊 Section Ordering & Page Breaks",
        "📑 Header, Footer & Notice",
        "🔲 Table Formatting",
        "💾 Template Management",
    ])

    cfg_cur = st.session_state.report_cfg

    # ── 1. PAGE SETUP & MARGINS
    with des_tab_layout:
        st.markdown("### 📐 Page Dimensions, Orientation & Margins")
        col_g1, col_g2 = st.columns(2)
        with col_g1:
            page_sizes = ["A4", "Letter", "Legal"]
            cur_ps = cfg_cur.get("page_size", "A4")
            sel_ps = st.selectbox("Page Size", page_sizes, index=page_sizes.index(cur_ps) if cur_ps in page_sizes else 0, key="cfg_ps")
            cfg_cur["page_size"] = sel_ps

            orientations = ["portrait", "landscape"]
            cur_orient = cfg_cur.get("orientation", "portrait")
            sel_orient = st.selectbox("Orientation", orientations, index=orientations.index(cur_orient) if cur_orient in orientations else 0, key="cfg_orient")
            cfg_cur["orientation"] = sel_orient

        with col_g2:
            st.markdown("**Page Margins (Points — 72 pt = 1 inch):**")
            m_top = st.slider("Top Margin (space for header)", 60, 160, int(cfg_cur.get("margin_top", 115)), 5, key="cfg_m_top")
            cfg_cur["margin_top"] = m_top
            m_bot = st.slider("Bottom Margin (space for footer)", 20, 80, int(cfg_cur.get("margin_bottom", 40)), 5, key="cfg_m_bot")
            cfg_cur["margin_bottom"] = m_bot
            m_left = st.slider("Left Margin", 20, 80, int(cfg_cur.get("margin_left", 40)), 5, key="cfg_m_left")
            cfg_cur["margin_left"] = m_left
            m_right = st.slider("Right Margin", 20, 80, int(cfg_cur.get("margin_right", 40)), 5, key="cfg_m_right")
            cfg_cur["margin_right"] = m_right

        st.markdown("---")
        st.markdown("### 📏 Spacing & Vertical Flow")
        sp1, sp2, sp3 = st.columns(3)
        with sp1:
            line_sp = st.slider("Line Spacing (Leading Factor)", 1.0, 1.8, float(cfg_cur.get("line_spacing", 1.3)), 0.05, key="cfg_linesp")
            cfg_cur["line_spacing"] = line_sp
        with sp2:
            para_sp = st.slider("Paragraph Spacing (pt)", 1, 15, int(cfg_cur.get("paragraph_spacing", 3)), 1, key="cfg_parasp")
            cfg_cur["paragraph_spacing"] = para_sp
        with sp3:
            sec_sp = st.slider("Section Spacing (pt)", 2, 25, int(cfg_cur.get("section_spacing", 6)), 1, key="cfg_secsp")
            cfg_cur["section_spacing"] = sec_sp

    # ── 2. BRANDING & COLORS
    with des_tab_brand:
        st.markdown("### 🎨 Theme Presets & Color Palette")
        preset_names = list(THEME_PRESETS.keys()) + ["Custom"]
        cur_preset = cfg_cur.get("theme_preset", "Innomatics Crimson")
        sel_preset = st.selectbox("Select Theme Preset", preset_names, index=preset_names.index(cur_preset) if cur_preset in preset_names else 0, key="cfg_preset")

        if sel_preset != "Custom" and sel_preset in THEME_PRESETS:
            theme_vals = THEME_PRESETS[sel_preset]
            cfg_cur["theme_preset"] = sel_preset
            cfg_cur["primary_color"] = theme_vals["primary_color"]
            cfg_cur["secondary_color"] = theme_vals["secondary_color"]
            cfg_cur["accent_color"] = theme_vals["accent_color"]
            cfg_cur["header_bg_color"] = theme_vals["header_bg_color"]
            cfg_cur["text_color"] = theme_vals["text_color"]
            cfg_cur["heading_text_color"] = theme_vals["heading_text_color"]
            cfg_cur["gold_accent"] = theme_vals["gold_accent"]

        st.markdown("##### Brand & Accent Colors")
        c_col1, c_col2, c_col3, c_col4 = st.columns(4)
        with c_col1:
            cfg_cur["primary_color"] = st.color_picker("Primary Brand Color", cfg_cur.get("primary_color", "#B91C1C"), key="cp_primary")
            cfg_cur["header_bg_color"] = st.color_picker("Table Header Background", cfg_cur.get("header_bg_color", cfg_cur["primary_color"]), key="cp_header_bg")
        with c_col2:
            cfg_cur["secondary_color"] = st.color_picker("Secondary / Slate", cfg_cur.get("secondary_color", "#1E293B"), key="cp_sec")
            cfg_cur["accent_color"] = st.color_picker("Card Accent Background", cfg_cur.get("accent_color", "#F8FAFC"), key="cp_accent")
        with c_col3:
            cfg_cur["heading_text_color"] = st.color_picker("Heading Text Color", cfg_cur.get("heading_text_color", "#0F172A"), key="cp_hd_text")
            cfg_cur["text_color"] = st.color_picker("Body Text Color", cfg_cur.get("text_color", "#334155"), key="cp_body_text")
        with c_col4:
            cfg_cur["gold_accent"] = st.color_picker("Gold Accent Trim", cfg_cur.get("gold_accent", "#C9A84C"), key="cp_gold")

        st.markdown("---")
        st.markdown("### 🏫 Institute Logo & Placement")
        l_col1, l_col2, l_col3 = st.columns([2, 1, 1])
        with l_col1:
            logo_up = st.file_uploader("Upload Institute Logo (PNG / JPG)", type=["png","jpg","jpeg"], key="cfg_logo_up")
            if logo_up:
                st.session_state.logo_bytes = logo_up.read()
                st.caption("✅ Custom Logo Uploaded:")
                st.image(io.BytesIO(st.session_state.logo_bytes), width=150)
                if st.button("🔄 Reset to Default Logo", key="btn_reset_logo_s3"):
                    st.session_state.logo_bytes = None
                    st.rerun()
            elif st.session_state.get("logo_bytes"):
                st.caption("✅ Custom Logo Active:")
                st.image(io.BytesIO(st.session_state.logo_bytes), width=150)
                if st.button("🔄 Reset to Default Logo", key="btn_reset_logo_s3_exist"):
                    st.session_state.logo_bytes = None
                    st.rerun()
            elif os.path.exists("logo/innomatics_logo.png"):
                st.caption("🖼️ Default Logo (`logo/innomatics_logo.png`):")
                st.image("logo/innomatics_logo.png", width=150)
        with l_col2:
            align_opts = ["center", "left", "right"]
            cur_align = cfg_cur.get("logo_align", "center")
            cfg_cur["logo_align"] = st.selectbox("Logo Alignment", align_opts, index=align_opts.index(cur_align) if cur_align in align_opts else 0, key="cfg_logo_align")
            cfg_cur["show_logo"] = st.toggle("Show Logo on Report", value=cfg_cur.get("show_logo", True), key="cfg_show_logo")
        with l_col3:
            cfg_cur["logo_width"] = st.slider("Max Logo Width (pt)", 100, 450, int(cfg_cur.get("logo_width", 260)), 10, key="cfg_logo_w")
            cfg_cur["logo_height"] = st.slider("Max Logo Height (pt)", 30, 150, int(cfg_cur.get("logo_height", 70)), 5, key="cfg_logo_h")

        st.markdown("---")
        st.markdown("### 📝 Branding Titles")
        cfg_cur["company_name"] = st.text_input("Institute / Company Name", cfg_cur.get("company_name", "Innomatics Research Labs"), key="cfg_company")
        cfg_cur["report_title"] = st.text_input("Report Title Heading", cfg_cur.get("report_title", "MOCK INTERVIEW EVALUATION REPORT"), key="cfg_title")
        cfg_cur["report_subtitle"] = st.text_input("Report Subtitle (Optional)", cfg_cur.get("report_subtitle", ""), key="cfg_subtitle")
        cfg_cur["intro_text"] = st.text_area("Report Introduction Paragraph", cfg_cur.get("intro_text", ""), height=70, key="cfg_intro")
        st.session_state.intro_text = cfg_cur["intro_text"]

    # ── 3. TYPOGRAPHY & HIERARCHY (WITH FLOAT-SAFE SLIDERS)
    with des_tab_type:
        st.markdown("### 🔤 Font Family & Size Scaling")
        fonts = ["Helvetica", "Times-Roman", "Courier"]
        cur_f = cfg_cur.get("font_family", "Helvetica")
        cfg_cur["font_family"] = st.selectbox("Primary Font Family", fonts, index=fonts.index(cur_f) if cur_f in fonts else 0, key="cfg_font_family")

        st.markdown("##### Point Sizes (Font Hierarchy)")
        t_c1, t_c2, t_c3 = st.columns(3)
        with t_c1:
            cfg_cur["title_font_size"] = st.slider("Report Title Size (pt)", 10, 24, int(cfg_cur.get("title_font_size", 13)), 1, key="cfg_sz_title")
            cfg_cur["heading_font_size"] = st.slider("Section Heading Size (pt)", 8, 18, int(cfg_cur.get("heading_font_size", 10)), 1, key="cfg_sz_head")
        with t_c2:
            cfg_cur["body_font_size"] = st.slider("Body Text Size (pt)", 6, 14, int(cfg_cur.get("body_font_size", 8)), 1, key="cfg_sz_body")
            cfg_cur["table_font_size"] = st.slider("Table Cell Size (pt)", 6.0, 14.0, float(cfg_cur.get("table_font_size", 7.5)), 0.5, key="cfg_sz_cell")
        with t_c3:
            cfg_cur["footer_font_size"] = st.slider("Footer Notice Size (pt)", 5.0, 10.0, float(cfg_cur.get("footer_font_size", 6.5)), 0.5, key="cfg_sz_foot")

    # ── 4. SECTION ORDERING & PAGE BREAKS
    with des_tab_sec:
        st.markdown("### 📊 Section Pipeline Ordering & Page Breaks")
        st.caption("Change the order in which sections render on the PDF using ⬆️ and ⬇️. Toggle 'Start on New Page' to force sections onto clean pages!")

        sec_list = st.session_state.sections
        del_idx = None

        if not sec_list:
            st.warning("⚠️ No sections currently configured. Add sections in Step 2 or click below to load defaults.")
            if st.button("Load Standard Sections"):
                st.session_state.sections = json.loads(json.dumps(DEFAULT_REPORT_CONFIG["sections"]))
                st.rerun()
        else:
            for idx, sec in enumerate(sec_list):
                stype = sec.get("type", "marks_table")
                is_enabled = sec.get("enabled", True)
                is_page_break = sec.get("page_break_before", False)
                badge_icon = "✅" if is_enabled else "⏸️"
                pb_icon = "📄 [New Page]" if is_page_break else ""

                exp_label = f"{badge_icon} #{idx+1}: {sec.get('name', 'Section')} ({stype}) {pb_icon}"
                with st.expander(exp_label, expanded=(idx < 2)):
                    r1, r2, r3, r4, r5 = st.columns([1, 1, 3, 2, 1])
                    with r1:
                        if st.button("⬆️", key=f"s3_sec_up_{idx}", disabled=(idx == 0)):
                            sec_list[idx-1], sec_list[idx] = sec_list[idx], sec_list[idx-1]
                            st.session_state.sections = sec_list
                            st.rerun()
                    with r2:
                        if st.button("⬇️", key=f"s3_sec_down_{idx}", disabled=(idx == len(sec_list) - 1)):
                            sec_list[idx], sec_list[idx+1] = sec_list[idx+1], sec_list[idx]
                            st.session_state.sections = sec_list
                            st.rerun()
                    with r3:
                        sec["title"] = st.text_input("PDF Heading Title", sec.get("title", sec.get("name", "")), key=f"s3_stitle_{idx}")
                    with r4:
                        sec["page_break_before"] = st.toggle("Start on New Page", value=is_page_break, key=f"s3_spgbreak_{idx}")
                    with r5:
                        sec["enabled"] = st.toggle("Visible", value=is_enabled, key=f"s3_senabled_{idx}")

    # ── 5. HEADER, FOOTER & NOTICE
    with des_tab_hf:
        st.markdown("### 📑 Header Styling")
        hf1, hf2, hf3 = st.columns(3)
        with hf1:
            cfg_cur["show_header"] = st.toggle("Display Top Header", value=cfg_cur.get("show_header", True), key="cfg_show_hdr")
        with hf2:
            hdr_styles = ["banner", "clean_line", "minimal"]
            cur_hs = cfg_cur.get("header_style", "banner")
            cfg_cur["header_style"] = st.selectbox("Header Style", hdr_styles, index=hdr_styles.index(cur_hs) if cur_hs in hdr_styles else 0, key="cfg_hdr_style")
        with hf3:
            cfg_cur["show_date"] = st.toggle("Display Date in Header", value=cfg_cur.get("show_date", True), key="cfg_show_dt")
            cfg_cur["date_format"] = st.text_input("Date Format", value=cfg_cur.get("date_format", "%d %B %Y"), key="cfg_dt_fmt")

        st.markdown("---")
        st.markdown("### 📑 Footer & Page Numbers")
        ft1, ft2 = st.columns(2)
        with ft1:
            cfg_cur["show_footer"] = st.toggle("Display Bottom Footer", value=cfg_cur.get("show_footer", True), key="cfg_show_ftr")
            cfg_cur["show_page_numbers"] = st.toggle("Display Page Numbers", value=cfg_cur.get("show_page_numbers", True), key="cfg_show_pgnum")
            cfg_cur["page_number_format"] = st.text_input("Page Number Format", value=cfg_cur.get("page_number_format", "Page {page}"), key="cfg_pgnum_fmt")
        with ft2:
            cfg_cur["legal_notice"] = st.text_area("Footer Disclaimer & Legal Notice", value=cfg_cur.get("legal_notice", ""), height=70, key="cfg_legal_notice")

        st.markdown("---")
        st.markdown("### 🔗 Call-to-Action Link & Mentor Note")
        cta1, cta2 = st.columns(2)
        with cta1:
            cfg_cur["footer_link_text"] = st.text_input("Footer CTA Button Label", value=cfg_cur.get("footer_link_text", "Schedule Mock Interview"), key="cfg_ftr_link_txt")
            cfg_cur["footer_link_url"] = st.text_input("Footer CTA Destination URL", value=cfg_cur.get("footer_link_url", "https://example.com/schedule"), key="cfg_ftr_link_url")
        with cta2:
            cfg_cur["footer_deadline_text"] = st.text_input("Submission / Booking Deadline", value=cfg_cur.get("footer_deadline_text", ""), key="cfg_ftr_deadline")
            cfg_cur["footer_mentor_text"] = st.text_input("Assigned Mentor / Lead Evaluator", value=cfg_cur.get("footer_mentor_text", ""), key="cfg_ftr_mentor")

        st.markdown("---")
        st.markdown("### 🔐 Watermark & Security")
        wm1, wm2 = st.columns(2)
        with wm1:
            cfg_cur["show_watermark"] = st.toggle("Enable Diagonal Watermark", value=cfg_cur.get("show_watermark", False), key="cfg_show_wm")
        with wm2:
            cfg_cur["watermark_text"] = st.text_input("Watermark Text", value=cfg_cur.get("watermark_text", "CONFIDENTIAL"), key="cfg_wm_text")

    # ── 6. TABLE FORMATTING
    with des_tab_table:
        st.markdown("### 🔲 Table Formatting & Styling Controls")
        tb1, tb2 = st.columns(2)
        with tb1:
            b_styles = ["grid", "horizontal_only", "boxed", "subtle", "minimal"]
            cur_bs = cfg_cur.get("table_border_style", "grid")
            cfg_cur["table_border_style"] = st.selectbox("Table Border Style", b_styles, index=b_styles.index(cur_bs) if cur_bs in b_styles else 0, key="cfg_tb_bstyle")
            cfg_cur["table_border_color"] = st.color_picker("Border Color", cfg_cur.get("table_border_color", "#D1D5DB"), key="cfg_tb_bcolor")
            cfg_cur["table_border_width"] = st.slider("Border Thickness (pt)", 0.2, 2.0, float(cfg_cur.get("table_border_width", 0.5)), 0.1, key="cfg_tb_bwidth")
        with tb2:
            cfg_cur["table_cell_padding_v"] = st.slider("Cell Vertical Padding (pt)", 2, 14, int(cfg_cur.get("table_cell_padding_v", 5)), 1, key="cfg_tb_padv")
            cfg_cur["table_cell_padding_h"] = st.slider("Cell Horizontal Padding (pt)", 3, 18, int(cfg_cur.get("table_cell_padding_h", 7)), 1, key="cfg_tb_padh")
            cfg_cur["table_zebra_stripes"] = st.toggle("Alternating Row Stripes", value=cfg_cur.get("table_zebra_stripes", True), key="cfg_tb_stripes")
            cfg_cur["table_stripe_color"] = st.color_picker("Stripe Color", cfg_cur.get("table_stripe_color", "#F8FAFC"), key="cfg_tb_stripe_color")

    # ── 7. TEMPLATE MANAGEMENT
    with des_tab_tpl:
        st.markdown("### 💾 Template Management System")
        st.caption("Save, load, duplicate, delete, and export/import multiple report configurations.")

        tpl_c1, tpl_c2 = st.columns(2)
        with tpl_c1:
            st.markdown("##### 📂 Load or Manage Existing Template")
            all_saved = list_templates()
            if all_saved:
                sel_load = st.selectbox("Saved Templates", all_saved, key="tpl_mgr_select")
                t_btn1, t_btn2, t_btn3 = st.columns(3)
                with t_btn1:
                    if st.button("📂 Load", key="btn_tpl_load", type="primary", use_container_width=True):
                        ok, tpl_cfg, msg = load_template(sel_load)
                        if ok:
                            apply_loaded_template(tpl_cfg)
                            st.success(f"Loaded template: '{sel_load}'")
                            st.rerun()
                        else:
                            st.error(msg)
                with t_btn2:
                    dup_name = st.text_input("New Name for Duplicate", f"{sel_load}_copy", key="tpl_dup_name")
                    if st.button("📋 Duplicate", key="btn_tpl_dup", use_container_width=True):
                        ok, msg = duplicate_template(sel_load, dup_name)
                        if ok:
                            st.success(msg)
                            st.rerun()
                        else:
                            st.error(msg)
                with t_btn3:
                    if st.button("🗑️ Delete", key="btn_tpl_del", use_container_width=True):
                        ok, msg = delete_template(sel_load)
                        if ok:
                            st.warning(msg)
                            st.rerun()
                        else:
                            st.error(msg)
            else:
                st.caption("No templates found in templates/ directory.")

        with tpl_c2:
            st.markdown("##### 💾 Save Current Template")
            new_tpl_name = st.text_input("Template Name", "Corporate Template", key="tpl_new_name")
            if st.button("💾 Save Current Configuration", type="primary", key="btn_save_current_tpl", use_container_width=True):
                cur_full = get_current_full_config(st.session_state.df)
                ok, msg = save_template(new_tpl_name, cur_full)
                if ok:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)

            st.markdown("---")
            st.markdown("##### 📤 Import / Export JSON")
            cur_json = export_template_json(get_current_full_config())
            st.download_button(
                "⬇️ Export Active Template (JSON)",
                cur_json,
                f"{safe_filename(new_tpl_name)}.json",
                "application/json",
                key="btn_export_json",
                use_container_width=True
            )

            import_file = st.file_uploader("Upload Template JSON", type=["json"], key="tpl_import_up")
            if import_file:
                content_up = import_file.read().decode("utf-8")
                ok, imp_cfg, msg = import_template_json(content_up)
                if ok:
                    apply_loaded_template(imp_cfg)
                    st.success("Template imported successfully! Click Rerun to apply.")
                    st.rerun()
                else:
                    st.error(msg)

    # ── LIVE PREVIEW PANEL
    st.markdown("---")
    st.markdown("### 👁️ Live Report PDF Preview")
    st.caption("Instant real-time preview of the PDF rendered with your active formatting settings.")

    pv_col1, pv_col2 = st.columns([2, 1])
    with pv_col1:
        if st.button("🔄 Refresh Live Preview", type="primary", key="btn_refresh_preview"):
            st.rerun()
    with pv_col2:
        pass

    try:
        active_cfg = get_current_full_config(st.session_state.df)

        if st.session_state.df is not None and len(st.session_state.df) > 0:
            preview_row = st.session_state.df.iloc[0]
            preview_source_name = str(preview_row.get(st.session_state.pdf_name_col, "student_1"))
        else:
            preview_row = pd.Series({
                "Student Name": "Arjun Mehta",
                "Enrollment ID": "INN-2026-001",
                "Batch #": "Data Science Core",
                "Python Programming": "Excellent",
                "SQL Database Skill": "Good",
                "Communication & Soft Skills": "Excellent",
                "Python Prog (out of 100)": 92,
                "EDA Foundation (out of 100)": 88,
                "MySQL and PowerBI (out of 100)": 95,
                "Aptitude (out of 100)": 90,
                "Remarks": "Outstanding developer mindset with strong analytical foundations."
            })
            preview_source_name = "Sample_Preview"

        gc = get_groq_client(api_key) if enable_ai else None
        preview_pdf = build_single_report(preview_row, active_cfg, gc, st.session_state.logo_bytes)

        st.download_button(
            label=f"⬇️ Download Live Preview PDF ({preview_source_name}.pdf)",
            data=preview_pdf,
            file_name=f"{safe_filename(preview_source_name)}_preview.pdf",
            mime="application/pdf",
            key="btn_dl_live_prev",
        )

        b64_pdf = base64.b64encode(preview_pdf).decode("utf-8")
        st.markdown(
            f'<iframe src="data:application/pdf;base64,{b64_pdf}" width="100%" height="820" type="application/pdf" style="border:1px solid #CBD5E1;border-radius:8px;margin-top:10px;"></iframe>',
            unsafe_allow_html=True
        )
    except Exception as prev_err:
        st.error(f"❌ Preview generation failed: {prev_err}")
        import traceback
        st.code(traceback.format_exc())


# ══════════════════════════════════════════════════════════════
# TAB 4 — STEP 4: BATCH PDF GENERATION (UNCHANGED PRESERVED LOGIC)
# ══════════════════════════════════════════════════════════════
with tab_gen:
    st.markdown("## ⚡ Step 4: Batch PDF Generation")

    if st.session_state.df is None:
        st.info("📁 Upload learner data in the **📁 Step 1: Data Sources & Merger** tab first.")
    else:
        gen_df = st.session_state.df
        all_cols = gen_df.columns.tolist()

        active_cfg = get_current_full_config(gen_df)
        all_sections = active_cfg.get("sections", [])
        pdf_col = active_cfg.get("pdf_name_col", all_cols[0])

        _name_candidates = [c for c in all_cols if c.lower() == "student name"] or \
                           [c for c in all_cols if "student" in c.lower() and "name" in c.lower()] or \
                           [c for c in all_cols if "name" in c.lower()]
        name_col = _name_candidates[0] if _name_candidates else all_cols[0]

        ic1, ic2, ic3 = st.columns(3)
        ic1.metric("Total Learners", len(gen_df))
        ic2.metric("Report Sections", len([s for s in all_sections if s.get("enabled", True)]))
        ic3.metric("AI Feedback", "ON" if enable_ai else "OFF")

        enabled_names = " | ".join(s["name"] for s in all_sections if s.get("enabled", True)) or "⚠️ None"
        st.info(f"**Active Sections:** {enabled_names}  |  **PDF names from:** `{pdf_col}`")

        # ── SINGLE STUDENT ON-DEMAND GENERATION
        with st.expander("👁️ Generate / Preview Single Student Report", expanded=False):
            students = gen_df[name_col].astype(str).tolist()
            sel_s = st.selectbox("Select student:", students, key="g_prev_sel")
            p_idx = students.index(sel_s)

            if st.button("🔎 Generate Single Report", key="g_prev_btn"):
                with st.spinner("Building report PDF..."):
                    try:
                        active_cfg = get_current_full_config(gen_df)
                        gc = get_groq_client(api_key) if enable_ai else None
                        pb = build_single_report(gen_df.iloc[p_idx], active_cfg, gc, st.session_state.logo_bytes)
                        fname_val = str(gen_df.iloc[p_idx].get(pdf_col, sel_s))
                        st.success(f"✅ Report ready for **{sel_s}**")
                        st.download_button("⬇️ Download PDF", pb,
                                           f"{safe_filename(fname_val)}.pdf",
                                           "application/pdf", key="g_prev_dl")
                        sn, en, _ = get_student_identity_vals(gen_df.iloc[p_idx], active_cfg.get("identity_columns", []), fallback_name=sel_s)
                        log_audit_event("Downloaded (Preview)", active_cfg.get("report_title", "Evaluation Report"), sn, en, f"PREV_{safe_filename(fname_val)}")
                    except Exception as e:
                        st.error(f"❌ {e}")

        st.markdown("---")
        st.markdown(f"### 🚀 Generate Full Batch ({len(gen_df)} Learner Reports)")

        # Manual eligibility override panel
        _MI_OVERRIDE_LIMIT = 50
        if st.session_state.enable_mock_interview:
            with st.expander("🎯 Mock Interview Eligibility — Manual Override", expanded=False):
                if len(gen_df) > _MI_OVERRIDE_LIMIT:
                    st.warning(f"⚠️ Manual override disabled for large batches (> {_MI_OVERRIDE_LIMIT} rows). Auto-computed eligibility will be used.")
                else:
                    st.caption(f"Eligibility is auto-computed against the shortlist benchmark ({st.session_state.benchmark_shortlist}%). You may manually override below:")
                    _new_overrides = {}
                    for _oi, (_, _orow) in enumerate(gen_df.iterrows()):
                        _oname_raw = _orow.get(name_col, f"Row_{_oi}")
                        _oname = str(_oname_raw).strip()
                        _okey  = f"{_oi}__{_oname}"
                        _auto_label, _auto_short, _auto_score = compute_mock_interview_eligibility(
                            _orow, all_sections, gen_df, active_cfg
                        )
                        _auto_chip = "✅ Shortlisted" if _auto_short else f"❌ {MOCK_LABEL_REVISION}"
                        _cur_override = st.session_state.mi_eligibility_overrides.get(_okey, _auto_short)

                        _rc1, _rc2, _rc3 = st.columns([3, 2, 1])
                        with _rc1:
                            st.markdown(f"`{_oname}`")
                        with _rc2:
                            st.markdown(f"<span style='color:{'#16a34a' if _auto_short else '#dc2626'};font-size:0.82rem;'>{_auto_chip} ({_auto_score}%)</span>", unsafe_allow_html=True)
                        with _rc3:
                            _toggled = st.toggle("Shortlisted", value=_cur_override, key=f"mi_override_{_oi}", label_visibility="collapsed")
                            _new_overrides[_okey] = _toggled

                    st.session_state.mi_eligibility_overrides = _new_overrides
                    if st.button("🔄 Reset Overrides to Auto-computed", key="btn_reset_mi"):
                        st.session_state.mi_eligibility_overrides = {}
                        st.rerun()

        is_dup, dup_record = check_duplicate(gen_df, active_cfg.get("report_title", "Evaluation Report"))
        if is_dup:
            st.info(f"ℹ️ This dataset was previously generated on `{dup_record['generated_at']}` for **{dup_record['student_count']}** students.")

        go = st.button("🚀 Run Batch Generation", key="g_gen_btn", type="primary")

        if go:
            p_bar = st.progress(0)
            status_text = st.empty()
            gc = get_groq_client(api_key) if enable_ai else None
            active_cfg = get_current_full_config(gen_df)

            def on_progress(done, total):
                p_bar.progress(done / total)
                status_text.text(f"Generating reports: {done}/{total}")

            try:
                zip_bytes = build_all_reports(
                    gen_df, active_cfg, gc,
                    logo_bytes=st.session_state.logo_bytes,
                    id_column=pdf_col,
                    progress_callback=on_progress
                )
                st.session_state.last_zip = zip_bytes
                st.session_state.last_zip_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                record_generation(gen_df, active_cfg.get("report_title", "Evaluation Report"), len(gen_df), len(gen_df))
                log_audit_event("Generated (Batch)", active_cfg.get("report_title", "Evaluation Report"), f"{len(gen_df)} Students", "Batch", f"BATCH_{st.session_state.last_zip_ts}")

                res_list = []
                for _, r in gen_df.iterrows():
                    s_name = str(r.get(name_col, "Student"))
                    f_name = safe_filename(str(r.get(pdf_col, s_name))) + ".pdf"
                    grd, sc_pct = compute_grade(r, all_sections, gen_df)
                    res_list.append({"Student": s_name, "File": f_name, "Result": "Success", "Grade": grd, "Score": f"{sc_pct:.1f}%"})
                st.session_state.gen_results = res_list

                st.success(f"🎉 Batch generation completed! Generated {len(gen_df)} reports.")
            except Exception as b_err:
                st.error(f"Batch generation failed: {b_err}")
                import traceback
                st.code(traceback.format_exc())

        if st.session_state.last_zip is not None:
            def _on_batch_zip_download():
                log_audit_event(
                    "Downloaded (Batch ZIP)",
                    active_cfg.get("report_title", "Evaluation Report"),
                    f"{len(gen_df)} Students",
                    "Batch",
                    f"BATCH_{st.session_state.last_zip_ts}.zip"
                )

            st.download_button(
                "📦 Download All Reports (ZIP Archive)",
                data=st.session_state.last_zip,
                file_name=f"reports_{st.session_state.last_zip_ts}.zip",
                mime="application/zip",
                key="btn_dl_batch_zip",
                type="primary",
                on_click=_on_batch_zip_download
            )


# ══════════════════════════════════════════════════════════════
# TAB 5 — STEP 5: COHORT ANALYTICS (PRESERVED LOGIC)
# ══════════════════════════════════════════════════════════════
with tab_cohort:
    st.markdown("## 📈 Step 5: Cohort Analytics Dashboard")

    if st.session_state.df is None:
        st.info("📁 Upload learner data in the **📁 Step 1: Data Sources & Merger** tab first.")
    else:
        cohort_df = st.session_state.df.copy()
        cols = cohort_df.columns.tolist()

        name_candidates = [c for c in cols if c.lower() == "student name"] or \
                           [c for c in cols if "student" in c.lower() and "name" in c.lower()] or \
                           [c for c in cols if "name" in c.lower()]
        name_col = name_candidates[0] if name_candidates else cols[0]

        batch_candidates = [c for c in cols if c.lower() in ("batch", "cohort", "batch no", "batch no.")] or \
                            [c for c in cols if "batch" in c.lower() or "cohort" in c.lower()]
        batch_col = batch_candidates[0] if batch_candidates else None

        active_cfg = get_current_full_config(cohort_df)
        all_sections = active_cfg.get("sections", [])

        computed_list = []
        for ri, row in cohort_df.iterrows():
            scores = []
            for sec in all_sections:
                if sec.get("type") != "rating_table":
                    continue
                for col in sec.get("columns", []):
                    if col in row.index:
                        scores.append(to_numeric_rating(str(row[col])))

            marks_total_scored = 0.0
            marks_total_max = 0.0
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
                            marks_total_max += max_s
                        except Exception:
                            pass

            if marks_total_max > 0:
                overall_pct = (marks_total_scored / marks_total_max) * 100
            elif scores:
                overall_pct = (sum(scores) / len(scores)) / 4.0 * 100
            else:
                overall_pct = 0.0

            def get_grade_and_verdict(sc_pct):
                sc = sc_pct / 25.0
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

        st.markdown("### 🔍 Interactive Filters")
        fc1, fc2, fc3 = st.columns([1.5, 1.5, 2])
        with fc1:
            batches = ["All Batches"] + sorted(comp_df["Batch"].unique().tolist())
            sel_batch = st.selectbox("Cohort / Batch Group", batches, index=0, key="co_sel_batch")
        with fc2:
            grade_tiers = ["All Grades", "Mastery (A/A+)", "Passing (B)", "Focus Required (C/D)"]
            sel_tier = st.selectbox("Performance Tier", grade_tiers, index=0, key="co_sel_tier")
        with fc3:
            search_query = st.text_input("🔍 Search Candidate Name", placeholder="Type name...", key="co_search")

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

        total_students = len(filtered_df)
        if total_students > 0:
            avg_score = round(filtered_df["Score (%)"].mean(), 1)
            pass_threshold = active_cfg.get("marks_pass_threshold", 75)
            passing_students = len(filtered_df[filtered_df["Score (%)"] >= pass_threshold])
            pass_rate = round((passing_students / total_students) * 100, 1)
            mastery_students = len(filtered_df[filtered_df["Grade"].isin(["A+", "A"])])
            mastery_rate = round((mastery_students / total_students) * 100, 1)
        else:
            avg_score, pass_rate, mastery_rate = 0.0, 0.0, 0.0

        st.markdown(f"""
        <div class="stat-grid">
          <div class="stat-card"><div class="val">{total_students}</div><div class="lbl">Filtered Cohort Size</div></div>
          <div class="stat-card"><div class="val">{avg_score}%</div><div class="lbl">Average Performance</div></div>
          <div class="stat-card"><div class="val" style="color:#059669">{pass_rate}%</div><div class="lbl">Passing Rate (Threshold: {active_cfg.get('marks_pass_threshold', 75)}%)</div></div>
          <div class="stat-card"><div class="val" style="color:#2563EB">{mastery_rate}%</div><div class="lbl">Mastery Level (Grade A/A+)</div></div>
        </div>
        """, unsafe_allow_html=True)
        st.markdown("<br/>", unsafe_allow_html=True)

        if total_students > 0:
            cc1, cc2 = st.columns(2)
            with cc1:
                st.markdown("#### 📊 Grade Distribution")
                grade_counts = filtered_df["Grade"].value_counts().reindex(["A+", "A", "B", "C", "D"]).fillna(0)
                st.bar_chart(grade_counts, color="#2563EB")
            with cc2:
                st.markdown("#### 📈 Average Ratings / Marks per Topic")
                topic_averages = {}
                for sec in all_sections:
                    if sec.get("type") in ("rating_table", "marks_table"):
                        for col in sec.get("columns", []):
                            if col in cohort_df.columns:
                                student_indices = filtered_df["RowIndex"].tolist()
                                data_slice = cohort_df.loc[cohort_df.index.intersection(student_indices), col]
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
                    st.bar_chart(pd.Series(topic_averages), color="#16A34A")

            st.markdown("---")
            st.markdown("### 📋 Student Cohort Performance Roster")
            st.dataframe(filtered_df[["Student", "Batch", "Score (%)", "Grade", "Verdict"]], width='stretch', height=280)


# ══════════════════════════════════════════════════════════════
# TAB 6 — STEP 6: GENERATION HISTORY
# ══════════════════════════════════════════════════════════════
with tab_hist:
    st.markdown("## 🗂️ Step 6: Results & Generation History")
    history_records = get_all_history()

    if not history_records:
        st.info("No batch generation history recorded yet. Use the **⚡ Step 4: Batch PDF Generation** tab to create reports.")
    else:
        st.markdown(f"Displaying **{len(history_records)}** historical batch generation runs:")
        h_df = pd.DataFrame(history_records)
        st.dataframe(h_df, width='stretch')

    if st.session_state.gen_results:
        st.markdown("---")
        st.markdown("### 📥 Active Session Reports")
        st.dataframe(pd.DataFrame(st.session_state.gen_results), width='stretch', height=240)


# ══════════════════════════════════════════════════════════════
# TAB 7 — STEP 7: AUDIT & COMPLIANCE
# ══════════════════════════════════════════════════════════════
with tab_audit:
    st.markdown("## 🔍 Step 7: Audit & Accountability Dashboard")
    st.caption("Live security logs, data download accountability tracker, and mock interview status pipeline.")

    audit_master_df = get_deduplicated_audit_dataframe()
    curr_file = st.session_state.get("source_file", "N/A")

    col_hdr1, col_hdr2, col_hdr3 = st.columns([2, 1, 1])
    with col_hdr1:
        st.markdown(f"📂 Current Cohort Dataset: **{curr_file}**")
    with col_hdr2:
        show_all = st.checkbox("Show all historical logs", value=False, key="audit_show_all_logs")
    with col_hdr3:
        if st.button("🗑️ Clear Audit History", key="audit_top_clear_btn", type="secondary"):
            clear_audit_log()
            st.success("✅ Audit logs successfully cleared!")
            st.rerun()

    if not show_all and curr_file != "N/A":
        if "Source File" in audit_master_df.columns:
            audit_df = audit_master_df[audit_master_df["Source File"] == curr_file]
        else:
            audit_df = audit_master_df
    else:
        audit_df = audit_master_df

    if audit_df.empty:
        st.info("💡 **No active records found for the currently active dataset.** Generate reports first to track audit events.")
    else:
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.metric("Total Events Logged", f"{len(audit_df)}")
        with c2:
            st.metric("Unique Students Tracked", f"{len(audit_df['Student Name'].unique())}")
        with c3:
            st.metric("Generated Events", f"{len(audit_df[audit_df['Action'].str.contains('Generated', na=False)])}")
        with c4:
            st.metric("Download Events", f"{len(audit_df[audit_df['Action'].str.contains('Downloaded', na=False)])}")

        st.markdown("---")
        st.markdown("### 🗄️ Full Searchable Audit Logs")
        st.dataframe(audit_df, width='stretch', height=350)

        st.download_button(
            "📋 Export Filtered Audit Logs CSV",
            audit_df.to_csv(index=False).encode("utf-8"),
            f"audit_logs_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
            "text/csv",
            key="audit_export_filtered",
        )

# ─────────────────────────────────────────────────────────────
# FOOTER
# ─────────────────────────────────────────────────────────────
st.markdown("""
<div style='text-align:center; color: var(--text-sec); font-size:.75rem; margin-top:3rem;
            padding:1.4rem 0 0.8rem; border-top:1.5px solid var(--border);'>
    <span style="font-size:1rem; vertical-align:middle; margin-right:4px;">🎓</span>
    <strong style="color: var(--primary); font-family: 'Plus Jakarta Sans', sans-serif;">Innomatics Research Labs – Operations Report Generator &amp; Custom Designer</strong>
    &nbsp;&middot;&nbsp; Automated Student Assessment Reports &middot; Enterprise Report Engine
</div>
""", unsafe_allow_html=True)