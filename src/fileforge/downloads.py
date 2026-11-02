"""Signed, expiring download links for produced files (checklist item 2).

A token is ``<payload>.<signature>`` where payload is base64url of
``"<file_name>|<expiry_unix>"`` and signature is base64url of
HMAC-SHA256(secret, payload). The secret comes from
FILEFORGE_DOWNLOAD_SECRET; in production it must be set explicitly -
if it is missing the server generates an ephemeral one at boot (links
then die on restart, which is safe but inconvenient, and a warning is
logged).

Links expire exactly when the janitor deletes the file
(``limits.RETENTION_HOURS`` after creation), matching the landing page
promise that files are deleted 24 hours after processing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

from fileforge import limits
from fileforge.tools import OUTPUT_DIR

_SECRET = os.environ.get("FILEFORGE_DOWNLOAD_SECRET") or secrets.token_hex(32)
EPHEMERAL_SECRET = "FILEFORGE_DOWNLOAD_SECRET" not in os.environ


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(file_name: str, now: float | None = None) -> tuple[str, int]:
    """Create a download token for a file in OUTPUT_DIR.

    Returns (token, expiry_unix). Expiry is min(now + retention, the
    file's own mtime + retention) so a link never outlives its file.
    """
    now = time.time() if now is None else now
    expiry = int(now + limits.RETENTION_HOURS * 3600)
    path = OUTPUT_DIR / file_name
    if path.is_file():
        expiry = min(expiry, int(path.stat().st_mtime + limits.RETENTION_HOURS * 3600))
    payload = _b64(f"{file_name}|{expiry}".encode())
    sig = _b64(hmac.new(_SECRET.encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}", expiry


def verify_token(token: str, now: float | None = None) -> Path:
    """Resolve a token to an existing output file path.

    Raises ValueError with a user-readable reason for every failure
    (bad format, bad signature, expired, file already deleted).
    """
    now = time.time() if now is None else now
    try:
        payload, sig = token.split(".", 1)
    except ValueError:
        raise ValueError("Invalid download link.") from None
    expected = _b64(hmac.new(_SECRET.encode(), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig):
        raise ValueError("Invalid download link.")
    try:
        file_name, expiry_text = _unb64(payload).decode().rsplit("|", 1)
        expiry = int(expiry_text)
    except Exception:
        raise ValueError("Invalid download link.") from None
    # File names we issue are single path components (see tools._output_path);
    # reject anything trying to escape OUTPUT_DIR.
    if Path(file_name).name != file_name or file_name in {"", ".", ".."}:
        raise ValueError("Invalid download link.")
    if now > expiry:
        raise ValueError("This download link has expired (files are kept for 24 hours).")
    path = OUTPUT_DIR / file_name
    if not path.is_file():
        raise ValueError("This file is no longer available (files are deleted after 24 hours).")
    return path


def download_url(file_name: str) -> tuple[str | None, int]:
    """Absolute download URL if a public base URL is configured."""
    token, expiry = make_token(file_name)
    base = os.environ.get("FILEFORGE_PUBLIC_BASE_URL", "").rstrip("/")
    if not base:
        return None, expiry
    return f"{base}/download/{token}", expiry
