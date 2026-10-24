"""
email_sender.py
===============
Core email sending engine for the Report Generator — Email Dispatcher.

No Streamlit imports. All logic is UI-independent so it can be
tested and reused independently of the page layer.

Exports:
    validate_email, find_pdf_in_folder, find_pdf_in_zip_bytes,
    extract_zip_to_temp, markdown_to_html, render_jinja,
    build_html_email, html_to_plain,
    SMTP_PROVIDERS, connect_smtp, send_one, save_dispatch_log,
    BLOCK_DEFAULTS
"""

import os
import re
import csv
import time
import smtplib
import zipfile
import tempfile
import base64
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from typing import Optional

try:
    from jinja2 import Template, TemplateError
    JINJA2_AVAILABLE = True
except ImportError:
    JINJA2_AVAILABLE = False

LOG_DIR = "email_logs"

# ─────────────────────────────────────────────────────────────────────
# VALIDATION
# ─────────────────────────────────────────────────────────────────────

def validate_email(email: str) -> bool:
    """Return True if the email address has a valid format."""
    pattern = r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, str(email).strip()))


# ─────────────────────────────────────────────────────────────────────
# PDF ATTACHMENT RESOLUTION
# ─────────────────────────────────────────────────────────────────────

def _clean(val: str) -> str:
    s = str(val).strip()
    return "" if s.lower() in ("nan", "none", "", "n/a") else s


def find_pdf_in_folder(
    folder_path: str,
    enrollment_id: str = "",
    name: str = "",
) -> tuple[Optional[str], str]:
    """
    Locate a student's PDF in a local folder.
    Match priority: Enrollment ID (exact) → Name (exact) → Name (fuzzy substring).
    Returns (absolute_filepath | None, status_message).
    """
    if not folder_path or not os.path.isdir(folder_path):
        return None, "Folder Not Found"

    eid = _clean(enrollment_id)
    nm  = _clean(name)

    # 1 ─ Exact enrollment-ID filename
    if eid:
        p = os.path.join(folder_path, f"{eid}.pdf")
        if os.path.exists(p):
            return p, "ID Match"

    # 2 ─ Exact sanitised-name filename
    if nm:
        safe = re.sub(r'[\\/:*?"<>|]', "_", nm)
        p = os.path.join(folder_path, f"{safe}.pdf")
        if os.path.exists(p):
            return p, "Name Match"

    # 3 ─ Fuzzy: name appears anywhere in filename
    if nm:
        nm_low = nm.lower()
        for fname in os.listdir(folder_path):
            if fname.lower().endswith(".pdf") and nm_low in fname.lower():
                return os.path.join(folder_path, fname), "Fuzzy Match"

    return None, "Not Found"


def find_pdf_in_zip_bytes(
    zip_bytes: bytes,
    enrollment_id: str = "",
    name: str = "",
) -> tuple[Optional[bytes], str]:
    """
    Locate a student's PDF inside in-memory ZIP bytes.
    Returns (pdf_bytes | None, status_message).
    """
    if not zip_bytes:
        return None, "No ZIP"

    import io as _io
    eid = _clean(enrollment_id)
    nm  = _clean(name)

    try:
        with zipfile.ZipFile(_io.BytesIO(zip_bytes)) as zf:
            pdf_names = [n for n in zf.namelist() if n.lower().endswith(".pdf")]

            if eid:
                for n in pdf_names:
                    if eid.lower() in os.path.basename(n).lower():
                        return zf.read(n), " ID Match (ZIP)"

            if nm:
                nm_low = nm.lower()
                safe   = re.sub(r'[\\/:*?"<>|]', "_", nm).lower()
                for n in pdf_names:
                    bn = os.path.basename(n).lower()
                    if safe in bn or nm_low in bn:
                        return zf.read(n), "Name Match (ZIP)"
    except Exception as exc:
        return None, f"ZIP Error: {exc}"

    return None, "Not Found"


