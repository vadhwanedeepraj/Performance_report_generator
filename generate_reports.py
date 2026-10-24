import pandas as pd
import os
import re
import groq
from groq import Groq

from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import TableStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import PageBreak

# =====================================================
# CONFIGURATION
# =====================================================

COMPANY_NAME = "Innomatics Research Labs"
REPORT_NAME = "Python Technical Interview Evaluation Report"

INPUT_FILE = "Python_Mock_interview.csv"
OUTPUT_FOLDER = "reports"

LOGO_PATH = "logo/innomatics_logo.png"


# Required Column Configuration
STUDENT_INFO_COLUMNS = [
    "Enrollement ID",
    "Student Name",
    "Branch",
    "Updated Branch",
    "Batch No",
    "Interviewer Name",
    "How Many Questions Attempted",
    "Total Marks"
]

TECHNICAL_COLUMNS = [
    "Concept  Understanding \n1.Poor\n2. Needs Improvement\n3. Good\n4. Excellent [Identifiers | Data Types]",
    "Concept  Understanding \n1.Poor\n2. Needs Improvement\n3. Good\n4. Excellent [Conditional Statements]",
    "Concept  Understanding \n1.Poor\n2. Needs Improvement\n3. Good\n4. Excellent [Loops]",
    "Concept  Understanding \n1.Poor\n2. Needs Improvement\n3. Good\n4. Excellent [Functions]",
    "Concept  Understanding \n1.Poor\n2. Needs Improvement\n3. Good\n4. Excellent [OOPS (Theory Understanding)]",
    "Concept  Understanding \n1.Poor\n2. Needs Improvement\n3. Good\n4. Excellent [Problem Solving]"
]

COMMUNICATION_COLUMNS = [
    "Communication \n\n1.Poor\n2. Needs Improvement\n3. Good\n4. Excellent [Self Introduction]",
    "Communication \n\n1.Poor\n2. Needs Improvement\n3. Good\n4. Excellent [Explanation Skills]",
    "Communication \n\n1.Poor\n2. Needs Improvement\n3. Good\n4. Excellent [Soft Skills ]",
    "Communication \n\n1.Poor\n2. Needs Improvement\n3. Good\n4. Excellent [Confidence]"
]

FEEDBACK_COLUMNS = [
    "Action Plan",
    "Remarks"
]


# =====================================================
# LOAD DATA
# =====================================================

def load_data():
    df = pd.read_csv(INPUT_FILE)
    df.columns = [c.strip() for c in df.columns]
    df.rename(columns={'Udated Branch': 'Updated Branch'}, inplace=True)
    return df

    
# =====================================================
# VALIDATE REQUIRED COLUMNS
# =====================================================

def validate_columns(df):

    required = (
        STUDENT_INFO_COLUMNS
        + TECHNICAL_COLUMNS
        + COMMUNICATION_COLUMNS
        + FEEDBACK_COLUMNS
    )

    missing = [col for col in required if col not in df.columns]

    if missing:
        missing_str = "\n".join(missing)
        raise ValueError(f"Missing Columns in File:\n{missing_str}")


# =====================================================
# CLEAN COLUMN NAME
# =====================================================

def clean_column_name(col):

    if "[" in col and "]" in col:
        return col.split("[")[-1].replace("]", "").strip()

    return col



# =====================================================
# SKILL GAP DETECTION (Helper for Groq)
# =====================================================

def detect_skill_gaps(row):
    weak = []
    for col in TECHNICAL_COLUMNS:
        val = str(row[col]).strip().lower()
        if "poor" in val or "needs improvement" in val:
            if "[" in col and "]" in col:
                weak.append(col.split("[")[-1].replace("]", "").strip())
            else:
                weak.append(col)
    return weak

# =====================================================
# GROQ AI ACTION PLAN
# =====================================================

