"""Minimal self tests for FileForge tools.

Generates sample inputs under samples/, calls the three implemented
tools, and validates the outputs by re-opening them.

Run from the project root:
    PYTHONPATH=src .venv/bin/python tests/run_selftest.py
Exits non-zero if any check fails.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pymupdf
from openpyxl import load_workbook
from PIL import Image, ImageDraw

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLES = PROJECT_ROOT / "samples"

sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fileforge import tools  # noqa: E402


def make_table_pdf(path: Path) -> None:
    """Draw a small ruled 3x3 table so pdfplumber can detect it."""
    rows = [
        ["Item", "Qty", "Price"],
        ["Apples", "3", "1.50"],
        ["Pears", "2", "2.25"],
    ]
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)  # A4
    x0, y0, col_w, row_h = 72, 72, 120, 28
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            rect = pymupdf.Rect(
                x0 + c * col_w, y0 + r * row_h, x0 + (c + 1) * col_w, y0 + (r + 1) * row_h
            )
            page.draw_rect(rect, color=(0, 0, 0), width=0.8)
            page.insert_text((rect.x0 + 6, rect.y0 + 18), text, fontsize=11)
    doc.save(path)
    doc.close()


def make_text_pdf(path: Path, marker: str) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), marker, fontsize=20)
    doc.save(path)
    doc.close()


def make_image(path: Path, color: str, label: str) -> None:
    img = Image.new("RGB", (400, 300), color)
    draw = ImageDraw.Draw(img)
    draw.text((20, 20), label, fill="white")
    img.save(path)


def check(name: str, condition: bool, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" - {detail}" if detail else ""))
    return condition


def main() -> int:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    results: list[bool] = []

    # --- fixtures -------------------------------------------------------
    table_pdf = SAMPLES / "table.pdf"
    make_table_pdf(table_pdf)
    pdf_a, pdf_b = SAMPLES / "a.pdf", SAMPLES / "b.pdf"
    make_text_pdf(pdf_a, "PAGE-A-MARKER")
    make_text_pdf(pdf_b, "PAGE-B-MARKER")
    img_1, img_2 = SAMPLES / "one.png", SAMPLES / "two.jpg"
    make_image(img_1, "steelblue", "IMG-ONE")
    make_image(img_2, "darkorange", "IMG-TWO")
    print(f"fixtures written to {SAMPLES}")

    # --- test 1: pdf_tables_to_excel ------------------------------------
    info = tools.pdf_tables_to_excel(str(table_pdf))
    out = Path(info["file_path"])
    ok = out.is_file() and out.stat().st_size > 0
    wb = load_workbook(out)
    first_sheet = wb[wb.sheetnames[0]]
    cell_a1 = first_sheet["A1"].value
    cell_b2 = first_sheet["B2"].value
    results.append(check("pdf_tables_to_excel: xlsx exists and opens", ok, str(out.name)))
    results.append(
        check(
            "pdf_tables_to_excel: content matches sample table",
            cell_a1 == "Item" and str(cell_b2) == "3",
            f"A1={cell_a1!r} B2={cell_b2!r} tables={info['tables_found']}",
        )
    )

    # --- test 2: merge_pdfs ----------------------------------------------
    info = tools.merge_pdfs([str(pdf_a), str(pdf_b)])
    out = Path(info["file_path"])
    with pymupdf.open(out) as doc:
        text = "".join(page.get_text() for page in doc)
        pages = doc.page_count
    results.append(
        check(
            "merge_pdfs: 2 pages, both markers present, in order",
            pages == 2 and text.index("PAGE-A-MARKER") < text.index("PAGE-B-MARKER"),
            f"pages={pages} size={info['size_bytes']}B",
        )
    )

    # --- test 3: images_to_pdf -------------------------------------------
    info = tools.images_to_pdf([str(img_1), str(img_2)])
    out = Path(info["file_path"])
    with pymupdf.open(out) as doc:
        pages = doc.page_count
        has_images = all(page.get_images() for page in doc)
    results.append(
        check(
            "images_to_pdf: 2 pages, each page embeds an image",
            pages == 2 and has_images,
            f"pages={pages} size={info['size_bytes']}B",
        )
    )

    # --- test 4: pdf_form_fill is an explicit stub ------------------------
    try:
        tools.pdf_form_fill(str(pdf_a), {"name": "x"})
        results.append(check("pdf_form_fill: stub raises NotImplementedError", False))
    except NotImplementedError:
        results.append(check("pdf_form_fill: stub raises NotImplementedError", True))

    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
