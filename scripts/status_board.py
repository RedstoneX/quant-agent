#!/usr/bin/env python3
"""Generate the human status board by RE-DERIVING every claim from the system.

Why this exists
---------------
Every human-facing status document in this repo has gone stale, repeatedly and
expensively. `PROJECT_COMPASS.md` carried a production SHA three deploys out of
date and a claim that the account had no margin (it is a 4x margin account).
`STATE.md` named a production version three deploys stale. The remediation spec
said Phase 2b was undeployed when it had been live for a day. Five separate
wrong claims inside two days, every one of them a fact somebody had to REMEMBER
to update and did not.

So this board records nothing. It reads `docs/phases.yaml` — where each phase
carries mechanically checkable evidence rules — re-evaluates every rule against
the current tree, reads live state off the production box and its database, and
renders what it found.

The important output is not "phase 3 is done". It is the DISAGREEMENT case: a
phase recorded as done whose evidence no longer holds is reported as
`CONTRADICTED`, loudly. That is the rot detector, and it is the whole point.

Anything that cannot be established mechanically renders as `unknown`. It never
guesses, and it never falls back to the recorded claim.

Usage
-----
    python scripts/status_board.py --out data/board/index.html

Runs read-only. It never writes to the production checkout or its database.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PROD_CHECKOUT = Path("/home/qamc/quant-agent")
ET = ZoneInfo("America/New_York")

PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"

#: A test/symbol rule must name an identifier, not describe one in prose.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


# --------------------------------------------------------------------------
# shelling out
# --------------------------------------------------------------------------

def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 20) -> tuple[int, str]:
    """Run a command, never raise. Returns (returncode, stdout+stderr)."""
    try:
        p = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, capture_output=True,
            text=True, timeout=timeout,
        )
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as exc:  # noqa: BLE001 - a broken probe must not kill the board
        return 127, f"{type(exc).__name__}: {exc}"


def _read_prod(rel: str) -> tuple[bool, str]:
    """Read a file from the production checkout, whoever we happen to be.

    On the box this script runs as `qamc` and can read directly. From the
    engineering account it needs `sudo -n -u qamc`, which is configured. If
    neither works we report unknown rather than inventing a value — the whole
    point of this board.
    """
    target = PROD_CHECKOUT / rel
    if os.access(target, os.R_OK):
        try:
            return True, target.read_text()
        except OSError:
            pass
    rc, out = _run(["sudo", "-n", "-u", "qamc", "cat", str(target)])
    return (rc == 0), out


def _prod_git(*args: str) -> tuple[bool, str]:
    direct = ["git", "-C", str(PROD_CHECKOUT), *args]
    rc, out = _run(direct)
    if rc == 0:
        return True, out
    rc, out = _run(["sudo", "-n", "-u", "qamc", *direct])
    return (rc == 0), out


# --------------------------------------------------------------------------
# evidence rules
# --------------------------------------------------------------------------

@dataclass
class RuleResult:
    kind: str
    verdict: str
    note: str
    detail: str = ""


def _setting(cfg: dict, dotted: str) -> Any:
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return KeyError
        node = node[part]
    return node


def check_rule(rule: dict, cfg: dict, repo_root: Path = REPO_ROOT) -> RuleResult:
    kind = str(rule.get("kind", "?"))
    note = str(rule.get("note", ""))

    if kind == "manual":
        return RuleResult(kind, UNKNOWN, note, "needs a human to confirm")

    if kind == "commit_in_main":
        sha = str(rule.get("sha", ""))
        rc, _ = _run(["git", "merge-base", "--is-ancestor", sha, "origin/main"], repo_root)
        ok = rc == 0
        return RuleResult(kind, PASS if ok else FAIL, note,
                          f"{sha[:9]} {'is' if ok else 'is NOT'} in main")

    if kind == "pr_merged":
        num = rule.get("number")
        # Ask git before asking GitHub. A merged PR leaves its own merge
        # commit in main's history ("Merge pull request #N from ..."), which
        # is the same fact, checkable offline, with no credential.
        #
        # This matters where the board actually runs. `gh` is installed on the
        # production box but the runtime account is not authenticated, and
        # putting a GitHub token on the account that trades is a credential
        # decision for the owner, not a convenience for this script. Without
        # the git path, 13 of the manifest's rules would report `unknown` on
        # the box for no better reason than that.
        #
        # `repo_root` defaults to this checkout but is injectable so tests can
        # point it at a throwaway repo with known history — the production
        # box's git history is not a fixture and a CI runner's shallow clone
        # is not full history either.
        rc, out = _run(
            ["git", "log", "origin/main", "--merges", "--format=%s",
             f"--grep=^Merge pull request #{num} from ", "-1"],
            repo_root,
        )
        if rc == 0 and out.strip():
            return RuleResult(kind, PASS, note, f"PR #{num} merge commit is in main")
        # No merge commit found. That is not proof of absence — a squash or
        # rebase merge leaves none — so fall through to GitHub rather than
        # calling it a failure, and report unknown if that is unavailable too.
        rc, out = _run(["gh", "pr", "view", str(num), "--repo", "RedstoneX/quant-agent",
                        "--json", "state", "-q", ".state"], repo_root)
        if rc != 0:
            return RuleResult(
                kind, UNKNOWN, note,
                f"PR #{num}: no merge commit in main, and GitHub is unreachable "
                "from here (the runtime account has no gh credential)")
        return RuleResult(kind, PASS if out.strip() == "MERGED" else FAIL, note,
                          f"PR #{num} is {out.strip()}")

    if kind == "file_exists":
        p = REPO_ROOT / str(rule.get("path", ""))
        return RuleResult(kind, PASS if p.exists() else FAIL, note, str(rule.get("path")))

    if kind == "symbol_in_file":
        p = REPO_ROOT / str(rule.get("path", ""))
        sym = str(rule.get("symbol", ""))
        if not p.exists():
            return RuleResult(kind, FAIL, note, f"{rule.get('path')} is missing")
        try:
            found = sym in p.read_text(errors="replace")
        except OSError as exc:
            return RuleResult(kind, UNKNOWN, note, str(exc))
        return RuleResult(kind, PASS if found else FAIL, note,
                          f"{sym!r} {'found' if found else 'NOT found'} in {rule.get('path')}")

    if kind == "test_exists":
        p = REPO_ROOT / str(rule.get("path", ""))
        test = str(rule.get("test", ""))
        # A malformed rule must never read as a failing system. The first real
        # run of this board reported Phase 1 as CONTRADICTED because its rule
        # carried a prose description ("test_context.py exists as a dedicated
        # test module ... (27 tests per the spec's own note)") where a test
        # identifier belongs. The file was fine; the ruler was bent. A broken
        # instrument is reported as unknown, loudly, and never as rot.
        if not _IDENTIFIER.match(test):
            return RuleResult(kind, UNKNOWN, note,
                              f"malformed rule: {test!r} is prose, not a test name")
        if not p.exists():
            return RuleResult(kind, FAIL, note, f"{rule.get('path')} is missing")
        try:
            found = test in p.read_text(errors="replace")
        except OSError as exc:
            return RuleResult(kind, UNKNOWN, note, str(exc))
        return RuleResult(kind, PASS if found else FAIL, note,
                          f"{test} {'present' if found else 'MISSING'}")

    if kind == "setting_equals":
        got = _setting(cfg, str(rule.get("key", "")))
        if got is KeyError:
            return RuleResult(kind, FAIL, note, f"{rule.get('key')} not present in settings")
        want = rule.get("value")
        ok = str(got) == str(want)
        return RuleResult(kind, PASS if ok else FAIL, note,
                          f"{rule.get('key')} = {got!r} (expected {want!r})")

    if kind == "setting_present":
        # Unlike setting_equals, this rule makes no claim about the value —
        # only that the key exists at all. That is the shape needed for a
        # setting that is expected to keep changing (a stopgap re-tuned over
        # time): pinning a specific value would make every legitimate re-tune
        # look like rot, and the day the key is finally removed on purpose is
        # exactly the moment a value-pinned rule would go quiet instead of
        # flagging that the phase needs re-evaluating.
        got = _setting(cfg, str(rule.get("key", "")))
        present = got is not KeyError
        return RuleResult(kind, PASS if present else FAIL, note,
                          f"{rule.get('key')} {'is present' if present else 'is NOT present'} "
                          "in settings")

    return RuleResult(kind, UNKNOWN, note, f"unrecognised rule kind {kind!r}")


# --------------------------------------------------------------------------
# is this summary written for HIM, or for a developer?
# --------------------------------------------------------------------------
#
# `plain_summary` exists so the owner — who reads this board and nothing
# else, and is not a developer — can tell what happened without opening the
# code. It lives inside docs/phases.yaml, an engineering document maintained
# by engineering agents, so left unwatched it fills back up with PR numbers,
# file paths and function names the moment the next agent writes one. That
# already happened: several summaries below carry exactly that.
#
# Rejecting jargon outright — refusing to accept a summary that contains it —
# was tried and rejected, correctly. Blocking "bad words" produces
# jargon-free prose that is still useless to him; it does not produce good
# writing. What this board can do honestly is detect the MECHANICAL SHAPE of
# engineering text — a path, a PR number, a hash, a code identifier — and
# show it to him, the one person who can actually judge whether a summary
# reads like it was written for him. It never blocks and never rewrites: a
# bad description still renders, because an unreadable description is more
# useful than no description at all.
#
# Deliberately absent: a wordlist of "technical-sounding" words. That would
# flag ordinary sentences as readily as real jargon and teach him to ignore
# the marker — the exact failure mode blocking on it was rejected for.

_JARGON_PATH_EXT = re.compile(
    r"\b[\w-]+\.(?:py|ya?ml|md|html|json|db|sh|toml|cfg|ini|log|txt)\b", re.I)
#: A leading "/" not glued onto a digit, so "3/15/2026" (a date) and "1/3" (a
#: fraction) don't read as `/home/qamc/quant-agent` does.
_JARGON_ABS_PATH = re.compile(r"(?<!\w)/[\w.-]+(?:/[\w.-]+)+")
#: A directory this repo actually has, one more path segment deep, with no
#: extension required — catches a bare directory mention like `src/backtest/`
#: that `_JARGON_PATH_EXT` would miss. Anchored to real top-level directory
#: names (not e.g. bare "data/") so an ordinary slash pairing in prose —
#: "cost/benefit", "buy/sell", "his/her" — never matches: none of those
#: words is followed by a second "/segment".
_JARGON_DIR_ONLY = re.compile(r"\b(?:docs|src|scripts|tests|config|data)/[\w.-]+/[\w.-]*")
_JARGON_PR_REF = re.compile(r"#\d+\b")
_JARGON_HEX_TOKEN = re.compile(r"\b[0-9a-fA-F]{7,40}\b")
_JARGON_BACKTICK = re.compile(r"`[^`]+`")
_JARGON_SNAKE_CASE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
#: `word(...)`, but not `word(s)` / `word(es)` — ordinary English pluralises
#: that way ("trade(s)") and it must not read as a function call.
_JARGON_FUNC_CALL = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\((?!s\)|es\))[^()]*\)")


def _is_commit_hash_token(token: str) -> bool:
    """7+ hex characters is also a plain 7-digit number — a dollar figure, a
    count, a year. A decimal number can't contain a-f, so require at least
    one of those letters before calling it a hash; otherwise an ordinary
    big number sitting in the text would read as a commit reference."""
    return any(c in "abcdefABCDEF" for c in token)


def summary_engineering_markers(summary: str) -> list[str]:
    """The mechanical, engineer-facing markers found in `summary` — empty if
    none. Each one is a SHAPE (a path, a reference number, a hash, a code
    token), never a word choice, so this cannot flag a summary for sounding
    technical — only for literally containing developer syntax. See the
    section comment above for why that line is drawn there.
    """
    found: list[str] = []
    if (_JARGON_PATH_EXT.search(summary) or _JARGON_ABS_PATH.search(summary)
            or _JARGON_DIR_ONLY.search(summary)):
        found.append("a file path")
    if _JARGON_PR_REF.search(summary):
        found.append("a PR or issue number")
    if any(_is_commit_hash_token(t) for t in _JARGON_HEX_TOKEN.findall(summary)):
        found.append("a commit hash")
    if (_JARGON_BACKTICK.search(summary) or _JARGON_SNAKE_CASE.search(summary)
            or _JARGON_FUNC_CALL.search(summary)):
        found.append("a code identifier")
    return found


def summary_is_engineer_facing(summary: str) -> tuple[bool, str]:
    """Whether `summary` reads as written for an engineer instead of the
    owner, and, in his own words, why — the reason is what actually renders
    on the board, so he never has to take the flag on faith.

    A missing or empty summary is flagged too, with its own reason: silence
    is not neutral here, it is a plain-English description nobody wrote.
    """
    text = (summary or "").strip()
    if not text:
        return True, "no plain-English description was written for this item"
    markers = summary_engineering_markers(text)
    if not markers:
        return False, ""
    return True, "reads like engineering notes — it contains " + ", ".join(markers)


# --------------------------------------------------------------------------
# phases
# --------------------------------------------------------------------------

@dataclass
class PhaseView:
    id: str
    title: str
    summary: str
    recorded: str
    confidence: str
    results: list[RuleResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.verdict == PASS)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.verdict == FAIL)

    @property
    def unknown(self) -> int:
        return sum(1 for r in self.results if r.verdict == UNKNOWN)

    @property
    def checkable(self) -> int:
        return self.passed + self.failed

    @property
    def summary_flagged(self) -> bool:
        """True when `summary` reads as written for an engineer, not him —
        see `summary_is_engineer_facing` for what that checks and why."""
        flagged, _ = summary_is_engineer_facing(self.summary)
        return flagged

    @property
    def summary_flag_reason(self) -> str:
        """Plain-English reason `summary_flagged` is True; "" when it isn't."""
        _, reason = summary_is_engineer_facing(self.summary)
        return reason

    @property
    def verdict(self) -> str:
        """CONFIRMED / CONTRADICTED / UNVERIFIED.

        CONTRADICTED is the one that matters: the manifest says this phase is
        done, but its own proof no longer holds. That is documentation rot
        caught the moment it happens instead of five days later.
        """
        if self.failed:
            return "CONTRADICTED"
        if self.checkable == 0:
            return "UNVERIFIED"
        return "CONFIRMED"


