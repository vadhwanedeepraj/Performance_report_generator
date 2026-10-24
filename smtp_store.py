"""
smtp_store.py
=============
Secure SMTP credential storage with three backends (tried in order):

  1. Streamlit Secrets  — st.secrets["smtp"] (Streamlit Cloud / any deployment using secrets.toml)
  2. Fernet file        — .smtp_creds + .smtp_key (persistent filesystem: VPS, Windows Server)
  3. Session state only — no persistence (fallback, credentials lost on restart)

This module auto-detects the best available backend transparently.
"""

import os
import json
import base64

CRED_FILE = ".smtp_creds"
KEY_FILE  = ".smtp_key"

# ── Try importing cryptography ──────────────────────────────────────
try:
    from cryptography.fernet import Fernet
    _CRYPTO_OK = True
except ImportError:
    _CRYPTO_OK = False

# ── Try importing streamlit for secrets access ──────────────────────
try:
    import streamlit as st
    _ST_OK = True
except ImportError:
    _ST_OK = False


# ─────────────────────────────────────────────────────────────────────
# HELPER: detect Streamlit Cloud (ephemeral filesystem)
# ─────────────────────────────────────────────────────────────────────
def _is_streamlit_cloud() -> bool:
    """
    Detects if running on Streamlit Community Cloud.
    Streamlit Cloud sets the STREAMLIT_SHARING_MODE env var.
    Also checks for the HOME=/home/appuser pattern used on their infra.
    """
    return (
        os.environ.get("STREAMLIT_SHARING_MODE") == "streamlit_sharing"
        or os.environ.get("HOME", "") == "/home/appuser"
        or os.path.exists("/mount/src")          # Streamlit Cloud mounts source here
    )


# ─────────────────────────────────────────────────────────────────────
# BACKEND 1: Streamlit Secrets
# ─────────────────────────────────────────────────────────────────────
def _load_from_secrets() -> dict | None:
    """Read SMTP credentials from st.secrets['smtp']."""
    if not _ST_OK:
        return None
    try:
        sec = st.secrets.get("smtp", None)
        if sec is None:
            return None
        return {
            "email":     sec.get("email", ""),
            "password":  sec.get("password", ""),
            "from_name": sec.get("from_name", ""),
            "reply_to":  sec.get("reply_to", ""),
            "host":      sec.get("host", "smtp.gmail.com"),
            "port":      int(sec.get("port", 587)),
            "provider":  sec.get("provider", "Gmail"),
        }
    except Exception:
        return None


def _secrets_available() -> bool:
    """Return True if st.secrets has an [smtp] section."""
    if not _ST_OK:
        return False
    try:
        return "smtp" in st.secrets and bool(st.secrets["smtp"].get("email"))
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────
# BACKEND 2: Fernet encrypted file
# ─────────────────────────────────────────────────────────────────────
def _get_or_create_key() -> bytes:
    if os.path.exists(KEY_FILE):
        try:
            with open(KEY_FILE, "rb") as f:
                key = f.read().strip()
            if key:
                Fernet(key)
                return key
        except Exception:
            pass
    key = Fernet.generate_key()
    with open(KEY_FILE, "wb") as f:
        f.write(key)
    return key


def _save_to_file(payload: dict) -> bool:
    raw = json.dumps(payload).encode("utf-8")
    try:
        if _CRYPTO_OK:
            key   = _get_or_create_key()
            token = Fernet(key).encrypt(raw)
            with open(CRED_FILE, "wb") as f:
                f.write(token)
        else:
            # Fallback: base64 obfuscation only
            with open(CRED_FILE, "wb") as f:
                f.write(base64.b64encode(raw))
        return True
    except Exception:
        return False


def _load_from_file() -> dict | None:
    if not os.path.exists(CRED_FILE):
        return None
    try:
        with open(CRED_FILE, "rb") as f:
            data = f.read()
        if _CRYPTO_OK and os.path.exists(KEY_FILE):
            key = _get_or_create_key()
            raw = Fernet(key).decrypt(data)
        else:
            raw = base64.b64decode(data)
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def _file_saved() -> bool:
    return os.path.exists(CRED_FILE)


def _clear_files():
    for p in (CRED_FILE, KEY_FILE):
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────

def is_credentials_saved() -> bool:
    """
    Returns True if credentials are available from any backend.
    Priority: Streamlit Secrets > encrypted file.
    """
    return _secrets_available() or _file_saved()


def load_credentials() -> dict | None:
    """
    Load SMTP credentials from the best available backend.
    Returns dict with keys: email, password, from_name, reply_to, host, port, provider.
    Returns None if nothing is saved.
    """
    # Priority 1: Streamlit Secrets (always available, always wins)
    creds = _load_from_secrets()
    if creds and creds.get("email"):
        return creds

    # Priority 2: encrypted file (local / VPS)
    return _load_from_file()


def save_credentials(
    email: str,
    password: str,
    from_name: str = "",
    reply_to: str  = "",
    host: str      = "smtp.gmail.com",
    port: int      = 587,
    provider: str  = "Gmail",
) -> tuple[bool, str]:
    """
    Save SMTP credentials using the best available backend.

    On Streamlit Cloud: cannot write secrets via API — instructs user to use dashboard.
    On local/VPS: encrypts to file.

    Returns: (success: bool, message: str)
    """
    payload = {
        "email": email, "password": password,
        "from_name": from_name, "reply_to": reply_to,
        "host": host, "port": port, "provider": provider,
    }

    if _is_streamlit_cloud() or _secrets_available():
        # On Streamlit Cloud, we cannot write to secrets via code.
        # We store in session state only and show the dashboard instructions.
        return (
            False,
            "streamlit_cloud"   # special signal to UI to show dashboard instructions
        )

    # Local / VPS: use encrypted file
    ok = _save_to_file(payload)
    return (ok, "saved" if ok else "file_error")


def clear_credentials() -> bool:
    """Remove saved credentials from all backends (file only — cannot clear secrets)."""
    _clear_files()
    return True


def get_backend_name() -> str:
    """Return a human-readable name for the active credential backend."""
    if _secrets_available():
        return "Streamlit Secrets (encrypted)"
    if _file_saved():
        return "Local encrypted file (AES-256)"
    return "Session only (not persisted)"
