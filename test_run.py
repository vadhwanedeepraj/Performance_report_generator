import pandas as pd
from report_engine import build_single_report

cfg = {
    "company_name": "Test Company",
    "report_title": "Test Title",
    "primary_color": "#1F4E79",
    "secondary_color": "#f24b50",
    "accent_color": "#EEF2FF",
    "identity_columns": ["Student Name", "Enrollement ID"],
    "sections": [
        {"name": "Written Test", "type": "written_test", "enabled": True,
         "columns": {
             "Q1": {"label": "Q1", "min_req": 2, "max": 10},
         }},
        {"name": "Interview", "type": "rating_table", "enabled": True,
         "columns": ["Concept A", "Concept B"],
         "rating_scale": "1=Poor | 4=Excellent"},
        {"name": "Feedback", "type": "text_field", "enabled": True,
         "columns": ["Action Plan"]},
    ],
    "show_performance_summary": True,
    "performance_sections": ["Interview"],
    "show_watermark": True,
    "watermark_text": "VALIDATION TEST",
    "legal_notice": "Disclaimer: This is a custom test disclaimer notice for validation.",
}

row_data = {
    "Student Name": "Alice <unclosed-tag & malformed-ampersand",
    "Enrollement ID": "12345",
    "Q1": "8",
    "Concept A": "Good > test",
    "Concept B": "Excellent",
    "Action Plan": "Keep coding! <br> and <unmatched_bracket>"
}

row = pd.Series(row_data)

try:
    pdf_bytes = build_single_report(row, cfg)
    with open("test_out.pdf", "wb") as f:
        f.write(pdf_bytes)
    print("VALIDATION SUCCESS: PDF generated successfully.")
except Exception as e:
    print(f"VALIDATION FAILED: {e}")