def load_phases(manifest: Path, cfg: dict) -> list[PhaseView]:
    raw = yaml.safe_load(manifest.read_text())
    entries = raw["phases"] if isinstance(raw, dict) and "phases" in raw else raw
    views: list[PhaseView] = []
    for e in entries:
        v = PhaseView(
            id=str(e.get("id", "?")),
            title=str(e.get("title", "?")),
            summary=str(e.get("plain_summary", "")),
            recorded=str(e.get("status", "?")),
            confidence=str(e.get("confidence", "?")),
        )
        for rule in e.get("evidence", []) or []:
            v.results.append(check_rule(rule, cfg))
        views.append(v)
    return views


# --------------------------------------------------------------------------
# live state
# --------------------------------------------------------------------------

def live_state() -> dict[str, Any]:
    s: dict[str, Any] = {}

    _run(["git", "fetch", "origin", "--quiet"], REPO_ROOT)
    rc, main_sha = _run(["git", "rev-parse", "origin/main"], REPO_ROOT)
    s["main_sha"] = main_sha[:9] if rc == 0 else None

    ok, box_sha = _prod_git("rev-parse", "HEAD")
    # The full, untruncated SHA is what gets stamped into the page for the
    # serve-time freshness check (src/api/server.py) to compare against a
    # freshly-read live SHA — a 9-char prefix is fine for a human footer but
    # is a needless (if tiny) collision risk for a machine equality check.
    s["box_sha_full"] = box_sha.strip() if ok else None
    s["box_sha"] = s["box_sha_full"][:9] if s["box_sha_full"] else None

    if s["main_sha"] and s["box_sha"]:
        rc, out = _run(
            ["git", "log", "--oneline", "--merges", f"{box_sha.strip()}..origin/main"],
            REPO_ROOT,
        )
        merges = [l for l in out.splitlines() if "Merge pull request" in l]
        s["undeployed_merges"] = len(merges)
        s["in_sync"] = box_sha.strip().startswith(main_sha.strip()[:9])
    else:
        s["undeployed_merges"] = None
        s["in_sync"] = None

    ok, dirty = _prod_git("status", "--porcelain")
    if ok:
        tracked = [l for l in dirty.splitlines() if l and not l.startswith("??")]
        s["box_uncommitted"] = len(tracked)
    else:
        s["box_uncommitted"] = None

    # --- the ledger, read strictly read-only -------------------------------
    s.update(_read_ledger())
    return s