def extract_zip_to_temp(zip_bytes: bytes) -> Optional[str]:
    """Extract ZIP bytes to a temp dir. Returns the path containing the PDFs."""
    import io as _io
    try:
        tmp = tempfile.mkdtemp()
        with zipfile.ZipFile(_io.BytesIO(zip_bytes)) as zf:
            zf.extractall(tmp)
        # Walk to find the deepest folder that actually contains PDFs
        for root, _dirs, files in os.walk(tmp):
            if any(f.lower().endswith(".pdf") for f in files):
                return root
        return tmp
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────
# MARKDOWN → HTML CONVERTER
# ─────────────────────────────────────────────────────────────────────

def markdown_to_html(text: str) -> str:
    """Convert a subset of Markdown to email-safe HTML."""
    if not text:
        return ""
    # Bold **text**
    text = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', text)
    # Italic *text*
    text = re.sub(r'\*(.*?)\*', r'<em>\1</em>', text)
    # Links [label](url)
    text = re.sub(
        r'\[(.*?)\]\((https?://[^\)]+)\)',
        r'<a href="\2" target="_blank" style="color:#2563eb;text-decoration:underline;">\1</a>',
        text,
    )
    # Bullet lists
    lines = text.split("\n")
    out, in_ul = [], False
    for line in lines:
        if line.strip().startswith("- "):
            if not in_ul:
                out.append('<ul style="margin:8px 0 8px 20px;padding:0;">')
                in_ul = True
            out.append(f'<li style="margin:3px 0;">{line.strip()[2:]}</li>')
        else:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append(line)
    if in_ul:
        out.append("</ul>")
    return "<br>".join(out)


def html_to_plain(html: str) -> str:
    """Strip HTML tags to produce a plain-text email fallback."""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ─────────────────────────────────────────────────────────────────────
# JINJA2 RENDERING
# ─────────────────────────────────────────────────────────────────────

def render_jinja(template_str: str, row_dict: dict) -> str:
    """
    Render a Jinja2 template with row_dict values.
    Falls back to simple string-replace if jinja2 is not installed.
    NaN/None values in row_dict are coerced to empty strings.
    """
    import pandas as pd
    clean = {}
    for k, v in row_dict.items():
        try:
            clean[k] = "" if (v is None or (isinstance(v, float) and pd.isna(v))) else str(v)
        except Exception:
            clean[k] = str(v) if v is not None else ""

    if JINJA2_AVAILABLE:
        try:
            return Template(template_str).render(**clean)
        except TemplateError as e:
            return f"[Template Error: {e}]"
    else:
        result = template_str
        for k, v in clean.items():
            result = result.replace(f"{{{{ {k} }}}}", v).replace(f"{{{{{k}}}}}", v)
        return result


# ─────────────────────────────────────────────────────────────────────
# BLOCK DEFAULTS
# ─────────────────────────────────────────────────────────────────────

BLOCK_DEFAULTS: dict = {
    "header":  {
        "type": "header",
        "title": "📊 Your Assessment Results",
        "subtitle": "{{ Name }} — {{ Batch }}",
        "bg_color": "#1B3A6B",
        "text_color": "#FFFFFF",
    },
    "text": {
        "type": "text",
        "content": (
            "Dear **{{ Name }}**,\n\n"
            "We are pleased to share your assessment results. "
            "Please find your detailed report attached.\n\n"
            "Your overall grade: **{{ Grade }}**\n"
            "Your score: **{{ Score }}%**"
        ),
        "font_size": "15px",
    },
    "image": {
        "type": "image",
        "src": "",
        "alt": "Image",
        "width": "100%",
        "link": "",
    },
    "button": {
        "type": "button",
        "label": "Schedule Mock Interview",
        "url": "https://example.com/schedule",
        "bg_color": "#2563eb",
        "text_color": "#ffffff",
    },
    "divider": {
        "type": "divider",
        "color": "#DDE3EE",
        "thickness": "1px",
    },
    "spacer": {
        "type": "spacer",
        "height": "20px",
    },
    "footer": {
        "type": "footer",
        "content": (
            "Best regards,\n**Your Name**\n"
            "Placement Coordinator | Innomatics Research Labs\n\n"
            "---\n"
            "*This is an automated email. Please do not reply directly to this message.*"
        ),
        "font_size": "12px",
        "text_color": "#64748B",
    },
}


