"""What a model-policy exam fixture may be built from — checked, not trusted.

Owner rule, 2026-09-14 (docs/INCIDENT_HISTORY.md, "seat exams were built on
the desk's own recordings"): an exam that replays what the desk recorded
inherits every defect of the code that recorded it. Its derived values
(indicators, levels, position facts, coverage labels) came from old code, and
its agent outputs (analyst reports, narratives, PM / risk / reviewer
decisions) came from old prompts and old models. A model scored against such
a fixture is scored against the old desk, not the seat as it runs today.

So a fixture may contain RAW FACTS ONLY, fetched fresh from the ORIGINAL
public source (yfinance bars, SEC EDGAR filings, ...), with the source and the
fetch date recorded per data section. Every derived value is recomputed by
today's live code when the exam runs. A fixture that breaks any of these
rules is QUARANTINED: `ops/model_policy/benchmark_models.py` refuses to run a
scenario built on it, and names why.

**2026-09-14, PM practice-day extension.** A PM-input fixture legitimately
needs analyst-seat OUTPUT (analyses, macro_analysis, news_intel, ...) — that
is what the live PM actually reads. A section may declare
`"kind": "fresh_analyst_output"` in its `_provenance.sections` entry instead
of the raw-facts `source`/`fetched_on`/`fetch` triple; that entry must then
name `model`, `route`, `timestamp` (ISO) and `source_fixture` (the raw-facts
manifest, itself checked by this same module, that the analyst call was run
against). Such a section is exempt from the agent-output / old-code-derived
key ban below, because being agent output is the whole point of it. It is
NOT exempt from the desk-source check: a fresh analyst call over desk data is
still refused. A second `kind`, `"synthetic_account_state"`, covers the one
other thing a PM fixture legitimately needs that is neither a public fact nor
an analyst output: a starting account (cash, positions) — a REAL account is
desk state, so this must be a labelled synthetic one (see
`SYNTHETIC_ACCOUNT_KIND`). This does not relax anything for a fixture that
has no such `kind` — those sections still need raw external provenance and
still ban agent-output keys exactly as before.

Desk-recorded data is not banned forever. It is refused until
`DESK_DATA_TRUSTED_FROM` is set, and then only for rows dated on or after
that date. Setting it is a reviewed change: `tests/test_fixture_policy.py`
fails unless docs/INCIDENT_HISTORY.md carries a justification naming the
setting and the exact date.

This module is the single definition of those rules. It does no I/O beyond
reading the fixture directory, and makes no network call.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

#: Earliest date from which desk-recorded rows may be used in a fixture.
#: None = no desk data is trusted at all. Change only with a justification
#: entry in docs/INCIDENT_HISTORY.md that names this setting and the date
#: (enforced by tests/test_fixture_policy.py).
DESK_DATA_TRUSTED_FROM: str | None = None

#: Where an external raw fact may come from. A section's `source` must name
#: one of these (the original public provider the live code itself calls).
EXTERNAL_SOURCES = (
    "https://www.sec.gov/",
    "https://data.sec.gov/",
    "https://efts.sec.gov/",
    "yfinance",
    # FRED (macro_analyst's live provider, src/data/macro.py:325 `Fred(...)`).
    "https://api.stlouisfed.org/",
    # RSS feeds news_analyst's live provider fetches
    # (src/data/news.py:RSS_FEEDS) — one prefix per feed's own host.
    "https://search.cnbc.com/",
    "https://feeds.marketwatch.com/",
    "https://finance.yahoo.com/",
    "https://seekingalpha.com/",
    "https://www.investing.com/",
    "https://www.nasdaq.com/",
    "https://feeds.bbci.co.uk/",
    "https://feeds.npr.org/",
    "https://www.federalreserve.gov/",
)

#: Anything that points at the desk's own records. Matched case-insensitively
#: against every string in a fixture manifest.
DESK_SOURCE_MARKERS = (
    "quant_agent.db",
    "agent_logs",
    "specialist_evidence",
    "/home/qamc",
    "data/checkpoints",
    "data/evening_replays",
    "data/news/",
    "data/smart_money/",
    "data/earnings/",
    "data/tech/",
    "data/macro/",
    "127.0.0.1:8800",
    "mission control",
)

#: A desk run id anywhere in a manifest means desk provenance.
DESK_RUN_ID_RE = re.compile(
    r"\b(?:run|midday|close|evening|intra_check|earnings_preprocess|meta)-[0-9a-f]{8}\b"
)

#: Keys that only exist on an AGENT OUTPUT — an analyst report, a narrative,
#: or a decision-seat verdict. A raw-facts fixture never has them.
AGENT_OUTPUT_KEYS = frozenset({
    "analyses", "tech_analyses", "earnings_analyses", "earnings_results",
    "analysis", "news_intel", "macro_analysis", "macro_narrative",
    "previous_narrative", "news_narrative", "last_state", "state_changes",
    "stock_news", "pm_briefing", "market_sentiment", "rating", "conviction",
    "reasoning", "reasoning_chain", "key_thesis", "investment_implications",
    "portfolio_decision", "decisions", "targets", "verdict", "modifications",
    "rejected_symbols", "actions", "review", "findings", "prior_ratings",
    "yesterday_insights", "memory", "what_the_live_desk_did",
    "recorded_response", "weekly_narrative", "active_state_changes",
    "own_recent_decisions", "trade_grade_summary", "calibration_note",
})

#: Keys that only exist on a value some code DERIVED. A fixture stores the
#: raw fact; today's code derives these again at exam time.
OLD_CODE_DERIVED_KEYS = frozenset({
    "indicators", "computed_levels", "computed_level_touches", "risk_reward",
    "support_levels", "resistance_levels", "position_facts", "metric_deltas",
    "heat", "coverage", "macro_coverage", "news_coverage", "event_coverage",
    "fomc_coverage", "macro_summary", "event_risk_block", "text_excerpt",
    "xbrl_facts", "stock_mentions", "news_text", "recorded_user_message",
    "user_message", "rendered_prompt", "fidelity", "signal_class",
    "economic_role", "freshness", "admission_eligible", "compact_payload",
    "recorded_compact_payload", "sections_verbatim", "chunk_header",
})


#: A section's provenance entry may declare this `kind` instead of the raw
#: source/fetched_on/fetch triple (see module docstring, "PM practice-day
#: extension"). It must then carry `model`, `route`, `timestamp` and
#: `source_fixture`.
FRESH_ANALYST_OUTPUT_KIND = "fresh_analyst_output"

#: A section's provenance entry may instead declare this `kind` for a
#: labelled SYNTHETIC starting account state (cash + positions) that is
#: neither a public fact nor an analyst-seat output — a real account is
#: desk state, which this policy refuses. It must carry a non-empty `label`
#: naming it synthetic; no source/fetch is meaningful for it, so none is
#: required. It is still walked by the desk-source and forbidden-key checks
#: like everything else, and it may not contain a price, quote or news item
#: (that would be an invented market fact, not an account state).
SYNTHETIC_ACCOUNT_KIND = "synthetic_account_state"


class FixtureQuarantined(RuntimeError):
    """A scenario was asked to run on a fixture that breaks the policy."""


@dataclass
class FixtureVerdict:
    path: Path
    problems: list[str] = field(default_factory=list)

    @property
    def admissible(self) -> bool:
        return not self.problems

    def reason(self) -> str:
        return f"{self.path.name} is QUARANTINED: " + "; ".join(self.problems)


def desk_data_trusted_from() -> date | None:
    return date.fromisoformat(DESK_DATA_TRUSTED_FROM) if DESK_DATA_TRUSTED_FROM else None


def _walk(node, path: str = ""):
    """Yield (json_path, key_or_None, value) for every node."""
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            yield child, key, value
            yield from _walk(value, child)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            child = f"{path}[{i}]"
            yield child, None, value
            yield from _walk(value, child)


def _desk_rows_trusted(provenance: dict) -> tuple[bool, str]:
    cutoff = desk_data_trusted_from()
    if cutoff is None:
        return False, "desk-recorded data is refused while DESK_DATA_TRUSTED_FROM is unset"
    rows = provenance.get("desk_rows") or []
    if not rows:
        return False, "names desk data but lists no dated `_provenance.desk_rows`"
    for row in rows:
        try:
            dated = date.fromisoformat(str(row.get("dated"))[:10])
        except (TypeError, ValueError, AttributeError):
            return False, f"desk row without a readable date: {row!r}"
        if dated < cutoff:
            return False, f"desk row dated {dated} is before DESK_DATA_TRUSTED_FROM {cutoff}"
    return True, ""


def check_fixture(path: Path) -> FixtureVerdict:
    """Every rule, applied to one fixture manifest (a `.json` file)."""
    verdict = FixtureVerdict(path=path)
    problems = verdict.problems
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        problems.append(f"unreadable manifest ({type(exc).__name__})")
        return verdict
    if not isinstance(data, dict):
        problems.append("manifest is not a JSON object")
        return verdict

    provenance = data.get("_provenance")
    if not isinstance(provenance, dict):
        problems.append("no `_provenance` block")
        provenance = {}
    sections = provenance.get("sections")
    if not isinstance(sections, dict):
        problems.append("`_provenance.sections` missing: no external source is named per data section")
        sections = {}

    # (a) every data section names an external source and a fetch date —
    # OR declares itself fresh analyst output over an already-checked
    # raw-facts fixture (see FRESH_ANALYST_OUTPUT_KIND).
    fresh_analyst_sections: set[str] = set()
    for key in data:
        if key.startswith("_"):
            continue
        sec = sections.get(key)
        if not isinstance(sec, dict):
            problems.append(f"data section `{key}` has no provenance entry")
            continue
        if sec.get("kind") == SYNTHETIC_ACCOUNT_KIND:
            if not str(sec.get("label") or "").strip():
                problems.append(f"data section `{key}` (synthetic_account_state) names no `label`")
            continue
        if sec.get("kind") == FRESH_ANALYST_OUTPUT_KIND:
            fresh_analyst_sections.add(key)
            if not str(sec.get("model") or "").strip():
                problems.append(f"data section `{key}` (fresh_analyst_output) names no `model`")
            if not str(sec.get("route") or "").strip():
                problems.append(f"data section `{key}` (fresh_analyst_output) names no `route`")
            try:
                date.fromisoformat(str(sec.get("timestamp"))[:10])
            except (TypeError, ValueError):
                problems.append(f"data section `{key}` (fresh_analyst_output) has no ISO `timestamp`")
            source_fixture = str(sec.get("source_fixture") or "").strip()
            if not source_fixture:
                problems.append(
                    f"data section `{key}` (fresh_analyst_output) names no `source_fixture`"
                )
            else:
                source_path = FIXTURES_DIR / source_fixture
                if not source_path.exists():
                    problems.append(
                        f"data section `{key}`'s source_fixture `{source_fixture}` does not exist"
                    )
                elif source_path.resolve() != path.resolve():
                    source_verdict = check_fixture(source_path)
                    if not source_verdict.admissible:
                        problems.append(
                            f"data section `{key}`'s source_fixture `{source_fixture}` is itself "
                            f"QUARANTINED: {'; '.join(source_verdict.problems)}"
                        )
            continue
        source = str(sec.get("source") or "")
        if not source.startswith(EXTERNAL_SOURCES):
            problems.append(f"data section `{key}` names no external source (got {source!r})")
        try:
            date.fromisoformat(str(sec.get("fetched_on")))
        except (TypeError, ValueError):
            problems.append(f"data section `{key}` has no ISO `fetched_on` date")
        if not str(sec.get("fetch") or "").strip():
            problems.append(f"data section `{key}` does not name the fetch function or URL")

    # (b) nothing points at the desk, unless the trust cut-off admits it.
    desk_hits: list[str] = []
    for json_path, key, value in _walk(data):
        if isinstance(value, str):
            low = value.lower()
            marker = next((m for m in DESK_SOURCE_MARKERS if m in low), None)
            if marker or DESK_RUN_ID_RE.search(value):
                desk_hits.append(f"{json_path} ({marker or DESK_RUN_ID_RE.search(value).group(0)})")
    if desk_hits:
        trusted, why = _desk_rows_trusted(provenance)
        if not trusted:
            problems.append(
                f"names desk records as a source — {why}: " + ", ".join(desk_hits[:5])
                + (f" (+{len(desk_hits) - 5} more)" if len(desk_hits) > 5 else "")
            )

    # (c) no agent-output or old-code-derived fields — except inside a
    # section declared `fresh_analyst_output` above, where being agent
    # output is the point.
    def _under_fresh_section(json_path: str) -> bool:
        top = json_path.split(".", 1)[0].split("[", 1)[0]
        return top in fresh_analyst_sections

    for json_path, key, _value in _walk(data):
        if key is None or json_path.startswith("_provenance"):
            continue
        if _under_fresh_section(json_path):
            continue
        if key in AGENT_OUTPUT_KEYS:
            problems.append(f"agent-output field `{json_path}`")
        elif key in OLD_CODE_DERIVED_KEYS:
            problems.append(f"derived field `{json_path}` (must be recomputed by today's code)")
    if len(problems) > 40:
        del problems[40:]
        problems.append("… (truncated)")

    # Blobs: raw bytes stored beside the manifest, pinned by hash.
    for name, meta in (data.get("_blobs") or {}).items():
        blob = path.parent / name
        if not blob.exists():
            problems.append(f"blob `{name}` is missing")
            continue
        digest = hashlib.sha256(blob.read_bytes()).hexdigest()
        if digest != (meta or {}).get("sha256"):
            problems.append(f"blob `{name}` does not match its recorded sha256")
        source = str((meta or {}).get("source") or "")
        if not source.startswith(EXTERNAL_SOURCES):
            problems.append(f"blob `{name}` names no external source (got {source!r})")
    return verdict


def check_all(directory: Path = FIXTURES_DIR) -> dict[str, FixtureVerdict]:
    """Every manifest, plus a verdict for any file no admissible manifest owns."""
    verdicts: dict[str, FixtureVerdict] = {}
    owned: set[str] = set()
    for manifest in sorted(directory.glob("*.json")):
        verdict = check_fixture(manifest)
        verdicts[manifest.name] = verdict
        try:
            owned.update((json.loads(manifest.read_text()).get("_blobs") or {}).keys())
        except (OSError, ValueError, AttributeError):
            pass
    for other in sorted(directory.iterdir()):
        if other.suffix == ".json" or other.name in owned or other.name.startswith("."):
            continue
        stray = FixtureVerdict(path=other)
        stray.problems.append("file is not a manifest and no manifest pins it as a blob")
        verdicts[other.name] = stray
    return verdicts


def assert_admissible(name: str, directory: Path = FIXTURES_DIR) -> None:
    verdict = check_fixture(directory / name)
    if not verdict.admissible:
        raise FixtureQuarantined(verdict.reason())


def load_blob(manifest_name: str, blob_name: str, directory: Path = FIXTURES_DIR) -> bytes:
    """A pinned blob's bytes, after re-checking the manifest that owns it."""
    import gzip

    assert_admissible(manifest_name, directory)
    raw = (directory / blob_name).read_bytes()
    return gzip.decompress(raw) if blob_name.endswith(".gz") else raw
