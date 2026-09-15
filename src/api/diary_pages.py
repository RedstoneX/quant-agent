"""Read-only desk-diary files under gitignored ``data/diary/``.

The owner's bot writes dated HTML pages. Mission Control only serves them
(``src/api/server.py`` mounts the directory at ``/diary``) and this helper
rebuilds the listing. Nothing here invents diary content or touches trading.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

DIARY_DIR = Path(__file__).resolve().parents[2] / "data" / "diary"

# Filenames the listing treats as a day page: 2026-09-15.html.
_DAY_PAGE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.html$")

_EMPTY_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QAMC Desk Diary</title>
<style>
body{font-family:system-ui,sans-serif;max-width:42rem;margin:2rem auto;padding:0 1rem;line-height:1.5;color:#111}
a{color:#1d4ed8}
.muted{color:#4b5563}
</style>
</head>
<body>
<h1>Desk diary</h1>
<p class="muted">No diary entries yet. Daily pages are written into this
folder on the live box; this listing stays empty until the first one
lands.</p>
<p><a href="/cockpit/">Back to Mission Control</a></p>
</body>
</html>
"""


def empty_index_html() -> str:
    return _EMPTY_INDEX_HTML


def day_pages(directory: Path) -> list[Path]:
    """Dated HTML pages, newest date first. Ignores anything else in the folder."""
    pages = [p for p in directory.glob("????-??-??.html") if _DAY_PAGE_RE.match(p.name)]
    pages.sort(key=lambda p: p.stem, reverse=True)
    return pages


def index_html_for(pages: list[Path]) -> str:
    if not pages:
        return empty_index_html()
    items = "\n".join(
        f'  <li><a href="{p.name}">{p.stem}</a></li>' for p in pages
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QAMC Desk Diary</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:42rem;margin:2rem auto;padding:0 1rem;line-height:1.5;color:#111}}
a{{color:#1d4ed8}}
.muted{{color:#4b5563}}
ul{{padding-left:1.2rem}}
li{{margin:0.35rem 0}}
</style>
</head>
<body>
<h1>Desk diary</h1>
<p class="muted">Daily notes from the desk. Read-only.</p>
<ul>
{items}
</ul>
<p><a href="/cockpit/">Back to Mission Control</a></p>
</body>
</html>
"""


def ensure_diary_dir(directory: Path | None = None) -> Path:
    """Create the diary folder and a placeholder index so ``/diary`` never 500s.

    StaticFiles refuses a missing directory at mount time, and ``html=True``
    404s when there is no ``index.html``. Either case is a blank owner
    surface, not a server fault — so we create an empty listing instead.
    Never overwrites an existing index (the rebuild script owns that file
    once day pages exist).
    """
    root = directory if directory is not None else DIARY_DIR
    root.mkdir(parents=True, exist_ok=True)
    index = root / "index.html"
    if not index.is_file():
        index.write_text(empty_index_html(), encoding="utf-8")
    return root


def rebuild_diary_index(directory: Path | None = None) -> Path:
    """Rewrite ``index.html`` from ``YYYY-MM-DD.html`` pages, newest first."""
    root = ensure_diary_dir(directory)
    index = root / "index.html"
    index.write_text(index_html_for(day_pages(root)), encoding="utf-8")
    return index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild data/diary/index.html from dated day pages (newest first)."
    )
    parser.add_argument(
        "--dir",
        default=str(DIARY_DIR),
        help="Diary directory (default: data/diary at the repo root)",
    )
    args = parser.parse_args(argv)
    index = rebuild_diary_index(Path(args.dir))
    pages = day_pages(index.parent)
    print(f"wrote {index} ({len(pages)} day page(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
