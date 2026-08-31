"""Deterministic news deduplication — stage 1 of the news cascade.

WHY THIS EXISTS
---------------
A single event (an earnings beat, a downgrade, an acquisition) is syndicated
across many outlets. Before this stage, the News Analyst received all of them,
so five copies of one story read as five independent confirmations of a
thesis. That is *false corroboration*: the model becomes over-confident on
evidence that has a sample size of one.

The fix is not to save tokens (news is a small share of LLM spend). It is to
make the evidence honest: one event → one item, tagged with how many outlets
carried it. Breadth of syndication is real information about *salience* — it
is simply not information about *independent confirmation* — so the collapsed
count is preserved on the representative rather than discarded.

WHERE THIS SITS IN THE CASCADE
------------------------------
``docs/RESEARCH_FINDINGS.md`` §2 prescribes a three-stage cascade:

    1. dedup            <- THIS MODULE
    2. novelty scoring  <- NOT BUILT (see the seam below)
    3. model on the residual only

Only stage 1 is implemented. The seam for stage 2 is deliberate and narrow:
``NewsCluster.novelty`` is carried as ``None`` and never read by anything
here. A novelty stage should consume ``cluster_news()`` output, compare each
cluster against a rolling 48-72h per-ticker buffer, populate ``novelty``, and
hand the surviving clusters to stage 3. Nothing in this module needs to change
for that to happen.

METHOD AND WHY
--------------
Cosine similarity over L2-normalized term-frequency vectors of the normalized,
stopword-filtered title, blended with the same measure over title+summary.
Deterministic, dependency-free, O(n^2) on a batch of ~60 headlines
(microseconds), and fully explainable — the score is just length-normalized
token overlap, so you can always point at the shared words that caused a merge.

Rejected alternatives, each rejected on measurement rather than taste:

  * **Word-Jaccard on raw titles** — the previous implementation, flagged as
    "weak" in ``docs/AGENT_ROLE_AUDIT.md`` §2.2. On the real archive it does
    not separate at any threshold: distinct same-day events scored 0.30-0.33
    while genuine duplicates scored 0.30-0.54, fully overlapping.
  * **IDF weighting** (tried, measured, dropped). It looks like the obvious
    upgrade — weight rare proper nouns and figures above shared topic words —
    but it was strictly worse here on both axes. Document frequency computed
    over the day's batch makes the score depend on *what else happened to be
    fetched*, so the same pair of articles can merge on one day and not the
    next; that is not explainable behaviour. Document frequency computed over
    a baked-in corpus fixes that but ages, and measured against the same
    labelled pairs it caught fewer true duplicates at equal precision (6/11
    vs 8/11 at the same threshold). Plain term frequency won.
  * **Embeddings.** Would likely beat all of the above, but needs a model call
    or a new dependency; the mandate for this stage is deterministic and free.

THRESHOLD
---------
``SIMILARITY_THRESHOLD`` is calibrated against 589 real cached headlines from
the production news archive (``data/news/``, 10 trading days,
2026-08-14..2026-08-27), hand-labelled into 11 same-event pairs and 7
different-event pairs. Those pairs are pinned in ``tests/test_news_dedup.py``.

It is not a round number picked by feel. The highest-scoring pair of genuinely
*different* same-day events measured 0.429. Recall is flat at 8/11 across the
whole band 0.44-0.47, so the threshold is set at the top of that band: same
recall, the largest available margin above the worst measured false merge.

There are TWO thresholds, because there are two evidence regimes. Measured on
the same pairs with the summaries stripped — the degraded case where a feed
supplies a headline and nothing else — the worst false pair rises from 0.429
to 0.503: titles alone genuinely cannot tell "Trump says a deal is reachable"
from "Manitoba's premier says Canada should fight". Rather than pick one
number that is too loose for headlines-only or too tight for everything else,
a pair with no summary on either side must clear the stricter
``TITLE_ONLY_THRESHOLD``. Less evidence, more required similarity.

Recall is deliberately capped below 100%. A false merge destroys information —
an event silently disappears from the analyst's view — while a missed merge
merely leaves the pre-existing, already-tolerated behaviour in place. When in
doubt, under-merge.

CAVEAT worth keeping in view: 18 labelled pairs is a small calibration set
drawn from 10 trading days on which two of nine feeds were dead (Reuters and
AP — see the audit), which suppresses exactly the wire-syndication case this
stage targets. Re-measure as the archive grows.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

# Calibrated against real cached headlines; see the module docstring. The worst
# observed FALSE pair (two distinct same-day stories) scored 0.429, and recall
# does not improve anywhere in 0.44-0.47, so this sits at the top of that band
# for maximum margin at no cost. Lowering it past ~0.43 is known to produce
# false merges on real data.
SIMILARITY_THRESHOLD = 0.47

# Applied when NEITHER article has a summary, so the score collapses to
# title-only overlap. Measured worst false pair in that regime is 0.503, hence
# the stricter bar. It buys precision at a real cost in recall, which is the
# correct trade when the only evidence is a headline.
TITLE_ONLY_THRESHOLD = 0.55

# Weight on the title-only similarity vs the title+summary similarity. Titles
# carry the event; summaries add corroborating detail but also outlet
# boilerplate, so they inform rather than dominate.
_TITLE_WEIGHT = 0.7
_BODY_WEIGHT = 1.0 - _TITLE_WEIGHT

# Tokens too common to carry event identity. Kept deliberately small — IDF
# already down-weights frequent terms; this list only removes function words
# that would otherwise inflate similarity between unrelated headlines.
_STOPWORDS = frozenset("""
a an the of to in on for at by with and or as is are was were be been from
that this it its into over under after before amid says say said will would
could new his her their your our not no more less than about up down what
whats who how why when where do does did has have had can may might
""".split())

_PUNCT_RE = re.compile(r"[^a-z0-9%$.,]+")
_APOSTROPHE_RE = re.compile(r"[‘’'`]")
_WS_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Lowercase, strip apostrophes and punctuation, collapse whitespace.

    Apostrophes are removed rather than replaced with a space so that
    "Canada's" and "Canadas" and "Canada" converge on the same stem-ish form.
    """
    s = _APOSTROPHE_RE.sub("", (text or "").lower())
    s = _PUNCT_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def tokenize(text: str) -> list[str]:
    """Normalized, stopword-filtered tokens. Order is preserved (callers may
    care); duplicates are preserved so term frequency is available."""
    out: list[str] = []
    for raw in normalize_text(text).split():
        tok = raw.strip(".,")
        if len(tok) > 1 and tok not in _STOPWORDS:
            out.append(tok)
    return out


