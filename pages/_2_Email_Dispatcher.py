"""
=============================================================================
EMAIL DISPATCHER PRO (DETACHED / STASHED FEATURE)
=============================================================================
STATUS: CURRENTLY DETACHED FROM APPLICATION
-----------------------------------------------------------------------------
This file contains the complete, working implementation of Email Dispatcher Pro.
It is detached from Streamlit application routing by prefixing the filename
with an underscore `_` (Streamlit standard for hidden/ignored page files).

HOW TO RE-ATTACH THIS FEATURE TO THE APPLICATION LATER:
1. Rename this file:
     pages/_2_Email_Dispatcher.py  -->  pages/2_Email_Dispatcher.py
2. In `streamlit_app.py`, re-enable the navigation button around line 3192.
=============================================================================
Feature Capabilities:
  - Persistent sidebar wizard navigation (7 workflow sub-steps)
  - Manual email entry + CSV + direct PDF upload
  - Dual-body email composer (With PDF / Without PDF)
  - Live recipient preview panel
  - Encrypted SMTP credential storage (Fernet AES)
  - Launch Campaign screen with audit metrics
  - Dispatch state machine (Pause / Resume / Cancel)
  - Analytics charts and dispatch logs
=============================================================================
"""

import io
import os
import re
import json
import time
import copy
import base64
import zipfile
from datetime import datetime

import numpy as np
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from email_sender import (
    validate_email, find_pdf_in_folder, find_pdf_in_zip_bytes,
    markdown_to_html, render_jinja, build_html_email,
    html_to_plain, connect_smtp, send_one,
    save_dispatch_log, SMTP_PROVIDERS, BLOCK_DEFAULTS, LOG_DIR,
)
from smtp_store import (
    save_credentials, load_credentials,
    clear_credentials, is_credentials_saved,
    get_backend_name, _is_streamlit_cloud, _secrets_available,
)

# ─────────────────────────────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Innomatics Research Labs – Operations & Dispatch Report Manager — Email Dispatcher",
    page_icon="📧",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────────────────────────────
# SESSION STATE DEFAULTS
# ─────────────────────────────────────────────────────────────────────
def _ss(k, v):
    if k not in st.session_state:
        st.session_state[k] = v

# Data
_ss("ed_df",              None)
_ss("ed_pdf_files",       {})      # {filename: bytes} uploaded directly
_ss("ed_zip_bytes",       None)
_ss("ed_folder_path",     "")
_ss("ed_email_col",       "")
_ss("ed_name_col",        "")
_ss("ed_id_col",          "")
_ss("ed_pdf_source",      "None (Email Only)")

# Compose — standard (with PDF)
_ss("ed_subject",         "Important Update — {{ Name }}")
_ss("ed_body",            "Dear {{ Name }},\n\nPlease find your document attached.\n\nBest regards")
_ss("ed_body_mode",       "Markdown")   # Markdown | HTML

# Compose — alternate (no PDF)
_ss("ed_subject_nopdf",   "Important Update — {{ Name }}")
_ss("ed_body_nopdf",      "Dear {{ Name }},\n\nWe are writing to inform you about your assessment.\n\nBest regards")

# Style
_ss("ed_bg_color",        "#F1F5F9")
_ss("ed_content_bg",      "#FFFFFF")
_ss("ed_font_family",     "'Inter', Arial, sans-serif")
_ss("ed_signature",       "")
_ss("ed_template_style",  "Simple Email (Standard)")

# SMTP (session only — persisted version loaded from smtp_store)
_ss("smtp_email",         "")
_ss("smtp_pass",          "")
_ss("smtp_provider",      "Gmail")
_ss("smtp_host",          "smtp.gmail.com")
_ss("smtp_port",          587)
_ss("smtp_from_name",     "")
_ss("smtp_reply_to",      "")
_ss("smtp_cc",            "")
_ss("smtp_bcc",           "")
_ss("smtp_cc_col",        "")
_ss("smtp_rate",          0.5)
_ss("smtp_batch_size",    0)
_ss("smtp_batch_pause",   10)
_ss("smtp_retries",       3)
_ss("smtp_creds_locked",  False)

# Dispatch state machine
_ss("dispatch_active",    False)
_ss("dispatch_paused",    False)
_ss("dispatch_index",     0)
_ss("dispatch_queue",     [])
_ss("dispatch_results_temp", [])
_ss("dispatch_results",   [])
_ss("dispatch_srv",       None)

# Navigation
_ss("ed_nav_page",        "📋 Recipients")

# ── Auto-load saved credentials on first run ──────────────────────────
if not st.session_state.smtp_creds_locked:
    _saved = load_credentials()
    if _saved:
        st.session_state.smtp_email     = _saved.get("email", "")
        st.session_state.smtp_pass      = _saved.get("password", "")
        st.session_state.smtp_from_name = _saved.get("from_name", "")
        st.session_state.smtp_reply_to  = _saved.get("reply_to", "")
        st.session_state.smtp_host      = _saved.get("host", "smtp.gmail.com")
        st.session_state.smtp_port      = _saved.get("port", 587)
        st.session_state.smtp_provider  = _saved.get("provider", "Gmail")
        st.session_state.smtp_creds_locked = True

# ─────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────
def _guess_col(cols, keywords):
    for kw in keywords:
        for c in cols:
            if kw in c.lower():
                return c
    return cols[0] if cols else ""

