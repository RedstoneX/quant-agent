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

**What the record actually shows — do not overstate this.** The archive holds
exactly THREE exit-path risk reviews (rows 296, 319, 330). All three carry both
banners. All three APPROVED, with zero modifications and zero refusals: 8 of 8
exits allowed. The seat reasoned its way out of the trap every time — row 296:
"the missing `continuity_check` and `premortem_check` are a concern for PM's
internal discipline, but the plan itself is sound". So the claim that these
inputs made the seat refuse an exit is NOT supported: n=3 and the measured
refusal count is zero. Three reviews is also far too small to show the bias is
absent. Both statements are true and neither should be dropped.

**The harm the record DOES prove is to the audit trail.** Rows 319 and 330 both
wrote the falsehood into their own permanent `overall` field — 330: "both
continuity_check and premortem_check are MISSING — the two mandatory red-team
steps were skipped" — and 330 set `reason_category: "data_degraded"` on that
basis, which feeds PM's self-calibration. The system recorded, permanently and
untruthfully, that an analyst skipped a safety check.

**Why the direction still matters.** A veto on the morning path stops a
PURCHASE, and not buying costs nothing. A veto on THIS path stops a SALE: the
position stays on the book overnight with only the broker stop behind it.
`docs/OUTCOME.md` records under-trading, not over-trading, as this desk's
measured failure. So the cost of these inputs is asymmetric even where it has
not yet been paid.

**And the banner has never been right in production.** Across the 14 archived
MORNING risk reviews it has fired zero times — PM has never actually skipped
either step. (Corrected 2026-09-14: this read "15". The archive holds 17
`risk_manager` rows, 3 of them the exit reviews below, so the morning count is
14. The banner text "NOT PERFORMED" appears in the stored `input_message` of
exactly those 3 rows and none of the 14.) Its entire production output to date
is the three false statements above. That is a strong argument that the banner should be deleted rather than
routed around; it is left standing here only because deleting it changes the
morning seat's behaviour on a case that has not yet occurred, which is a
separate decision with a separate blast radius. See the PR discussion.

**The schema followed the prompt on 2026-09-14.** PR #343 gave this path its
own header and its own `_CHAIN_ROWS`, but both seats still answered one
schema, so the exit seat was still being ASKED for things this file had just
told it were inapplicable: `modifications` and `scale_all_buys`, which only
`_apply_risk_modifications` applies and which it is called only from the
morning `RiskStage`; and mandatory `min_length=1` answers to `rr_audit`,
`sizing_sanity` and `event_risk`, the three steps the header above stands down
or inverts. The exit path now answers `models.ExitRiskVerdict` /
`ExitRiskReasoningChain`, which are the morning shapes minus exactly those.
`models.ExitReviewChain` does the same on the INPUT side for `news_check`, the
one PM slot with no exit counterpart and no rendered row, which was carrying a
placeholder string only to satisfy `min_length=1`. `RiskVerdict`,
`RiskReasoningChain` and `ReasoningChain` are unchanged.

`NOT_AUTHORED` below is deliberately NOT part of that cleanup. It is not
residue: it marks a field the position reviewer's OWN schema makes mandatory,
so an empty one really does mean the reviewer's output degraded, and saying so
is true.

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