def _read_ledger() -> dict[str, Any]:
    """Read today's spend and the circuit latch from the production database.

    Opened through a read-only URI so this can never mutate the live ledger,
    and copied first if we cannot open it in place.
    """
    out: dict[str, Any] = {
        "spend_today": None, "day": None, "circuit": None,
        "sessions_today": None, "costs_exact": None,
    }
    db = PROD_CHECKOUT / "data" / "quant_agent.db"
    tmp: Path | None = None
    try:
        if os.access(db, os.R_OK):
            src = db
        else:
            tmp = Path("/tmp") / f"board-snapshot-{os.getpid()}.db"
            rc, _ = _run(["bash", "-c", f"sudo -n cat {db} > {tmp}"], timeout=60)
            if rc != 0 or not tmp.exists():
                return out
            src = tmp
        conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        today = datetime.now(ET).strftime("%Y-%m-%d")
        row = conn.execute(
            "SELECT day, incremental_cost_usd AS spend, costs_exact "
            "FROM llm_budget_days WHERE day=?", (today,),
        ).fetchone()
        if row:
            out["day"] = row["day"]
            out["spend_today"] = float(row["spend"] or 0)
            out["costs_exact"] = bool(row["costs_exact"])
        st = conn.execute(
            "SELECT suspended, trigger_code FROM llm_circuit_state WHERE singleton=1"
        ).fetchone()
        if st:
            out["circuit"] = ("halted: " + str(st["trigger_code"] or "unknown trigger")
                              if int(st["suspended"] or 0) else "clear")
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM llm_budget_sessions WHERE day=?", (today,)
        ).fetchone()
        out["sessions_today"] = int(n["n"]) if n else None
        # A day with no sessions has no budget row, which is not the same thing
        # as a budget that could not be read. Reporting "unknown" there is a
        # lie of omission: the board sat next to "0 sessions ran today" and
        # still claimed it could not tell what had been spent. If the ledger
        # opened and nothing ran, the answer is exactly zero.
        if out["spend_today"] is None and out["sessions_today"] == 0:
            out["spend_today"] = 0.0
            out["costs_exact"] = True
            out["day"] = today
        conn.close()
    except Exception as exc:  # noqa: BLE001 - unknown beats a wrong number
        out["circuit"] = None
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if tmp and tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return out