def normalize_link(link: str) -> str:
    """Normalize an article URL for exact cross-outlet matching.

    Strips query parameters (utm_*, ?ref=, ?source=) and fragments, lowercases
    the host, drops the trailing slash. Many outlets republish the same wire
    story under an identical underlying URL differing only in tracking params;
    that is an exact duplicate and does not need the similarity pass.
    """
    if not link:
        return ""
    try:
        parts = urlsplit(link)
        return urlunsplit((
            parts.scheme.lower(),
            (parts.netloc or "").lower(),
            (parts.path or "").rstrip("/"),
            "",
            "",
        ))
    except ValueError:
        return link.strip().lower()


@dataclass
class NewsCluster:
    """One underlying event, plus every article that reported it.

    ``representative`` is the single article that should be shown to a model.
    ``members`` includes the representative, so ``collapsed_count`` is the
    total number of articles that reported this event (1 for an event that
    only one outlet carried).
    """

    representative: object
    members: list[object]
    #: Stage-2 seam. Populated by a future novelty stage that scores this
    #: cluster against a rolling per-ticker buffer; nothing in this module
    #: reads it. See the module docstring.
    novelty: float | None = None

    @property
    def collapsed_count(self) -> int:
        """How many articles reported this event. "Twelve outlets carried
        this" is real information about salience — it is NOT twelve
        independent confirmations."""
        return len(self.members)

    @property
    def sources(self) -> list[str]:
        """Distinct outlet names, in first-seen order."""
        seen: list[str] = []
        for m in self.members:
            src = getattr(m, "source", "") or ""
            if src and src not in seen:
                seen.append(src)
        return seen

    @property
    def source_count(self) -> int:
        return len(self.sources)

    @property
    def earliest_published(self) -> datetime | None:
        stamps = [
            p for p in (getattr(m, "published", None) for m in self.members)
            if p is not None
        ]
        return min(stamps) if stamps else None


def _unit_vector(text: str) -> dict[str, float]:
    """L2-normalized term-frequency bag of tokens.

    No corpus statistics are involved, deliberately: the score for a pair of
    articles depends only on those two articles, never on what else was in
    the batch. See the module docstring for the IDF variant that was measured
    and rejected.
    """
    vec: dict[str, float] = {}
    for tok in tokenize(text):
        vec[tok] = vec.get(tok, 0.0) + 1.0
    norm = math.sqrt(sum(v * v for v in vec.values()))
    if norm == 0.0:
        return {}
    return {k: v / norm for k, v in vec.items()}


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    return sum(w * b[t] for t, w in a.items() if t in b)


def _full_text(item: object) -> str:
    title = getattr(item, "title", "") or ""
    summary = getattr(item, "summary", "") or ""
    return f"{title} {summary}".strip()


