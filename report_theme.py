"""
report_theme.py
===============
ReportLab design system & style generator for Report-Generator.
Centralizes all typography, color palettes, page geometry calculations,
and table styles based on configuration dictionaries.
"""

from typing import Dict, Any, Tuple
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, letter, legal, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT, TA_JUSTIFY
from reportlab.platypus import TableStyle

# ─────────────────────────────────────────────────────────────────────────────
# PAGE GEOMETRY RESOLUTION
# ─────────────────────────────────────────────────────────────────────────────
PAGE_SIZE_MAP = {
    "a4": A4,
    "letter": letter,
    "legal": legal,
}

def get_page_geometry(cfg: Dict[str, Any]) -> Tuple[float, float, float, Tuple[float, float]]:
    """
    Computes dimensions for the document:
    Returns (page_width, page_height, content_width, raw_pagesize_tuple).
    """
    ps_key = str(cfg.get("page_size", "A4")).lower().strip()
    base_size = PAGE_SIZE_MAP.get(ps_key, A4)

    is_landscape = str(cfg.get("orientation", "portrait")).lower().strip() == "landscape"
    final_pagesize = landscape(base_size) if is_landscape else base_size

    page_w, page_h = final_pagesize

    margin_left = float(cfg.get("margin_left", 40))
    margin_right = float(cfg.get("margin_right", 40))
    content_w = page_w - margin_left - margin_right

    return page_w, page_h, content_w, final_pagesize


# ─────────────────────────────────────────────────────────────────────────────
# TYPOGRAPHY & PARAGRAPH STYLES
# ─────────────────────────────────────────────────────────────────────────────
FONT_FAMILY_MAP = {
    "helvetica": ("Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique"),
    "times-roman": ("Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic"),
    "times": ("Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic"),
    "courier": ("Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique"),
}

def resolve_font_family(family_name: str) -> Tuple[str, str, str, str]:
    clean = str(family_name or "helvetica").lower().strip()
    return FONT_FAMILY_MAP.get(clean, FONT_FAMILY_MAP["helvetica"])


