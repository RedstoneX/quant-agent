## item 210 — the split plan was amended 2026-10-01 and now says when it runs

**Open.** The written plan for splitting the two oversized pipeline files was
re-measured against the current code on 2026-10-01 and amended: its figures
were all stale, one module boundary described a stop-moving method as if it
placed no orders (it has been reassigned to the protection module), two new
exit helpers were given a home, and a source-text check was re-pointed from
the wrong step to the right one. No code moved in that pass; it is a document
change only.

**When it runs.** Measured over the last fourteen days, 125 commits touched the
larger file — twenty inside the first step's range and sixty-three inside the
exits range — so a pure-move change survives about a day for the first step and
a few hours for the exits step. Ruling made on the risk route, 2026-10-01: the
split starts after today's trading sessions finish, not before, and the file
mapping is re-run immediately before the first step is opened. The owner
ratified the milestone and its ordering, not this timing call.
