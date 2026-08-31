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
    s["box_sha"] = box_sha.strip()[:9] if ok else None

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

#: How old the rendered page can be before the staleness banner appears.
STALE_AFTER_HOURS = 6


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
    return (
        f'<tr><td><span class="pill {cls}">{_esc(label)}</span></td>'
        f'<td><b>{_esc(p.title)}</b>'
        f'<i>{_esc(p.summary)}</i>'
        f'<u>recorded as &ldquo;{_esc(p.recorded.lower())}&rdquo; &middot; {detail}</u></td></tr>'
    )


def _age_words(hours: float) -> str:
    if hours >= 47.5:
        days = round(hours / 24)
        return f"{days} day{'s' if days != 1 else ''}"
    whole = round(hours)
    return f"{whole} hour{'s' if whole != 1 else ''}"


def _staleness_banner(generated_at: datetime) -> str:
    """Warn a reader whose board has quietly stopped being rebuilt.

    The rebuild fires on change, not on a clock. A broken trigger therefore
    produces no error anywhere — it produces silence, and a page that still
    looks authoritative while describing last week. Nothing watches the
    watcher here on purpose: watchers watching watchers is a regress with no
    natural end. Instead the page carries its own build time and checks it
    against the reader's clock when opened, so the thing that reports the
    failure is the thing the reader was already looking at.

    This is the only script on the page. It does arithmetic on an embedded
    timestamp — no network, no libraries, nothing to fetch. A build-time check
    cannot do this job: at build time the page is always zero hours old, so
    the warning could never fire.
    """
    return (
        f'<div class="stale" id="stale" hidden data-built="{generated_at.isoformat()}"></div>'
        "<script>(function(){"
        "var e=document.getElementById('stale');if(!e)return;"
        "var h=(Date.now()-new Date(e.dataset.built).getTime())/36e5;"
        f"if(!(h>{STALE_AFTER_HOURS}))return;"
        "var d=h>=47.5,n=d?Math.round(h/24):Math.round(h),u=d?'day':'hour';"
        "e.textContent='';"
        "var b=document.createElement('b');"
        "b.textContent='This board was last rebuilt '+n+' '+u+(n!==1?'s':'')+' ago.';"
        "e.appendChild(b);"
        "e.appendChild(document.createTextNode(' It may not reflect current reality.'));"
        "e.hidden=false;})();</script>"
    )

def render(phases: list[PhaseView], state: dict[str, Any], template: Path) -> str:
    now = datetime.now(ET)
    total_rules = sum(len(p.results) for p in phases)
    total_pass = sum(p.passed for p in phases)
    total_fail = sum(p.failed for p in phases)
    total_unknown = sum(p.unknown for p in phases)
    contradicted = [p for p in phases if p.verdict == "CONTRADICTED"]

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

    stale_banner = _staleness_banner(now)

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
    body = body.replace("{{STAMP}}", now.strftime("%A %-d %B %Y &middot; %H:%M ET"))
    body = body.replace("{{STALE_BANNER}}", stale_banner)
    body = body.replace("{{DEPLOY}}", deploy)
    body = body.replace("{{CIRCUIT}}", _fmt(state.get("circuit")))
    body = body.replace("{{SPEND}}", f"${spend:.2f}" if spend is not None else
                        '<span class="unk">unknown</span>')
    body = body.replace("{{SPEND_PCT}}", str(pct if pct is not None else 0))
    body = body.replace("{{SPEND_NOTE}}",
                        f"of the ${limit:.2f} daily limit &mdash; {pct}% used"
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
