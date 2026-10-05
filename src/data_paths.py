"""The single resolved location of the desk's on-disk data.

Every writer asks this module where the data lives instead of re-deriving it
from its own ``__file__`` position.  Before this existed, five modules each
computed ``data/quant_agent.db`` themselves and, because they sit at different
nesting depths, they spelled it with different numbers of ``.parent`` hops --
two from ``src/*.py``, three from ``src/notifier/*.py``.  Both forms landed on
the same file, but nothing in the tree said so, and nothing stopped the sixth
writer from getting the depth wrong.

Everything here is COMPUTED ON EACH CALL.  Nothing is stored in a module-level
path constant, nothing is cached on disk, and no absolute path is written down:
the root is read from the location of this file, so a second checkout resolves
to its own data directory without any configuration.
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "repo_root",
    "data_dir",
    "db_path",
    "alerting_dir",
    "board_dir",
    "diary_dir",
    "parse_failure_dir",
]


def repo_root() -> Path:
    """The checkout root: the parent of the ``src`` package holding this file."""
    return Path(__file__).resolve().parents[1]


def data_dir() -> Path:
    """The checkout's ``data`` directory."""
    return repo_root() / "data"


def db_path() -> Path:
    """The desk's SQLite audit trail."""
    return data_dir() / "quant_agent.db"


def alerting_dir() -> Path:
    """Where the watchdogs keep their heartbeat and pause files."""
    return data_dir() / "alerting"


def board_dir() -> Path:
    """Where the status board writes its rendered pages."""
    return data_dir() / "board"


def diary_dir() -> Path:
    """Where the diary pages are written."""
    return data_dir() / "diary"


def parse_failure_dir() -> Path:
    """Where an analyst stashes a payload it could not parse."""
    return data_dir() / "parse_failures"
