"""
test_designer_and_engine.py
===========================
Automated test suite validating:
1. Dynamic page sizes: A4, Letter, Legal
2. Orientations: Portrait, Landscape
3. Custom page margins and spacing
4. Font families: Helvetica, Times-Roman, Courier
5. Table formatting: borders, padding, zebra stripes
6. Section ordering and page breaks ('page_break_before')
7. Template management: load, save, duplicate, delete, export, import
8. Batch PDF generation to ZIP
9. Corrupted/incomplete config fallback & recovery
"""

import os
import json
import io
import zipfile
import pandas as pd

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
from report_engine import build_single_report, build_all_reports

def run_all_tests():
    print("==================================================")
    print("RUNNING REPORT GENERATOR & DESIGNER TEST SUITE")
    print("==================================================")

    # ── Test 1: Page Geometries (A4, Letter, Legal & Landscape)
    print("\n--- Test 1: Page Geometry Calculations ---")
    for ps in ["A4", "Letter", "Legal"]:
        for orient in ["portrait", "landscape"]:
            cfg = {"page_size": ps, "orientation": orient, "margin_left": 30, "margin_right": 30}
            pw, ph, cw, raw = get_page_geometry(cfg)
            assert pw > 0 and ph > 0 and cw == (pw - 60), f"Geometry failed for {ps} {orient}"
            print(f"  [PASS] {ps} {orient}: W={pw:.1f}, H={ph:.1f}, ContentW={cw:.1f}")

    # ── Test 2: Font Families
    print("\n--- Test 2: Font Families & Styles ---")
    for ff in ["Helvetica", "Times-Roman", "Courier"]:
        cfg = {"font_family": ff, "title_font_size": 15, "heading_font_size": 11}
        styles = build_styles(cfg)
        assert "doc_title" in styles and "cell" in styles, f"Styles missing for {ff}"
        print(f"  [PASS] Font family '{ff}' built {len(styles)} styles successfully.")

    # ── Test 3: Table Styles & Padding
    print("\n--- Test 3: Table Formatting Variations ---")
    for bs in ["grid", "horizontal_only", "boxed", "subtle", "minimal"]:
        cfg = {
            "table_border_style": bs,
            "table_border_color": "#123456",
            "table_cell_padding_v": 8,
            "table_cell_padding_h": 12,
            "table_zebra_stripes": True,
        }
        ts = build_corporate_table_style(cfg)
        assert ts is not None, f"Table style creation failed for {bs}"
        print(f"  [PASS] Table style '{bs}' created with custom padding.")

    # ── Test 4: PDF Generation with Custom Section Ordering & Page Break
    print("\n--- Test 4: PDF Generation with Dynamic Section Ordering & Page Break ---")
    sample_row = pd.Series({
        "Student Name": "Bjarne Stroustrup",
        "Enrollment ID": "CPP-1985-001",
        "Batch #": "Systems Core",
        "Python Prog (out of 100)": 98,
        "EDA Foundation (out of 100)": 94,
        "MySQL and PowerBI (out of 100)": 90,
        "Communication & Soft Skills": "Excellent",
        "Remarks": "Remarkable architecture and design principles.",
    })

    cfg_custom = {
        "company_name": "Antigravity Engineering Academy",
        "report_title": "ADVANCED SYSTEM EVALUATION",
        "page_size": "A4",
        "orientation": "portrait",
        "margin_top": 115,
        "margin_bottom": 40,
        "margin_left": 40,
        "margin_right": 40,
        "font_family": "Helvetica",
        "primary_color": "#1B365D",
        "secondary_color": "#2C3E50",
        "gold_accent": "#D4AF37",
        "identity_columns": ["Enrollment ID", "Student Name", "Batch #"],
        "sections": [
            {
                "id": "sec_id",
                "name": "Candidate Profile",
                "title": "Candidate Details",
                "type": "identity_card",
                "enabled": True,
                "page_break_before": False,
            },
            {
                "id": "sec_marks",
                "name": "Technical Performance",
                "title": "Module Performance Breakdown",
                "type": "marks_table",
                "columns": ["Python Prog (out of 100)", "EDA Foundation (out of 100)", "MySQL and PowerBI (out of 100)"],
                "enabled": True,
                "page_break_before": False,
                "col_cfg": {
                    "Python Prog (out of 100)": {"label": "C++ & Python Systems", "max": 100},
                    "EDA Foundation (out of 100)": {"label": "Data Architecture", "max": 100},
                    "MySQL and PowerBI (out of 100)": {"label": "Databases & Storage", "max": 100},
                }
            },
            {
                "id": "sec_page2",
                "name": "Instructor Feedback (Page 2)",
                "title": "Detailed Instructor Remarks & Evaluation",
                "type": "text_field",
                "columns": ["Remarks"],
                "enabled": True,
                "page_break_before": True,  # FORCE PAGE BREAK!
            }
        ],
        "legal_notice": "Confidential Academy Report. Authorized distribution only.",
        "show_page_numbers": True,
    }

    pdf_bytes = build_single_report(sample_row, cfg_custom)
    assert len(pdf_bytes) > 5000, "PDF too small or empty"
    with open("test_custom_designer_report.pdf", "wb") as f:
        f.write(pdf_bytes)
    print(f"  [PASS] Single report generated with PageBreak ({len(pdf_bytes)} bytes) -> test_custom_designer_report.pdf")

    # ── Test 5: Template CRUD
    print("\n--- Test 5: Template Management (Save, Load, Duplicate, Delete, Export, Import) ---")
    test_tpl_name = "_Automated_Test_Template_"
    save_ok, save_msg = save_template(test_tpl_name, cfg_custom)
    assert save_ok, f"Save template failed: {save_msg}"
    print(f"  [PASS] {save_msg}")

    load_ok, loaded_cfg, load_msg = load_template(test_tpl_name)
    assert load_ok and loaded_cfg["report_title"] == "ADVANCED SYSTEM EVALUATION", "Load template failed"
    print(f"  [PASS] {load_msg}")

    dup_tpl_name = "_Automated_Test_Template_Copy_"
    dup_ok, dup_msg = duplicate_template(test_tpl_name, dup_tpl_name)
    assert dup_ok, f"Duplicate template failed: {dup_msg}"
    print(f"  [PASS] {dup_msg}")

    json_str = export_template_json(loaded_cfg)
    imp_ok, imp_cfg, imp_msg = import_template_json(json_str)
    assert imp_ok and imp_cfg["company_name"] == "Antigravity Engineering Academy", "Export/Import JSON failed"
    print(f"  [PASS] Export and re-import JSON validated successfully.")

    del_ok1, del_msg1 = delete_template(test_tpl_name)
    del_ok2, del_msg2 = delete_template(dup_tpl_name)
    assert del_ok1 and del_ok2, "Delete template failed"
    print(f"  [PASS] Cleaned up temporary test templates.")

    # ── Test 6: Batch Generation to ZIP
    print("\n--- Test 6: Batch PDF Generation to ZIP Archive ---")
    df_batch = pd.DataFrame([
        {
            "Enrollment ID": f"INN-2026-{i:03d}",
            "Student Name": f"Candidate {i}",
            "Batch #": "Batch 455",
            "Python Prog (out of 100)": 80 + i * 2,
            "EDA Foundation (out of 100)": 75 + i * 3,
            "MySQL and PowerBI (out of 100)": 85 + i,
            "Remarks": f"Strong student performance in candidate {i}.",
        }
        for i in range(1, 6)
    ])

    zip_bytes = build_all_reports(df_batch, cfg_custom, id_column="Enrollment ID")
    assert len(zip_bytes) > 10000, "ZIP archive too small or empty"
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        file_list = zf.namelist()
        assert len(file_list) == 5, f"Expected 5 PDFs in zip, got {len(file_list)}"
        for name in file_list:
            assert name.endswith("_report.pdf"), f"Unexpected filename in zip: {name}"
    print(f"  [PASS] Batch generated {len(file_list)} PDFs in ZIP archive ({len(zip_bytes)} bytes).")

    # ── Test 7: Fallback & Recovery on Corrupted Config
    print("\n--- Test 7: Fallback & Recovery on Corrupted / Partial Config ---")
    corrupted_cfg = {
        "page_size": "NonExistentSize",
        "margin_top": "invalid_string_number",
        "font_family": "UnknownFont",
        "sections": "not_a_list",
    }
    recovered_cfg = validate_and_normalize_config(corrupted_cfg)
    assert recovered_cfg["page_size"] == "A4" or recovered_cfg["margin_top"] == 115, "Recovery failed"
    pdf_recovered = build_single_report(sample_row, recovered_cfg)
    assert len(pdf_recovered) > 5000, "Failed to render PDF using recovered configuration"
    print(f"  [PASS] Corrupted configuration gracefully recovered and rendered PDF ({len(pdf_recovered)} bytes).")

    print("\n==================================================")
    print("ALL 7 TEST SUITE STAGES PASSED CLEANLY!")
    print("==================================================")

if __name__ == "__main__":
    run_all_tests()
