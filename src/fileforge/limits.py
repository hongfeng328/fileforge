"""Central safety limits for FileForge.

Every limit is overridable by environment variable so deployment can
tune them without code changes. Values are read at import time; tests
set the environment before importing the package.

These limits are the malicious-input / abuse defence for a public
endpoint (checklist item 7): they bound CPU, memory and disk per call
before any heavy parsing happens.
"""

from __future__ import annotations

import os


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# --- inbound payloads -------------------------------------------------------
MAX_PDF_BYTES = _int_env("FILEFORGE_MAX_PDF_MB", 25) * 1024 * 1024
MAX_IMAGE_BYTES = _int_env("FILEFORGE_MAX_IMAGE_MB", 15) * 1024 * 1024
MAX_FILES_PER_CALL = _int_env("FILEFORGE_MAX_FILES_PER_CALL", 50)

# --- document structure -------------------------------------------------------
MAX_PDF_PAGES = _int_env("FILEFORGE_MAX_PDF_PAGES", 500)
MAX_MERGED_PAGES = _int_env("FILEFORGE_MAX_MERGED_PAGES", 1000)
MAX_IMAGE_PIXELS = _int_env("FILEFORGE_MAX_IMAGE_PIXELS", 40_000_000)

# --- processing ---------------------------------------------------------------
TOOL_TIMEOUT_SECONDS = _int_env("FILEFORGE_TOOL_TIMEOUT_SECONDS", 120)
RATE_LIMIT_PER_MINUTE = _int_env("FILEFORGE_RATE_LIMIT_PER_MINUTE", 30)

# --- outputs --------------------------------------------------------------------
# The landing page promises files are deleted 24h after processing.
# Download links expire at the same moment the janitor removes the file.
RETENTION_HOURS = _int_env("FILEFORGE_RETENTION_HOURS", 24)
