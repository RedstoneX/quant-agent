## item 225 — RETIRED 2026-10-01, citations now name an AST-verified symbol instead of a line number

Measurement, 2026-10-01, against origin/main. Method is a throwaway script, not committed.

- The guard is rule 7 in `src/number_sources.py` (`broken_citations`, lines 1135-1170, regex at 1121-1125).
- It checks only that the cited file exists and that the cited line number is not past the end of the file.
- Its own docstring (lines 164-168) admits a drifted citation is not caught.
- 336 ledger rows; 247 file-plus-line citations across 176 rows [measured: regex from the guard, applied to every prose field].
- 55 of the 247 cite `src/pipeline.py` or a `src/pipeline_*.py` file [measured, same script].
- No mechanical verdict is possible for most citations, because prose rarely quotes the text it points at.
- Only 9 of the 247 carry a backticked snippet right after the citation, so only 9 could be checked mechanically [measured]; 6 of those 9 do not contain the snippet, but some are false alarms (a dotted YAML key, a prose snippet), so treat 6 as an upper bound, not a count.
- The 55 pipeline citations have no nearby backticked identifier, so none could be checked mechanically; the three examples below were read by hand.

Examples a reader can confirm by opening the line:

- `src.pipeline` row for `_EMERGENCY_LIMIT_CUSHION_PCT` says it is "defined at src/pipeline.py:1466"; line 1466 is a different function, and the constant is at line 1619.
- Row for `_PM_PROFILE_SYMBOL_CAP` cites src/pipeline.py:125-131 as its use site; line 125 is `class CarryForward:`.
- Row for `_another_session_recently_active(within_minutes)` cites src/pipeline.py:14620-14626 as a docstring; line 14620 is unrelated comment text.

Why it matters: the split of the two largest files will move thousands of lines, and the guard will keep passing.