#: Header block prepended to the exit-review message.
#:
#: Every line here is a statement of fact about the code path — which review
#: this is, which lever actually does something, which of the standing
#: checklist items cannot be answered here and why. None of it is a new
#: threshold or a new policy.
#:
#: It is deliberately short. An earlier draft ran ~3,800 characters against an
#: archived exit prompt of 7,775-8,428, i.e. it grew the message by half again,
#: almost all of it negative instruction. Whether that much "do not" improves a
#: model's judgement is untested in both directions and this desk has no rig
#: that can test a prompt rewrite (see the memory note on what validates what),
#: so the standing bias is to say the true thing once and stop.
_EXIT_REVIEW_HEADER = """## Review Mode: EXIT REVIEW — not the morning plan review

You are reviewing the **position reviewer's decisions to CLOSE or REDUCE
positions the desk already holds**. Some standing instructions were written for
the morning plan and are wrong here.

**Which way a mistake costs.** Refusing a BUY means not buying, which costs
nothing. Refusing an exit leaves the position ON THE BOOK overnight with only
the broker stop behind it.

**Refusal is your only lever.** `modifications` and `scale_all_buys` are **not
fields of this path's output** — do not emit them; nothing applies either. The
% is the position reviewer's call on its own position; do not size it and do
not comment on it. Approve, or name the symbol in `rejected_symbols`.

**Does not apply here.** `rr_audit`, `sizing_sanity` and `event_risk` are
OPTIONAL here — omit them rather than explaining why they do not apply.
`signal_fidelity`, `correlation_check` and `overall` are still required.

- **Checklist 1** — this chain is the POSITION REVIEWER's, not PM's, and has NO
  `continuity_check` and NO `premortem_check`; those exist only in PM's schema.
  Their absence is **not a skipped audit step**. Do not write that a red-team
  step did not happen, and do not tag `data_degraded` for it.
- **Checklist 2 / Risk-Reward** — `$0.0` entry, stop and target are structural:
  an exit has no entry geometry. No ratio to audit, none to compute.
- **Checklist 5** — no BUYs or SHORTs here to size.
- **Checklist 4** — the instruction INVERTS. "Downsize or reject" on an event
  inside the window was written for an entry, where refusing carries LESS risk
  through the event; refusing HERE carries the position THROUGH it. Event
  proximity is **not a reason to refuse an exit**, and an unfetched calendar is
  **not `data_degraded`** here. Report a date that bears on the exit, or omit.

**Checklist 8 still applies and is the substance of your job.** Four Python
gates run on each exit — but after you speak, and all four are narrow: the
named-trigger gate checks only that the reason says recognised words, not that
the claim is true; the noise band is bypassed whenever the reason cites
external information, which the trigger gate all but requires; the
metric-contradiction veto does not run at all without recorded prior metrics
for that symbol; and `holding_discipline_claim_check` examines only a claimed
regime flip or HIGH-conviction bearish state change, only while the position is
still structurally protected, and passes every unverifiable claim by design.
**None of them can catch a plausibly-worded, deterministically-clean exit that
is simply wrong.** That gap is the job: does the named trigger hold up against
the blocks you were given, and is closing the right response to it?

A block marked unavailable below is a fact about this code path, not an
analyst's omission, and is not on its own a reason to refuse. Say which
questions you could not answer rather than answering them from something you
were not shown.
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
        # The macro seat's reasoning chain reaches PM verbatim under "audit
        # these for logic errors". This row is where PM's answer arrives, so
        # the seat that audits PM can see whether the audit happened at all.
        ("Macro logic audit", "macro_audit", True),
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
            "the midday or close loop, so there are no technical signals this "
            "session. Nobody skipped a step. Checklist 8 tells you to check "
            "the News and Tech blocks for the claimed trigger; the Tech half "
            "cannot be done here. Do not infer a rating, do not read the "
            "silence as bearish or bullish, and do not refuse an exit for "
            "lacking Tech confirmation. Say so in `signal_fidelity`."
        ),
    },
    "news": {
        MORNING_PLAN: "## News Intelligence\n(not provided)\n",
        EXIT_REVIEW: (
            "## News Intelligence\n"
            "NOT AVAILABLE THIS RUN — this loop does fetch news before the "
            "position review, so absence here means that fetch returned "
            "nothing or failed (recorded in the run's coverage log). Treat "
            "today's news as UNKNOWN rather than as quiet and say so in "
            "`signal_fidelity`. A news claim you cannot verify is a reason to "
            "say it is unverified, not on its own a reason to refuse.\n"
        ),
    },
}


def absent_block(block: str, mode) -> str:
    """Rendering for a block that is not present, honest about why."""
    return _ABSENT[block][normalize(mode)]