def read_settings() -> dict:
    """Prefer the box's live settings; fall back to the repo's."""
    ok, text = _read_prod("config/settings.yaml")
    if not ok:
        try:
            text = (REPO_ROOT / "config" / "settings.yaml").read_text()
        except OSError:
            return {}
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return {}


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------

def _esc(s: Any) -> str:
    return html.escape(str(s), quote=True)


def _fmt(value: Any, unit: str = "") -> str:
    if value is None:
        return '<span class="unk">unknown</span>'
    return f"{_esc(value)}{unit}"


VERDICT_PILL = {
    "CONFIRMED": ("p-done", "Verified"),
    "CONTRADICTED": ("p-urgent", "Proof failed"),
    "UNVERIFIED": ("p-none", "Not checkable"),
}

#: The one recorded status that means "nothing left to do, and the board's
#: own re-check agrees." Everything else -- PARTIAL, NOT STARTED, OPEN,
#: OVERTAKEN, or a status the board can't confirm -- stays in the visible,
#: uncollapsed list. This governs presentation only; it reads fields the
#: renderer already computes and changes no evidence rule.
_SETTLED_STATUSES = {"done and live"}


def _is_settled(p: PhaseView) -> bool:
    """Fully verified AND finished: the manifest claims the canonical done
    state, and the board's live re-check still confirms it. A phase that is
    merely CONFIRMED but recorded as PARTIAL/OPEN/etc. still has open work
    and must stay visible, not tucked into the collapsed section."""
    return p.verdict == "CONFIRMED" and p.recorded.strip().lower() in _SETTLED_STATUSES


