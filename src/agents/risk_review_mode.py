"""Which review the AI Risk Manager seat is sitting in, and what it can see there.

One seat, two callers. `pipeline_stages.RiskStage` puts the MORNING PLAN in
front of it — the Portfolio Manager's new orders, with every specialist block
the research stage produced. `pipeline._risk_review_exits` puts the POSITION
REVIEWER's SELL / REDUCE / COVER decisions in front of the same seat, on the
midday and close loops, where most of those blocks were never produced by
anything.

Until 2026-09-13 the exit path reused the morning renderer unchanged, and the
renderer's honest-about-absence machinery — built for the morning path, where
an absent field really does mean an analyst skipped a mandatory step — turned
into a series of false statements on the exit path:

- `ReasoningChain.continuity_check` and `.premortem_check` are mandatory in
  the PORTFOLIO MANAGER's prompt. The position reviewer's schema has no such
  fields and never did, so `_risk_review_exits` could not set them and the
  renderer stamped both with "[MISSING — ... Treat the audit step as NOT
  PERFORMED]". `config/prompts/risk_manager.md` then tells the seat that a
  missing audit step means "do not extend the plan the benefit of the doubt
  elsewhere". On every exit review ever run, the seat was told the analyst had
  skipped both mandatory red-team steps. It had not. The steps do not exist on
  that path.
- The checklist tells the seat to "check the News and Tech blocks yourself for
  the trigger PM claims". No TechAnalyst call runs on the midday or close
  loop, so the Tech block is absent by construction — the seat was asked to
  verify a claim against a block that cannot exist.

**Why the direction of that bias matters.** A veto on the morning path stops a
PURCHASE, and not buying costs nothing. A veto on THIS path stops a SALE: a
position whose thesis has broken stays on the book overnight with only the
broker stop behind it. Every input above pushed the seat toward refusing, and
`docs/OUTCOME.md` records under-trading, not over-trading, as this desk's
measured failure. The bias ran in the dangerous direction.

The rule this module exists to hold, and the one thing to preserve if it is
ever rewritten:

    **Never tell the seat a check was skipped when that check does not apply
    to the path it is on, and never tell it to verify against a block that is
    absent by construction. An absence that is nobody's fault must be
    described as unavailable-by-design, never as an analyst's omission.**

The fix is NOT to invent placeholder content for the absent fields. A
fabricated "n/a" that reads as a real answer is how the defect started — the
call site was already writing `or "n/a"` into six chain fields.
"""

MORNING_PLAN = "morning_plan"
EXIT_REVIEW = "exit_review"

#: Every one of `ReasoningChain`'s seven core fields carries `min_length=1`,
#: so the exit call site CANNOT leave a borrowed slot empty — that constraint
#: is why the original code wrote `or "n/a"` into six of them. "n/a" is the
#: problem: it is three characters that read to the seat as a substantive
#: answer to a question nobody answered. These two constants say what is
#: actually true instead, and they are the ONLY values the exit path is
#: allowed to substitute.
NOT_AUTHORED = (
    "[NOT AUTHORED — the position reviewer's own schema makes this field "
    "mandatory, so an empty one means its output degraded, NOT that the "
    "reviewer skipped a step. Treat this step as unavailable to you and say "
    "so; it is not an omission to hold against the exit.]"
)

#: A PM-schema field with no counterpart on the exit path. Never rendered to
#: the seat (it is absent from `_CHAIN_ROWS[EXIT_REVIEW]`); it exists only to
#: satisfy `min_length=1` and to be unmistakable in a stored agent log.
UNUSED_SLOT = "[unused on the exit-review path — PM-schema field, no counterpart]"

VALID_MODES = (MORNING_PLAN, EXIT_REVIEW)


def normalize(mode) -> str:
    """Any unrecognised value falls back to the morning path.

    Deliberately permissive rather than raising: an unknown string arriving
    here should degrade to the strictest, most-complete rendering, not abort a
    risk review.
    """
    text = str(mode or MORNING_PLAN).strip().lower()
    return text if text in VALID_MODES else MORNING_PLAN


def is_exit_review(mode) -> bool:
    return normalize(mode) == EXIT_REVIEW