def _sanitize_cols(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [re.sub(r'\W+', '_', str(c)).strip('_') for c in df.columns]
    return df

def _nan_clean(row_dict: dict) -> dict:
    out = {}
    for k, v in row_dict.items():
        try:
            out[k] = "" if (v is None or (isinstance(v, float) and np.isnan(v))) else str(v)
        except Exception:
            out[k] = str(v) if v is not None else ""
    return out

def _body_to_blocks(body_text: str, mode: str) -> list:
    """Convert textarea body into a single text block for build_html_email."""
    return [{"type": "text", "content": body_text, "font_size": "15px"}]

def _build_preview_html(body: str, mode: str, row_dict: dict) -> str:
    """Render a preview HTML email from body text."""
    rendered = render_jinja(body, row_dict)
    blocks = [{"type": "text", "content": rendered, "font_size": "15px"}]
    if st.session_state.ed_signature:
        blocks.append({"type": "footer", "content": st.session_state.ed_signature, "font_size": "12px", "text_color": "#64748B"})
    return build_html_email(
        blocks, row_dict,
        bg_color=st.session_state.ed_bg_color,
        content_bg=st.session_state.ed_content_bg,
        font_family=st.session_state.ed_font_family,
        template_style=st.session_state.get("ed_template_style", "Simple Email (Standard)"),
    )

def _get_sample_row() -> dict:
    df = st.session_state.ed_df
    if df is not None and len(df) > 0:
        return _nan_clean(df.iloc[0].to_dict())
    return {"Name": "John Doe", "Email": "john@example.com", "Grade": "A", "Score": "92"}

def _merge_manual_emails(raw_text: str):
    """Parse manual email entries and merge into ed_df."""
    entries = re.split(r'[,;\n]+', raw_text)
    rows = []
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        # Support: "Jane Doe <jane@example.com>" or plain email
        m = re.match(r'^(.*?)\s*<([^>]+)>', entry)
        if m:
            name, email = m.group(1).strip(), m.group(2).strip()
        else:
            name, email = entry, entry
        if validate_email(email):
            rows.append({"Name": name, "Email": email})
    if rows:
        new_df = pd.DataFrame(rows)
        if st.session_state.ed_df is not None:
            st.session_state.ed_df = pd.concat(
                [st.session_state.ed_df, new_df], ignore_index=True
            ).drop_duplicates(subset=["Email"])
        else:
            st.session_state.ed_df = new_df
        # Auto-detect columns
        if not st.session_state.ed_email_col:
            st.session_state.ed_email_col = "Email"
        if not st.session_state.ed_name_col:
            st.session_state.ed_name_col = "Name"
    return len(rows)

# ─────────────────────────────────────────────────────────────────────
# GLOBAL CSS
# ─────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800;900&family=Inter:wght@300;400;500;600;700;800;900&family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap');

html, body, h1, h2, h3, h4, h5, h6, p, li, input, textarea, select {
    font-family: 'Outfit', 'Plus Jakarta Sans', 'Inter', -apple-system, sans-serif;
    font-size: 14.5px !important;
}
[class^="stMarkdown"], [class^="stText"], [class^="stCaption"] {
    font-family: 'Outfit', 'Plus Jakarta Sans', 'Inter', sans-serif;
}

/* Base elements */
p, li, span, label, input, select, textarea {
    font-family: 'Outfit', 'Plus Jakarta Sans', 'Inter', sans-serif !important;
    font-size: 13.8px !important;
}

/* ─ Custom Scrollbar ─────────────────────────────────── */
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: #F4F6FA !important; }
::-webkit-scrollbar-thumb {
    background: #B8C4D9 !important;
    border-radius: 9999px !important;
    border: 2.3px solid #F4F6FA !important;
}
::-webkit-scrollbar-thumb:hover { background: #1E4D99 !important; }

/* ── Sidebar brand ── */
.sidebar-brand {
    display:flex; align-items:center; gap:10px;
    padding:10px 0 18px; border-bottom:1px solid #E2E8F0;
    margin-bottom:12px;
}
.sidebar-brand-icon { font-size:1.6rem; }
.sidebar-brand-text { font-size:1.15rem; font-weight:800; color:#1B3A6B; letter-spacing:-0.3px; }

/* ── User footer ── */
.sidebar-user {
    display:flex; align-items:center; gap:10px;
    padding:12px 0 4px; border-top:1px solid #E2E8F0;
    margin-top:8px;
}
.sidebar-avatar {
    width:36px; height:36px; border-radius:50%;
    background:linear-gradient(135deg,#6366F1,#2563EB);
    display:flex; align-items:center; justify-content:center;
    font-size:0.9rem; font-weight:700; color:#fff;
}
.sidebar-user-info { flex:1; }
.sidebar-user-name { font-size:0.88rem; font-weight:700; color:#1E293B; }
.sidebar-user-role { font-size:0.75rem; color:#64748B; }

/* ── Page heading ── */
.page-heading { margin-bottom:4px; font-size:1.6rem; font-weight:800; color:#0D1117; letter-spacing:-0.5px; }
.page-subheading { font-size:0.90rem; color:#64748B; margin-bottom:20px; }

/* ── Recipient cards ── */
.rcp-card { background:#FFFFFF; border:1.5px solid #E2E8F0; border-radius:14px; padding:20px; box-shadow:0 2px 8px rgba(0,0,0,0.04); min-height:180px; }
.rcp-card-title { font-size:0.95rem; font-weight:700; color:#1E293B; margin-bottom:6px; display:flex; align-items:center; gap:8px; }
.rcp-card-badge { font-size:0.70rem; font-weight:700; padding:2px 8px; border-radius:99px; background:#F1F5F9; color:#64748B; }

/* ── Email composer ── */
.composer-shell { background:#FFFFFF; border:1.5px solid #E2E8F0; border-radius:14px; overflow:hidden; box-shadow:0 2px 12px rgba(0,0,0,0.06); }
.composer-bar { display:flex; align-items:center; justify-content:space-between; padding:12px 18px; background:#F8FAFC; border-bottom:1.5px solid #E2E8F0; }
.composer-bar-label { font-size:0.8rem; font-weight:800; letter-spacing:1px; color:#64748B; text-transform:uppercase; }
.composer-bar-mode { font-size:0.8rem; font-weight:700; color:#2563EB; padding:4px 12px; border-radius:6px; background:#EFF6FF; border:1px solid #BFDBFE; }
.composer-field { display:flex; align-items:center; gap:0; padding:10px 18px; border-bottom:1px solid #F1F5F9; }
.composer-field-label { font-size:0.82rem; color:#94A3B8; font-weight:600; width:65px; flex-shrink:0; }
.composer-field-value { font-size:0.88rem; color:#334155; font-weight:500; }
.composer-field-value-blue { color:#2563EB; }

/* ── Preview card ── */
.preview-shell { background:#FFFFFF; border:1.5px solid #E2E8F0; border-radius:14px; overflow:hidden; box-shadow:0 2px 12px rgba(0,0,0,0.06); }
.preview-bar { display:flex; align-items:center; gap:8px; padding:12px 16px; background:#F8FAFC; border-bottom:1.5px solid #E2E8F0; font-size:0.88rem; font-weight:700; color:#1E293B; }
.preview-field { font-size:0.88rem; padding:8px 16px; border-bottom:1px solid #F1F5F9; color:#334155; }
.preview-field span { color:#94A3B8; width:52px; display:inline-block; font-weight:600; }

/* ── Settings cards ── */
.settings-card { background:#FFFFFF; border:1.5px solid #E2E8F0; border-radius:14px; padding:20px; box-shadow:0 2px 8px rgba(0,0,0,0.04); margin-bottom:16px; }
.settings-card-title { font-size:0.95rem; font-weight:700; color:#1E293B; margin-bottom:14px; display:flex; align-items:center; gap:8px; }
.creds-locked-box { background:#F0FDF4; border:1.5px solid #86EFAC; border-radius:10px; padding:12px 16px; font-size:0.85rem; color:#166534; font-weight:600; display:flex; align-items:center; gap:8px; margin-bottom:10px; }
.creds-locked-sub { font-size:0.78rem; color:#4ADE80; font-weight:400; }
.encrypted-note { font-size:0.80rem; color:#94A3B8; margin-bottom:14px; }

/* ── Metric cards ── */
.audit-metric { background:#FFFFFF; border:1.5px solid #E2E8F0; border-radius:12px; padding:18px 20px; box-shadow:0 1px 4px rgba(0,0,0,0.04); text-align:center; }
.audit-metric-label { font-size:0.82rem; font-weight:700; letter-spacing:0.8px; text-transform:uppercase; color:#94A3B8; margin-bottom:6px; }
.audit-metric-value { font-size:2.2rem; font-weight:800; color:#0D1117; }
.audit-metric-value.green { color:#16A34A; }
.audit-metric-value.purple { color:#7C3AED; }
.audit-metric-value.orange { color:#EA580C; }

/* ── Metric override ── */
[data-testid="metric-container"] { background:#FFFFFF; border:1.5px solid #E2E8F0; border-radius:10px; padding:14px !important; }

/* ── Token chip ── */
.token-chip { display:inline-block; padding:3px 10px; margin:3px; background:#EFF6FF; border:1px solid #BFDBFE; border-radius:6px; font-size:0.8rem; font-weight:600; color:#1D4ED8; font-family:monospace; cursor:pointer; }

/* ── Nav active badge ── */
[data-testid="stSidebarNavItems"] { display:none; }

/* ── Premium Custom Radio Navigation ── */
div[data-testid="stRadio"] { padding: 8px 0 !important; }
div[data-testid="stRadio"] > label { display: none !important; }
div[data-testid="stRadio"] [role="radiogroup"] { gap: 6px !important; }
div[data-testid="stRadio"] label {
    background: #F8FAFC !important; border: 1px solid #E2E8F0 !important; border-radius: 10px !important;
    padding: 10px 14px !important; margin: 2px 10px !important;
    transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1) !important;
    cursor: pointer !important; display: flex !important; align-items: center !important;
    box-shadow: 0 1px 3px rgba(15, 23, 42, 0.02) !important;
}
div[data-testid="stRadio"] label:hover {
    background: #EFF6FF !important; border-color: #BFDBFE !important;
    transform: translateX(4px); box-shadow: 0 4px 12px rgba(15, 23, 42, 0.04) !important;
}
div[data-testid="stRadio"] label > div:first-child { display: none !important; }
div[data-testid="stRadio"] label:has(input:checked) {
    background: linear-gradient(135deg, #1B3A6B 0%, #1E4D99 100%) !important;
    border-color: #1B3A6B !important; box-shadow: 0 8px 20px rgba(27, 58, 107, 0.22) !important;
    transform: translateX(6px);
}
div[data-testid="stRadio"] label:has(input:checked) div[data-testid="stMarkdownContainer"] p {
    color: #FFFFFF !important; font-weight: 700 !important;
}
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("""
    <div class="sidebar-brand">
      <span class="sidebar-brand-icon">🎓</span>
      <span class="sidebar-brand-text">Innomatics Operations</span>
    </div>
    """, unsafe_allow_html=True)

    nav_pages = [
        "👤 Admin Panel",
        "📋 Recipients",
        "📝 Template",
        "⚙️  Settings",
        "🚀 Dispatch",
        "📜 History",
        "📊 Analytics",
    ]
    page = st.radio("", nav_pages, key="ed_nav_page", label_visibility="collapsed")

    df_cnt = len(st.session_state.ed_df) if st.session_state.ed_df is not None else 0
    st.caption(f"Recipients loaded: **{df_cnt}**")

    sender_label = st.session_state.smtp_email[:20] + "…" if len(st.session_state.smtp_email) > 20 else st.session_state.smtp_email or "Not configured"
    initials = (st.session_state.smtp_from_name or "A")[:1].upper()
    st.markdown(f"""
    <div class="sidebar-user">
      <div class="sidebar-avatar">{initials}</div>
      <div class="sidebar-user-info">
        <div class="sidebar-user-name">{sender_label}</div>
        <div class="sidebar-user-role">{"Sender" if st.session_state.smtp_email else "Admin"}</div>
      </div>
    </div>
    """, unsafe_allow_html=True)
    if st.button("↩ Sign Out / Clear Credentials", key="btn_signout", use_container_width=True):
        clear_credentials()
        st.session_state.smtp_email = ""
        st.session_state.smtp_pass  = ""
        st.session_state.smtp_creds_locked = False
        st.success("Credentials cleared.")
        st.rerun()

    st.page_link("streamlit_app.py", label="← Back to Report Generator", icon="🏠")

# ══════════════════════════════════════════════════════════════════════
# PAGE: ADMIN PANEL
# ══════════════════════════════════════════════════════════════════════
if page == "👤 Admin Panel":
    st.markdown('<div class="page-heading">Admin Panel</div>', unsafe_allow_html=True)
    st.markdown('<div class="page-subheading">Campaign overview and quick-start checklist</div>', unsafe_allow_html=True)

    df_loaded  = st.session_state.ed_df is not None
    smtp_ok    = bool(st.session_state.smtp_email and st.session_state.smtp_pass)
    body_ok    = bool(st.session_state.ed_body.strip())
    zip_loaded = st.session_state.ed_zip_bytes is not None

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Recipients",   len(st.session_state.ed_df) if df_loaded else 0)
    c2.metric("SMTP Ready",   "✅ Yes" if smtp_ok else "❌ No")
    c3.metric("Template",     "✅ Set" if body_ok else "⬜ Empty")
    c4.metric("Last Dispatch", len(st.session_state.dispatch_results))

    st.markdown("### ✅ Quick-Start Checklist")
    checklist = [
        (df_loaded, "Recipients loaded",      "Go to **📋 Recipients**"),
        (smtp_ok,   "SMTP credentials saved", "Go to **⚙️ Settings**"),
        (body_ok,   "Email template written", "Go to **📝 Template**"),
    ]
    for ok, label, action in checklist:
        icon = "✅" if ok else "⬜"
        note = "" if ok else f" — {action}"
        st.markdown(f"{icon} **{label}**{note}")

    st.info(
        "📦 **Report Dispatch** — Generate reports in the main app first, then come here to dispatch them.\n\n"
        "✉️ **Custom Dispatch** — Upload any CSV + PDFs to send emails for any purpose."
    )

    with st.expander("📖 How to get a Gmail App Password", expanded=False):
        st.markdown("""
1. Go to **Google Account** → **Security** → **2-Step Verification** (enable if needed)
2. Search for **App passwords** at the top of the Google Account page
3. Select app: **Mail** | Device: **Windows Computer** (or Other)
4. Click **Generate** — copy the **16-character password**
5. Paste it (no spaces) in the **⚙️ Settings** page
> ⚠️ This is NOT your regular Gmail password. It is a special one-time app password.
        """)

# ══════════════════════════════════════════════════════════════════════
# PAGE: RECIPIENTS
# ══════════════════════════════════════════════════════════════════════
elif page == "📋 Recipients":
    st.markdown('<div class="page-heading">Recipients</div>', unsafe_allow_html=True)
    st.markdown('<div class="page-subheading">Upload your CSV or Excel file with recipient data</div>', unsafe_allow_html=True)

    col_csv, col_manual, col_pdf = st.columns(3)

    with col_csv:
        with st.container(border=True):
            st.image("logo/spreadsheet_icon.png", width=75)
            st.markdown('<div class="rcp-card-title">📊 Spreadsheet Upload</div>', unsafe_allow_html=True)
            up_csv = st.file_uploader(
                "Drop CSV or Excel file", type=["csv", "xlsx", "xls"],
                key="csv_up", label_visibility="collapsed",
                help="CSV or Excel file with Email, Name, and optionally an ID column"
            )
            if up_csv is not None:
                try:
                    raw = pd.read_csv(up_csv) if up_csv.name.lower().endswith(".csv") else pd.read_excel(up_csv)
                    raw = _sanitize_cols(raw)
                    for c in raw.columns:
                        if "name" in c.lower():
                            raw[c] = raw[c].astype(str).apply(lambda x: x.title() if x.lower() not in ("nan","none") else x)
                    st.session_state.ed_df = raw
                    cols = list(raw.columns)
                    st.session_state.ed_email_col = _guess_col(cols, ["email","mail"])
                    st.session_state.ed_name_col  = _guess_col(cols, ["name","student"])
                    st.session_state.ed_id_col    = _guess_col(cols, ["id","enrollment","roll","usn"])
                    st.success(f"✅ Loaded {len(raw)} rows")
                except Exception as e:
                    st.error(f"❌ {e}")

    with col_manual:
        with st.container(border=True):
            st.image("logo/email_icon.png", width=75)
            st.markdown(
                '<div class="rcp-card-title">✉️ Manual Email Entry '
                '<span class="rcp-card-badge">optional</span></div>',
                unsafe_allow_html=True
            )
            manual_txt = st.text_area(
                "Emails (comma, semicolon or newline separated)",
                placeholder="john@example.com\nJane Doe <jane@example.com>;",
                height=100, key="manual_emails_in", label_visibility="collapsed"
            )
            if st.button("➕ Add to Recipients List", key="btn_add_manual", type="primary", use_container_width=True):
                if manual_txt.strip():
                    added = _merge_manual_emails(manual_txt)
                    if added:
                        st.success(f"✅ Added {added} recipient(s)")
                    else:
                        st.warning("No valid emails found.")
                else:
                    st.warning("Enter at least one email address.")

    with col_pdf:
        with st.container(border=True):
            st.image("logo/pdf_icon.png", width=75)
            st.markdown(
                '<div class="rcp-card-title">📎 PDF Attachments '
                '<span class="rcp-card-badge">optional</span></div>',
                unsafe_allow_html=True
            )
            pdf_src = st.radio(
                "Source", ["None (Email Only)", "Upload PDFs", "Upload ZIP", "Local Folder"],
                key="pdf_src_radio", label_visibility="collapsed"
            )
            st.session_state.ed_pdf_source = pdf_src

            if pdf_src == "Upload PDFs":
                up_pdfs = st.file_uploader(
                    "Upload PDF files", type=["pdf"], accept_multiple_files=True,
                    key="pdf_up_direct", label_visibility="collapsed"
                )
                if up_pdfs:
                    for f in up_pdfs:
                        st.session_state.ed_pdf_files[f.name] = f.getvalue()
                    st.success(f"✅ {len(st.session_state.ed_pdf_files)} PDF(s) loaded")
            elif pdf_src == "Upload ZIP":
                up_zip = st.file_uploader("Upload PDF ZIP", type=["zip"], key="zip_up", label_visibility="collapsed")
                if up_zip:
                    st.session_state.ed_zip_bytes = up_zip.getvalue()
                    try:
                        with zipfile.ZipFile(io.BytesIO(up_zip.getvalue())) as zf:
                            n = sum(1 for x in zf.namelist() if x.lower().endswith(".pdf"))
                        st.success(f"✅ ZIP: {n} PDFs")
                    except Exception as e:
                        st.error(f"❌ {e}")
            elif pdf_src == "Local Folder":
                st.session_state.ed_folder_path = st.text_input(
                    "Folder path", value=st.session_state.ed_folder_path, key="folder_in", label_visibility="collapsed"
                )
                if st.session_state.ed_folder_path and os.path.isdir(st.session_state.ed_folder_path):
                    n = len([f for f in os.listdir(st.session_state.ed_folder_path) if f.lower().endswith(".pdf")])
                    st.success(f"✅ Folder: {n} PDFs")
            else:
                st.session_state.ed_zip_bytes = None
                st.session_state.ed_pdf_files = {}
                st.session_state.ed_folder_path = ""

    main_df  = st.session_state.get("df", None)
    main_zip = st.session_state.get("last_zip", None)
    if main_df is not None or main_zip is not None:
        with st.expander("📦 Import from Report Generator (last batch)", expanded=False):
            if main_df is not None:
                if st.button("📥 Import student dataset", key="btn_import_df"):
                    st.session_state.ed_df = main_df.copy()
                    cols = list(main_df.columns)
                    st.session_state.ed_email_col = _guess_col(cols, ["email","mail"])
                    st.session_state.ed_name_col  = _guess_col(cols, ["name","student"])
                    st.session_state.ed_id_col    = _guess_col(cols, ["id","enrollment","roll","usn"])
                    st.success(f"✅ Imported {len(main_df)} students")
            if main_zip is not None:
                if st.button("📥 Import generated PDFs (ZIP)", key="btn_import_zip"):
                    st.session_state.ed_zip_bytes = main_zip
                    st.session_state.ed_pdf_source = "Upload ZIP"
                    st.success("✅ PDF ZIP imported")

    df = st.session_state.ed_df
    if df is not None:
        st.markdown("---")
        st.markdown("### 🗂️ Recipients Table")
        cols = list(df.columns)

        rc1, rc2, rc3 = st.columns(3)
        st.session_state.ed_email_col = rc1.selectbox(
            "Email Column *", cols,
            index=cols.index(st.session_state.ed_email_col) if st.session_state.ed_email_col in cols else 0,
            key="sel_email"
        )
        name_opts = ["(none)"] + cols
        name_idx  = (cols.index(st.session_state.ed_name_col) + 1) if st.session_state.ed_name_col in cols else 0
        sel_name  = rc2.selectbox("Name Column", name_opts, index=name_idx, key="sel_name")
        st.session_state.ed_name_col = "" if sel_name == "(none)" else sel_name

        id_opts = ["(none)"] + cols
        id_idx  = (cols.index(st.session_state.ed_id_col) + 1) if st.session_state.ed_id_col in cols else 0
        sel_id  = rc3.selectbox("Enrollment ID Column", id_opts, index=id_idx, key="sel_id")
        st.session_state.ed_id_col = "" if sel_id == "(none)" else sel_id

        cc_opts = ["(none)"] + cols
        sel_cc  = st.selectbox("Dynamic CC Column (per-row CC from CSV)", cc_opts, index=0, key="sel_cc")
        st.session_state.smtp_cc_col = "" if sel_cc == "(none)" else sel_cc

        q = st.text_input("🔎 Filter recipients", placeholder="Search by name or email…", key="rec_search")
        if q:
            mask = df.apply(lambda row: q.lower() in str(row).lower(), axis=1)
            st.dataframe(df[mask], use_container_width=True, height=280)
        else:
            st.dataframe(df, use_container_width=True, height=280)
            st.caption(f"{len(df)} total recipients")

        if st.button("🗑️ Clear All Recipients", key="btn_clear_df"):
            st.session_state.ed_df = None
            st.session_state.ed_email_col = ""
            st.session_state.ed_name_col = ""
            st.rerun()


# ══════════════════════════════════════════════════════════════════════
# PAGE: TEMPLATE
# ══════════════════════════════════════════════════════════════════════
elif page == "📝 Template":
    st.markdown('<div class="page-heading">Email Composer</div>', unsafe_allow_html=True)
    st.markdown('<div class="page-subheading">Design and personalize your campaign message templates</div>', unsafe_allow_html=True)

    df = st.session_state.ed_df
    sample_row = _get_sample_row()

    col_compose, col_preview = st.columns([3, 2])

    with col_compose:
        from_display = st.session_state.smtp_email or "your-smtp-account@gmail.com"
        to_display   = st.session_state.ed_email_col or "No contacts loaded"
        subj_preview = render_jinja(st.session_state.ed_subject, sample_row)

        st.markdown(f"""
<div class="composer-shell">
  <div class="composer-bar">
    <span class="composer-bar-label">● New Message</span>
    <span class="composer-bar-mode">Rich Text / HTML</span>
  </div>
  <div class="composer-field">
    <span class="composer-field-label">From:</span>
    <span class="composer-field-value">{from_display}</span>
  </div>
  <div class="composer-field">
    <span class="composer-field-label">To:</span>
    <span class="composer-field-value composer-field-value-blue">
      {"👥 " + str(len(df)) + " recipients loaded" if df is not None else "📋 No contacts loaded"}
    </span>
  </div>
</div>
""", unsafe_allow_html=True)

        st.session_state.ed_subject = st.text_input(
            "Subject", value=st.session_state.ed_subject, key="subj_in",
            placeholder="Important Update — {{ Name }}"
        )

        if df is not None:
            chips = "".join(f'<span class="token-chip">{{{{ {c} }}}}</span>' for c in df.columns)
            st.markdown(f'<div style="margin:4px 0 8px;line-height:2.2;">{chips}</div>', unsafe_allow_html=True)
        else:
            st.markdown('<div style="font-size:0.75rem;color:#94A3B8;margin-bottom:8px;">Load recipients to see available tokens (e.g. {{ Name }}, {{ Grade }})</div>', unsafe_allow_html=True)

        body_tab1, body_tab2 = st.tabs(["📎 1. Standard Body (With PDF)", "✉️ 2. Alternate Body (No PDF)"])

        with body_tab1:
            st.caption("Sent to recipients whose PDF report was **matched and attached**.")
            st.session_state.ed_body = st.text_area(
                "Body", value=st.session_state.ed_body, height=240, key="body_in",
                help="Supports **bold**, *italic*, [links](url), - bullet lists, {{ Token }} variables",
                label_visibility="collapsed",
                placeholder="Dear {{ Name }},\n\nPlease find your report attached.\n\nBest regards,\nYour Team",
            )
            st.caption("Supports **bold**, *italic*, [links](url), - bullet lists, `{{ Token }}` for personalization")

        with body_tab2:
            st.caption("Sent to recipients who have **no matching PDF** (email-only).")
            st.session_state.ed_body_nopdf = st.text_area(
                "Alternate body", value=st.session_state.ed_body_nopdf, height=240, key="body_nopdf_in",
                label_visibility="collapsed",
                placeholder="Dear {{ Name }},\n\nWe are writing to inform you about your assessment results.\n\nBest regards,\nYour Team",
            )
            st.session_state.ed_subject_nopdf = st.text_input(
                "Alternate Subject (optional, leave blank to use main subject)",
                value=st.session_state.ed_subject_nopdf, key="subj_nopdf_in"
            )

        with st.expander("✍️ Signature (appended after body)", expanded=False):
            st.session_state.ed_signature = st.text_area(
                "Signature", value=st.session_state.ed_signature, height=80, key="sig_in",
                placeholder="Best regards,\n**Your Name**\nOrganization Name"
            )

        with st.expander("🎨 Email Style", expanded=False):
            tmpl_options = ["Simple Email (Standard)", "Card Container (Styled)"]
            st.session_state.ed_template_style = st.selectbox(
                "Template Layout Style", tmpl_options,
                index=0 if st.session_state.ed_template_style == "Simple Email (Standard)" else 1,
                key="tmpl_style_sel"
            )
            sc1, sc2 = st.columns(2)
            st.session_state.ed_bg_color   = sc1.color_picker("Outer Background", st.session_state.ed_bg_color)
            st.session_state.ed_content_bg = sc2.color_picker("Content Background", st.session_state.ed_content_bg)
            font_options = ["'Inter', Arial, sans-serif", "'Georgia', serif", "'Courier New', monospace", "Arial, sans-serif"]
            st.session_state.ed_font_family = st.selectbox("Font Family", font_options, key="font_sel")

        st.markdown("---")
        st.markdown("##### 🧪 Send Test Email to Yourself")
        qt1, qt2 = st.columns([3, 1])
        test_to = qt1.text_input("Test address", value=st.session_state.smtp_email, key="test_to", label_visibility="collapsed")
        qt2.markdown("<br>", unsafe_allow_html=True)
        if qt2.button("Send Test", key="btn_test", use_container_width=True):
            if not st.session_state.smtp_email or not st.session_state.smtp_pass:
                st.error("❌ Configure SMTP in **⚙️ Settings** first.")
            elif not validate_email(test_to):
                st.error("❌ Invalid test address.")
            elif not st.session_state.ed_body.strip():
                st.error("❌ Write a body first.")
            else:
                try:
                    html_test = _build_preview_html(st.session_state.ed_body, st.session_state.ed_body_mode, sample_row)
                    with st.spinner("Connecting to SMTP…"):
                        srv = connect_smtp(
                            st.session_state.smtp_email, st.session_state.smtp_pass,
                            st.session_state.smtp_host, st.session_state.smtp_port,
                        )
                        ok, err = send_one(
                            srv, st.session_state.smtp_email,
                            st.session_state.smtp_from_name, test_to,
                            f"[TEST] {st.session_state.ed_subject}", html_test,
                        )
                        srv.quit()
                    if ok:
                        st.success(f"✅ Test email sent to **{test_to}**!")
                    else:
                        st.error(f"❌ {err}")
                except Exception as exc:
                    st.error(f"❌ SMTP Error: {exc}")

        if st.button("⚙️ Next: Settings →", key="btn_next_settings", type="primary", use_container_width=True):
            st.session_state.ed_nav_page = "⚙️  Settings"
            st.rerun()

    with col_preview:
        st.markdown(f"""
<div class="preview-shell">
  <div class="preview-bar">👁️ Live Recipient Preview</div>
  <div class="preview-field"><span>From:</span> {from_display}</div>
  <div class="preview-field"><span>To:</span> {sample_row.get(st.session_state.ed_email_col, "recipient@gmail.com") if st.session_state.ed_email_col else "recipient@gmail.com"}</div>
  <div class="preview-field"><span>Subject:</span> {render_jinja(st.session_state.ed_subject, sample_row)}</div>
</div>
""", unsafe_allow_html=True)

        if df is not None and len(df) > 1:
            prev_idx = st.slider("Preview recipient #", 1, min(10, len(df)), 1, key="prev_sl") - 1
            sample_row = _nan_clean(df.iloc[prev_idx].to_dict())

        if st.session_state.ed_body.strip():
            html_prev = _build_preview_html(st.session_state.ed_body, st.session_state.ed_body_mode, sample_row)
            prev_mode = st.radio("View as", ["HTML Email", "Plain Text"], horizontal=True, key="prev_mode")
            if prev_mode == "HTML Email":
                components.html(html_prev, height=480, scrolling=True)
            else:
                st.text_area("Plain text", value=html_to_plain(html_prev), height=300, key="plain_prev_view")
        else:
            st.markdown("""
<div style="text-align:center;padding:60px 20px;color:#CBD5E1;">
  <div style="font-size:2rem;margin-bottom:8px;">✉️</div>
  <div style="font-size:0.82rem;">Write your email body to see a preview</div>
</div>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════
# PAGE: SETTINGS
# ══════════════════════════════════════════════════════════════════════
elif page == "⚙️  Settings":
    st.markdown('<div class="page-heading">SMTP Settings</div>', unsafe_allow_html=True)

    locked = st.session_state.smtp_creds_locked and bool(st.session_state.smtp_email)
    on_cloud = _is_streamlit_cloud()
    using_secrets = _secrets_available()
    backend_name = get_backend_name()

    if locked or using_secrets:
        if using_secrets:
            st.markdown('<div class="encrypted-note">🔐 Credentials loaded from <b>Streamlit Secrets</b> (dashboard-managed, fully encrypted).</div>', unsafe_allow_html=True)
        elif on_cloud:
            st.markdown('<div class="encrypted-note">☁️ Running on Streamlit Cloud — credentials are stored in <b>session state only</b> (lost on restart). Use the Secrets Manager below to make them persistent.</div>', unsafe_allow_html=True)
        else:
            st.markdown('<div class="encrypted-note">🔐 Credentials encrypted with AES-256 and stored on the local server filesystem (survive restarts).</div>', unsafe_allow_html=True)

    left_col, right_col = st.columns([3, 2])

    with left_col:
        st.markdown('<div class="settings-card">', unsafe_allow_html=True)
        st.markdown('<div class="settings-card-title">✉️ Gmail SMTP Authentication</div>', unsafe_allow_html=True)

        if locked:
            st.markdown(f"""
<div class="creds-locked-box">
  ✅ SMTP Credentials Locked ✓
  <span class="creds-locked-sub">&nbsp;— Sender: {st.session_state.smtp_email}</span>
</div>
""", unsafe_allow_html=True)
            st.caption("Emails will be sent using the server-side stored app password. You do not need to re-enter it.")
            if st.button("🗑️ Clear Saved Credentials", key="btn_clear_creds", type="secondary", use_container_width=True):
                clear_credentials()
                st.session_state.smtp_pass = ""
                st.session_state.smtp_creds_locked = False
                st.warning("Credentials cleared. Re-enter to activate again.")
                st.rerun()
        else:
            provider = st.selectbox(
                "Provider", list(SMTP_PROVIDERS.keys()),
                index=list(SMTP_PROVIDERS.keys()).index(st.session_state.smtp_provider),
                key="prov_sel"
            )
            st.session_state.smtp_provider = provider
            if provider != "Custom":
                st.session_state.smtp_host = SMTP_PROVIDERS[provider]["host"]
                st.session_state.smtp_port = SMTP_PROVIDERS[provider]["port"]
                st.caption(f"Host: `{st.session_state.smtp_host}` | Port: `{st.session_state.smtp_port}`")
            else:
                pc1, pc2 = st.columns(2)
                st.session_state.smtp_host = pc1.text_input("SMTP Host", value=st.session_state.smtp_host)
                st.session_state.smtp_port = pc2.number_input("Port", value=st.session_state.smtp_port, min_value=1, max_value=65535, step=1)

            cr1, cr2 = st.columns(2)
            st.session_state.smtp_email     = cr1.text_input("Sender Email", value=st.session_state.smtp_email, key="smtp_email_in")
            st.session_state.smtp_pass      = cr2.text_input("App Password", value=st.session_state.smtp_pass, type="password", key="smtp_pass_in")
            st.session_state.smtp_from_name = cr1.text_input("Display Name (From)", value=st.session_state.smtp_from_name, key="smtp_fn_in")
            st.session_state.smtp_reply_to  = cr2.text_input("Reply-To (optional)", value=st.session_state.smtp_reply_to, key="smtp_rt_in")

            if st.button("🔐 Save & Lock Credentials", key="btn_save_creds", type="primary", use_container_width=True):
                if not st.session_state.smtp_email or not st.session_state.smtp_pass:
                    st.error("Enter both email and password.")
                elif not validate_email(st.session_state.smtp_email):
                    st.error("Invalid email address.")
                else:
                    ok, msg = save_credentials(
                        st.session_state.smtp_email, st.session_state.smtp_pass,
                        from_name=st.session_state.smtp_from_name,
                        reply_to=st.session_state.smtp_reply_to,
                        host=st.session_state.smtp_host,
                        port=st.session_state.smtp_port,
                        provider=st.session_state.smtp_provider,
                    )
                    if msg == "streamlit_cloud":
                        st.session_state.smtp_creds_locked = True
                        st.warning("⚠️ **Running on Streamlit Cloud** — credentials saved for this session only. They will be lost when the app restarts.\n\n🔒 **To make them permanent**, add them to your Streamlit Cloud Secrets Manager (see the blue box below).")
                        st.rerun()
                    elif ok:
                        st.session_state.smtp_creds_locked = True
                        st.success("✅ Credentials saved and locked!")
                        st.rerun()
                    else:
                        st.error("❌ Failed to save credentials. Check file permissions.")
        st.markdown('</div>', unsafe_allow_html=True)

        if on_cloud or using_secrets:
            st.markdown("""
<div style="background:#FFF7ED;border:1.5px solid #FED7AA;border-radius:12px;padding:18px 20px;margin-top:4px;">
  <div style="font-weight:800;color:#C2410C;margin-bottom:8px;">🧨 Streamlit Cloud — Secrets Manager Setup</div>
  <div style="font-size:0.84rem;color:#7C2D12;">
    To keep credentials between restarts, go to your app's dashboard on
    <a href="https://share.streamlit.io" target="_blank">share.streamlit.io</a>:
    <br><br>
    <b>App Settings → Secrets</b> and paste:
  </div>
  <pre style="background:#FEF3C7;border-radius:8px;padding:12px;font-size:0.78rem;color:#78350F;margin-top:8px;">[smtp]
email = "your@gmail.com"
password = "your-16-char-app-password"
from_name = "Your Name"
reply_to = ""
host = "smtp.gmail.com"
port = 587
provider = "Gmail"</pre>
  <div style="font-size:0.78rem;color:#92400E;margin-top:6px;">
    ✅ Secrets are encrypted by Streamlit — never shown in your code or git repo.
  </div>
</div>
""", unsafe_allow_html=True)

        st.markdown('<div class="settings-card">', unsafe_allow_html=True)
        st.markdown('<div class="settings-card-title">⚡ Test Connection</div>', unsafe_allow_html=True)
        st.markdown("**SEND TEST EMAIL TO**")
        test_addr = st.text_input("Test address", value=st.session_state.smtp_email, key="conn_test_addr", label_visibility="collapsed")
        tc1, tc2 = st.columns([3, 1])
        tc2.markdown("<br>", unsafe_allow_html=True)
        if tc2.button("Send Test", key="btn_conn_test", type="primary", use_container_width=True):
            if not st.session_state.smtp_email or not st.session_state.smtp_pass:
                st.error("Enter credentials first.")
            else:
                try:
                    with st.spinner("Connecting…"):
                        srv = connect_smtp(
                            st.session_state.smtp_email, st.session_state.smtp_pass,
                            st.session_state.smtp_host, st.session_state.smtp_port,
                        )
                        ok, err = send_one(
                            srv, st.session_state.smtp_email,
                            st.session_state.smtp_from_name,
                            test_addr, "[CONNECTION TEST] Email Dispatcher Pro",
                            "<p>✅ Connection test successful from <b>Report Generator — Email Dispatcher Pro</b>.</p>",
                        )
                        srv.quit()
                    if ok:
                        st.success(f"✅ Test email sent to **{test_addr}**!")
                    else:
                        st.error(f"❌ Connected but send failed: {err}")
                except Exception as exc:
                    st.error(f"❌ SMTP Error: {exc}\n\n*Check App Password — do not use your regular Gmail password.*")
        st.markdown('</div>', unsafe_allow_html=True)

    with right_col:
        st.markdown('<div class="settings-card">', unsafe_allow_html=True)
        st.markdown('<div class="settings-card-title">📋 CC / BCC Headers</div>', unsafe_allow_html=True)
        st.markdown("**CC (OPTIONAL)**")
        st.session_state.smtp_cc  = st.text_input("CC", value=st.session_state.smtp_cc, key="cc_in", placeholder="cc@example.com", label_visibility="collapsed")
        st.markdown("**BCC (OPTIONAL)**")
        st.session_state.smtp_bcc = st.text_input("BCC", value=st.session_state.smtp_bcc, key="bcc_in", placeholder="bcc@example.com", label_visibility="collapsed")
        st.markdown('</div>', unsafe_allow_html=True)

        st.markdown('<div class="settings-card">', unsafe_allow_html=True)
        st.markdown('<div class="settings-card-title">⏱️ Rate Limiting</div>', unsafe_allow_html=True)
        st.markdown("**Delay between emails**")
        st.session_state.smtp_rate = st.slider("Rate", 0.0, 10.0, float(st.session_state.smtp_rate), 0.1, key="rate_sl", label_visibility="collapsed")
        st.caption("Gmail safe limit is 0.5s. Set higher (2–3s) for large batches.")
        st.markdown("**Max Retries per Email**")
        st.session_state.smtp_retries = st.slider("Retries", 1, 5, int(st.session_state.smtp_retries), key="retry_sl", label_visibility="collapsed")
        st.markdown("**Batch Pause** — pause after N emails (0 = off)")
        bpc1, bpc2 = st.columns(2)
        st.session_state.smtp_batch_size  = bpc1.number_input("After N emails", 0, 500, int(st.session_state.smtp_batch_size), key="bn")
        st.session_state.smtp_batch_pause = bpc2.number_input("Pause (s)", 1, 120, int(st.session_state.smtp_batch_pause), key="bp")
        st.markdown('</div>', unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════
# PAGE: DISPATCH
# ══════════════════════════════════════════════════════════════════════
elif page == "🚀 Dispatch":
    st.markdown('<div class="page-heading">Launch Campaign</div>', unsafe_allow_html=True)
    st.markdown('<div class="page-subheading">Review the audit, then send</div>', unsafe_allow_html=True)

    df        = st.session_state.ed_df
    email_col = st.session_state.ed_email_col
    name_col  = st.session_state.ed_name_col
    id_col    = st.session_state.ed_id_col
    zip_bytes = st.session_state.ed_zip_bytes
    folder    = st.session_state.ed_folder_path
    pdf_files = st.session_state.ed_pdf_files

    total_rcpt = len(df) if df is not None else 0
    valid_emails = 0
    pdfs_matched = 0
    missing_pdfs = 0
    has_pdf_src = bool(zip_bytes or (folder and os.path.isdir(folder)) or pdf_files)

    if df is not None and email_col:
        for _, row in df.iterrows():
            email = str(row.get(email_col, "")).strip()
            if validate_email(email):
                valid_emails += 1
            if has_pdf_src:
                name = str(row.get(name_col, "")) if name_col else ""
                eid  = str(row.get(id_col,   "")) if id_col   else ""
                if zip_bytes:
                    _, ps = find_pdf_in_zip_bytes(zip_bytes, eid, name)
                elif pdf_files:
                    matched = False
                    for fn in pdf_files:
                        fn_low = fn.lower()
                        if (eid and eid.lower() in fn_low) or (name and name.lower() in fn_low):
                            matched = True; break
                    ps = "✅ Match" if matched else "❌ Not Found"
                elif folder:
                    _, ps = find_pdf_in_folder(folder, eid, name)
                else:
                    ps = "N/A"
                if "✅" in ps: pdfs_matched += 1
                else: missing_pdfs += 1

    m1, m2, m3, m4 = st.columns(4)
    m1.markdown(f'<div class="audit-metric"><div class="audit-metric-label">Total Recipients</div><div class="audit-metric-value">{total_rcpt}</div></div>', unsafe_allow_html=True)
    m2.markdown(f'<div class="audit-metric"><div class="audit-metric-label">Valid Emails</div><div class="audit-metric-value green">{valid_emails}</div></div>', unsafe_allow_html=True)
    m3.markdown(f'<div class="audit-metric"><div class="audit-metric-label">PDFs Matched</div><div class="audit-metric-value purple">{pdfs_matched if has_pdf_src else "—"}</div></div>', unsafe_allow_html=True)
    m4.markdown(f'<div class="audit-metric"><div class="audit-metric-label">Missing PDFs</div><div class="audit-metric-value orange">{missing_pdfs if has_pdf_src else "—"}</div></div>', unsafe_allow_html=True)
    st.markdown("")

    ready = (df is not None and email_col and st.session_state.ed_body.strip() and st.session_state.smtp_email and st.session_state.smtp_pass)

    if not ready:
        missing = []
        if df is None:                              missing.append("Load recipients in **📋 Recipients**")
        if not email_col:                           missing.append("Select email column in **📋 Recipients**")
        if not st.session_state.ed_body.strip():    missing.append("Write email body in **📝 Template**")
        if not st.session_state.smtp_email:         missing.append("Set SMTP email in **⚙️ Settings**")
        if not st.session_state.smtp_pass:          missing.append("Set App Password in **⚙️ Settings**")
        for m in missing: st.warning(f"⚠️ {m}")
        if st.button("← Back", key="btn_back_disp"):
            st.session_state.ed_nav_page = "📋 Recipients"
            st.rerun()
    else:
        n_total  = len(df)
        est_secs = int(n_total * (st.session_state.smtp_rate + 0.5))
        est_str  = f"{est_secs//60}m {est_secs%60}s" if est_secs >= 60 else f"{est_secs}s"

        st.markdown(f"""
<div style="background:#EFF6FF;border:1.5px solid #BFDBFE;border-radius:10px;padding:16px 20px;margin-bottom:16px;">
  <b style="color:#1D4ED8;">📋 Campaign Summary</b><br>
  Recipients: <b>{n_total}</b> &nbsp;|&nbsp; Valid Emails: <b>{valid_emails}</b> &nbsp;|&nbsp;
  SMTP: <b>{st.session_state.smtp_email}</b> ({st.session_state.smtp_provider})<br>
  Rate: <b>{st.session_state.smtp_rate}s/email</b> &nbsp;|&nbsp; Estimated time: <b>~{est_str}</b>
</div>
""", unsafe_allow_html=True)

        if "dispatch_active" not in st.session_state:
            st.session_state.dispatch_active = False

        if not st.session_state.dispatch_active:
            dsp1, dsp2, dsp3 = st.columns(3)
            go_full   = dsp1.button("🚀 Launch Full Campaign",  key="btn_go_full",   type="primary",    use_container_width=True)
            go_test3  = dsp2.button("🧪 Send to First 3 Only", key="btn_go_test3",  type="secondary",  use_container_width=True)
            go_failed = dsp3.button("🔄 Retry Failed Only",    key="btn_go_failed", type="secondary",  use_container_width=True,
                                    disabled=not bool(st.session_state.dispatch_results))

            def _start(rows):
                try:
                    srv = connect_smtp(st.session_state.smtp_email, st.session_state.smtp_pass, st.session_state.smtp_host, st.session_state.smtp_port)
                    st.session_state.dispatch_srv = srv
                    st.session_state.dispatch_queue = rows
                    st.session_state.dispatch_index = 0
                    st.session_state.dispatch_results_temp = []
                    st.session_state.dispatch_active = True
                    st.session_state.dispatch_paused = False
                    st.rerun()
                except Exception as exc:
                    st.error(f"❌ Cannot connect to SMTP: {exc}")

            if go_full:   _start([(i, row) for i, (_, row) in enumerate(df.iterrows())])
            elif go_test3: _start([(i, row) for i, (_, row) in enumerate(df.head(3).iterrows())])
            elif go_failed:
                failed_rows = [(i, df.iloc[i]) for i, r in enumerate(st.session_state.dispatch_results) if "Sent" not in r.get("Status", "") and i < len(df)]
                _start(failed_rows)

        if st.session_state.dispatch_active:
            st.markdown("### ⚡ Dispatching Campaign…")
            c_pause, c_cancel = st.columns(2)

            if st.session_state.dispatch_paused:
                if c_pause.button("▶️ Resume", key="btn_resume", type="primary", use_container_width=True):
                    st.session_state.dispatch_paused = False; st.rerun()
            else:
                if c_pause.button("⏸ Pause", key="btn_pause", type="secondary", use_container_width=True):
                    st.session_state.dispatch_paused = True; st.rerun()

            if c_cancel.button("⏹ Cancel Campaign", key="btn_cancel", type="secondary", use_container_width=True):
                try: st.session_state.dispatch_srv.quit()
                except Exception: pass
                st.session_state.dispatch_active = False
                st.session_state.dispatch_results = st.session_state.dispatch_results_temp
                save_dispatch_log(st.session_state.dispatch_results, st.session_state.ed_subject[:30])
                st.warning("⚠️ Campaign cancelled."); st.rerun()

            idx   = st.session_state.dispatch_index
            queue = st.session_state.dispatch_queue
            total = len(queue)

            if idx < total:
                pct = int(idx / total * 100)
                remaining = int((total - idx) * (st.session_state.smtp_rate + 0.5))
                eta = f"{remaining//60}m {remaining%60}s" if remaining >= 60 else f"{remaining}s"
                st.progress(pct)
                orig_idx, row = queue[idx]
                email = str(row.get(email_col, "")).strip()
                name  = str(row.get(name_col, "")) if name_col else ""
                eid   = str(row.get(id_col,   "")) if id_col   else ""
                st.markdown(f"Sending to: `{email}` (**{idx+1}/{total}**) · {pct}% · ETA **{eta}**")

                if st.session_state.dispatch_paused:
                    st.info("⏸ Paused — click Resume to continue.")
                else:
                    if not validate_email(email):
                        st.session_state.dispatch_results_temp.append({"#": orig_idx+1, "Name": name, "Email": email, "Status": "⏭️ Skipped (invalid)", "PDF": "—", "Time": datetime.now().strftime("%H:%M:%S")})
                    else:
                        row_dict = _nan_clean(row.to_dict())
                        pdf_bytes_content = None; pdf_status = "No PDF"
                        pdf_fname = f"{eid or name or str(orig_idx+1)}.pdf"

                        if zip_bytes:
                            pdf_bytes_content, pdf_status = find_pdf_in_zip_bytes(zip_bytes, eid, name)
                        elif pdf_files:
                            for fn, fb in pdf_files.items():
                                fn_low = fn.lower()
                                if (eid and eid.lower() in fn_low) or (name and name.lower() in fn_low):
                                    pdf_bytes_content = fb; pdf_fname = fn; pdf_status = "✅ Match (Upload)"; break
                            if not pdf_bytes_content: pdf_status = "❌ Not Found"
                        elif folder:
                            pdf_path, pdf_status = find_pdf_in_folder(folder, eid, name)
                            if pdf_path:
                                with open(pdf_path, "rb") as f: pdf_bytes_content = f.read()
                                pdf_fname = os.path.basename(pdf_path)

                        use_alt = (pdf_bytes_content is None and has_pdf_src)
                        body_text = st.session_state.ed_body_nopdf if use_alt else st.session_state.ed_body
                        subject   = render_jinja((st.session_state.ed_subject_nopdf or st.session_state.ed_subject) if use_alt else st.session_state.ed_subject, row_dict)
                        html_body = _build_preview_html(body_text, st.session_state.ed_body_mode, row_dict)

                        row_cc = ""
                        if st.session_state.smtp_cc_col and st.session_state.smtp_cc_col in row.index:
                            row_cc = str(row[st.session_state.smtp_cc_col]).strip()
                            if row_cc.lower() in ("nan","none",""): row_cc = ""
                        combined_cc = ", ".join(filter(None, [st.session_state.smtp_cc, row_cc]))
                        attachments = [(pdf_fname, pdf_bytes_content)] if pdf_bytes_content else []

                        ok, err = False, ""
                        srv = st.session_state.dispatch_srv
                        for attempt in range(max(1, st.session_state.smtp_retries)):
                            ok, err = send_one(srv, st.session_state.smtp_email, st.session_state.smtp_from_name, email, subject, html_body, attachments=attachments, cc=combined_cc, bcc=st.session_state.smtp_bcc, reply_to=st.session_state.smtp_reply_to)
                            if ok: break
                            time.sleep(2)
                            try: srv.quit()
                            except Exception: pass
                            try:
                                srv = connect_smtp(st.session_state.smtp_email, st.session_state.smtp_pass, st.session_state.smtp_host, st.session_state.smtp_port)
                                st.session_state.dispatch_srv = srv
                            except Exception: break

                        st.session_state.dispatch_results_temp.append({
                            "#": orig_idx + 1, "Name": name, "Email": email,
                            "Status": "✅ Sent" if ok else f"❌ {err[:60]}",
                            "PDF": pdf_status, "Alt Body": "✅ (No PDF)" if use_alt else "",
                            "Time": datetime.now().strftime("%H:%M:%S"),
                        })

                    st.session_state.dispatch_index += 1
                    time.sleep(st.session_state.smtp_rate)
                    bs = st.session_state.smtp_batch_size
                    if bs > 0 and (idx + 1) % bs == 0 and (idx + 1) < total:
                        time.sleep(st.session_state.smtp_batch_pause)
                    st.rerun()
            else:
                try: st.session_state.dispatch_srv.quit()
                except Exception: pass
                st.session_state.dispatch_active  = False
                st.session_state.dispatch_results = st.session_state.dispatch_results_temp
                save_dispatch_log(st.session_state.dispatch_results, st.session_state.ed_subject[:30])
                st.rerun()

        if not st.session_state.dispatch_active and st.session_state.dispatch_results:
            results = st.session_state.dispatch_results
            sent    = sum(1 for r in results if "✅" in r["Status"])
            failed  = sum(1 for r in results if "❌" in r["Status"])
            skipped = sum(1 for r in results if "⏭️" in r["Status"])
            if sent > 0: st.balloons()
            st.success(f"🎉 Campaign Complete — ✅ {sent} Sent | ❌ {failed} Failed | ⏭️ {skipped} Skipped")
            st.dataframe(pd.DataFrame(results), use_container_width=True)
            st.download_button("📥 Download Dispatch Log", pd.DataFrame(results).to_csv(index=False).encode("utf-8"), file_name="dispatch_log.csv", mime="text/csv", key="dl_log")


# ══════════════════════════════════════════════════════════════════════
# PAGE: HISTORY
# ══════════════════════════════════════════════════════════════════════
elif page == "📜 History":
    st.markdown('<div class="page-heading">Dispatch History</div>', unsafe_allow_html=True)
    st.markdown('<div class="page-subheading">All past email campaigns</div>', unsafe_allow_html=True)

    master_log = os.path.join(LOG_DIR, "master_dispatch_history.csv")
    if os.path.exists(master_log):
        try:
            hist_df = pd.read_csv(master_log)
            h1, h2, h3 = st.columns(3)
            h1.metric("Total Dispatched", len(hist_df))
            h2.metric("Total Sent",    hist_df["Status"].str.contains("✅").sum() if "Status" in hist_df.columns else "—")
            h3.metric("Total Failed",  hist_df["Status"].str.contains("❌").sum() if "Status" in hist_df.columns else "—")

            search = st.text_input("🔎 Search history", placeholder="Email, name, status…", key="hist_search")
            if search:
                hist_df = hist_df[hist_df.apply(lambda r: search.lower() in str(r).lower(), axis=1)]

            st.dataframe(hist_df.iloc[::-1].reset_index(drop=True), use_container_width=True, height=360)

            col_dl, col_clr = st.columns([3, 1])
            col_dl.download_button("📥 Download Full History", hist_df.to_csv(index=False).encode("utf-8"), file_name="master_email_history.csv", mime="text/csv", key="dl_hist")
            if col_clr.button("🗑️ Clear History", key="btn_clr"):
                os.remove(master_log); st.success("History cleared."); st.rerun()
        except Exception as e:
            st.error(f"Could not read history log: {e}")
    else:
        st.info("📭 No dispatch history yet. Run your first campaign in **🚀 Dispatch**.")

    if os.path.exists(LOG_DIR):
        sess_files = sorted([f for f in os.listdir(LOG_DIR) if f.endswith(".csv") and f != "master_dispatch_history.csv"], reverse=True)
        if sess_files:
            st.markdown("---")
            st.markdown("### Per-Session Logs")
            sel = st.selectbox("Select a session log", sess_files, key="sel_sess")
            if sel:
                try:
                    sdf = pd.read_csv(os.path.join(LOG_DIR, sel))
                    st.dataframe(sdf, use_container_width=True)
                    st.download_button(f"📥 {sel}", sdf.to_csv(index=False).encode("utf-8"), file_name=sel, mime="text/csv", key="dl_sess")
                except Exception as e:
                    st.error(f"Could not read: {e}")


# ══════════════════════════════════════════════════════════════════════
# PAGE: ANALYTICS
# ══════════════════════════════════════════════════════════════════════
elif page == "📊 Analytics":
    st.markdown('<div class="page-heading">Analytics</div>', unsafe_allow_html=True)
    st.markdown('<div class="page-subheading">Campaign performance and delivery insights</div>', unsafe_allow_html=True)

    master_log = os.path.join(LOG_DIR, "master_dispatch_history.csv")
    if not os.path.exists(master_log):
        st.info("📭 No history data yet. Run a campaign first to see analytics.")
    else:
        try:
            hist = pd.read_csv(master_log)
            if "Status" in hist.columns:
                sent    = int(hist["Status"].str.contains("✅").sum())
                failed  = int(hist["Status"].str.contains("❌").sum())
                skipped = int(hist["Status"].str.contains("⏭️").sum())
                ac1, ac2, ac3 = st.columns(3)
                ac1.metric("Total Sent",    sent)
                ac2.metric("Total Failed",  failed)
                ac3.metric("Total Skipped", skipped)
                st.markdown("### 📊 Delivery Breakdown")
                chart_df = pd.DataFrame({"Status": ["Sent ✅", "Failed ❌", "Skipped ⏭️"], "Count": [sent, failed, skipped]})
                st.bar_chart(chart_df.set_index("Status"), use_container_width=True, color="#2563EB")

            if "Time" in hist.columns:
                st.markdown("### 📅 Activity Timeline")
                hist["Date"] = pd.to_datetime(hist["Time"], format="%H:%M:%S", errors="coerce")
                daily = hist.groupby(hist["Time"].str[:5]).size().reset_index(name="Emails")
                daily.columns = ["Time", "Emails Sent"]
                st.area_chart(daily.set_index("Time"), use_container_width=True, color="#6366F1")

            if "Email" in hist.columns:
                st.markdown("### 📋 Recent Dispatches")
                st.dataframe(hist.tail(20).iloc[::-1].reset_index(drop=True), use_container_width=True)

        except Exception as e:
            st.error(f"Could not load analytics: {e}")
