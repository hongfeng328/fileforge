"""FileForge MCP server - thin transport layer over fileforge.tools.

Runs a remote MCP server over Streamable HTTP (the transport the Claude
Connectors Directory and other registries require; SSE is deprecated).

Run:
    PYTHONPATH=src .venv/bin/python -m fileforge.server
Environment:
    FILEFORGE_HOST  bind host (default 127.0.0.1; use 0.0.0.0 in deployment)
    FILEFORGE_PORT  bind port (default 8765)
    FILEFORGE_OUTPUT_DIR  where result files are written

The endpoint is served at http://<host>:<port>/mcp

Directory-submission notes (Claude Connectors Directory):
- Tool names must be <= 64 characters: pdf_tables_to_excel (19),
  merge_pdfs (10), images_to_pdf (13). OK.
- Every tool carries a human title and a clear description of what it
  does and what it returns.
- pdf_form_fill is intentionally NOT registered yet: it is a stub in
  tools.py, and a tool that always errors would fail directory review.
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from fileforge import tools

mcp = FastMCP(
    "FileForge",
    instructions=(
        "FileForge performs deterministic file processing: extract tables "
        "from a PDF into Excel, merge PDFs, and combine images into a PDF. "
        "Tools take local file paths and return information about the "
        "produced file."
    ),
)

# Tools write new files to the output directory; they never modify or
# delete the user's input files.
_WRITE_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)


@mcp.tool(
    title="Extract PDF tables to Excel",
    description=(
        "Extract all tables detected in a PDF file into an Excel (.xlsx) "
        "workbook, one worksheet per table. Input: path to a PDF whose "
        "tables have visible ruling lines or clearly aligned columns. "
        "Returns the produced file's name, path and size."
    ),
    annotations=_WRITE_ANNOTATIONS,
)
def pdf_tables_to_excel(pdf_path: str) -> dict[str, Any]:
    return tools.pdf_tables_to_excel(pdf_path)


@mcp.tool(
    title="Merge PDFs",
    description=(
        "Merge two or more PDF files into a single PDF, in the order "
        "given. Input: a list of PDF file paths. Returns the produced "
        "file's name, path, size and page count."
    ),
    annotations=_WRITE_ANNOTATIONS,
)
def merge_pdfs(pdf_paths: list[str]) -> dict[str, Any]:
    return tools.merge_pdfs(pdf_paths)


@mcp.tool(
    title="Combine images into a PDF",
    description=(
        "Combine one or more images (PNG/JPEG) into a single PDF, one "
        "image per page, pages sized to each image. Input: a list of "
        "image file paths. Returns the produced file's name, path, size "
        "and page count."
    ),
    annotations=_WRITE_ANNOTATIONS,
)
def images_to_pdf(image_paths: list[str]) -> dict[str, Any]:
    return tools.images_to_pdf(image_paths)


def main() -> None:
    host = os.environ.get("FILEFORGE_HOST", "127.0.0.1")
    port = int(os.environ.get("FILEFORGE_PORT", "8765"))
    # FastMCP reads host/port from its settings object.
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