#: Header block prepended to the exit-review message. It has to do three jobs:
#: say which review this is, say what the seat can and cannot see on it, and
#: say what the deterministic layer has already settled so the seat is not
#: asked to re-litigate it. All three are statements of fact about the code
#: path — none of them is a new policy or a new threshold.
_EXIT_REVIEW_HEADER = """## Review Mode: EXIT REVIEW — this is NOT the morning plan review

You are reviewing the **position reviewer's decisions to CLOSE or REDUCE
positions the desk already holds**, on the midday or close loop. Read the
following before anything else; several standing instructions in your prompt
were written for the morning plan and do not apply here.

**What a veto does here.** On the morning plan, refusing a trade means not
buying, which costs nothing. Here, refusing means the position STAYS ON THE
BOOK — overnight, with only the broker stop behind it. A wrongly-refused exit
is a live loss, not a missed opportunity. Refuse an exit only when you can
name what is wrong with THIS exit, from the data you were actually given.

**These sections of your standing prompt do not apply on this path:**

- **Checklist 1 (Reasoning Chain Audit) reads a DIFFERENT chain.** The chain
  below is the POSITION REVIEWER's, not the Portfolio Manager's. It has its
  own fields and it has NO `continuity_check` and NO `premortem_check` — those
  two exist only in the Portfolio Manager's schema. Their absence here is not
  a skipped audit step, there is no `pm_audit_step_missing` advisory to
  address, and the "a missing audit step is a finding" guardrail is not in
  play. Do not say the red-team step did not happen, and do not withhold the
  benefit of the doubt on that basis.
- **Checklist 2 and the whole Risk/Reward section do not apply.** These rows
  are exits. `Entry`, `Stop` and `Target` render as `$0.0` because an exit has
  no entry geometry to price — that is a structural zero, not a real level and
  not a data error. There is no reward:risk figure to audit and you must not
  compute one.
- **Checklist 5 (Sizing Sanity) and `scale_all_buys` do not apply.** There are
  no BUYs or SHORTs in this plan. `allocation_pct` on these rows is the % OF
  THE EXISTING POSITION to close (100 = full close), never a portfolio weight
  — never compare it to a position cap, and never set it to 0 (0 = silently
  cancel the exit).
- **Checklist 8 (Holding-discipline compliance) is not yours on this path.**
  Do not re-derive it from the reasoning text. See what already ran, below.

**What the deterministic layer decides about these exits, without you.** Every
exit below is independently checked in Python, in the same session, before any
order can reach the broker:

- the **named-trigger gate** — an exit whose reason names no recognised
  trigger (thesis invalidation, adverse news, earnings, regime shift, sector
  shock, stop hit) is dropped; price action and soft flags do not qualify;
- the **noise band** — an adverse move smaller than this position's own
  volatility-scaled band is dropped, unless the reason cites external
  information;
- the **metric-contradiction veto** — an exit claiming deterioration while the
  desk's own recorded numbers for that position improved is dropped;
- **`holding_discipline_claim_check`** — a named trigger that the desk's own
  data shows is PROVABLY FALSE drops the exit. (An unverifiable claim is
  allowed through on purpose: absence of proof is not proof, and trapping the
  desk in a losing position is the worse failure.)

Those four are the real gate and they do not need your help. **You are a
second opinion on exit QUALITY**, and the failure posture on this path is
FAIL OPEN — an errored or unparseable verdict lets the exits through.

**What you can and cannot see on this path** is stated block by block below.
Where a block says it is unavailable on this path, that is a fact about the
code path, not an omission by any analyst, and it is not on its own a reason
to refuse anything. Say plainly in `reasoning_chain` which questions you could
not answer, rather than answering them from something you were not shown.
"""


def mode_header(mode) -> str:
    """The block prepended to the user message, or "" on the morning path."""
    return _EXIT_REVIEW_HEADER if is_exit_review(mode) else ""


#: Section title + preamble for the reasoning-chain block. On the exit path
#: the chain is authored by the position reviewer, so calling it "PM's" would
#: point the seat's audit at the wrong author.
def reasoning_chain_heading(mode) -> tuple[str, str]:
    """Return `(title_line, preamble)` for the reasoning-chain section."""
    if is_exit_review(mode):
        return (
            "## Position Reviewer Reasoning Chain — the reviewer's CLAIMS "
            "about its own exits, not evidence",
            "Audit these against the blocks above. Where a claim cites a "
            "number,\ncheck it against the Account / Positions / Portfolio "
            "Risk data you were\ngiven; where you cannot check it, say so "
            "rather than accepting it.\nThis chain is the POSITION REVIEWER's "
            "and has no continuity or pre-mortem\nfield — those belong to the "
            "Portfolio Manager's schema and are not part of\nthis path. Their "
            "absence is not a skipped audit step.",
        )
    return (
        "## PM Reasoning Chain — PM's CLAIMS about its own plan, not evidence",
        "Audit these against the blocks above. Where a claim cites a number,\n"
        "check it against the Account / Positions / Tech / Macro data you were\n"
        "given; where you cannot check it, say so rather than accepting it.",
    )