def similarity(a: object, b: object) -> float:
    """Similarity in [0, 1] between two news items.

    Exposed so a merge can always be explained after the fact, and so the
    calibration tests can assert on the number rather than only on the
    clustering outcome.
    """
    title_sim = _cosine(
        _unit_vector(getattr(a, "title", "") or ""),
        _unit_vector(getattr(b, "title", "") or ""),
    )
    body_sim = _cosine(_unit_vector(_full_text(a)), _unit_vector(_full_text(b)))
    return _TITLE_WEIGHT * title_sim + _BODY_WEIGHT * body_sim


def _representative_rank(item: object) -> tuple:
    """Sort key choosing the best representative of a cluster (max wins).

    Preference order:
      1. The most informative article — longest title+summary. The downstream
         consumer is a language model reading text, so the richest version of
         the story is the most useful one to keep.
      2. The earliest published, as a tie-break — closest to the original
         report rather than a later reprint.
      3. Source name, purely so the choice is deterministic.

    Note the deliberate omission: source *credibility* is not weighted here.
    The audit (§2.2) flags that a Fed press release and an aggregator are
    treated as equal-weight text blocks; that is a real gap, but it is a
    separate change and fixing it inside a dedup stage would hide it.
    """
    published = getattr(item, "published", None)
    # Earlier is better, so negate via a descending-friendly ordering: use the
    # negative POSIX timestamp. Missing timestamps rank last.
    if published is None:
        recency_key = float("-inf")
    else:
        try:
            recency_key = -published.timestamp()
        except (OSError, OverflowError, ValueError):
            recency_key = float("-inf")
    return (
        len(_full_text(item)),
        recency_key,
        getattr(item, "source", "") or "",
    )


def cluster_news(
    items: Iterable[object],
    threshold: float = SIMILARITY_THRESHOLD,
    title_only_threshold: float = TITLE_ONLY_THRESHOLD,
) -> list[NewsCluster]:
    """Group articles that report the same underlying event.

    Returns one :class:`NewsCluster` per event, in the order the first member
    of each cluster appeared in ``items``.

    Guards against false merges, in order of how much they matter:

    * **Complete linkage.** A candidate joins a cluster only if it exceeds the
      threshold against *every* existing member, not merely the nearest one.
      Single-linkage chaining is the classic way two distinct events get
      welded together through an intermediate article that resembles both.
    * **A threshold above the measured false-pair ceiling**, raised further
      for pairs with no summary text, where headlines alone are measurably
      less able to distinguish two events. See the module docstring.
    * **Exact matches bypass scoring entirely** (identical normalized title,
      or identical normalized URL), so the fuzzy pass never has to be tuned
      loose enough to catch the easy cases.
    """
    items = list(items)
    if len(items) < 2:
        return [NewsCluster(representative=i, members=[i]) for i in items]

    title_vecs = [_unit_vector(getattr(i, "title", "") or "") for i in items]
    body_vecs = [_unit_vector(_full_text(i)) for i in items]

    has_summary = [bool((getattr(i, "summary", "") or "").strip()) for i in items]

    def score(i: int, j: int) -> float:
        return (
            _TITLE_WEIGHT * _cosine(title_vecs[i], title_vecs[j])
            + _BODY_WEIGHT * _cosine(body_vecs[i], body_vecs[j])
        )

    def bar(i: int, j: int) -> float:
        """Required similarity for this pair. Headline-only pairs must clear
        a higher bar because a headline alone carries measurably less
        evidence about which event is being described."""
        if threshold != SIMILARITY_THRESHOLD:
            # Caller pinned an explicit threshold; honour it verbatim so
            # experiments and tests stay predictable.
            return threshold
        if has_summary[i] or has_summary[j]:
            return threshold
        return max(threshold, title_only_threshold)

    norm_titles = [normalize_text(getattr(i, "title", "") or "") for i in items]
    norm_links = [normalize_link(getattr(i, "link", "") or "") for i in items]

    def is_exact_dup(i: int, j: int) -> bool:
        if norm_titles[i] and norm_titles[i] == norm_titles[j]:
            return True
        return bool(norm_links[i]) and norm_links[i] == norm_links[j]

    clusters: list[list[int]] = []
    for idx in range(len(items)):
        placed = False
        for members in clusters:
            # Complete linkage: must match EVERY member, not just one.
            if all(
                is_exact_dup(idx, m) or score(idx, m) >= bar(idx, m)
                for m in members
            ):
                members.append(idx)
                placed = True
                break
        if not placed:
            clusters.append([idx])

    out: list[NewsCluster] = []
    for members in clusters:
        member_items = [items[m] for m in members]
        rep = max(member_items, key=_representative_rank)
        out.append(NewsCluster(representative=rep, members=member_items))
    return out
