"""The technical seat must be told the history depth the code actually fetches.

THE DEFECT THIS CLOSES (board item 168). `config/prompts/tech_analyst.md` told
the model "indicators are computed from ~120 days of history upstream" while
every site that assembles this seat's `symbols_data` fetches
`config.trading.lookback_days` calendar days — 1800 since 2026-08-27. The seat
was told it judges trend off about four months of price action when it is
really about five years of it. Same sentence, same halt-capable seat and the
same root cause as board item 98's 20-vs-40 bar count: the number had a second
home in prose, and prose is not checked by anything.

WHY THIS IS A SEPARATE FILE FROM `test_tech_analyst_bar_count.py`. Two numbers,
two mechanisms. The bar count is a CODE CONSTANT (`_BARS_PER_SYMBOL`); the
history depth is an OPERATOR-TUNABLE SETTING that can be changed in
`config/settings.yaml` without a code review, which is the more dangerous of
the two and the reason the check below renders against values the repo has
never used rather than asserting today's 1800.

WHAT THIS FILE REFUSES TO DO. It never asserts the sheet says "1800", and it
never asserts it says "1285". Both would pass today and rot on the next
settings edit — the identical shape of defect, one number later. Everything
below is either a reconstruction from the live setting or a residue scan for a
second home.

THE DELIBERATE-BREAKAGE LIST these checks were built against is in the PR.
Units swapped (calendar stated as sessions), off-by-one on the weekday
arithmetic, a stale copy of the old number left in the sheet's summary line, a
literal typed back over the placeholder, the placeholder rewritten with inner
spaces, the pipeline no longer passing the setting through, a divergence
between `config/settings.yaml` and the `AppConfig` that parses it, and the
history claim moved into the Python-assembled user message where no prompt-file
scan can see it.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from src.agents.tech_analyst import (
    _HISTORY_PLACEHOLDER,
    _LONGEST_INDICATOR_PLACEHOLDER_RE,
    _HISTORY_PLACEHOLDER_RE,
    _HISTORY_UNAVAILABLE,
    _SETTINGS_PATH,
    PROMPT_PATH,
    TechAnalystAgent,
    format_history_window,
    render_tech_placeholders,
    settings_lookback_days,
    weekday_sessions_in,
)
from src.config import TradingConfig
from src.models import OHLCV, TechnicalIndicators

#: Spelled numbers a future editor might write instead of typing digits — the
#: exact evasion board item 98's test file had to learn about.
_SPELLED = (
    r"one|two|three|four|five|six|seven|eight|nine|ten|twelve|twenty|thirty"
    r"|forty|fifty|sixty|ninety|hundred|thousand"
)
_N = r"(?:\d[\d,]*|" + _SPELLED + r")"
_QUALIFIER = r"(?:calendar\s+|trading\s+|weekday\s+|market\s+|business\s+)?"
_UNIT = r"(?:day|month|year|session|bar|candle)s?"

#: The words a depth claim gets written around. The first draft anchored on
#: "history" alone and adversary review broke it by swapping one noun
#: ("1800 calendar days of DATA", "a 5-year WINDOW", "sessions of RECORD",
#: "the upstream FETCH is 1800 calendar days").
_DEPTH = (
    r"(?:histor\w*|price\s+action|lookback|look-?back|back\s*fill|"
    r"data|record|series|depth|window|upstream|fetch\w*)"
)

#: A claim about how much price history sits behind the seat's inputs.
#:
#: SHAPE, NOT PROXIMITY, and that distinction is the whole check. The second
#: draft of this file matched a number near a depth word within 25 characters.
#: It still missed two rewordings, and adversary review then broke it the other
#: way: five of seven ordinary technical-analysis sentences FAILED it, because
#: "window", "data", "record", "series" and "price action" are everyday words
#: on this sheet ("a rating more than 5 days old is stale data"). A check that
#: fires on innocent edits gets an exemption added, and an exemption is how a
#: check gets hollowed out — so this matches the GRAMMAR of a depth claim
#: instead: <n> <unit> OF <depth>, an <n>-<unit> <depth>, <n> <unit> upstream,
#: <depth>: <n> <unit>, or <depth> covers/is/reaches <n> <unit>. Measured
#: against thirteen rewordings and ten innocent sentences: thirteen caught,
#: zero false positives.
_HISTORY_CLAIM_RE = re.compile(
    r"(?:" + _N + r"[\s~-]*" + _QUALIFIER + _UNIT
    + r"\s+(?:of|worth\s+of)\s+(?:\w+\s+){0,2}?" + _DEPTH
    + r"|" + _N + r"[\s~]*-\s*" + _UNIT[:-2] + r"\s+" + _DEPTH
    + r"|" + _N + r"[\s~-]*" + _QUALIFIER + _UNIT + r"\s+(?:upstream|of\s+it|back)\b"
    + r"|" + _DEPTH + r"\s*[:=]\s*" + _N + r"[\s~-]*" + _QUALIFIER + _UNIT
    + r"|" + _DEPTH + r"\s+(?:\w+\s+){0,2}?"
    + r"(?:of|is|was|covers?|reach\w*|goes?\s+back|spans?)\s+"
    + r"(?:\w+\s+){0,2}?" + _N + r"[\s~-]*" + _QUALIFIER + _UNIT + r")",
    re.IGNORECASE,
)

#: Rewordings the scan must catch, and ordinary sentences it must NOT. Both
#: lists are asserted below, because a residue scan that cannot be wrong about
#: innocent text is the version that survives its first false alarm.
_MUST_CATCH = (
    "indicators are computed from ~120 days of history upstream",
    "indicators are computed upstream from ~120 days of data",
    "Indicators rest on 1800 calendar days of price action",
    "built off a 5-year window",
    "Trend is judged over roughly 1250 sessions of record",
    "a 120-day lookback",
    "history of 1800 calendar days",
    "the series covers 1800 calendar days",
    "depth: 1800 calendar days",
    "five years of price action",
    "levels use 1800 days of series depth",
    "indicators use 1800 calendar days upstream",
    "The upstream fetch is 1800 calendar days.",
)
_MUST_NOT_CATCH = (
    "A rating more than 5 days old is stale data — re-read the chart.",
    "Earnings within 3 days — the price action into the print is unreliable.",
    "Hold for the 5-15 day swing horizon; the series of higher lows must stay intact.",
    "A gap older than 10 sessions has usually lost its record as a magnet.",
    "Do not chase more than 3 days after the breakout window opened.",
    "Require a 2 day closing confirmation before calling a level broken.",
    "Signal age over 7 days means the prior rating is no longer current data.",
    "the most recent 40 daily bars",
    "Same rating, age 8+ days without progress to target",
    "expected_horizon_sessions of 5-15 trading days",
)

#: The sheet's data-sufficiency floor — "return neutral below this much
#: history" — is a RULE the seat applies, not a claim about what it was sent.
#: Board item 98 said to leave it alone and this file does too; it is removed
#: before the residue scan so it cannot masquerade as a second home for the
#: history depth. Shared spelling with `test_tech_analyst_bar_count.py` on
#: purpose: if the sheet reworded it, both files fail and a human decides.
_DATA_SUFFICIENCY_PHRASE = "< 20 bars of history"

#: Depths the repo has never configured, spanning both sides of today's 1800
#: and of the 320 it used before 2026-08-27. All far enough above zero that
#: the weekday arithmetic is meaningful.
_PRETEND_LOOKBACKS = (37, 411, 999, 2555, 5000)


def _prompt_source() -> str:
    return PROMPT_PATH.read_text()


def _without_exemptions(text: str) -> str:
    stripped = text.replace(_DATA_SUFFICIENCY_PHRASE, "<data-sufficiency-floor>")
    assert _DATA_SUFFICIENCY_PHRASE not in stripped
    return stripped


def _history_claims(text: str) -> list[str]:
    return [m.group(0) for m in _HISTORY_CLAIM_RE.finditer(_without_exemptions(text))]


def _bars(count: int) -> list[OHLCV]:
    start = date(2026, 1, 5)
    return [
        OHLCV(
            date=start + timedelta(days=i),
            open=float(i), high=float(i), low=float(i), close=float(i),
            volume=1_000_000,
        )
        for i in range(count)
    ]


def _indicators() -> TechnicalIndicators:
    return TechnicalIndicators(
        symbol="SPY", ma_20=505.0, ma_50=498.0, ma_200=450.0, rsi_14=58.0,
        macd=1.0, macd_signal=0.5, macd_hist=0.5,
        bb_upper=520.0, bb_middle=505.0, bb_lower=490.0,
        atr_14=8.5, volume_change_pct=15.0,
    )


def _agent(lookback_days: int | None = None) -> TechAnalystAgent:
    with patch("anthropic.Anthropic"):
        return TechAnalystAgent(
            api_key="test", model="claude-sonnet-4-6-20250514",
            lookback_days=lookback_days,
        )


# --------------------------------------------------------------------------
# The sheet: one home for the number
# --------------------------------------------------------------------------

def test_data_sufficiency_rule_is_still_present_and_untouched() -> None:
    """The exemption above must describe something that actually exists."""
    assert _DATA_SUFFICIENCY_PHRASE in _prompt_source(), (
        f"The technical seat's data-sufficiency rule {_DATA_SUFFICIENCY_PHRASE!r} "
        f"is gone from {PROMPT_PATH.name}. That rule is SEPARATE from the "
        f"history depth this file checks. If it was deliberately reworded, "
        f"update _DATA_SUFFICIENCY_PHRASE in BOTH this file and "
        f"tests/test_tech_analyst_bar_count.py and say in the PR why."
    )


def test_prompt_source_carries_no_hand_typed_history_depth() -> None:
    """No literal history depth may survive anywhere in the sheet.

    This is the check that would have caught the original "~120 days of
    history" the day `trading.lookback_days` moved off 120, and the one that
    catches a stale copy left behind in the summary line at the bottom of the
    sheet after the main sentence is fixed.
    """
    literals = _history_claims(_prompt_source())
    assert not literals, (
        f"{PROMPT_PATH.name} states a hand-typed history depth {literals} "
        f"instead of {_HISTORY_PLACEHOLDER}. The depth the seat's indicators "
        f"are built from has ONE home — `trading.lookback_days` in "
        f"config/settings.yaml — and the sheet must render it, not restate it. "
        f"This is board item 168 coming back; the sheet said ~120 days while "
        f"the code fetched 1800."
    )


def test_input_section_tells_the_seat_its_history_depth() -> None:
    """The brief must be in the section that describes the seat's inputs.

    Board item 168's whole subject is the sentence under `## Input`. Deleting
    it while a mention survives in the one-line summary at the bottom of the
    sheet would pass a whole-file presence check and still leave the seat
    unbriefed where it reads about its data — so the section is checked, not
    just the file.
    """
    source = _prompt_source()
    start = source.index("\n## Input\n")
    section = source[start:source.index("\n## ", start + 5)]
    assert _HISTORY_PLACEHOLDER_RE.search(section), (
        f"the `## Input` section of {PROMPT_PATH.name} no longer states how "
        f"much price history the seat's indicators and structural levels are "
        f"computed from. Restore {_HISTORY_PLACEHOLDER} there."
    )


def test_prompt_source_actually_uses_the_placeholder() -> None:
    """Deleting every mention would pass the residue scan and brief nobody."""
    assert _HISTORY_PLACEHOLDER_RE.search(_prompt_source()), (
        f"{PROMPT_PATH.name} no longer tells the technical seat how much price "
        f"history its indicators and structural levels are computed from. If "
        f"that claim was deliberately dropped, record the reasoning against "
        f"board item 168 in docs/WORK.md — otherwise restore "
        f"{_HISTORY_PLACEHOLDER}."
    )


# --------------------------------------------------------------------------
# Rendering: reconstructed from the setting, not from today's value
# --------------------------------------------------------------------------

@pytest.mark.parametrize("pretend", _PRETEND_LOOKBACKS)
def test_rendered_sheet_reconstructs_exactly_from_the_setting(pretend: int) -> None:
    """Render the real sheet against depths the repo has never used.

    Every claim the sheet makes about history must be the one string
    `format_history_window` built from the setting. Strip that string and no
    history claim may remain — so a hard-coded depth fails here even when it
    is the value that happens to be right today.
    """
    rendered = render_tech_placeholders(_prompt_source(), lookback_days=pretend)
    phrase = format_history_window(pretend)
    assert phrase in rendered, (
        f"With `trading.lookback_days` = {pretend} the sheet does not carry "
        f"{phrase!r}. The history claim is not rendered from the setting."
    )
    residue = _history_claims(rendered.replace(phrase, "<history-window>"))
    assert not residue, (
        f"With `trading.lookback_days` = {pretend} the sheet still claims "
        f"{residue}. That is a second home for the history depth, which is "
        f"exactly the defect board item 168 records."
    )


@pytest.mark.parametrize("pretend", _PRETEND_LOOKBACKS)
def test_rendered_numbers_are_the_setting_and_its_weekday_subset(pretend: int) -> None:
    """Units, and only the two legitimate numbers.

    Catches the unit swap (calendar days presented as sessions), an
    off-by-one in the weekday arithmetic, and any third number appearing in
    the phrase.
    """
    phrase = format_history_window(pretend)
    numbers = [int(n) for n in re.findall(r"\d+", phrase)]
    sessions = weekday_sessions_in(pretend)
    assert numbers == [pretend, sessions], (
        f"{phrase!r} states {numbers}; the setting is {pretend} calendar days "
        f"and its weekday subset is {sessions}."
    )
    assert "calendar day" in phrase and "weekday session" in phrase, (
        f"{phrase!r} does not label which figure is calendar days and which is "
        f"sessions. The unlabelled version of this sentence is what let the "
        f"seat read a calendar-day span as a bar count."
    )
    assert sessions < pretend, (
        "the weekday subset of a calendar span cannot be larger than the span"
    )
    assert phrase.index(str(pretend)) < phrase.index(str(sessions)) or sessions == pretend, (
        f"{phrase!r} states the two figures in an order that does not match "
        f"their labels — check the units have not been swapped."
    )


def test_weekday_arithmetic_matches_the_desks_own_session_counter() -> None:
    """An off-by-one guard, and nothing more than that — stated plainly.

    BOTH SIDES OF THIS COMPARISON COUNT MONDAY-TO-FRIDAY. `trading_sessions_held`
    says in its own docstring that it has no market-holiday calendar, because
    holiday detection on this desk needs a live broker connection. So this test
    cannot and does not show that the figure in the sheet matches the bars the
    provider really returns; it shows that the phase-free five-in-seven
    arithmetic agrees with the desk's existing weekday counter, which is what
    catches an off-by-one or a swapped unit.

    The residual overstatement is real and measured: `config/settings.yaml`
    records 1,254 bars actually returned for a 1800-day fetch against the 1285
    weekdays in that span — about 2.5%, all of it market holidays. The sheet
    says "fewer after market holidays" for that reason. Pinning the 1,254
    instead would be fitting a constant to one symbol on one date, which this
    desk's own doctrine forbids.
    """
    from src.trading_calendar import trading_sessions_held
    for span in (37, 411, 999, 1800, 2555):
        reals = [
            trading_sessions_held(
                date(2026, 9, 20) + timedelta(days=offset - span),
                date(2026, 9, 20) + timedelta(days=offset),
            )
            for offset in range(7)
        ]
        stated = weekday_sessions_in(span)
        # Averaged over all seven start weekdays the phase cancels exactly, so
        # this is a MEASURED equality and not a tolerance: an off-by-one in the
        # arithmetic moves `stated` off it immediately.
        assert stated == sum(reals) // 7, (
            f"a {span}-calendar-day window really averages "
            f"{sum(reals) / 7:.2f} weekday sessions across the seven possible "
            f"start weekdays, but the sheet would state {stated}. Check the "
            f"weekday arithmetic for an off-by-one."
        )
        for offset, real in enumerate(reals):
            end = date(2026, 9, 20) + timedelta(days=offset)
            # Which weekday a span starts on moves the real weekday count
            # within +/-2 of the phase-free 5-in-7 arithmetic, and that is the
            # whole error budget of this figure. Deliberately not chased any
            # tighter: making the number depend on today's date would make the
            # standing sheet change from one session to the next, and market
            # holidays (which this desk has no offline calendar for, per
            # src/trading_calendar.py) remove an order of magnitude more
            # sessions than this residue. Hence "about N, fewer after market
            # holidays" in the sheet rather than a precise count.
            assert abs(real - stated) <= 2, (
                f"a {span}-calendar-day window ending {end} really holds {real} "
                f"weekday sessions, but the sheet would state {stated} — "
                f"outside the +/-2 the start-weekday phase can explain. Check "
                f"the weekday arithmetic for an off-by-one or a unit swap."
            )


@pytest.mark.parametrize(
    "bad", [None, 0, -5, "1800", 1800.0, True, False, object()],
)
def test_unresolvable_depth_states_no_number_at_all(bad: object) -> None:
    """A depth that cannot be read must produce prose, never a guess.

    The alternative — a default typed into the code — reinstates the second
    home. And raising is not available on this seat: it is the only one
    allowed to halt the desk, so a raise at agent construction is a halt.
    """
    phrase = format_history_window(bad)
    assert phrase == _HISTORY_UNAVAILABLE
    assert not re.search(r"\d", phrase), (
        f"the fallback history phrase {phrase!r} contains a number. An "
        f"unresolvable setting must brief the seat with no depth claim rather "
        f"than with a stale or invented one."
    )


def test_rendered_sheet_leaves_no_template_syntax() -> None:
    """Any surviving braces — a renamed or re-spaced placeholder ships literally."""
    rendered = render_tech_placeholders(_prompt_source(), lookback_days=1234)
    leftovers = re.findall(r"\{\{[^}]*\}\}", rendered)
    assert not leftovers, (
        f"unsubstituted template syntax survived rendering: {leftovers}. The "
        f"technical seat would be shown this verbatim."
    )


def test_placeholder_tolerates_inner_whitespace() -> None:
    """`{{ tech.history_window }}` must substitute too.

    The bar-count fix shipped a bare `str.replace` first and it stopped
    substituting the moment someone wrote the placeholder with spaces — with
    every test still green. Same trap, same guard.
    """
    rendered = render_tech_placeholders(
        "history: {{ tech.history_window }}", lookback_days=411,
    )
    assert rendered == f"history: {format_history_window(411)}"


# --------------------------------------------------------------------------
# The live path: agent, pipeline, and the settings file itself
# --------------------------------------------------------------------------

def test_agent_system_prompt_states_the_depth_it_was_built_with() -> None:
    """The rendering is on the live path, not only in the helper above."""
    agent = _agent(lookback_days=999)
    prompt = agent.system_prompt
    assert format_history_window(999) in prompt, (
        "TechAnalystAgent.system_prompt does not state the `lookback_days` it "
        "was constructed with, so the coupling is not on the live path"
    )
    assert not re.search(r"\{\{[^}]*\}\}", prompt)


def test_agent_falls_back_to_the_settings_file() -> None:
    """A build with no depth passed still briefs the seat off the real setting."""
    agent = _agent(lookback_days=None)
    configured = settings_lookback_days()
    assert configured, "trading.lookback_days could not be read at all"
    assert format_history_window(configured) in agent.system_prompt


def test_pipeline_passes_the_configured_depth_to_the_seat() -> None:
    """The one construction site must hand over the live setting.

    Without this the agent silently falls back to re-reading the settings
    file, which is right for a script and wrong for production: the pipeline's
    `AppConfig` is what `market.get_ohlcv` is actually called with, and an
    environment that overrode it would brief the seat with the file's value.
    """
    source = (Path(__file__).resolve().parents[1] / "src" / "pipeline.py").read_text()
    start = source.index("self.tech_analyst = TechAnalystAgent(")
    block = source[start:source.index("self.portfolio_manager", start)]
    assert "lookback_days=config.trading.lookback_days" in block, (
        "src/pipeline.py builds TechAnalystAgent without passing "
        "`config.trading.lookback_days`, so the seat's brief and the OHLCV "
        "fetch no longer read the same number (board item 168)."
    )


def test_settings_file_and_parsed_config_agree() -> None:
    """The sheet's source and the fetch's source must be the same number.

    `settings_lookback_days` reads the YAML directly; the pipeline fetches
    with the value `AppConfig` parsed out of that same file. A default that
    silently wins over the file (src/pipeline.py documents that failure mode
    for other keys) would put a different depth in the sheet than in the
    fetch.
    """
    import yaml
    raw = yaml.safe_load(_SETTINGS_PATH.read_text())["trading"]
    parsed = TradingConfig(**raw).lookback_days
    assert settings_lookback_days() == parsed, (
        f"config/settings.yaml yields {settings_lookback_days()} but AppConfig "
        f"parses {parsed}. The seat would be briefed with one and fetched with "
        f"the other."
    )


def test_user_message_states_no_history_depth() -> None:
    """The assembled user message must not become the second home either.

    `docs/OUTCOME.md`: neither confirmed prompt-drift defect on this desk ever
    lived in a prompt FILE — both were Python-assembled strings, invisible to
    any file scan.
    """
    agent = _agent(lookback_days=1800)
    msg = agent.build_user_message(
        symbols_data=[
            {"symbol": "SPY", "bars": _bars(300), "indicators": _indicators()},
        ],
        prior_macro_regime="risk_off",
        prior_macro_outlook="cautious",
        intraday_context={"SPY": {"live_price": 300.0, "prev_close": 295.0}},
    )
    claims = _history_claims(msg)
    assert not claims, (
        f"The technical seat's USER MESSAGE claims a history depth {claims}. A "
        f"depth written into an assembled string is invisible to any "
        f"prompt-file scan — the exact class docs/OUTCOME.md records as the one "
        f"that bit this desk twice."
    )


# --------------------------------------------------------------------------
# The two blocks the sheet used to send without explaining (board item 168)
# --------------------------------------------------------------------------

#: Each block the code really sends, and a phrase from the sheet that briefs
#: the seat on how to weigh it. Both halves are asserted: the block must still
#: be assembled, and the sheet must still explain it. Drop either and the seat
#: is back to receiving data it was never told what to do with.
_EXPLAINED_BLOCKS = (
    ("CURRENT SESSION (TODAY, INCOMPLETE", "## Today's session"),
    ("Macro Context", "## Macro context"),
)


#: The kwarg each block arrives on, and every site that assembles a technical
#: batch. Adversary review of this change caught the block test proving only
#: that the renderer renders when handed data: deleting `intraday_context=`
#: from all three sites left every test green while the seat stopped receiving
#: the block the sheet promises to explain. Same shape as board item 98's first
#: draft, which passed against a slice taking the OLDEST bars.
_BLOCK_KWARGS = ("intraday_context=", "prior_macro_regime=", "prior_macro_outlook=")
_ASSEMBLY_SITES = ("src/pipeline.py", "src/pipeline_stages.py")


def _analyze_batch_calls() -> list[str]:
    """The text of every `analyze_batch(...)` call in the assembly sites.

    Scoped to the call rather than counted over the file, because a union
    count over two whole files is restored by the kwarg name appearing in a
    comment or an unrelated helper — which would re-hide a deleted site.
    """
    repo = Path(__file__).resolve().parents[1]
    calls: list[str] = []
    for site in _ASSEMBLY_SITES:
        source = (repo / site).read_text()
        at = source.find("analyze_batch(")
        while at != -1:
            depth, i = 0, source.index("(", at)
            for j in range(i, len(source)):
                if source[j] == "(":
                    depth += 1
                elif source[j] == ")":
                    depth -= 1
                    if depth == 0:
                        calls.append(source[at:j + 1])
                        break
            at = source.find("analyze_batch(", at + 1)
    return calls


@pytest.mark.parametrize("kwarg", _BLOCK_KWARGS)
def test_the_blocks_are_still_passed_by_every_assembly_site(kwarg: str) -> None:
    """The blocks must really reach the seat, not merely render when handed."""
    calls = _analyze_batch_calls()
    assert len(calls) >= 3, (
        f"found {len(calls)} `analyze_batch(...)` call(s) across "
        f"{', '.join(_ASSEMBLY_SITES)}; there were three when board item 168 "
        f"was closed."
    )
    missing = [c.split("(")[0] for c in calls if kwarg not in c]
    assert not missing, (
        f"`{kwarg}` is absent from {len(missing)} of the {len(calls)} "
        f"`analyze_batch(...)` calls across {', '.join(_ASSEMBLY_SITES)}. The "
        f"seat is no longer sent a block {PROMPT_PATH.name} promises to "
        f"explain. If a site was deliberately dropped, say so in the PR and "
        f"check whether the sheet should still tell the seat to expect it."
    )


@pytest.mark.parametrize("marker,heading", _EXPLAINED_BLOCKS)
def test_every_sent_block_is_explained_in_the_sheet(marker: str, heading: str) -> None:
    agent = _agent(lookback_days=1800)
    msg = agent.build_user_message(
        symbols_data=[
            {"symbol": "SPY", "bars": _bars(300), "indicators": _indicators()},
        ],
        prior_macro_regime="risk_off",
        prior_macro_outlook="cautious",
        intraday_context={"SPY": {
            "live_price": 300.0, "prev_close": 295.0,
            "session_open": 296.0, "session_high": 301.0,
            "session_low": 295.5, "session_volume": 4_000_000,
        }},
    )
    assert marker in msg, (
        f"the technical seat is no longer sent the {marker!r} block, but "
        f"{PROMPT_PATH.name} still carries the {heading!r} section telling it "
        f"how to use one. Remove the section, or restore the block."
    )
    assert heading in _prompt_source(), (
        f"the technical seat is sent the {marker!r} block every run and "
        f"{PROMPT_PATH.name} no longer explains how to weigh it. Unexplained "
        f"input on the only seat allowed to halt the desk is board item 168."
    )


# --------------------------------------------------------------------------
# The third number the sheet states about its own inputs
# --------------------------------------------------------------------------

#: How deep the DEEPEST indicator reaches. A different question from how much
#: history is fetched and from how many bars are attached, and the adversary
#: review of this very change caught it being typed by hand inside the fix for
#: the other two. Three numbers, three renderings, no literals.
_PRETEND_INDICATOR_WINDOWS = (13, 77, 321)


def test_longest_indicator_window_is_rendered_not_typed() -> None:
    from src.data.technical import LONGEST_INDICATOR_WINDOW
    source = _prompt_source()
    assert _LONGEST_INDICATOR_PLACEHOLDER_RE.search(source), (
        f"{PROMPT_PATH.name} no longer renders the deepest indicator window "
        f"from `LONGEST_INDICATOR_WINDOW`. Telling the seat its indicators "
        f"reach a depth the code does not use is board item 168 in miniature."
    )
    rendered = render_tech_placeholders(source, lookback_days=1800)
    assert f"{LONGEST_INDICATOR_WINDOW} sessions" in rendered


@pytest.mark.parametrize("pretend", _PRETEND_INDICATOR_WINDOWS)
def test_longest_indicator_window_tracks_the_constant(pretend: int, monkeypatch) -> None:
    """Rendered against depths no indicator on this desk has ever used."""
    import src.data.technical as technical
    monkeypatch.setattr(technical, "LONGEST_INDICATOR_WINDOW", pretend)
    rendered = render_tech_placeholders(_prompt_source(), lookback_days=1800)
    stated = set(re.findall(
        r"(?:longest|deepest)(?:\s+of\s+them)?(?:\s+reach\w+)?\s+(\d+)\s+sessions", rendered,
    ))
    assert stated == {str(pretend)}, (
        f"with `LONGEST_INDICATOR_WINDOW` = {pretend} the sheet states "
        f"{sorted(stated)}. Some indicator-depth claim is hard-coded."
    )


# --------------------------------------------------------------------------
# The scan itself, in both directions
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", _MUST_CATCH)
def test_the_residue_scan_catches_every_known_rewording(text: str) -> None:
    assert _history_claims(text), (
        f"the residue scan does not see {text!r} as a depth claim. Every entry "
        f"in _MUST_CATCH is a rewording that adversary review walked past an "
        f"earlier draft of this scan."
    )


@pytest.mark.parametrize("text", _MUST_NOT_CATCH)
def test_the_residue_scan_leaves_ordinary_sentences_alone(text: str) -> None:
    """A check that fires on innocent edits is a check that gets exempted away.

    Every sentence here is the sort of thing this sheet says about signal age,
    holding horizon or earnings proximity. An earlier draft failed five of
    them, which would have turned CI red on an unrelated edit with the message
    "this is board item 168 coming back" — and the fix for that is always an
    exemption, which is how the scan stops scanning.
    """
    assert not _history_claims(text), (
        f"the residue scan reads {text!r} as a depth claim. It is not one — "
        f"tighten the grammar rather than exempting the phrase."
    )
