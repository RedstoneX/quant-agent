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

**Step 0 has landed (no code moved).** The first of the twelve steps adds only
guards, so it cannot collide with a trading session's own edits. Two things now
exist for every later step to lean on. The first is a frozen inventory of what
each of the two oversized files contains — every module-level function and
every method name on every class, read from the abstract syntax tree rather
than by importing, so a move that breaks an import still reports the truth.
Any change to that set fails the build with instructions: a deliberate move
re-records the inventory in the same change, and the diff of that record is
the reviewable statement of what moved where; anything else is a method that
vanished or was duplicated. A second check asserts no method name is defined
twice across the classes that will be combined into one object, because under
mixins the first base silently wins.

The second is a migration helper for the number ledger. Every ledgered number
is keyed by an id that embeds its module, so a move makes those ids lie, and a
new file left out of the ledger's scope list drops its numbers out of the guard
with no error at all. The helper plans a move, applies it, and — the part that
matters — re-reads the result afterwards and reports anything left behind,
duplicated or unscoped, so the rewrite is checked rather than trusted. Both
guards are proved able to fail: the inventory check is run against source with
a method added, renamed and removed, and the verifier against a deliberately
half-applied move.

**One correction to the plan, measured not assumed.** Section 5 says the ledger
carries 36 ids under the larger file and 22 under the smaller. Measured against
current main today: 34 and 22. The 36 is stale by two; the 22 is right. The
counts are now pinned by a test, so the next drift is a red build rather than a
discovery. Eleven steps remain and this item stays open.