def build_styles(cfg: Dict[str, Any]) -> Dict[str, ParagraphStyle]:
    """
    Builds dynamic ReportLab ParagraphStyle dictionary customized by user configuration.
    """
    base = getSampleStyleSheet()

    # Font setup
    font_reg, font_bold, font_italic, font_bold_italic = resolve_font_family(cfg.get("font_family", "Helvetica"))

    # Sizing
    sz_title   = float(cfg.get("title_font_size", 13))
    sz_heading = float(cfg.get("heading_font_size", 10))
    sz_body    = float(cfg.get("body_font_size", 8))
    sz_cell    = float(cfg.get("table_font_size", 7.5))
    sz_footer  = float(cfg.get("footer_font_size", 6.5))

    line_mult  = float(cfg.get("line_spacing", 1.3))
    para_space = float(cfg.get("paragraph_spacing", 3))

    # Colors
    c_primary = cfg.get("primary_color", "#B91C1C")
    c_header_bg = cfg.get("header_bg_color", c_primary)
    c_text = cfg.get("text_color", "#334155")
    c_heading_text = cfg.get("heading_text_color", "#0F172A")
    c_gold = cfg.get("gold_accent", "#C9A84C")

    def ps(name, **kw):
        return ParagraphStyle(name, parent=base["Normal"], **kw)

    return {
        # Document Titles
        "doc_title": ps("DocTitle",
            fontName=font_bold, fontSize=sz_title, alignment=TA_CENTER,
            textColor=colors.HexColor(c_primary), spaceAfter=2, leading=sz_title * line_mult),

        "doc_subtitle": ps("DocSub",
            fontName=font_reg, fontSize=sz_body * 1.05, alignment=TA_CENTER,
            textColor=colors.HexColor("#64748B"), spaceAfter=para_space, leading=sz_body * line_mult),

        "center": ps("CenterText",
            fontName=font_reg, fontSize=sz_body * 1.15, alignment=TA_CENTER,
            textColor=colors.HexColor(c_text), leading=sz_body * line_mult * 1.15),

        # Section Headings
        "section_head": ps("SecHead",
            fontName=font_bold, fontSize=sz_heading, textColor=colors.HexColor(c_primary),
            spaceBefore=para_space * 2, spaceAfter=para_space, leading=sz_heading * line_mult),

        "section_sub": ps("SecSub",
            fontName=font_bold, fontSize=sz_body, textColor=colors.HexColor(c_heading_text),
            spaceBefore=para_space * 1.5, spaceAfter=para_space * 0.7, leading=sz_body * line_mult),

        # Body Text
        "body": ps("Body",
            fontName=font_reg, fontSize=sz_body, textColor=colors.HexColor(c_text),
            leading=sz_body * line_mult, spaceAfter=para_space),

        "body_italic": ps("BodyI",
            fontName=font_italic, fontSize=sz_body, textColor=colors.HexColor("#64748B"),
            leading=sz_body * line_mult, spaceAfter=para_space),

        "body_bold": ps("BodyB",
            fontName=font_bold, fontSize=sz_body, textColor=colors.HexColor(c_heading_text),
            leading=sz_body * line_mult, spaceAfter=para_space * 0.7),

        # Table Cells
        "cell": ps("Cell",
            fontName=font_reg, fontSize=sz_cell, textColor=colors.HexColor(c_text),
            leading=sz_cell * line_mult),

        "cell_bold": ps("CellB",
            fontName=font_bold, fontSize=sz_cell, textColor=colors.HexColor(c_heading_text),
            leading=sz_cell * line_mult),

        "cell_header": ps("CellH",
            fontName=font_bold, fontSize=sz_cell + 0.5, textColor=colors.white,
            leading=(sz_cell + 0.5) * line_mult),

        "cell_color": ps("CellC",
            fontName=font_bold, fontSize=sz_cell + 0.5,
            leading=(sz_cell + 0.5) * line_mult),

        # Bullets
        "bullet": ps("Bullet",
            fontName=font_reg, fontSize=sz_body, textColor=colors.HexColor(c_text),
            leftIndent=12, firstLineIndent=-9, leading=sz_body * line_mult, spaceAfter=para_space * 0.6),

        "bullet_bold": ps("BulletB",
            fontName=font_bold, fontSize=sz_body, textColor=colors.HexColor(c_primary),
            leftIndent=12, firstLineIndent=-9, leading=sz_body * line_mult, spaceAfter=para_space * 0.6),

        # Intro Paragraph
        "intro": ps("Intro",
            fontName=font_italic, fontSize=sz_body, textColor=colors.HexColor("#475569"),
            leading=sz_body * line_mult, spaceAfter=para_space, leftIndent=8, rightIndent=8),

        # Rank / KPI
        "rank_text": ps("Rank",
            fontName=font_bold, fontSize=sz_title * 1.1, textColor=colors.HexColor(c_gold),
            alignment=TA_CENTER, leading=sz_title * 1.3),

        # Footer
        "footer_text": ps("Footer",
            fontName=font_reg, fontSize=sz_footer, textColor=colors.HexColor("#94A3B8"),
            alignment=TA_CENTER, leading=sz_footer * 1.2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# TABLE STYLES
# ─────────────────────────────────────────────────────────────────────────────
def build_corporate_table_style(cfg: Dict[str, Any], header_bg: str = None) -> TableStyle:
    """
    Builds a flexible TableStyle from user table customization settings.
    """
    c_header = header_bg or cfg.get("header_bg_color", cfg.get("primary_color", "#B91C1C"))
    c_border = cfg.get("table_border_color", "#D1D5DB")
    b_width  = float(cfg.get("table_border_width", 0.5))
    pad_v    = float(cfg.get("table_cell_padding_v", 5))
    pad_h    = float(cfg.get("table_cell_padding_h", 7))
    b_style  = str(cfg.get("table_border_style", "grid")).lower().strip()
    stripes  = bool(cfg.get("table_zebra_stripes", True))
    c_stripe = cfg.get("table_stripe_color", "#F8FAFC")
    align    = str(cfg.get("table_alignment", "left")).upper()
    if align not in ("LEFT", "CENTER", "RIGHT"):
        align = "LEFT"

    base_commands = [
        # Header Row
        ("BACKGROUND",    (0, 0), (-1, 0),  colors.HexColor(c_header)),
        ("TEXTCOLOR",     (0, 0), (-1, 0),  colors.white),
        ("FONTNAME",      (0, 0), (-1, 0),  resolve_font_family(cfg.get("font_family"))[1]),
        ("FONTSIZE",      (0, 0), (-1, 0),  float(cfg.get("table_font_size", 7.5)) + 1.0),
        ("TOPPADDING",    (0, 0), (-1, 0),  pad_v + 1.5),
        ("BOTTOMPADDING", (0, 0), (-1, 0),  pad_v + 1.5),
        # Body Rows
        ("FONTNAME",      (0, 1), (-1, -1), resolve_font_family(cfg.get("font_family"))[0]),
        ("FONTSIZE",      (0, 1), (-1, -1), float(cfg.get("table_font_size", 7.5))),
        ("TOPPADDING",    (0, 1), (-1, -1), pad_v),
        ("BOTTOMPADDING", (0, 1), (-1, -1), pad_v),
        ("LEFTPADDING",   (0, 0), (-1, -1), pad_h),
        ("RIGHTPADDING",  (0, 0), (-1, -1), pad_h),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN",         (0, 0), (-1, -1), align),
        ("LINEBELOW",     (0, 0), (-1, 0),  1.4, colors.HexColor(c_header)),
    ]

    # Borders based on style
    if b_style == "grid":
        base_commands.append(("GRID", (0, 0), (-1, -1), b_width, colors.HexColor(c_border)))
    elif b_style == "horizontal_only":
        base_commands.append(("LINEBELOW", (0, 1), (-1, -1), b_width, colors.HexColor(c_border)))
    elif b_style == "boxed":
        base_commands.append(("BOX", (0, 0), (-1, -1), b_width * 1.5, colors.HexColor(c_border)))
        base_commands.append(("LINEBELOW", (0, 0), (-1, 0), b_width * 1.2, colors.HexColor(c_border)))
    elif b_style == "subtle":
        base_commands.append(("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#E2E8F0")))
    # "minimal" has no extra grid lines, only header divider

    # Zebra stripes
    if stripes:
        base_commands.append(("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor(c_stripe)]))

    return TableStyle(base_commands)


def build_accent_table_style(cfg: Dict[str, Any]) -> TableStyle:
    """
    Subtle 2-column key-value table style.
    """
    c_border = cfg.get("table_border_color", "#D1D5DB")
    b_width  = float(cfg.get("table_border_width", 0.5))
    pad_v    = float(cfg.get("table_cell_padding_v", 5))
    pad_h    = float(cfg.get("table_cell_padding_h", 7))
    c_stripe = cfg.get("table_stripe_color", "#F8FAFC")

    return TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), colors.white),
        ("FONTNAME",      (0, 0), (0, -1),  resolve_font_family(cfg.get("font_family"))[1]),
        ("FONTNAME",      (1, 0), (1, -1),  resolve_font_family(cfg.get("font_family"))[0]),
        ("FONTSIZE",      (0, 0), (-1, -1), float(cfg.get("table_font_size", 7.5)) + 0.5),
        ("TOPPADDING",    (0, 0), (-1, -1), pad_v),
        ("BOTTOMPADDING", (0, 0), (-1, -1), pad_v),
        ("LEFTPADDING",   (0, 0), (-1, -1), pad_h + 2),
        ("RIGHTPADDING",  (0, 0), (-1, -1), pad_h + 2),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("GRID",          (0, 0), (-1, -1), b_width, colors.HexColor(c_border)),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, colors.HexColor(c_stripe)]),
    ])