#: `(label, ReasoningChain attribute, is_a_PM_mandatory_step)` per mode.
#:
#: MORNING_PLAN is the nine-field Portfolio Manager chain, unchanged — the last
#: two carry the `[MISSING — ... NOT PERFORMED]` banner, which is correct
#: there: PM's own prompt makes them mandatory while the schema defaults them
#: to "", so an empty one really is a skipped step.
#:
#: EXIT_REVIEW is the POSITION REVIEWER's chain, carried in the same
#: `ReasoningChain` container by `pipeline._risk_review_exits` because that is
#: the container `RiskManagerAgent` reads. The labels below are the reviewer's
#: OWN field names, not PM's: rendering `execution_rationale` under the label
#: "Sizing logic" asked the seat to audit sizing against a sentence about
#: execution. `continuity_check` and `premortem_check` are absent entirely —
#: the reviewer's schema has no such fields, so a NOT-PERFORMED banner there
#: is a false statement about an analyst who was never asked for them. So are
#: `news_check`: the call site had nothing real to put in it and was writing a
#: cross-reference string there, which reads as an answer and is not one. The
#: `earnings_check` SLOT is reused to carry the reviewer's
#: `thesis_progress_check` — the container field name is arbitrary here (it is
#: PM's schema, borrowed), and what the seat actually reads is the LABEL,
#: which names the reviewer's real field. Nothing about earnings is implied.
_CHAIN_ROWS = {
    MORNING_PLAN: (
        ("Macro filter", "macro_filter", False),
        ("News check", "news_check", False),
        ("Earnings check", "earnings_check", False),
        ("Signal conflicts", "signal_conflicts", False),
        ("Sizing logic", "sizing_logic", False),
        ("Portfolio balance", "portfolio_balance", False),
        ("Cash target", "cash_target", False),
        ("Continuity check", "continuity_check", True),
        ("Pre-mortem check", "premortem_check", True),
    ),
    EXIT_REVIEW: (
        ("Macro continuity check", "macro_filter", False),
        ("Thesis progress check", "earnings_check", False),
        ("Thesis integrity check", "signal_conflicts", False),
        ("Execution rationale", "sizing_logic", False),
        ("Winners discipline check", "portfolio_balance", False),
        ("Session disposition check", "cash_target", False),
    ),
}


def chain_rows(mode):
    """The reasoning-chain rows to render, in order, for this review mode."""
    return _CHAIN_ROWS[normalize(mode)]


#: Absence wording, per block, per mode. The morning strings are the existing
#: renderer text, unchanged. The exit strings say WHY the block is absent and
#: what the seat should therefore do — never "the analyst did not provide it".
_ABSENT = {
    "tech": {
        MORNING_PLAN: "## Tech Analyst Signals\n(not provided)",
        EXIT_REVIEW: (
            "## Tech Analyst Signals\n"
            "UNAVAILABLE BY DESIGN ON THIS PATH — no TechAnalyst call runs on "
            "the midday or close review loop, so there are no technical "
            "signals for these positions this session. Nobody skipped a step. "
            "Your standing checklist tells you to 'check the News and Tech "
            "blocks yourself for the trigger the plan claims': the Tech half "
            "of that instruction cannot be carried out here. Do NOT infer a "
            "rating, do NOT treat the silence as a bearish or a bullish "
            "signal, and do NOT refuse an exit for lacking Tech confirmation. "
            "Say in `signal_fidelity` that no Tech signal was available on "
            "this path."
        ),
    },
    "news": {
        MORNING_PLAN: "## News Intelligence\n(not provided)\n",
        EXIT_REVIEW: (
            "## News Intelligence\n"
            "NOT AVAILABLE THIS RUN — the midday/close loop does fetch news "
            "before the position review, so this block is normally present; "
            "its absence here means that fetch returned nothing or failed, "
            "which is recorded in the run's coverage log. Treat today's news "
            "as UNKNOWN rather than as quiet, and say so in "
            "`signal_fidelity`. An unverifiable news claim in the reasoning "
            "below is not, on its own, grounds to refuse an exit — the "
            "deterministic claim check has already dropped every exit whose "
            "named trigger the desk's own data shows to be false.\n"
        ),
    },
}


def absent_block(block: str, mode) -> str:
    """Rendering for a block that is not present, honest about why."""
    return _ABSENT[block][normalize(mode)]
