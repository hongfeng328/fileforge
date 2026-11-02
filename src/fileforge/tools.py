"""Core tool functions for FileForge.

Design rules:
- No MCP imports here. Functions are plain Python so they can be unit
  tested without a transport, and reused later behind a web API if needed.
- Inputs are local file paths (the transport layer is responsible for
  getting user files onto disk; see TODO in README about uploads).
- Every function writes its result into OUTPUT_DIR and returns a dict
  with at least: file_name, file_path, size_bytes.
- Errors are raised as ValueError with a user-readable message; the
  transport layer turns them into MCP tool errors.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pdfplumber
import pymupdf
from openpyxl import Workbook
from PIL import Image

from fileforge import limits

Image.MAX_IMAGE_PIXELS = limits.MAX_IMAGE_PIXELS

# Output directory: overridable via env for deployment; defaults to the
# project-local output/ folder (repo_root/output).
_DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[2] / "output"
OUTPUT_DIR = Path(os.environ.get("FILEFORGE_OUTPUT_DIR", _DEFAULT_OUTPUT_DIR))


def _output_path(stem: str, suffix: str) -> Path:
    """Build a collision-free output path inside OUTPUT_DIR."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    safe_stem = "".join(c if c.isalnum() or c in "-_" else "_" for c in stem).strip("_") or "fileforge"
    name = f"{safe_stem}-{uuid.uuid4().hex[:8]}{suffix}"
    return OUTPUT_DIR / name


def _file_info(path: Path, **extra: Any) -> dict[str, Any]:
    """Standard result payload for a produced file.

    TODO(download-links): once deployed, also return a signed, expiring
    download URL for the file instead of only a local path.
    """
    info: dict[str, Any] = {
        "file_name": path.name,
        "file_path": str(path),
        "size_bytes": path.stat().st_size,
    }
    info.update(extra)
    return info


def _require_file(path_str: str, label: str = "input") -> Path:
    p = Path(path_str)
    if not p.is_file():
        raise ValueError(f"{label} file not found: {path_str}")
    return p


# ---------------------------------------------------------------------------
# Tool 1: pdf_tables_to_excel
# ---------------------------------------------------------------------------

def pdf_tables_to_excel(pdf_path: str) -> dict[str, Any]:
    """Extract every table found in a PDF into an Excel workbook.

    Each detected table becomes its own worksheet, named after its page
    and position (e.g. ``p1_t1``). Cell text is written as-is; no type
    guessing is done in this MVP.

    Raises ValueError if the PDF contains no detectable tables (tables
    need ruling lines or clear cell alignment for pdfplumber to find).
    """
    src = _require_file(pdf_path, "PDF")
    workbook = Workbook()
    workbook.remove(workbook.active)  # drop the default empty sheet

    tables_found = 0
    with pdfplumber.open(src) as pdf:
        if len(pdf.pages) > limits.MAX_PDF_PAGES:
            raise ValueError(
                f"PDF has {len(pdf.pages)} pages; the limit is {limits.MAX_PDF_PAGES} pages."
            )
        for page_no, page in enumerate(pdf.pages, start=1):
            for table_no, table in enumerate(page.extract_tables() or [], start=1):
                if not table:
                    continue
                sheet = workbook.create_sheet(title=f"p{page_no}_t{table_no}"[:31])
                for row in table:
                    sheet.append(["" if cell is None else str(cell) for cell in row])
                tables_found += 1

    if tables_found == 0:
        raise ValueError(
            "No tables detected in this PDF. Tables need visible ruling "
            "lines or clearly aligned columns to be extracted."
        )

    out = _output_path(src.stem + "_tables", ".xlsx")
    workbook.save(out)
    return _file_info(out, tables_found=tables_found, sheets=tables_found)


# ---------------------------------------------------------------------------
# Tool 2: merge_pdfs
# ---------------------------------------------------------------------------

