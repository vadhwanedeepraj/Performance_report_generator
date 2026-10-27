"""
report_theme.py
===============
ReportLab design system & style generator for Report-Generator.
Centralizes page geometry, intro card styles, and table style variants.

All defaults mirror the existing Innomatics branding — zero visual change
unless the user explicitly overrides via the Streamlit UI.
"""

from typing import Dict, Any, Tuple
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, letter, legal, landscape
from reportlab.platypus import TableStyle

# ─────────────────────────────────────────────────────────────────────────────
# PAGE GEOMETRY RESOLUTION
# ─────────────────────────────────────────────────────────────────────────────
PAGE_SIZE_MAP = {
    "a4":     A4,
    "letter": letter,
    "legal":  legal,
}

def get_page_geometry(cfg: Dict[str, Any]) -> Tuple[float, float, float, tuple]:
    """
    Compute page dimensions from the cfg dict.

    Returns (page_width, page_height, content_width, final_pagesize_tuple).

    Defaults:
        page_size   = "A4"
        orientation = "portrait"
        margin_left = 40
        margin_right= 40
    These reproduce the current hardcoded behaviour exactly.
    """
    ps_key = str(cfg.get("page_size", "A4")).lower().strip()
    base_size = PAGE_SIZE_MAP.get(ps_key, A4)

    is_landscape = str(cfg.get("orientation", "portrait")).lower().strip() == "landscape"
    final_pagesize = landscape(base_size) if is_landscape else base_size

    page_w, page_h = final_pagesize

    margin_left  = float(cfg.get("margin_left",  40))
    margin_right = float(cfg.get("margin_right", 40))
    content_w    = page_w - margin_left - margin_right

    return page_w, page_h, content_w, final_pagesize


# ─────────────────────────────────────────────────────────────────────────────
# INTRO CARD STYLE
# ─────────────────────────────────────────────────────────────────────────────

def build_intro_card_style(cfg: Dict[str, Any]) -> TableStyle:
    """
    Build a TableStyle for the intro text card.

    cfg keys used:
        intro_card_style  : "callout" | "banner" | "clean"   (default "callout")
        primary_color     : hex string                         (default "#B91C1C")
        intro_bg_color    : hex string                         (default "#F8FAFC")
        intro_border_color: hex string                         (default primary_color)

    "callout" — light bg + thick left red border (matches existing section heading bars)
    "banner"  — red top + full box border
    "clean"   — minimal padding, no decoration
    """
    c_primary = cfg.get("primary_color",      "#B91C1C")
    c_bg      = cfg.get("intro_bg_color",     "#F8FAFC")
    c_border  = cfg.get("intro_border_color", c_primary)
    style     = str(cfg.get("intro_card_style", "callout")).lower().strip()

    if style == "callout":
        return TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor(c_bg)),
            ("LINEBEFORE",    (0, 0), (0, -1),  3.5, colors.HexColor(c_border)),
            ("BOX",           (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
            ("TOPPADDING",    (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING",   (0, 0), (-1, -1), 10),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
        ])
    elif style == "banner":
        return TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor(c_bg)),
            ("LINEABOVE",     (0, 0), (-1, 0),  3.0, colors.HexColor(c_border)),
            ("BOX",           (0, 0), (-1, -1), 0.8, colors.HexColor(c_border)),
            ("TOPPADDING",    (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING",   (0, 0), (-1, -1), 10),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
        ])
    else:  # "clean" or any unknown value → minimal, no decoration
        return TableStyle([
            ("TOPPADDING",    (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LEFTPADDING",   (0, 0), (-1, -1), 2),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 2),
        ])


# ─────────────────────────────────────────────────────────────────────────────
# TABLE BORDER STYLE VARIANTS
# ─────────────────────────────────────────────────────────────────────────────

def apply_table_border_style(ts: TableStyle, cfg: Dict[str, Any], header_bg: str) -> TableStyle:
    """
    Apply the user-selected table border style to an existing TableStyle.

    cfg keys used:
        table_border_style: "grid" | "horizontal_only" | "boxed" | "subtle" | "minimal"
                            (default "grid" — matches current behaviour exactly)
        table_border_color: hex string (default "#D1D5DB")
        table_border_width: float     (default 0.4)

    Returns the same TableStyle object with border commands added.
    """
    b_style  = str(cfg.get("table_border_style", "grid")).lower().strip()
    c_border = cfg.get("table_border_color", "#D1D5DB")
    b_width  = float(cfg.get("table_border_width", 0.4))

    if b_style == "grid":
        ts.add("GRID", (0, 0), (-1, -1), b_width, colors.HexColor(c_border))
    elif b_style == "horizontal_only":
        ts.add("LINEBELOW", (0, 1), (-1, -1), b_width, colors.HexColor(c_border))
    elif b_style == "boxed":
        ts.add("BOX",      (0, 0), (-1, -1), b_width * 1.5, colors.HexColor(c_border))
        ts.add("LINEBELOW",(0, 0), (-1, 0),  b_width * 1.2, colors.HexColor(header_bg))
    elif b_style == "subtle":
        ts.add("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#E2E8F0"))
    # "minimal" — no grid, only the header underline already in corporate_table_style

    return ts