# ─────────────────────────────────────────────────────────────────────
# HTML EMAIL BUILDER
# ─────────────────────────────────────────────────────────────────────

def build_html_email(
    blocks: list,
    row_dict: dict,
    bg_color: str = "#F1F5F9",
    content_bg: str = "#FFFFFF",
    font_family: str = "'Inter', 'Segoe UI', Arial, sans-serif",
    signature_html: str = "",
    template_style: str = "Simple Email (Standard)",
) -> str:
    """
    Build a complete responsive HTML email from a list of block dicts.
    Jinja2 tokens inside block fields are rendered with row_dict.

    Supports template styles:
      - "Simple Email (Standard)": Clean, minimalist direct email presentation without card shadows.
      - "Card Container (Styled)": Styled card container layout with background fill and subtle shadow.
    """
    parts = []

    for block in blocks:
        btype = block.get("type", "text")

        if btype == "header":
            title = render_jinja(block.get("title", ""), row_dict)
            sub   = render_jinja(block.get("subtitle", ""), row_dict)
            bg    = block.get("bg_color", "#1B3A6B")
            fg    = block.get("text_color", "#FFFFFF")
            sub_h = (
                f'<p style="margin:10px 0 0;font-size:14px;color:{fg};opacity:0.9;">'
                f'{render_jinja(sub, row_dict)}</p>'
            ) if sub else ""
            parts.append(f"""
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{bg};">
  <tr><td style="padding:24px 28px;">
    <h1 style="margin:0;font-size:24px;font-weight:700;color:{fg};line-height:1.25;letter-spacing:-0.5px;">{title}</h1>
    {sub_h}
  </td></tr>
</table>""")

        elif btype == "text":
            raw  = render_jinja(block.get("content", ""), row_dict)
            html = markdown_to_html(raw)
            fs   = block.get("font_size", "15px")
            parts.append(f"""
<table width="100%" cellpadding="0" cellspacing="0" border="0">
  <tr><td style="padding:16px 24px;font-size:{fs};line-height:1.75;color:#334155;">{html}</td></tr>
</table>""")

        elif btype == "image":
            src   = block.get("src", "")
            alt   = block.get("alt", "")
            width = block.get("width", "100%")
            link  = block.get("link", "")
            if src:
                img = (
                    f'<img src="{src}" alt="{alt}" width="{width}" '
                    f'style="display:block;border:0;max-width:100%;height:auto;">'
                )
                if link:
                    img = f'<a href="{link}" target="_blank">{img}</a>'
                parts.append(f"""
<table width="100%" cellpadding="0" cellspacing="0" border="0">
  <tr><td style="padding:12px 24px;text-align:center;">{img}</td></tr>
</table>""")

        elif btype == "button":
            lbl = render_jinja(block.get("label", "Click Here"), row_dict)
            url = render_jinja(block.get("url", "#"), row_dict)
            bg  = block.get("bg_color", "#2563eb")
            fg  = block.get("text_color", "#ffffff")
            parts.append(f"""
<table width="100%" cellpadding="0" cellspacing="0" border="0">
  <tr><td style="padding:16px 24px;text-align:left;">
    <!--[if mso]><v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" href="{url}"
      style="height:40px;v-text-anchor:middle;width:200px;" arcsize="18%"
      stroke="f" fillcolor="{bg}"><w:anchorlock/><center style="color:{fg};font-weight:700;">
    <![endif]-->
    <a href="{url}" target="_blank"
       style="background:{bg};border-radius:6px;color:{fg};display:inline-block;
              font-family:sans-serif;font-size:14px;font-weight:700;line-height:40px;
              text-align:center;text-decoration:none;padding:0 24px;
              -webkit-text-size-adjust:none;">{lbl}</a>
    <!--[if mso]></center></v:roundrect><![endif]-->
  </td></tr>
</table>""")

        elif btype == "divider":
            color = block.get("color", "#DDE3EE")
            thick = block.get("thickness", "1px")
            parts.append(f"""
<table width="100%" cellpadding="0" cellspacing="0" border="0">
  <tr><td style="padding:6px 24px;">
    <hr style="border:none;border-top:{thick} solid {color};margin:0;">
  </td></tr>
</table>""")

        elif btype == "spacer":
            h = block.get("height", "20px")
            parts.append(
                f'<table width="100%" cellpadding="0" cellspacing="0" border="0">'
                f'<tr><td style="height:{h};line-height:{h};font-size:1px;">&nbsp;</td></tr></table>'
            )

        elif btype == "footer":
            raw   = render_jinja(block.get("content", ""), row_dict)
            html  = markdown_to_html(raw)
            fs    = block.get("font_size", "12px")
            color = block.get("text_color", "#64748B")
            parts.append(f"""
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#F8FAFC;">
  <tr><td style="padding:16px 24px;font-size:{fs};color:{color};line-height:1.6;
              border-top:1px solid #DDE3EE;">{html}</td></tr>
</table>""")

    body_inner = "\n".join(parts)

    sig = ""
    if signature_html:
        sig = f"""
<table width="100%" cellpadding="0" cellspacing="0" border="0">
  <tr><td style="padding:16px 24px;border-top:1.5px solid #E2E8F0;">{signature_html}</td></tr>
</table>"""

    is_simple = "simple" in str(template_style).lower()

    if is_simple:
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Email</title>
</head>
<body style="margin:0;padding:20px;background-color:#ffffff;font-family:{font_family};color:#1e293b;-webkit-font-smoothing:antialiased;">
  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:640px;margin:0 auto;background:#ffffff;">
    <tr>
      <td style="padding:10px 0;font-size:15px;line-height:1.7;color:#1e293b;">
{body_inner}
{sig}
      </td>
    </tr>
  </table>
