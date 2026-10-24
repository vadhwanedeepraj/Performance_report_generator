"""
config_manager.py
=================
Centralized configuration and template management system for Report-Generator.

Supports:
- Default formatting & layout schema
- Multiple independent report templates
- Validation, normalization, and graceful recovery from missing/corrupted values
- Theme presets and color palettes
- Safe save, update, duplicate, delete, export, and import of templates
"""

import os
import json
import re
from datetime import datetime
from typing import Dict, Any, List, Tuple

TEMPLATE_DIR = "templates"
APP_CONFIG_FILE = "app_config.json"

# ─────────────────────────────────────────────────────────────────────────────
# THEME PRESETS
# ─────────────────────────────────────────────────────────────────────────────
THEME_PRESETS: Dict[str, Dict[str, str]] = {
    "Innomatics Crimson": {
        "primary_color": "#B91C1C",
        "secondary_color": "#1E293B",
        "accent_color": "#F8FAFC",
        "header_bg_color": "#B91C1C",
        "header_text_color": "#FFFFFF",
        "text_color": "#334155",
        "heading_text_color": "#0F172A",
        "gold_accent": "#C9A84C",
    },
    "Corporate Navy & Gold": {
        "primary_color": "#1B365D",
        "secondary_color": "#2C3E50",
        "accent_color": "#F0F4F8",
        "header_bg_color": "#1B365D",
        "header_text_color": "#FFFFFF",
        "text_color": "#334155",
        "heading_text_color": "#0F172A",
        "gold_accent": "#D4AF37",
    },
    "Modern Emerald": {
        "primary_color": "#047857",
        "secondary_color": "#1E293B",
        "accent_color": "#F0FDF4",
        "header_bg_color": "#047857",
        "header_text_color": "#FFFFFF",
        "text_color": "#334155",
        "heading_text_color": "#064E3B",
        "gold_accent": "#F59E0B",
    },
    "Royal Slate & Indigo": {
        "primary_color": "#4338CA",
        "secondary_color": "#1E293B",
        "accent_color": "#EEF2FF",
        "header_bg_color": "#4338CA",
        "header_text_color": "#FFFFFF",
        "text_color": "#334155",
        "heading_text_color": "#1E1B4B",
        "gold_accent": "#EAB308",
    },
    "Executive Charcoal": {
        "primary_color": "#334155",
        "secondary_color": "#0F172A",
        "accent_color": "#F8FAFC",
        "header_bg_color": "#334155",
        "header_text_color": "#FFFFFF",
        "text_color": "#334155",
        "heading_text_color": "#0F172A",
        "gold_accent": "#D97706",
    },
    "Academic Monochrome": {
        "primary_color": "#1F2937",
        "secondary_color": "#111827",
        "accent_color": "#F9FAFB",
        "header_bg_color": "#1F2937",
        "header_text_color": "#FFFFFF",
        "text_color": "#374151",
        "heading_text_color": "#111827",
        "gold_accent": "#9CA3AF",
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# DEFAULT REPORT CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_REPORT_CONFIG: Dict[str, Any] = {
    # ── Branding & Metadata
    "company_name": "Innomatics Research Labs",
    "report_title": "MOCK INTERVIEW EVALUATION REPORT",
    "report_subtitle": "Performance & Assessment Evaluation",
    "intro_text": "This report presents a comprehensive summary of student evaluation prepared by {company_name}.",
    "theme_preset": "Innomatics Crimson",
    "primary_color": "#B91C1C",
    "secondary_color": "#1E293B",
    "accent_color": "#F8FAFC",
    "header_bg_color": "#B91C1C",
    "header_text_color": "#FFFFFF",
    "text_color": "#334155",
    "heading_text_color": "#0F172A",
    "gold_accent": "#C9A84C",
    
    # ── Logo & Image
    "logo_path": "logo/innomatics_logo.png",
    "logo_width": 260,          # points
    "logo_height": 70,          # points
    "logo_align": "center",     # "left", "center", "right"
    "show_logo": True,
    "logo_base64": "",

    # ── Page Geometry & Margins (in points: 72 points = 1 inch, ~2.83 pt/mm)
    "page_size": "A4",          # "A4", "Letter", "Legal"
    "orientation": "portrait",  # "portrait", "landscape"
    "margin_top": 115,          # top margin space (accommodates top header)
    "margin_bottom": 40,        # bottom margin space
    "margin_left": 40,          # left margin
    "margin_right": 40,         # right margin

    # ── Typography & Spacing
    "font_family": "Helvetica", # "Helvetica", "Times-Roman", "Courier"
    "title_font_size": 13,
    "heading_font_size": 10,
    "body_font_size": 8,
    "table_font_size": 7.5,
    "footer_font_size": 6.5,
    "line_spacing": 1.3,        # line height factor
    "paragraph_spacing": 3,     # points after paragraphs
    "section_spacing": 6,       # points between major sections

    # ── Header & Footer
    "show_header": True,
    "header_style": "banner",   # "banner", "clean_line", "minimal"
    "show_date": True,
    "date_format": "%d %B %Y",
    "show_footer": True,
    "show_page_numbers": True,
    "page_number_format": "Page {page}", # "Page {page}" or "Page {page} of {pages}"
    "legal_notice": "This report is system-generated for assessment and evaluation purposes only.",
    "show_watermark": False,
    "watermark_text": "CONFIDENTIAL",

    # ── Table Formatting
    "table_border_style": "grid", # "grid", "horizontal_only", "boxed", "subtle", "minimal"
    "table_border_color": "#D1D5DB",
    "table_border_width": 0.5,
    "table_cell_padding_v": 5,    # vertical padding (top/bottom)
    "table_cell_padding_h": 7,    # horizontal padding (left/right)
    "table_zebra_stripes": True,
    "table_stripe_color": "#F8FAFC",
    "table_alignment": "left",

    # ── Assessment & Scoring Logic
    "marks_pass_threshold": 75,
    "benchmark_shortlist": 90,
    "benchmark_strong": 90,
    "benchmark_revision": 50,
    "enable_mock_interview": True,
    "marks_show_rank": True,
    "marks_show_grade": True,
    "marks_show_analysis": True,
    "marks_tone": "Encouraging",
    "marks_module_context": "",
    "marks_benchmark_note": "<b><font color='#dc2626'>Important:</font></b> Your performance is evaluated relative to all candidates across all branches of Innomatics Research Labs who appeared for the DataAnalysis Job Fitment Assessment. The benchmarks shown below indicate the score required to be among the top 10% in each module.",
    "marks_show_benchmark_note": True,

    # ── Next Steps & Actions
    "footer_deadline_text": "Important: The mock interview scheduling link will remain active for 3 calendar days from the date of this report. Please ensure your booking is completed within this period.",
    "footer_mentor_text": "If you have any questions, feel free to connect with your batch mentor before scheduling.",
    "footer_link_text": "Schedule Mock Interview",
    "footer_link_url": "https://example.com/schedule",
    "show_honors_certificate": False,

    # ── Action Plan Master Controls
    "show_action_plan": True,
    "show_tech_action": True,
    "show_comm_action": True,
    "show_perf": True,

    # ── Data Column Mappings
    "identity_cols": ["Enrolment ID", "Full Name", "Batch #"],
    "pdf_name_col": "Enrolment ID",
    "wt_enabled": False,
    "written_name": "Written Test",
    "written_cfg": {},

    # ── Sections & Pipeline Ordering
    # Each section in this list is executed in order.
    # Attributes: id, name, title, type, enabled, page_break_before, columns, col_cfg
    "sections": [
        {
            "id": "sec_candidate_info",
            "name": "Candidate Details",
            "title": "Candidate Details",
            "type": "identity_card",
            "enabled": True,
            "page_break_before": False,
            "columns": [],
        },
        {
            "id": "sec_marks_table_1",
            "name": "Module-wise Performance",
            "title": "Module-wise Performance",
            "type": "marks_table",
            "enabled": True,
            "page_break_before": False,
            "columns": [],
            "col_cfg": {},
        },
        {
            "id": "sec_overall_readiness",
            "name": "Overall Readiness Summary",
            "title": "Overall Performance & Readiness",
            "type": "marks_motivation",
            "enabled": True,
            "page_break_before": False,
            "columns": [],
        },
        {
            "id": "sec_performance_insights",
            "name": "Performance Insights",
            "title": "Performance Insights",
            "type": "marks_analysis",
            "enabled": True,
            "page_break_before": False,
            "columns": [],
        },
        {
            "id": "sec_action_plan",
            "name": "Personalised Action Plan",
            "title": "Personalised Action Plan",
            "type": "action_plan_grid",
            "enabled": True,
            "page_break_before": False,
            "columns": [],
        },
        {
            "id": "sec_instructor_remarks",
            "name": "Instructor Remarks",
            "title": "Instructor Remarks",
            "type": "text_field",
            "enabled": True,
            "page_break_before": False,
            "columns": [],
        },
    ]
}


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG VALIDATION & NORMALIZATION
# ─────────────────────────────────────────────────────────────────────────────
def validate_and_normalize_config(raw_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensures a report configuration contains all required keys with valid types and bounds.
    Recovers gracefully from missing or legacy keys, guaranteeing full backward compatibility.
    """
    if not isinstance(raw_cfg, dict):
        return json.loads(json.dumps(DEFAULT_REPORT_CONFIG))

    cfg = json.loads(json.dumps(DEFAULT_REPORT_CONFIG))  # deep copy defaults

    # Copy top-level scalar & string values if present
    for k, default_val in DEFAULT_REPORT_CONFIG.items():
        if k in raw_cfg and raw_cfg[k] is not None:
            val = raw_cfg[k]
            # Type preservation & bounds check
            if isinstance(default_val, bool):
                cfg[k] = bool(val)
            elif isinstance(default_val, (int, float)):
                try:
                    num_val = float(val) if isinstance(default_val, float) else int(val)
                    # Safe range checks for layout values
                    if "margin" in k:
                        num_val = max(10, min(150, num_val))
                    elif "font_size" in k:
                        num_val = max(5, min(36, num_val))
                    elif "spacing" in k:
                        num_val = max(0, min(50, num_val))
                    elif "padding" in k:
                        num_val = max(1, min(30, num_val))
                    cfg[k] = num_val
                except (ValueError, TypeError):
                    cfg[k] = default_val
            elif isinstance(default_val, str):
                cfg[k] = str(val).strip()
            elif isinstance(default_val, list) and k != "sections":
                cfg[k] = list(val) if isinstance(val, (list, tuple)) else default_val
            elif isinstance(default_val, dict) and k != "sections":
                cfg[k] = dict(val) if isinstance(val, dict) else default_val

    # Backward compatibility: map legacy keys
    if "show_perf" in raw_cfg:
        cfg["show_performance_summary"] = bool(raw_cfg["show_perf"])
        cfg["show_perf"] = bool(raw_cfg["show_perf"])

    if "identity_cols" in raw_cfg:
        cfg["identity_columns"] = list(raw_cfg["identity_cols"])
        cfg["identity_cols"] = list(raw_cfg["identity_cols"])
    elif "identity_columns" in raw_cfg:
        cfg["identity_cols"] = list(raw_cfg["identity_columns"])

    # ── Normalize Sections
    raw_sections = raw_cfg.get("sections", [])
    if isinstance(raw_sections, list) and raw_sections:
        normalized_sections = []
        for i, sec in enumerate(raw_sections):
            if not isinstance(sec, dict):
                continue
            s = dict(sec)
            s_name = s.get("name") or f"Section {i+1}"
            s_type = s.get("type", "marks_table")
            s["id"] = s.get("id") or f"sec_{i+1}_{re.sub(r'[^a-zA-Z0-9_]', '_', s_name.lower())}"
            s["name"] = s_name
            s["title"] = s.get("title") or s_name
            s["type"] = s_type
            s["enabled"] = bool(s.get("enabled", True))
            s["page_break_before"] = bool(s.get("page_break_before", False))
            s["columns"] = list(s.get("columns", []))
            s["col_cfg"] = dict(s.get("col_cfg", {})) if isinstance(s.get("col_cfg"), dict) else {}
            normalized_sections.append(s)
        if normalized_sections:
            cfg["sections"] = normalized_sections

    # ── Preserve runtime and dynamic keys if present in raw_cfg
    if "_batch_df" in raw_cfg:
        cfg["_batch_df"] = raw_cfg["_batch_df"]
    if "mi_eligibility_overrides" in raw_cfg:
        cfg["mi_eligibility_overrides"] = dict(raw_cfg["mi_eligibility_overrides"])
    if "logo_base64" in raw_cfg and raw_cfg["logo_base64"]:
        cfg["logo_base64"] = str(raw_cfg["logo_base64"])
    if "content_width" in raw_cfg:
        cfg["content_width"] = raw_cfg["content_width"]
    if "ai_model" in raw_cfg:
        cfg["ai_model"] = raw_cfg["ai_model"]
    if "show_rank" in raw_cfg:
        cfg["show_rank"] = bool(raw_cfg["show_rank"])

    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# TEMPLATE MANAGEMENT FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────
def get_template_dir() -> str:
    os.makedirs(TEMPLATE_DIR, exist_ok=True)
    return TEMPLATE_DIR


def list_templates() -> List[str]:
    """Returns sorted list of saved template names (without .json extension)."""
    tdir = get_template_dir()
    templates = []
    try:
        for f in os.listdir(tdir):
            if f.endswith(".json") and not f.startswith("."):
                templates.append(f[:-5])
    except Exception:
        pass
    return sorted(templates)


def load_template(name: str) -> Tuple[bool, Dict[str, Any], str]:
    """
    Loads a template by name, validates it, and fills missing values from default.
    Returns (success: bool, config: dict, message: str).
    """
    tdir = get_template_dir()
    filepath = os.path.join(tdir, f"{name}.json")
    if not os.path.exists(filepath):
        return False, json.loads(json.dumps(DEFAULT_REPORT_CONFIG)), f"Template '{name}' not found."

    try:
        with open(filepath, "r", encoding="utf-8") as fh:
            raw_data = json.load(fh)
        normalized = validate_and_normalize_config(raw_data)
        return True, normalized, f"Template '{name}' loaded successfully."
    except Exception as e:
        return False, json.loads(json.dumps(DEFAULT_REPORT_CONFIG)), f"Failed to load '{name}': {str(e)}"


def save_template(name: str, config: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Saves a report template to JSON. Validates configuration before writing.
    Returns (success: bool, message: str).
    """
    safe_name = re.sub(r'[^A-Za-z0-9_\- ]', '', name).strip()
    if not safe_name:
        return False, "Invalid template name. Please use alphanumeric characters."

    tdir = get_template_dir()
    filepath = os.path.join(tdir, f"{safe_name}.json")

    normalized = validate_and_normalize_config(config)
    normalized["saved_at"] = datetime.now().isoformat()
    normalized["template_name"] = safe_name

    try:
        with open(filepath, "w", encoding="utf-8") as fh:
            json.dump(normalized, fh, indent=2)
        return True, f"Template '{safe_name}' saved successfully."
    except Exception as e:
        return False, f"Failed to save template '{safe_name}': {str(e)}"


def duplicate_template(source_name: str, new_name: str) -> Tuple[bool, str]:
    """Duplicates an existing template with a new name."""
    ok, cfg, msg = load_template(source_name)
    if not ok:
        return False, f"Cannot duplicate: {msg}"
    return save_template(new_name, cfg)


def delete_template(name: str) -> Tuple[bool, str]:
    """Deletes a saved template by name."""
    tdir = get_template_dir()
    filepath = os.path.join(tdir, f"{name}.json")
    if not os.path.exists(filepath):
        return False, f"Template '{name}' does not exist."
    try:
        os.remove(filepath)
        return True, f"Template '{name}' deleted."
    except Exception as e:
        return False, f"Failed to delete '{name}': {str(e)}"


def export_template_json(config: Dict[str, Any]) -> str:
    """Exports configuration as pretty JSON string."""
    normalized = validate_and_normalize_config(config)
    return json.dumps(normalized, indent=2)


def import_template_json(json_str: str) -> Tuple[bool, Dict[str, Any], str]:
    """Parses and validates an imported JSON configuration string."""
    try:
        raw = json.loads(json_str)
        if not isinstance(raw, dict):
            return False, {}, "Invalid JSON: top-level object must be a dictionary."
        normalized = validate_and_normalize_config(raw)
        return True, normalized, "Template imported and validated successfully."
    except Exception as e:
        return False, {}, f"Invalid JSON format: {str(e)}"
