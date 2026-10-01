## item 215 — RETIRED 2026-10-01, every level-backed claim now carries the level's measured zone span, and the claim itself is narrowed to the stop-distance bound the code already enforces (ruling in docs/INCIDENT_HISTORY.md)

Filed 2026-09-30 out of the item 55 measurement pass, as a SEPARATE defect that
item 55 surfaced and deliberately did not fix.

What the code does: `_level_backing_stop` (`src/portfolio_constructor.py`) walks
the computed structural levels on the protective side of entry, keeps those with
at least `risk.min_level_touches_for_stop_honor` touches, and honours the stop as
level-backed when `abs(stop - level) <= level_zone_halfwidth(...)`. Since item 55
that half-width is the MEASURED span of the bars that drew the level rather than a
flat 1% of price. That is the right bound for "is this stop resting on this level".
It is not a bound on how far the stop is from the level, and the two are reported
as the same thing.

Measured 2026-09-30, complete-linkage clustering, 400-day bars, 101-name universe:
704 levels, zone half-width min 0.53%, median 3.47%, max 22.11% of price.

Measured the same day against the 11 live positions and their live stops as the
production desk database holds them:

| symbol | entry | live stop | honouring level | level touches | zone | half-width | stop-to-level gap |
| --- | --- | --- | --- | --- | --- | --- | --- |
| ETN | 432.56 | 405.43 | 388.55 | 6 | 381.06-413.77 | 6.49% of level | 16.88 = 3.90% of entry |
| RKLB | 69.72 | 65.14 | 67.31 | 6 | 62.99-76.24 | 13.27% of level | 2.17 = 3.11% of entry |
| NOK | 10.30 | 9.39 | 9.78 | 5 | 9.54-10.33 | 5.62% of level | 0.39 = 3.79% of entry |

In each of the three the stop can be taken out with the level itself never broken,
and every owner-facing statement about the position still reads "protected by
structure". The other eight live positions are not level-backed either way, so
this defect is live on 3 of 11 names today.

Scope: this item is about what the desk SAYS, not about whether the exemption
should fire. Whether a stop inside a wide zone should count as backed at all is
the decision in DONE WHEN (b). No number is introduced by this item.
