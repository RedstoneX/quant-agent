## item 211 — RETIRED 2026-10-01, the mute is recorded and the backlog is now readable on the dashboard

Why the item existed. The owner muted every desk alert, protective ones
included, after 107 messages in four days — 44 of them one fault announced on
both of its edges. Three separate defects kept that from being safe to undo:
coalescing keyed to a window no real episode ever fell inside, the
"this position is unprotected" alert deduped per DAY rather than per symbol so
a second naked name was silenced outright, and the global mute dropping each
message before anything was written down.

What closed it. Coalescing now spans the whole episode, the unprotected-position
alert dedupes per symbol per day like its two siblings, the mute records every
message it drops with kind, text, symbols and timestamp, and the recorded
backlog is now shown on the dashboard, grouped by kind and by ET day with the
live-risk messages listed one by one and never folded into a total. The
dashboard is the right home: a muted desk cannot page its owner about its own
muting, and the backlog is something he reads at his leisure.

What this surface deliberately does NOT claim. The record began on 2026-10-01,
and the mute has been on since 2026-09-30. Everything the mute dropped in
between was never written down and cannot be recovered. The panel states that
gap in its own words rather than presenting a partial list as the whole period.

State at retirement [measured 2026-10-01, production database, read-only]: the
muted-message record holds zero rows — `notifier_sends` carries 213 rows, all
of them `sent`, spanning 2026-09-18 19:34:23 to 2026-09-30 15:15:47 UTC. The
recording is therefore UNPROVEN rather than POPULATING: the branch that writes
it is deployed on the box, but no muted row has been observed in production, so
nothing proves it writes. The empty backlog is a correct and good answer for
the period the record covers, and the surface is pinned by test to say so
rather than to report an error.

A live-risk message is one about a position whose protection is gone or never
arrived. That class is not invented here: it is the `MONEY_UNPROTECTED` fault
families in `src/log_health.py`, plus the literal owner-facing headlines the
desk's own live-risk alerts print. A headline announcing that a gap CLOSED is
excluded on purpose — it is good news about a gap, not an open one.

Nothing in this work un-mutes anything, sends anything, or touches
`TELEGRAM_DISABLED` or any other configuration.
