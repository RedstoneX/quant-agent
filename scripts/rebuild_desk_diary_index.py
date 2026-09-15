#!/usr/bin/env python3
"""Rebuild ``data/diary/index.html`` from dated day pages (newest first).

The owner's bot writes ``YYYY-MM-DD.html`` into gitignored ``data/diary/``.
This script only rewrites the listing. It never invents diary content.

    python scripts/rebuild_desk_diary_index.py
    python scripts/rebuild_desk_diary_index.py --dir /path/to/diary
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.api.diary_pages import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