def generate_groq_action_plan(row, client=None):
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return ""  # Skip if no API key is provided

    try:
        if client is None:
            client = Groq(api_key=api_key)
        
        student_name = str(row["Student Name"])
        tech_scores = {clean_column_name(col): str(row[col]) for col in TECHNICAL_COLUMNS}
        comm_scores = {clean_column_name(col): str(row[col]) for col in COMMUNICATION_COLUMNS}
        gaps = detect_skill_gaps(row)
        existing_plan = str(row.get("Action Plan", "")).strip()
        remarks = str(row.get("Remarks", "")).strip()

        tech_summary = "\n".join([f"- {k}: {v}" for k,v in tech_scores.items()])
        comm_summary = "\n".join([f"- {k}: {v}" for k,v in comm_scores.items()])

        prompt = f"""
        You are a highly experienced technical interviewer and mentor with 10+ years of experience in Python and software engineering.
        Your task is to provide a comprehensive, actionable, and structured Roadmap / Action Plan for a student based on their mock interview performance.

        Student Name: {student_name}

        Technical Evaluation:
        {tech_summary}

        Communication Evaluation:
        {comm_summary}

        Areas Needing Improvement (Skill Gaps):
        {", ".join(gaps) if gaps else "None"}

        Instructor's Action Plan:
        {existing_plan}

        Instructor's Remarks:
        {remarks}

        Please provide a structured response with EXACTLY the following sections. Ensure you use bullet points and clear actionable advice.
        
        **Key Strengths**
        - Add points here

        **Immediate Focus Areas (Next 1-2 Weeks)**
        - Add points here

        **60-Day Technical Upskilling Roadmap**
        - Add milestones here

        **Communication & Explanation Strategy**
        - Add points here

        **Daily Practice Routine**
        - Add points here

        Keep it professional, encouraging, and highly actionable. Return only the contents for these sections. Do not add conversational headers like "Sure, here is your response".
        """

        response = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
            temperature=0.4,
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"AI Action Plan Generation Skipped: {str(e)}"

def format_to_reportlab(text):
    if not text:
        return ""
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    text = text.replace("\n-", "<br/>&nbsp;&nbsp;&bull;&nbsp;")
    text = text.replace("\n", "<br/>")
    return text

# =====================================================
# GENERATE REPORTS
# =====================================================

def generate_reports():

    os.makedirs(OUTPUT_FOLDER, exist_ok=True)

    df = load_data()
    validate_columns(df)

    api_key = os.environ.get("GROQ_API_KEY")
    groq_client = Groq(api_key=api_key) if api_key else None

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "TitleCenter",
        parent=styles["Title"],
        alignment=TA_CENTER
    )

    subtitle_style = ParagraphStyle(
        "SubtitleCenter",
        parent=styles["Heading2"],
        alignment=TA_CENTER,
        textColor=colors.grey
    )

    for _, row in df.iterrows():

        student_name = str(row["Student Name"])
        enrollment_id = str(row["Enrollement ID"])

        story = []

        # ======================================
        # LOGO
        # ======================================

        if os.path.exists(LOGO_PATH):
            img = Image(LOGO_PATH, 2.8 * inch, 1 * inch)
            img.hAlign = "CENTER"
            story.append(img)

        story.append(Spacer(1, 20))

        story.append(Paragraph(REPORT_NAME, title_style))
        story.append(Paragraph(COMPANY_NAME, subtitle_style))

        story.append(Spacer(1, 25))

        # ======================================
        # INTRO TEXT
        # ======================================

        intro = f"""
        Dear <b>{student_name}</b>,<br/><br/>
        This report summarizes your performance in the Python Technical Interview
        conducted by <b>{COMPANY_NAME}</b>.
        """

        story.append(Paragraph(intro, styles["Normal"]))
        story.append(Spacer(1, 20))

        # ======================================
        # STUDENT INFO TABLE
        # ======================================

        info_rows = []

        for col in STUDENT_INFO_COLUMNS:
            # Highlight Total Marks
            if col == "Total Marks":
                val = Paragraph(f"<font color='#e42328'><b>{str(row[col])}</b></font>", styles["Normal"])
                col_name = Paragraph("<b>Total Marks</b>", styles["Normal"])
                info_rows.append([col_name, val])
            else:
                info_rows.append([col, str(row[col])])

        info_table = Table(info_rows, colWidths=[220, 240])

        info_table_style = [
            ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
            ('BACKGROUND', (0,0), (0,-1), colors.HexColor("#f1f2f6")), # Light grey for labels
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('TOPPADDING', (0,0), (-1,-1), 6),
            ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ]
        info_table.setStyle(TableStyle(info_table_style))

        story.append(Paragraph("Student Information", styles["Heading3"]))
        story.append(info_table)

        story.append(Spacer(1, 20))

