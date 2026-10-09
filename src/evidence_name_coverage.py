"""Per-name research coverage — the counting half of the evidence gate.

A STANDALONE PART. It imports nothing from `src.evidence_gate` and holds no
module-level view of the desk: the seat lists and the owner's blocking-seat
mandate set are handed to `build_name_coverage_recorder` BY VALUE, so this
record can be constructed and tested with plain values and no pipeline, no
config and no gate module anywhere in sight.

Disclosure only. Nothing here refuses, scores or holds a threshold; the
reasoning for that is kept verbatim below, where it was written.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.sentinel.counted import record_swallowed

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# THE COUNTING HALF — RECORDED, NOT SCORED (docs/WORK.md item 20)
# ---------------------------------------------------------------------------
# The half of the owner's design left open was "the seat answered about 40 of
# 65 companies — is that enough?". Two honest attempts at deriving that bar
# were made on 2026-10-01 and BOTH failed, for reasons that are written down
# here so the third attempt is not made blind:
#
#   1. From published practice: nothing published states how much of a
#      candidate list a research seat must cover before a decision is sound.
#      This is the same dead end the seat-count half hit in 2026-09-02.
#
#   2. From the desk's own record: the production evidence table cannot
#      answer it, because per-name coverage is only recorded for the seats
#      that happen to write symbol-scoped rows. Measured read-only against
#      the production DB on 2026-10-01 over the 228 runs that wrote any
#      symbol-scoped evidence: the technical seat covers a median 100% of the
#      names seen in a run, earnings peaks at 96%, smart-money at 100%,
#      macro at 50% — and the news seat writes NO symbol-scoped row at all,
#      in any run. News per-name coverage is therefore UNRECORDED, not zero.
#      A bar fitted to that record would be fitted to a hole in the record.
#
# So, per the owner's standing rulings — risk is read per name and never set
# as a global dial, and a number that cannot be sourced is not invented — the
# counting half is NOT a ratio with a bar. It collapses to the same
# categorical question the seat half already answers, asked once per name:
#
#     for THIS name, did this seat produce an answer about it, or not?
#
# That is a yes-or-no fact, it needs no bar, and it cannot be fitted. What
# ships here is the RECORDING of it. Nothing below refuses anything, scores
# anything, or holds a threshold. The entry path already enforces the one
# per-name coverage rule the desk has ratified — `pipeline._filter_supported_
# symbols` blocks a BUY or a SHORT on a name with no technical analysis — and
# this record is what would let a bar for the other seats be derived from
# measured desk data later, instead of picked.

#: Seats that answer ABOUT A NAME, so "did it cover this name" is meaningful.
NAME_SCOPED_SEATS = ("tech", "earnings", "news", "smart_money")

#: Seats whose answer is about the market, not about a name. Recorded
#: separately so a run-scoped seat is never booked as a per-name gap — that
#: would manufacture missing coverage out of a seat that cannot have any.
RUN_SCOPED_SEATS = ("macro",)


@dataclass(frozen=True)
class NameCoverage:
    """Which research seats produced an answer about ONE name.

    Disclosure only. No bar, no score, no ratio — see the block above.
    """

    symbol: str
    covered: list[str] = field(default_factory=list)
    uncovered: list[str] = field(default_factory=list)
    run_scoped: list[str] = field(default_factory=list)
    #: Seats that DID return something about this name which could not be
    #: read. A strict subset of `uncovered` — an answer that cannot be read
    #: is not an answer (board item 220). Carried separately only so the
    #: record tells the TRUE story: this seat spoke and the desk lost it,
    #: rather than this seat was never asked. Neither reads as agreement.
    unreadable: list[str] = field(default_factory=list)
    #: Seats that WERE asked about this name and produced nothing usable at
    #: all. THE THREE CAUSES ARE KEPT APART ON PURPOSE and a later reader
    #: must be able to tell them apart from the FIELDS, never from prose:
    #:   * `unreadable`        — asked, an answer came back, it could not be read
    #:   * `asked_no_answer`   — asked, nothing usable came back at all
    #:   * uncovered minus both — never asked about this name
    #: Same consequence (no answer, so no veto satisfied) but three different
    #: causes with three different fixes, and collapsing them would hide
    #: which one is happening.
    asked_no_answer: list[str] = field(default_factory=list)
    #: The seats that are allowed to stop the desk, HANDED IN BY VALUE by
    #: whoever built the recorder. It is deliberately NOT read from a module
    #: global: this record is constructed and tested on its own, so the
    #: mandate set has to arrive as an argument like every other collaborator.
    blocking_seats: tuple[str, ...] = ()

    def to_evidence(self) -> dict:
        return {
            "symbol": self.symbol,
            "covered_seats": list(self.covered),
            "uncovered_seats": list(self.uncovered),
            "unreadable_seats": list(self.unreadable),
            "asked_no_answer_seats": list(self.asked_no_answer),
            "never_asked_seats": list(self.never_asked),
            "blocking_seats_missing": list(self.blocking_missing),
            "run_scoped_seats": list(self.run_scoped),
            "summary": self.summary,
        }

    @property
    def never_asked(self) -> list[str]:
        """Uncovered seats with neither an unreadable row nor a lost answer."""
        return sorted(set(self.uncovered) - set(self.unreadable) - set(self.asked_no_answer))

    @property
    def blocking_missing(self) -> list[str]:
        """Uncovered seats that are allowed to stop the desk on this name.

        `blocking_seats` is the owner's 2026-09-18 mandate set, handed to
        this record when it was built; a seat in it that did not answer
        ABOUT THIS NAME is a missing veto, never a satisfied one.
        """
        return sorted(set(self.uncovered) & set(self.blocking_seats))

    @property
    def summary(self) -> str:
        unread = "".join(
            [
                (f" (returned an unreadable answer: {', '.join(self.unreadable)})" if self.unreadable else ""),
                (
                    f" (asked and produced nothing usable: {', '.join(self.asked_no_answer)})"
                    if self.asked_no_answer
                    else ""
                ),
            ]
        )
        return (
            f"{self.symbol}: answered about this name by "
            f"{', '.join(self.covered) or 'no seat'}; no answer about this "
            f"name from {', '.join(self.uncovered) or 'no seat'}{unread}; "
            f"market-wide seats not scoped to a name: "
            f"{', '.join(self.run_scoped) or 'none'}"
        )


class NameCoverageRecorder:
    """Records per-name seat coverage from the seat lists handed to it."""

    def __init__(self, *, name_scoped_seats, run_scoped_seats, blocking_seats):
        self._name_scoped = tuple(str(s) for s in name_scoped_seats)
        self._run_scoped = tuple(str(s) for s in run_scoped_seats)
        self._blocking_seats = tuple(sorted({str(s) for s in blocking_seats}))

    def names_missing_blocking_seat(self, coverage: dict) -> dict:
        """{name: [blocking seats that did not answer about it]}, non-empty only.

        Board item 220. The run-level `evaluate` asks whether a seat answered AT
        ALL this tick; it cannot see a seat that answered about nine names and
        lost the tenth. This is the per-name reading of the same mandate, and it
        is what lets the entry and the stay paths treat a lost row as a MISSING
        seat instead of as an absent objection.

        Disclosure, like everything else in the per-name half: it reports, it
        does not refuse. The refusals already exist and are categorical —
        `risk.rules.own_bar_block_reason` blocks ENTRY on "no technical read
        this review", and rotation's `ineligible_hold` tier drops a held name
        that fails that same bar out of the ranked survivors. NEVER raises.
        """
        try:
            return {
                name: list(cov.blocking_missing)
                for name, cov in (coverage or {}).items()
                if getattr(cov, "blocking_missing", None)
            }
        except Exception as exc:  # noqa: BLE001 — a record must never break a run
            record_swallowed("evidence_gate.blocking_gap_read", exc, log=logger)
            return {}

    def coverage(
        self,
        universe,
        seat_symbols,
        run_scoped=None,
        unreadable_by_seat=None,
        asked_no_answer_by_seat=None,
    ) -> dict:
        """Record, per name, which name-scoped seats answered about it.

        `seat_symbols` maps a seat to the symbols it produced an answer about.
        A seat absent from the mapping is a seat whose per-name coverage this
        desk does not record, and it is reported as uncovered for every name
        rather than silently assumed complete — claiming coverage that was
        never recorded is the failure this exists to stop.

        NEVER raises and NEVER judges: it returns a record, not a verdict.
        """
        try:
            names = sorted({str(s).strip().upper() for s in (universe or ()) if str(s).strip()})
            mapping = {}
            for seat, symbols in dict(seat_symbols or {}).items():
                mapping[str(seat)] = {str(s).strip().upper() for s in (symbols or ()) if str(s).strip()}

            def _symbol_map(raw):
                built = {}
                for seat, symbols in dict(raw or {}).items():
                    built[str(seat)] = {str(s).strip().upper() for s in (symbols or ()) if str(s).strip()}
                return built

            unread_map = _symbol_map(unreadable_by_seat)
            silent_map = _symbol_map(asked_no_answer_by_seat)
            scoped = sorted({str(s) for s in (self._run_scoped if run_scoped is None else (run_scoped or ()))})
            out = {}
            for name in names:
                covered = sorted(seat for seat in self._name_scoped if name in mapping.get(seat, set()))
                uncovered = sorted(set(self._name_scoped) - set(covered))
                # An unreadable answer is NOT coverage: the seat stays in
                # `uncovered` and is additionally named here. Reporting it as
                # covered would be the exact fabrication item 220 exists to stop.
                unreadable = sorted(seat for seat in uncovered if name in unread_map.get(seat, set()))
                # An unreadable row wins over "asked and silent" when both are
                # claimed for the same name: a row DID come back.
                asked_no_answer = sorted(
                    seat for seat in uncovered if name in silent_map.get(seat, set()) and seat not in unreadable
                )
                out[name] = NameCoverage(
                    symbol=name,
                    covered=covered,
                    uncovered=uncovered,
                    run_scoped=scoped,
                    unreadable=unreadable,
                    asked_no_answer=asked_no_answer,
                    blocking_seats=self._blocking_seats,
                )
            return out
        except Exception as exc:  # noqa: BLE001 — a record must never break a run
            record_swallowed("evidence_gate.name_coverage", exc, log=logger)
            return {}


def build_name_coverage_recorder(
    *,
    name_scoped_seats=NAME_SCOPED_SEATS,
    run_scoped_seats=RUN_SCOPED_SEATS,
    blocking_seats,
) -> NameCoverageRecorder:
    """Build the recorder from plain values — no module reaches back anywhere.

    `blocking_seats` has NO default on purpose: the owner's mandate set is a
    money rule, and a caller that forgets to hand it over must fail loudly
    rather than silently record every name as fully covered.
    """
    return NameCoverageRecorder(
        name_scoped_seats=name_scoped_seats,
        run_scoped_seats=run_scoped_seats,
        blocking_seats=blocking_seats,
    )