def _row(p: PhaseView) -> str:
    cls, label = VERDICT_PILL[p.verdict]
    detail = f"{p.passed} of {p.checkable} checks pass"
    if p.unknown:
        detail += f" &middot; {p.unknown} need a human"
    # The flag renders ABOVE the summary, never instead of it — the original
    # text still shows underneath even when it's flagged, because an
    # unreadable description is more useful to him than no description.
    jargon = ""
    if p.summary_flagged:
        jargon = (
            '<div class="jargon-flag">Not written for you &mdash; this reads '
            'like a note for a developer. Needs a plain-English rewrite.'
            f'<span class="jargon-why">{_esc(p.summary_flag_reason)}</span></div>'
        )
    return (
        f'<tr><td><span class="pill {cls}">{_esc(label)}</span></td>'
        f'<td><b>{_esc(p.title)}</b>'
        f'{jargon}'
        f'<i>{_esc(p.summary)}</i>'
        f'<u>recorded as &ldquo;{_esc(p.recorded.lower())}&rdquo; &middot; {detail}</u></td></tr>'
    )


def render(phases: list[PhaseView], state: dict[str, Any], template: Path) -> str:
    now = datetime.now(ET)
    total_rules = sum(len(p.results) for p in phases)
    total_pass = sum(p.passed for p in phases)
    total_fail = sum(p.failed for p in phases)
    total_unknown = sum(p.unknown for p in phases)
    contradicted = [p for p in phases if p.verdict == "CONTRADICTED"]
    jargon_flagged = [p for p in phases if p.summary_flagged]

    # Relevance ordering: anything CONTRADICTED first (loud, never
    # collapsed), then everything else still open or unverified, then --
    # only at the very bottom, and only inside a closed <details> -- the
    # phases that are both fully verified and recorded as finished. This
    # changes how phases are grouped and displayed, not which rule kind
    # produced which verdict.
    settled = [p for p in phases if _is_settled(p)]
    attention = contradicted + [p for p in phases if p not in contradicted and not _is_settled(p)]

    if attention:
        rows = f'<table class="plan">{"".join(_row(p) for p in attention)}</table>'
    else:
        rows = '<p class="lede">Nothing needs attention right now &mdash; every open item has settled.</p>'
    if settled:
        rows += (
            '<details class="finished">'
            f'<summary>{len(settled)} finished and verified &mdash; expand to review</summary>'
            f'<table class="plan">{"".join(_row(p) for p in settled)}</table>'
            '</details>'
        )

    alarm = ""
    if contradicted:
        names = ", ".join(_esc(p.title) for p in contradicted)
        alarm = (
            '<div class="item gap"><span class="tag t-gap">Rot detected</span>'
            f'<h3>{len(contradicted)} phase(s) claim to be done but no longer prove it</h3>'
            f'<p>{names}</p>'
            '<p>Something that was true has stopped being true. This is the failure '
            'this board exists to catch.</p></div>'
        )
    else:
        alarm = (
            '<div class="item done"><span class="tag t-done">Clean</span>'
            '<h3>Every recorded status still proves out</h3>'
            '<p>No phase claims to be finished on evidence that has since stopped '
            'holding.</p></div>'
        )

    # Reports, never gates: a flagged summary still renders in full further
    # down (see `_row`). This is only the count, placed where the freshness
    # banner already lives — right at the top, before he has to scroll past
    # anything else — so he does not have to hunt through the list to find
    # out how many descriptions were written for a developer instead of him.
    jargon_banner = ""
    if jargon_flagged:
        n = len(jargon_flagged)
        jargon_banner = (
            '<div class="jargon-banner"><b>'
            f'{n} description{"s" if n != 1 else ""} below {"are" if n != 1 else "is"} '
            'written for a developer, not for you.</b> They are marked where they '
            'appear so you can tell them apart from the ones already in plain '
            'English &mdash; nothing is hidden, they still say what they say.'
            '</div>'
        )

    if state.get("in_sync") is True:
        deploy = ('<span class="dot ok"></span> The machine is running the latest '
                  'finished work.')
    elif state.get("undeployed_merges"):
        deploy = (f'<span class="dot warn"></span> '
                  f'{state["undeployed_merges"]} finished change(s) are not on the '
                  'machine yet.')
    else:
        deploy = '<span class="dot unk"></span> Deploy state could not be read.'

    spend = state.get("spend_today")
    limit = 2.75
    pct = int(round(100 * spend / limit)) if isinstance(spend, (int, float)) else None

    body = template.read_text()
    body = body.replace("{{JARGON_BANNER}}", jargon_banner)
    body = body.replace("{{STAMP}}", now.strftime("%A %-d %B %Y &middot; %H:%M ET"))
    # The full commit this page was built against, stamped into a
    # machine-readable <meta> tag. src/api/server.py reads it back out at
    # serve time and compares it to a freshly-read live SHA — that comparison
    # (fact vs. fact), not page age, is what decides whether a freshness
    # banner is shown. Empty when the box's SHA could not be read, which the
    # server-side check treats as UNKNOWN, never as "fine".
    body = body.replace("{{BUILT_SHA}}", _esc(state.get("box_sha_full") or ""))
    body = body.replace("{{DEPLOY}}", deploy)
    body = body.replace("{{CIRCUIT}}", _fmt(state.get("circuit")))
    body = body.replace("{{SPEND}}", f"${spend:.2f}" if spend is not None else
                        '<span class="unk">unknown</span>')
    body = body.replace("{{SPEND_PCT}}", str(pct if pct is not None else 0))
    body = body.replace("{{SPEND_NOTE}}",
                        ("nothing has run today" if spend == 0
                         and state.get("sessions_today") == 0
                         else f"of the ${limit:.2f} daily limit &mdash; {pct}% used")
                        if pct is not None else "daily spend could not be read")
    body = body.replace("{{SESSIONS}}", _fmt(state.get("sessions_today")))
    body = body.replace("{{ROWS}}", rows)
    body = body.replace("{{ALARM}}", alarm)
    body = body.replace("{{RULES_TOTAL}}", str(total_rules))
    body = body.replace("{{RULES_PASS}}", str(total_pass))
    body = body.replace("{{RULES_FAIL}}", str(total_fail))
    body = body.replace("{{RULES_UNKNOWN}}", str(total_unknown))
    body = body.replace("{{BOX_SHA}}", _fmt(state.get("box_sha")))
    body = body.replace("{{MAIN_SHA}}", _fmt(state.get("main_sha")))
    return body


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="data/board/index.html")
    ap.add_argument("--manifest", default="docs/phases.yaml")
    ap.add_argument("--template", default="scripts/status_board_template.html")
    ap.add_argument("--json", action="store_true", help="also print the findings as JSON")
    ap.add_argument("--explain", metavar="PHASE_ID", default=None,
                    help="print every rule and its verdict for one phase, then exit")
    args = ap.parse_args()

    manifest = REPO_ROOT / args.manifest
    if not manifest.exists():
        print(f"manifest not found: {manifest}", file=sys.stderr)
        return 2

    cfg = read_settings()

    if args.explain:
        raw = yaml.safe_load(manifest.read_text())
        entries = raw["phases"] if isinstance(raw, dict) and "phases" in raw else raw
        hit = [e for e in entries if str(e.get("id")) == args.explain]
        if not hit:
            print(f"no phase with id {args.explain!r}. ids: "
                  + ", ".join(str(e.get('id')) for e in entries), file=sys.stderr)
            return 2
        e = hit[0]
        print(f"{e.get('title')}  —  recorded as {e.get('status')}")
        for rule in e.get("evidence", []) or []:
            r = check_rule(rule, cfg)
            mark = {PASS: " ok ", FAIL: "FAIL", UNKNOWN: " ?? "}[r.verdict]
            print(f"  [{mark}] {r.kind:16s} {r.detail}")
            if r.verdict == FAIL:
                print(f"           rule: {rule}")
        return 0

    phases = load_phases(manifest, cfg)
    state = live_state()

    out = Path(args.out)
    if not out.is_absolute():
        out = REPO_ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(phases, state, REPO_ROOT / args.template))

    contradicted = [p.title for p in phases if p.verdict == "CONTRADICTED"]
    if args.json:
        print(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "out": str(out),
            "contradicted": contradicted,
            "state": {k: v for k, v in state.items()},
            "phases": [
                {"id": p.id, "recorded": p.recorded, "verdict": p.verdict,
                 "pass": p.passed, "fail": p.failed, "unknown": p.unknown}
                for p in phases
            ],
        }, indent=2, default=str))
    else:
        print(f"wrote {out}")
        print(f"phases: {len(phases)}  contradicted: {len(contradicted)}")
        if contradicted:
            print("CONTRADICTED: " + ", ".join(contradicted))

    # A contradiction is worth a non-zero exit so a timer can alert on it.
    return 1 if contradicted else 0


if __name__ == "__main__":
    raise SystemExit(main())