</body>
</html>"""

    return f"""<!DOCTYPE html>
<html lang="en" xmlns:v="urn:schemas-microsoft-com:vml">
<head>
  <meta charset="UTF-8">
  <meta name="x-apple-disable-message-reformatting">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Email</title>
  <!--[if mso]>
  <xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml>
  <![endif]-->
</head>
<body style="margin:0;padding:0;background-color:{bg_color};">
  <table width="100%" cellpadding="0" cellspacing="0" border="0"
         style="background:{bg_color};">
    <tr>
      <td align="center" style="padding:30px 12px;">
        <table width="600" cellpadding="0" cellspacing="0" border="0"
               style="max-width:600px;width:100%;background:{content_bg};
                      border-radius:14px;overflow:hidden;
                      box-shadow:0 6px 30px rgba(0,0,0,0.09);">
          <tr><td>
{body_inner}
{sig}
          </td></tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────
# SMTP PROVIDERS
# ─────────────────────────────────────────────────────────────────────

SMTP_PROVIDERS: dict = {
    "Gmail":   {"host": "smtp.gmail.com",      "port": 587},
    "Outlook": {"host": "smtp.office365.com",  "port": 587},
    "Yahoo":   {"host": "smtp.mail.yahoo.com", "port": 587},
    "Custom":  {"host": "",                     "port": 587},
}


def connect_smtp(
    email: str,
    password: str,
    host: str = "smtp.gmail.com",
    port: int = 587,
) -> smtplib.SMTP:
    """
    Create and return an authenticated SMTP connection.
    Supports STARTTLS (port 587, etc.) and direct SSL (port 465).
    Raises smtplib.SMTPAuthenticationError on bad credentials.
    """
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=20)
        server.ehlo()
    else:
        server = smtplib.SMTP(host, port, timeout=20)
        server.ehlo()
        server.starttls()
        server.ehlo()
    server.login(email, password)
    return server


# ─────────────────────────────────────────────────────────────────────
# SINGLE EMAIL SEND
# ─────────────────────────────────────────────────────────────────────

