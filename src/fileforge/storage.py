"""Inbound file handling for the remote FileForge service.

The MVP tools in :mod:`fileforge.tools` work on local paths. This module
is the transport-side bridge (checklist item 1): it turns base64 payloads
sent by remote MCP clients into short-lived local files, safely.

Safety properties:
- Strict base64 validation (no silent skipping of bad characters).
- Size is checked on the encoded text first (cheap reject) and again on
  the decoded bytes.
- The real file type is sniffed from magic bytes; the caller-supplied
  filename is never trusted for the extension, and its stem is sanitised
  by ``tools._output_path``-equivalent rules before use.
- PDFs are page-count checked and images pixel-count checked here, and
  again inside the tools layer (defence in depth).
- Materialised files live under FILEFORGE_INCOMING_DIR and are meant to
  be deleted by the caller right after the tool ran (``discard``), with
  the janitor as backstop.
"""

from __future__ import annotations

import base64
import binascii
import os
import uuid
from pathlib import Path

import pymupdf
from PIL import Image

from fileforge import limits

_DEFAULT_INCOMING_DIR = Path(__file__).resolve().parents[2] / "incoming"
INCOMING_DIR = Path(os.environ.get("FILEFORGE_INCOMING_DIR", _DEFAULT_INCOMING_DIR))

Image.MAX_IMAGE_PIXELS = limits.MAX_IMAGE_PIXELS

_PDF_MAGIC = b"%PDF-"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"


def _sniff_kind(data: bytes) -> tuple[str, str] | None:
    """Return (kind, extension) from magic bytes, or None if unsupported."""
    if data.startswith(_PDF_MAGIC):
        return ("pdf", ".pdf")
    if data.startswith(_PNG_MAGIC):
        return ("image", ".png")
    if data.startswith(_JPEG_MAGIC):
        return ("image", ".jpg")
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ("image", ".webp")
    return None


def decode_payload(payload_b64: str, expected_kind: str) -> bytes:
    """Decode one base64 payload and validate size and real file type."""
    if not isinstance(payload_b64, str) or not payload_b64.strip():
        raise ValueError("Empty file payload: expected base64-encoded file content.")

    max_bytes = limits.MAX_PDF_BYTES if expected_kind == "pdf" else limits.MAX_IMAGE_BYTES
    # Cheap pre-check: base64 inflates size by ~4/3.
    if len(payload_b64) > (max_bytes * 4 // 3) + 1024:
        raise ValueError(
            f"File too large: limit is {max_bytes // (1024 * 1024)} MB per {expected_kind} file."
        )
    # Tolerate a data-URL prefix and stray whitespace/newlines.
    text = payload_b64.strip()
    if text.startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    text = "".join(text.split())
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("File payload is not valid base64.") from None
    if len(data) > max_bytes:
        raise ValueError(
            f"File too large: limit is {max_bytes // (1024 * 1024)} MB per {expected_kind} file."
        )
    sniffed = _sniff_kind(data)
    if sniffed is None:
        raise ValueError("Unsupported file type: content is not a PDF or PNG/JPEG/WebP image.")
    kind, _ext = sniffed
    if kind != expected_kind:
        raise ValueError(f"Expected a {expected_kind} file but received a {kind} file.")
    return data


def _validate_pdf(path: Path) -> None:
    try:
        with pymupdf.open(path) as doc:
            if doc.page_count > limits.MAX_PDF_PAGES:
                raise ValueError(
                    f"PDF has {doc.page_count} pages; the limit is {limits.MAX_PDF_PAGES} pages."
                )
            if doc.needs_pass:
                raise ValueError("Password-protected PDFs are not supported.")
    except ValueError:
        raise
    except Exception:
        raise ValueError("The PDF file could not be parsed (corrupt or malformed).") from None


def _validate_image(path: Path) -> None:
    try:
        with Image.open(path) as img:
            img.verify()
        with Image.open(path) as img:
            pixels = img.width * img.height
    except Exception:
        raise ValueError("The image file could not be parsed (corrupt or malformed).") from None
    if pixels > limits.MAX_IMAGE_PIXELS:
        raise ValueError(
            f"Image is {pixels:,} pixels; the limit is {limits.MAX_IMAGE_PIXELS:,} pixels."
        )


def materialize(payload_b64: str, expected_kind: str) -> Path:
    """Decode, validate and write one payload to the incoming directory."""
    data = decode_payload(payload_b64, expected_kind)
    _kind, ext = _sniff_kind(data)  # type: ignore[misc]
    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    path = INCOMING_DIR / f"in-{uuid.uuid4().hex}{ext}"
    path.write_bytes(data)
    try:
        if expected_kind == "pdf":
            _validate_pdf(path)
        else:
            _validate_image(path)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def materialize_many(payloads: list[str], expected_kind: str) -> list[Path]:
    """Materialise a list of payloads; all-or-nothing (cleans up on error)."""
    if not payloads:
        raise ValueError("At least one file is required.")
    if len(payloads) > limits.MAX_FILES_PER_CALL:
        raise ValueError(f"Too many files: the limit is {limits.MAX_FILES_PER_CALL} per call.")
    paths: list[Path] = []
    try:
        for payload in payloads:
            paths.append(materialize(payload, expected_kind))
    except BaseException:
        discard(paths)
        raise
    return paths


def discard(paths: list[Path]) -> None:
    """Best-effort delete of materialised input files after a tool call."""
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
