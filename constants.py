"""
constants.py
============
Single source of truth for all grade scales, rating maps, and shared
thresholds used across report_engine.py, streamlit_app.py, and any
future modules.

NEVER define GRADE_SCALE or RATING_MAP inline in other files —
always import from here.
"""

# ─────────────────────────────────────────────────────────────────────
# RATING MAP  (word → numeric, 1–4 scale)
# ─────────────────────────────────────────────────────────────────────
RATING_MAP: dict[str, int] = {
    "poor":      1,
    "average":   2,
    "good":      3,
    "excellent": 4,
}

# ─────────────────────────────────────────────────────────────────────
# GRADE SCALE — 0‑to‑4 float  (used by report_engine.py PDF renderer)
#
# Each tuple: (min_threshold, grade_label, hex_color, verdict_text)
# Listed highest→lowest so the first match wins.
# ─────────────────────────────────────────────────────────────────────
GRADE_SCALE_0_4: list[tuple] = [
    (3.5, "A+", "#059669", "Pass — Outstanding"),
    (3.0, "A",  "#16a34a", "Pass — Proficient"),
    (2.5, "B",  "#d97706", "Average — Needs Effort"),
    (1.5, "C",  "#ea580c", "Poor — Serious Gaps"),
    (0.0, "D",  "#dc2626", "Critical Gap — Action Now"),
]

# ─────────────────────────────────────────────────────────────────────
# GRADE SCALE — 0‑to‑100 percentage  (used by streamlit_app.py UI)
#
# Each tuple: (min_pct_threshold, grade_label, hex_color, verdict_text)
# Listed highest→lowest so the first match wins.
# ─────────────────────────────────────────────────────────────────────
GRADE_SCALE_PCT: list[tuple] = [
    (87.5, "A+", "#059669", "Pass — Outstanding"),
    (75.0, "A",  "#16a34a", "Pass — Proficient"),
    (62.5, "B",  "#d97706", "Average — Needs Effort"),
    (37.5, "C",  "#ea580c", "Poor — Serious Gaps"),
    (0.0,  "D",  "#dc2626", "Critical Gap — Action Now"),
]

# ─────────────────────────────────────────────────────────────────────
# HELPER FUNCTIONS
# ─────────────────────────────────────────────────────────────────────

def grade_label_from_score_0_4(score: float) -> tuple[str, str, str]:
    """
    Map a 0–4 float score to (grade_label, hex_color, verdict_text).
    Used by report_engine.py.
    """
    for threshold, label, color, verdict in GRADE_SCALE_0_4:
        if score >= threshold:
            return label, color, verdict
    return "D", "#dc2626", "Critical Gap — Action Now"


def grade_label_from_pct(score_pct: float) -> tuple[str, str, str]:
    """
    Map a 0–100 percentage to (grade_label, hex_color, verdict_text).
    Used by streamlit_app.py dashboard and compute_grade().
    """
    for threshold, label, color, verdict in GRADE_SCALE_PCT:
        if score_pct >= threshold:
            return label, color, verdict
    return "D", "#dc2626", "Critical Gap — Action Now"


# ─────────────────────────────────────────────────────────────────────
# MOCK INTERVIEW ELIGIBILITY STATUS LABELS
# ─────────────────────────────────────────────────────────────────────
MOCK_LABEL_SHORTLISTED: str = "Eligibility Shortlisted"
MOCK_LABEL_REVISION:    str = "Revision followed by Mock Interview"

MOCK_PDF_LABEL_SHORTLISTED: str = "Shortlisted"
MOCK_PDF_LABEL_REVISION:    str = "Revision followed by Mock Interview"


# ─────────────────────────────────────────────────────────────────────
# UPLOAD & PROCESSING LIMITS
# ─────────────────────────────────────────────────────────────────────
MAX_CSV_SIZE_MB:  int = 50        # reject CSV uploads larger than this
MAX_CSV_ROWS:     int = 10_000    # warn if row count exceeds this
MAX_BATCH_WORKERS: int = 8        # parallel PDF threads in ThreadPoolExecutor

# ─────────────────────────────────────────────────────────────────────
# AI CACHE
# ─────────────────────────────────────────────────────────────────────
AI_CACHE_DIR:      str = ".ai_cache"
AI_CACHE_TTL_DAYS: int = 7        # delete cache files older than this