def merge_pdfs(pdf_paths: list[str]) -> dict[str, Any]:
    """Merge two or more PDFs into a single PDF, in the given order."""
    if not pdf_paths or len(pdf_paths) < 2:
        raise ValueError("merge_pdfs needs at least two PDF files.")

    if len(pdf_paths) > limits.MAX_FILES_PER_CALL:
        raise ValueError(f"Too many files: the limit is {limits.MAX_FILES_PER_CALL} per call.")

    merged = pymupdf.open()
    try:
        for path_str in pdf_paths:
            src = _require_file(path_str, "PDF")
            with pymupdf.open(src) as doc:
                if doc.page_count > limits.MAX_PDF_PAGES:
                    raise ValueError(
                        f"PDF has {doc.page_count} pages; the limit is "
                        f"{limits.MAX_PDF_PAGES} pages per file."
                    )
                if merged.page_count + doc.page_count > limits.MAX_MERGED_PAGES:
                    raise ValueError(
                        f"Merged result would exceed {limits.MAX_MERGED_PAGES} pages."
                    )
                merged.insert_pdf(doc)
        out = _output_path("merged", ".pdf")
        merged.save(out)
    finally:
        merged.close()

    return _file_info(out, page_count=_page_count(out), inputs=len(pdf_paths))


# ---------------------------------------------------------------------------
# Tool 3: images_to_pdf
# ---------------------------------------------------------------------------

def images_to_pdf(image_paths: list[str], dpi: int = 200) -> dict[str, Any]:
    """Combine one or more images into a single PDF, one image per page.

    Each page is sized to the image's aspect ratio at the given DPI, and
    the image is placed to fill the page. Non-RGB images are converted.
    """
    if not image_paths:
        raise ValueError("images_to_pdf needs at least one image file.")
    if len(image_paths) > limits.MAX_FILES_PER_CALL:
        raise ValueError(f"Too many files: the limit is {limits.MAX_FILES_PER_CALL} per call.")

    doc = pymupdf.open()
    try:
        for path_str in image_paths:
            src = _require_file(path_str, "image")
            with Image.open(src) as img:
                if img.width * img.height > limits.MAX_IMAGE_PIXELS:
                    raise ValueError(
                        f"Image is {img.width * img.height:,} pixels; the limit is "
                        f"{limits.MAX_IMAGE_PIXELS:,} pixels."
                    )
                rgb = img.convert("RGB")
                width_pt = rgb.width * 72.0 / dpi
                height_pt = rgb.height * 72.0 / dpi
                page = doc.new_page(width=width_pt, height=height_pt)
                # insert_image needs a file/stream; use the original file
                # when possible, fall back to PNG bytes after conversion.
                if img.mode == "RGB" and src.suffix.lower() in {".jpg", ".jpeg", ".png"}:
                    page.insert_image(page.rect, filename=str(src))
                else:
                    import io

                    buf = io.BytesIO()
                    rgb.save(buf, format="PNG")
                    page.insert_image(page.rect, stream=buf.getvalue())
        out = _output_path("images", ".pdf")
        doc.save(out)
    finally:
        doc.close()

    return _file_info(out, page_count=_page_count(out), inputs=len(image_paths))


# ---------------------------------------------------------------------------
# Tool 4 (interface only): pdf_form_fill
# ---------------------------------------------------------------------------

def pdf_form_fill(pdf_path: str, fields: dict[str, str]) -> dict[str, Any]:
    """Fill a fillable PDF form's fields with the given values.

    TODO(form-fill): not implemented in this MVP skeleton.
    Implementation sketch:
    - Open with pymupdf; iterate ``page.widgets()`` to list field names
      and types, match against ``fields`` keys, set ``widget.field_value``
      and call ``widget.update()``.
    - Decide policy for unknown field names (reject vs ignore) and for
      checkbox/radio widgets (value conventions differ).
    - Optionally flatten the result so values cannot be edited.
    Do NOT register this tool on the MCP server until it is implemented
    and covered by a self test - a directory review would fail on a
    tool that always errors.
    """
    raise NotImplementedError("pdf_form_fill is not implemented yet (MVP skeleton).")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _page_count(pdf_path: Path) -> int:
    with pymupdf.open(pdf_path) as doc:
        return doc.page_count
