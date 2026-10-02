## item 220 — RETIRED 2026-10-01, every closing criterion met in shipped code and audited line by line

A technical row the model returned malformed was dropped and the name carried on with the
timing veto unrecorded. The fix shipped in `Item 220: a technical row the desk could not
read is a MISSING seat, never silence` (merged 2026-10-01 04:27 UTC), on top of item 20's
per-name coverage record (merged 2026-10-01 03:27 UTC). This note records the audit that
closed the item, criterion by criterion, so the closure can be checked rather than trusted.

### Criterion 1 — unreadable row recorded as uncovered AND unreadable, and the gate counts it — MET

`src/evidence_gate.py:898-918` (`name_coverage`): a seat named in `unreadable_by_seat` for a
name is computed as a subset of `uncovered`, never moved out of it, so the seat stays
missing and is additionally named. `NameCoverage.blocking_missing`
(`src/evidence_gate.py:801-810`) reads `uncovered & BLOCKING_SEATS`, so an unreadable
technical row keeps `tech` in the blocking gap, and `names_missing_blocking_seat`
(`src/evidence_gate.py:824-846`) therefore names it. `src/pipeline.py:16138-16146` feeds
`ctx.tech_unreadable` in as that seat's unreadable set and unions those symbols into the
universe, so a name whose ONLY appearance is the lost row still gets a row of its own.

### Criterion 2 — held names are inside the record's universe — MET

`src/pipeline.py:16127-16137`: every symbol in `ctx.positions` is added to the universe
before the record is built, with the reason written beside it. Held names therefore get the
same per-name seat record as entry candidates, which is what makes the staying decision
readable at all.

### Criterion 3 — the three causes are told apart FROM THE FIELDS — MET

`NameCoverage` carries `unreadable`, `asked_no_answer` and the derived `never_asked`
(`src/evidence_gate.py:757-791`), and `to_evidence()` emits `unreadable_seats`,
`asked_no_answer_seats` and `never_asked_seats` as separate keys. `never_asked` is
`uncovered - unreadable - asked_no_answer`, so the three partition the uncovered set with no
overlap; an unreadable row deliberately wins over "asked and silent" for the same name
(`src/evidence_gate.py:908-914`), because a row did come back. The raw inputs are produced by
the technical seat itself: `src/agents/tech_analyst.py:635-672` sets `last_unreadable`
(a row came back, could not be read) and `last_unanswered` (asked, nothing usable, no row to
blame), both reset per batch, and `src/pipeline_stages.py:5342-5349` carries them onto the
context. No reader has to parse prose to tell the causes apart.

### Criterion 4 — nothing added: no retry, no JSON repair, no new refusal — MET, proved from the diff

Checked as a negative against the whole merge diff, not asserted. The change touches seven
files and adds 362 lines. Every added line in `src/agents/tech_analyst.py` is either a
comment or one of: initialising `last_unreadable`/`last_unanswered`, passing the ALREADY
EXISTING `_malformed_sink` keyword on the single-chunk path, and populating the two
attributes from the sink after the call. The multi-chunk path's `_malformed_sink=unusable`,
its `_retries_left=0` and the bounded phase-2 recovery are unchanged context lines in the
diff, not additions. Grepping the added lines of the whole `src/` diff for `retry`,
`retries`, `repair`, `json.loads`, `refuse`, `refusal`, `block_reason`, `raise` and `attempt`
returns four hits, all of them comments that point AT the pre-existing refusals rather than
adding one. The entry refusal (`risk.rules.own_bar_block_reason`, "no technical read this
review") and the held-side drop (rotation's `ineligible_hold` tier) are untouched, so the
2026-09-25 opposition-only ruling still governs both: an unreadable answer is not opposition,
and a held name loses its claim to be kept on conviction without being sold on silence.
`names_missing_blocking_seat` and `name_coverage` both swallow every exception and return an
empty record, so the disclosure can never break the trading path it reports on.

### Recording status — UNPROVEN (not DEAD), and here is why that is the honest label

Production database read read-only on 2026-10-01: `specialist_evidence` holds 13,832 rows,
newest `2026-10-01 08:30:42`; rows whose payload matches `name_coverage` = 0; rows carrying
`covered_seats`, `uncovered_seats` or `asked_no_answer_seats` = 0. Zero is expected rather
than alarming: the newest run-level `evidence_gate` row is `2026-09-30 14:46:59`, and BOTH
commits that create this record landed after it (03:27 and 04:27 UTC on 2026-10-01). The
deployed tree does carry the code — `_record_name_coverage` and `asked_no_answer_by_seat`
are both present in the production `src/pipeline.py` — and the identical `_record` sink is
what already writes the run-level `evidence_coverage` rows that DO persist, so the write path
is the proven one. No desk session has run since the change deployed. **The first session
after deployment must be checked for `name_coverage` rows carrying the three fields; if that
session produces none, this record is DEAD and item 220 must be reopened.**
