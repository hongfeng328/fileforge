"""Janitor: delete expired files so the 24h promise is real (item 2).

- Output files older than ``limits.RETENTION_HOURS`` are deleted.
- Incoming (materialised upload) files older than 1 hour are deleted;
  they are normally removed right after each tool call, this is only a
  backstop for crashed calls.

Run once:   ``python -m fileforge.janitor``
Run by the server: started as a background thread when
FILEFORGE_JANITOR_ENABLE=1 (the deployment unit also runs it hourly
via a systemd timer, so files expire even if the app is down).
"""

from __future__ import annotations

import time
from pathlib import Path

from fileforge import limits
from fileforge.storage import INCOMING_DIR
from fileforge.tools import OUTPUT_DIR

INCOMING_MAX_AGE_SECONDS = 3600


def _sweep(directory: Path, max_age_seconds: float, now: float) -> int:
    if not directory.is_dir():
        return 0
    removed = 0
    for entry in directory.iterdir():
        try:
            if entry.is_file() and now - entry.stat().st_mtime > max_age_seconds:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def cleanup_expired(now: float | None = None) -> dict[str, int]:
    now = time.time() if now is None else now
    return {
        "outputs_removed": _sweep(OUTPUT_DIR, limits.RETENTION_HOURS * 3600, now),
        "incoming_removed": _sweep(INCOMING_DIR, INCOMING_MAX_AGE_SECONDS, now),
    }


def main() -> int:
    result = cleanup_expired()
    print(
        f"janitor: removed {result['outputs_removed']} output file(s), "
        f"{result['incoming_removed']} incoming file(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