#         # ======================================
#         # TECHNICAL EVALUATION
#         # ======================================

        tech_rows = [["Concept", "Rating"]]

        for i, col in enumerate(TECHNICAL_COLUMNS):
            concept = clean_column_name(col)
            tech_rows.append([concept, str(row[col])])

        tech_table = Table(tech_rows, colWidths=[320,140])

        tech_style = [
            ('BACKGROUND',(0,0),(-1,0),colors.HexColor("#e42328")), # Match Logo Red
            ('TEXTCOLOR',(0,0),(-1,0),colors.white),
            ('ALIGN',(0,0),(-1,0),'CENTER'),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('GRID',(0,0),(-1,-1),0.5,colors.grey),
            ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
            ('TOPPADDING',(0,0),(-1,-1), 6),
            ('BOTTOMPADDING',(0,0),(-1,-1), 6),
        ]

        # Alternating row colors
        for i in range(1, len(tech_rows)):
            if i % 2 == 0:
                tech_style.append(('BACKGROUND', (0, i), (-1, i), colors.HexColor("#f1f2f6")))

        tech_table.setStyle(TableStyle(tech_style))

        story.append(Paragraph("Technical Evaluation", styles["Heading3"]))
        story.append(tech_table)

        story.append(Spacer(1, 20))

        # ======================================
        # COMMUNICATION SKILLS
        # ======================================

        comm_rows = [["Skill", "Rating"]]

        for i, col in enumerate(COMMUNICATION_COLUMNS):
            skill = clean_column_name(col)
            comm_rows.append([skill, str(row[col])])

        comm_table = Table(comm_rows, colWidths=[320,140])

        comm_style = [
            ('BACKGROUND',(0,0),(-1,0),colors.HexColor("#e42328")), # Match Logo Red
            ('TEXTCOLOR',(0,0),(-1,0),colors.white),
            ('ALIGN',(0,0),(-1,0),'CENTER'),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('GRID',(0,0),(-1,-1),0.5,colors.grey),
            ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
            ('TOPPADDING',(0,0),(-1,-1), 6),
            ('BOTTOMPADDING',(0,0),(-1,-1), 6),
        ]

        for i in range(1, len(comm_rows)):
            if i % 2 == 0:
                comm_style.append(('BACKGROUND', (0, i), (-1, i), colors.HexColor("#f1f2f6")))

        comm_table.setStyle(TableStyle(comm_style))

        story.append(Paragraph("Communication Skills Evaluation", styles["Heading3"]))
        story.append(comm_table)

        story.append(Spacer(1, 20))

        # ======================================
        # FEEDBACK
        # ======================================

        feedback_rows = []

        for col in FEEDBACK_COLUMNS:
            col_name = Paragraph(f"<b><font color='white'>{col}</font></b>", styles["Normal"])
            feedback_text = str(row[col])
            if feedback_text.lower() == 'nan':
                feedback_text = ""
            p_text = Paragraph(feedback_text, styles["Normal"])
            feedback_rows.append([col_name, p_text])

        feedback_table = Table(feedback_rows, colWidths=[120, 340])

        feedback_table.setStyle(TableStyle([
            ('GRID',(0,0),(-1,-1),0.5,colors.grey),
            ('VALIGN',(0,0),(-1,-1),'TOP'),
            ('BACKGROUND', (0,0), (0,-1), colors.HexColor("#e42328")), # Match Logo Red for left labels
            ('TOPPADDING',(0,0),(-1,-1), 10),
            ('BOTTOMPADDING',(0,0),(-1,-1), 14)
        ]))

        story.append(Paragraph("Feedback", styles["Heading3"]))
        story.append(feedback_table)

        story.append(Spacer(1, 20))

        # ======================================
        # AI GENERATED ROADMAP (GROQ)
        # ======================================
        
        print(f"Generating AI Action Plan for: {student_name}")
        
        ai_plan = generate_groq_action_plan(row, client=groq_client)
        if ai_plan:
            story.append(Paragraph("AI-Generated Roadmap (Groq API)", styles["Heading3"]))
            formatted_plan = format_to_reportlab(ai_plan)
            story.append(Paragraph(formatted_plan, styles["Normal"]))
            story.append(Spacer(1, 20))

        # ======================================
        # FOOTER
        # ======================================

        footer = f"""
        This is a system-generated report prepared for student evaluation
        purposes by <b>{COMPANY_NAME}</b>.
        """

        story.append(Paragraph(footer, styles["Normal"]))

        # ======================================
        # SAVE PDF
        # ======================================

        filename_key = str(enrollment_id).strip()
        # Clean any slashes or weird chars if enrollment ID has them
        filename_key = re.sub(r'[^A-Za-z0-9_\-]', '_', filename_key)
        
        filepath = os.path.join(OUTPUT_FOLDER, f"{filename_key}_Report.pdf")

        pdf = SimpleDocTemplate(filepath, pagesize=A4)
        pdf.build(story)

    print("\nAll student reports generated successfully.")


# =====================================================
# RUN SCRIPT
# =====================================================

if __name__ == "__main__":
    def load_env():
        if os.path.exists(".env"):
            with open(".env", "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k:
                        os.environ.setdefault(k, v)
    
    load_env()
    generate_reports()