def send_one(
    smtp_conn: smtplib.SMTP,
    from_email: str,
    from_name: str,
    to_email: str,
    subject: str,
    html_body: str,
    attachments: Optional[list] = None,
    cc: str = "",
    bcc: str = "",
    reply_to: str = "",
) -> tuple[bool, str]:
    """
    Send a single HTML email.

    attachments: list of (filename: str, content: bytes | str_filepath)
    Returns: (success: bool, error_message: str)
    """
    try:
        # Parse inline base64 images and attach them as CID inline resources
        inline_images = []
        
        def _repl(match):
            mime_type = match.group(1)  # e.g. png, jpeg, gif, webp
            b64_data = match.group(2)
            try:
                img_data = base64.b64decode(b64_data)
                cid = f"img_{len(inline_images)}@deepraj.analytics"
                inline_images.append((cid, mime_type, img_data))
                return f'src="cid:{cid}"'
            except Exception:
                return match.group(0)
                
        html_body = re.sub(r'src="data:image/(png|jpeg|gif|webp);base64,([^"]+)"', _repl, html_body)

        plain = html_to_plain(html_body)
        from_hdr = f"{from_name} <{from_email}>" if from_name else from_email

        # If we have inline images OR attachments, use "related" or "mixed"
        if attachments:
            outer = MIMEMultipart("mixed")
        elif inline_images:
            outer = MIMEMultipart("related")
        else:
            outer = MIMEMultipart("alternative")

        outer["Subject"]  = subject
        outer["From"]     = from_hdr
        outer["To"]       = to_email
        
        # Robustly split CC/BCC on commas/semicolons and clean whitespace
        cc_list = [a.strip() for a in re.split(r'[,;]', cc) if a.strip()] if cc else []
        bcc_list = [a.strip() for a in re.split(r'[,;]', bcc) if a.strip()] if bcc else []
        
        if cc_list:
            outer["Cc"] = ", ".join(cc_list)
        if bcc_list:
            outer["Bcc"] = ", ".join(bcc_list)
        if reply_to: 
            outer["Reply-To"] = reply_to

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(plain, "plain", "utf-8"))
        alt.attach(MIMEText(html_body, "html", "utf-8"))

        if attachments or inline_images:
            # If mixed or related, we attach alternative body first
            outer.attach(alt)
            
            # Attach inline images
            for cid, mime_type, img_data in inline_images:
                part = MIMEBase("image", mime_type)
                part.set_payload(img_data)
                encoders.encode_base64(part)
                part.add_header("Content-ID", f"<{cid}>")
                part.add_header("Content-Disposition", "inline", filename=cid)
                outer.attach(part)

            # Attach external files
            if attachments:
                for fname, content in attachments:
                    part = MIMEBase("application", "octet-stream")
                    if isinstance(content, str) and os.path.isfile(content):
                        with open(content, "rb") as f:
                            part.set_payload(f.read())
                    elif isinstance(content, bytes):
                        part.set_payload(content)
                    else:
                        continue
                    encoders.encode_base64(part)
                    part.add_header("Content-Disposition", "attachment", filename=fname)
                    outer.attach(part)
        else:
            outer.attach(MIMEText(plain, "plain", "utf-8"))
            outer.attach(MIMEText(html_body, "html", "utf-8"))

        to_list = [a.strip() for a in re.split(r'[,;]', to_email) if a.strip()] if to_email else []
        all_rcpt = to_list + cc_list + bcc_list

        smtp_conn.sendmail(from_email, all_rcpt, outer.as_string())
        return True, ""

    except Exception as exc:
        return False, str(exc)


# ─────────────────────────────────────────────────────────────────────
# DISPATCH LOG
# ─────────────────────────────────────────────────────────────────────

def save_dispatch_log(results: list, session_name: str = "") -> str:
    """
    Save dispatch results to per-session CSV and append to master log.
    Returns the per-session filename.
    """
    if not results:
        return ""
    os.makedirs(LOG_DIR, exist_ok=True)
    ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug      = re.sub(r"[^a-zA-Z0-9_]", "_", session_name)[:30] or "dispatch"
    sess_file = os.path.join(LOG_DIR, f"{slug}_{ts}.csv")
    master    = os.path.join(LOG_DIR, "master_dispatch_history.csv")

    fields = list(results[0].keys())

    for fpath, append in [(sess_file, False), (master, True)]:
        mode = "a" if (append and os.path.exists(fpath)) else "w"
        with open(fpath, mode, newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            if not (append and os.path.exists(fpath)) or not append:
                writer.writeheader()
            writer.writerows(results)

    return sess_file
