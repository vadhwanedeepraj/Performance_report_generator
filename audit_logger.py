import os
import csv
import uuid
from datetime import datetime

AUDIT_FILE = "audit_logs.csv"

# Default fallback core columns in case of missing file
AUDIT_FIELDS = [
    "Log ID",
    "Timestamp",
    "Session ID",
    "Action",
    "Status",
    "Report Title",
    "Student Name",
    "Enrollment ID",
    "Email",
    "Batch",
    "Branch",
    "Report ID",
]


def _safe_str(val):
    """Convert a value to a clean string, returning 'N/A' for null/empty/nan."""
    s = str(val).strip() if val is not None else ""
    return s if s and s.lower() not in ("nan", "none", "n/a", "") else "N/A"


# ── Global session ID — generated once per process lifetime ──────────────────
_SESSION_ID = str(uuid.uuid4())[:8].upper()


def get_session_id() -> str:
    """Returns the current process-level session ID (short 8-char UUID)."""
    return _SESSION_ID


def log_audit_event(
    action: str,
    report_title: str,
    student_name: str,
    enrollment_id: str,
    report_id: str = "N/A",
    *,
    status: str = "Success",
    email: str = "N/A",
    batch: str = "N/A",
    branch: str = "N/A",
    session_id: str = None,
    **kwargs,
):
    """
    Logs a dynamically structured audit event to audit_logs.csv.
    Any extra keyword arguments (**kwargs) are dynamically added as columns in the CSV!
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_id = f"LOG_{datetime.now().strftime('%Y%m%d%H%M%S')}_{str(uuid.uuid4())[:8].upper()}"
    sid = session_id or _SESSION_ID

    # Base payload structure
    payload = {
        "Log ID": log_id,
        "Timestamp": timestamp,
        "Session ID": sid,
        "Action": _safe_str(action),
        "Status": _safe_str(status),
        "Report Title": _safe_str(report_title),
        "Student Name": _safe_str(student_name),
        "Enrollment ID": _safe_str(enrollment_id),
        "Email": _safe_str(email),
        "Batch": _safe_str(batch),
        "Branch": _safe_str(branch),
        "Report ID": _safe_str(report_id),
    }

    # Add custom dynamic kwargs to payload
    for k, v in kwargs.items():
        clean_k = str(k).strip()
        # Normalize source_file to Source File for consistency with UI check
        if clean_k == "source_file":
            clean_k = "Source File"
        # Prevent overriding critical base keys.
        # BUG-06 FIX: removed "Source File" and "source_file" from this block list
        # so the audit source file filter in the UI can work.
        if clean_k not in payload and clean_k not in ("Delivery Channel", "Notes", "delivery_channel", "notes"):
            payload[clean_k] = _safe_str(v)

    # ── Step 1: Inspect existing schema (headers only — no data read) ─────────
    headers = list(payload.keys())
    file_exists = os.path.exists(AUDIT_FILE) and os.path.getsize(AUDIT_FILE) > 0
    schema_changed = False
    existing_headers: list = []

    if file_exists:
        try:
            with open(AUDIT_FILE, "r", newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                existing_headers = next(reader, None) or []

            # Strip legacy columns no longer used
            existing_headers = [h for h in existing_headers
                                 if h not in ("Delivery Channel", "Notes")]

            # Build the merged header list (preserve existing order, append new ones)
            merged = list(existing_headers)
            for k in payload.keys():
                if k not in merged:
                    merged.append(k)
                    schema_changed = True
            headers = merged
        except Exception as e:
            print(f"[AuditLogger] Error reading schema: {e}")

    # ── Step 2: Row-count check via fast line scan (no data load) ─────────────
    if file_exists:
        try:
            with open(AUDIT_FILE, "r", encoding="utf-8") as f:
                row_count = sum(1 for _ in f) - 1   # subtract header line
        except Exception:
            row_count = 0

        if row_count >= 10000:
            # Rotate: read full data only at rotation time (rare event)
            try:
                import pandas as _pd
                df_arch = _pd.read_csv(AUDIT_FILE, encoding="utf-8", on_bad_lines="skip")
                archive_file = (
                    f"audit_logs_archive_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                )
                df_arch.to_csv(archive_file, index=False, encoding="utf-8")
                os.remove(AUDIT_FILE)
                file_exists = False
                schema_changed = False   # fresh file — no schema to merge
                headers = list(payload.keys())
                print(f"[AuditLogger] Rotated log ({row_count} rows) → {archive_file}")
            except Exception as arch_err:
                print(f"[AuditLogger] Failed to rotate audit log: {arch_err}")

    # ── Step 3a: Schema expansion needed — full rewrite (rare) ────────────────
    if schema_changed and file_exists:
        try:
            import pandas as _pd
            df_existing = _pd.read_csv(AUDIT_FILE, encoding="utf-8", on_bad_lines="skip")
            # Add missing columns with N/A
            for h in headers:
                if h not in df_existing.columns:
                    df_existing[h] = "N/A"
            df_existing = df_existing[headers]   # reorder to canonical header order
            new_row_df = _pd.DataFrame([{h: payload.get(h, "N/A") for h in headers}])
            df_final = _pd.concat([df_existing, new_row_df], ignore_index=True)
            df_final.to_csv(AUDIT_FILE, index=False, encoding="utf-8")
        except Exception as e:
            print(f"[AuditLogger] Schema-expansion rewrite failed: {e}")
        return

    # ── Step 3b: Fast append — O(1), no data load ─────────────────────────────
    try:
        write_header = not file_exists
        with open(AUDIT_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(headers)
            new_row = [payload.get(h, "N/A") for h in headers]
            writer.writerow(new_row)
    except Exception as e:
        print(f"[AuditLogger] Failed to append audit row: {e}")


def get_audit_log_bytes() -> bytes:
    """Reads the current audit_logs.csv and returns raw bytes for file download."""
    if os.path.exists(AUDIT_FILE) and os.path.getsize(AUDIT_FILE) > 0:
        with open(AUDIT_FILE, "rb") as f:
            return f.read()
    return b""


def get_audit_dataframe():
    """
    Returns the full audit log as a pandas DataFrame.
    Returns an empty DataFrame with core columns if no log file exists.
    """
    import pandas as pd
    if os.path.exists(AUDIT_FILE) and os.path.getsize(AUDIT_FILE) > 0:
        try:
            df = pd.read_csv(AUDIT_FILE, encoding="utf-8", on_bad_lines="skip")
            # Automatically returns all columns dynamically!
            return df
        except Exception:
            return pd.DataFrame(columns=AUDIT_FIELDS)
    return pd.DataFrame(columns=AUDIT_FIELDS)


def clear_audit_log():
    """Deletes the audit log file. Use with caution."""
    if os.path.exists(AUDIT_FILE):
        os.remove(AUDIT_FILE)


def get_deduplicated_audit_dataframe():
    """
    Returns the audit log as a deduplicated pandas DataFrame.

    Deduplication logic:
    - For 'Generated (Batch)' and 'Downloaded (Batch ZIP)' actions, keep only the
      LATEST row per (Student Name + Action + calendar-date). This prevents the
      same batch-run from producing duplicate visible rows when Streamlit rerenders.
    - All other actions (Mock Interview Status Update, etc.) are kept as-is since
      they represent distinct intentional events.
    """
    import pandas as pd
    df = get_audit_dataframe()
    if df.empty:
        return df

    DEDUP_ACTIONS = {"Generated (Batch)", "Downloaded (Batch ZIP)"}

    # Split into rows that need dedup vs rows that should be preserved verbatim
    mask_dedup = df["Action"].isin(DEDUP_ACTIONS)
    df_dedup   = df[mask_dedup].copy()
    df_keep    = df[~mask_dedup].copy()

    if not df_dedup.empty:
        # Parse date only (not time) for dedup key
        try:
            df_dedup["_date"] = pd.to_datetime(
                df_dedup["Timestamp"], errors="coerce"
            ).dt.date
        except Exception:
            df_dedup["_date"] = df_dedup["Timestamp"].astype(str).str[:10]

        # Keep the last (most recent) occurrence per student+action+date
        df_dedup = (
            df_dedup
            .sort_values("Timestamp", ascending=True)
            .drop_duplicates(
                subset=["Student Name", "Action", "_date"],
                keep="last",
            )
            .drop(columns=["_date"])
        )

    result = pd.concat([df_keep, df_dedup], ignore_index=True)
    # Re-sort by Timestamp so the table is chronological
    try:
        result = result.sort_values("Timestamp", ascending=True).reset_index(drop=True)
    except Exception:
        pass
    return result
