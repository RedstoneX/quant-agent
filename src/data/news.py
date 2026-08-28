import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen, Request
from xml.etree import ElementTree

import feedparser

from src.data.news_dedup import NewsCluster, cluster_news, normalize_link
from src.trading_calendar import et_now

logger = logging.getLogger(__name__)

RSS_FEEDS = {
    # Financial / Markets
    "CNBC Top News": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "CNBC Economy": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
    "MarketWatch Top": "https://feeds.marketwatch.com/marketwatch/topstories/",
    "MarketWatch Markets": "https://feeds.marketwatch.com/marketwatch/marketpulse/",
    # Yahoo Finance republishes a large share of Reuters/AP/Bloomberg wire
    # copy alongside its own reporting, which is the closest free substitute
    # for the wire breadth "Reuters Business" and "AP Business" used to
    # provide (see the removal note below). Verified live 2026-08-28:
    # https://finance.yahoo.com/news/rssindex returns HTTP 200, valid RSS
    # 2.0, ~49 items with same-day timestamps.
    "Yahoo Finance News": "https://finance.yahoo.com/news/rssindex",
    # Macro / Policy / Politics
    "BBC Business": "https://feeds.bbci.co.uk/news/business/rss.xml",
    "NPR Economy": "https://feeds.npr.org/1017/rss.xml",
    # Fed / Treasury
    "Fed Press Releases": "https://www.federalreserve.gov/feeds/press_all.xml",
}

# ---------------------------------------------------------------------------
# Removed 2026-08-28: "Reuters Business" and "AP Business" — both wires were
# returning zero items (404 / 403 respectively) and, worse, failing SILENTLY:
# fetch_news() logged a warning per feed and reported the run as complete
# regardless. That silent-drop bug is fixed below (see NewsCoverage), but the
# two feeds themselves turned out not to be fixable for free. Investigated
# live the same day, with a plausible "just needs a real User-Agent" fix
# tried FIRST since a browser UA fixes the majority of anti-scraping 403s:
#
#   - Reuters ("https://www.reutersagency.com/feed/?best-topics=..."): a
#     browser UA changes nothing. Reuters killed public RSS in June 2020
#     (widely documented, e.g. https://news.ycombinator.com/item?id=23576022
#     and https://www.fivefilters.org/2021/reuters-rss-feeds/). The URL
#     redirects (301) to reutersagency.com, which 200s but is now a HubSpot
#     marketing page selling paid Reuters Connect licensing — there is no
#     feed link left in the page at all, so this isn't a moved URL, it's a
#     retired product. reuters.com itself 401s on every path, including the
#     homepage, behind a DataDome JS/CAPTCHA wall (captcha-delivery.com) —
#     also not a User-Agent problem.
#   - AP ("https://rsshub.app/apnews/topics/business"): this was never AP's
#     own feed — rsshub.app is a third-party scraper that reformats AP's
#     site into RSS, and it is now sitting behind a Cloudflare managed JS
#     challenge ("Just a moment...", cRay/cZone markers in the response
#     body) that no User-Agent string can pass, since it requires executing
#     JS. AP's own site DOES advertise a real feed
#     (apnews.com/index.rss, found via <link rel="alternate"> autodiscovery)
#     but it answers HTTP 401 "Invalid client credentials" — that endpoint
#     now sits behind AP's paid Content/Breaking News API
#     (developerapi.ap.org, API key + OAuth2 required per AP's own
#     developer docs). Both AP's official route and the free proxy route
#     are dead.
#
# Net: the UA hypothesis was WRONG for both — the failures are a retired
# product (Reuters) and a paid-API gate plus a bot-walled proxy (AP), not a
# blocked header. Per the standing "no new paid dependency without owner
# approval" rule, this is a STOP-and-report, not a sign-up. If dedicated
# Reuters/AP wire coverage is wanted, that is an owner decision (Reuters
# Connect or the AP Content API, both paid) — tracked in docs/WORK.md rather
# than silently worked around.
# ---------------------------------------------------------------------------

USER_AGENT = "Mozilla/5.0 (quant-agent/0.1)"
FETCH_TIMEOUT = 10


@dataclass
class FeedFailure:
    """One configured feed that did not return data on a fetch_news() call.

    ``reason`` is the exception text, truncated — feed failures are almost
    always short (HTTPError, URLError, timeout), but a feedparser bozo
    exception on a genuinely malformed document can ramble, and this string
    ends up both in a log line and in the analyst's prompt.
    """

    name: str
    reason: str


# Exception text longer than this is truncated before it reaches a log line
# or the analyst prompt. Generous enough for any real HTTP/parse error
# message seen in practice, short enough that one verbose exception can't
# blow up the coverage section of the prompt.
_FAILURE_REASON_MAX_LEN = 200


@dataclass
class NewsCoverage:
    """How much of the configured wire coverage actually came back on one
    fetch_news() call — the fix for the 2026-08-28 incident where two dead
    feeds (Reuters 404, AP 403) were dropped with a log warning and the
    pipeline still reported the news stage "ok" regardless. The desk was
    making decisions believing it had read every wire when two had
    returned nothing, and nothing downstream of the warning could tell.

    This object is the single source of truth for that fact from here on.
    It is threaded into the analyst's prompt (build_user_message) AND into
    the deterministic data_status the operator surface reads
    (MorningResearchStage / trader_feed / notifier) — a log line alone was
    exactly the failure mode being fixed, so this must never be logged only.
    """

    configured: int
    succeeded: int
    failed: list[FeedFailure]

    @property
    def failed_count(self) -> int:
        return len(self.failed)

    @property
    def complete(self) -> bool:
        """True only when every configured feed returned successfully.

        Zero configured feeds is deliberately NOT complete — an empty feed
        dict is a configuration error, not full coverage of nothing.
        """
        return self.configured > 0 and self.failed_count == 0

    @property
    def status(self) -> str:
        """One word for data_status[...] / logs — mirrors the ok / partial /
        failed vocabulary MorningResearchStage already uses for `tech`
        (src/pipeline_stages.py), so this reuses an existing convention
        rather than inventing a parallel one.
        """
        if self.configured == 0 or self.succeeded == 0:
            return "failed"
        if self.failed:
            return "partial"
        return "ok"

    def describe(self) -> str:
        """Human-readable one-liner for the analyst prompt and log lines.

        Deliberately does not say "no news" or go quiet when coverage is
        bad — it says exactly what happened, by name, so a reader (human or
        model) cannot mistake missing input for a quiet news day.
        """
        if self.configured == 0:
            return "News coverage: NO feeds configured (misconfiguration)."
        if not self.failed:
            return f"News coverage: {self.succeeded}/{self.configured} feeds returned data. Full coverage."
        names = ", ".join(f"{f.name} ({f.reason})" for f in self.failed)
        return (
            f"News coverage: {self.succeeded}/{self.configured} feeds returned data "
            f"this run. FAILED: {names}. Treat this as a coverage GAP, not "
            f"confirmed silence — do not conclude a topic is quiet solely "
            f"because a failed feed would normally cover it."
        )


@dataclass
class NewsItem:
    title: str
    summary: str
    source: str
    published: datetime | None
    link: str
    # Syndication breadth, filled in by the dedup stage (src/data/news_dedup).
    # `collapsed_count` is how many articles reported this same event —
    # including this one — and `source_count` how many distinct outlets. They
    # default to 1 so an item that never went through dedup reads correctly.
    #
    # These exist because "twelve outlets carried this" is real information
    # about SALIENCE, while being emphatically NOT twelve independent
    # confirmations. Dropping the duplicates without recording the count would
    # trade one distortion for another.
    collapsed_count: int = 1
    source_count: int = 1


class NewsDataProvider:
    def __init__(self, feeds: dict[str, str] | None = None, lookback_hours: int = 24):
        self.feeds = feeds or RSS_FEEDS
        self.lookback_hours = lookback_hours

    def fetch_news(
        self, lookback_hours_override: int | None = None,
    ) -> tuple[list[NewsItem], NewsCoverage]:
        """Fetch recent news from all RSS feeds.

        Default lookback is 24h, fine for Tue-Fri morning runs. On Monday
        morning the previous trading day was Friday, so a 24h window
        misses ~72h of weekend news (Fed pressers, geopolitical events,
        earnings pre-announcements all routinely land on weekends). The
        Monday-aware path: if today is Monday, automatically extend the
        lookback to cover the gap. The caller can also override via
        `lookback_hours_override` for hand-tuning / replay scenarios.

        Returns `(items, coverage)`. Before 2026-08-28 this returned only
        `items`, and a feed that failed simply contributed zero of them —
        indistinguishable from a feed that fetched fine and had nothing new
        to say. `NewsCoverage` is the fix: every caller now gets an explicit
        accounting of how many feeds were configured, how many actually
        returned data, and which ones failed and why, so "the wires were
        read" and "two wires returned nothing" can never again look the
        same downstream.
        """
        if lookback_hours_override is not None:
            effective_lookback = lookback_hours_override
        else:
            today = et_now()
            # weekday(): Monday=0 .. Sunday=6. Monday morning needs to
            # cover Fri close → Mon morning ≈ 72h. Tue after a Mon
            # holiday would also benefit but holiday awareness lives in
            # broker.is_trading_day; that's overkill here — Monday is
            # the 95% case.
            if today.weekday() == 0:  # Monday
                effective_lookback = max(self.lookback_hours, 72)
                logger.info(
                    "fetch_news: Monday detected — extending lookback "
                    "from %dh to %dh to cover weekend news",
                    self.lookback_hours, effective_lookback,
                )
            else:
                effective_lookback = self.lookback_hours
        cutoff = datetime.now(timezone.utc) - timedelta(hours=effective_lookback)
        all_items: list[NewsItem] = []
        succeeded = 0
        failures: list[FeedFailure] = []

        for source_name, url in self.feeds.items():
            try:
                items = self._fetch_feed(source_name, url, cutoff)
                all_items.extend(items)
                succeeded += 1
            except Exception as e:
                # This is the ONE place a dead feed becomes visible. Before
                # 2026-08-28 this branch logged the warning below and moved
                # on — the feed contributed zero items, identically to a
                # feed that fetched fine and simply had no fresh headlines,
                # and nothing past this loop could tell the two apart.
                # `failures` is what makes the difference reach the caller
                # instead of dying here as a log line only.
                logger.warning("Failed to fetch %s: %s", source_name, e)
                reason = str(e) or type(e).__name__
                failures.append(FeedFailure(
                    name=source_name, reason=reason[:_FAILURE_REASON_MAX_LEN],
                ))

        coverage = NewsCoverage(
            configured=len(self.feeds), succeeded=succeeded, failed=failures,
        )

        # Deduplicate by title similarity and sort by time (newest first)
        deduped = self._deduplicate(all_items)
        deduped.sort(key=lambda x: x.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

        logger.info(
            "Fetched %d news items from %d/%d sources (after dedup from %d); "
            "coverage=%s%s",
            len(deduped), succeeded, len(self.feeds), len(all_items),
            coverage.status,
            f" failed={sorted(f.name for f in failures)}" if failures else "",
        )
        return deduped, coverage

    def _fetch_feed(self, source_name: str, url: str, cutoff: datetime) -> list[NewsItem]:
        """Fetch and parse a single RSS feed.

        Raises on a genuine failure (network/HTTP error, or a document
        feedparser can't parse at all) rather than swallowing it into an
        empty list. That distinction matters: `fetch_news` is the only place
        that builds `NewsCoverage`, and it can only tell "this feed is
        broken" apart from "this feed has nothing new right now" if broken
        actually raises. A feed that parses fine and simply has zero entries
        (or zero entries newer than `cutoff`) is NOT a failure and still
        returns `[]` normally below — that is a real, healthy "nothing new".
        """
        req = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            raw = resp.read()

        feed = feedparser.parse(raw)

        if feed.bozo and not feed.entries:
            # A document feedparser could not make sense of at all (HTML
            # error page served with a 200, truncated XML, etc.) — this is
            # the fetch equivalent of an HTTP error, not "no stories today".
            raise ValueError(f"unparseable feed: {feed.bozo_exception}")

        items = []
        for entry in feed.entries:
            published = self._parse_date(entry)
            if published and published < cutoff:
                continue

            title = entry.get("title", "").strip()
            if not title:
                continue

            summary = entry.get("summary", entry.get("description", "")).strip()
            # Truncate long summaries
            if len(summary) > 300:
                summary = summary[:297] + "..."

            items.append(NewsItem(
                title=title,
                summary=summary,
                source=source_name,
                published=published,
                link=entry.get("link", ""),
            ))

        return items

    def _parse_date(self, entry) -> datetime | None:
        """Parse the published date from a feed entry."""
        parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        if parsed:
            try:
                from calendar import timegm
                ts = timegm(parsed)
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except (ValueError, OverflowError):
                return None
        return None

    @staticmethod
    def _normalize_link(link: str) -> str:
        """Normalize an article URL for cross-source dedup.

        Thin wrapper over :func:`src.data.news_dedup.normalize_link`, kept as
        a method because callers and tests reference it here.
        """
        return normalize_link(link)

    def cluster_news(self, items: list[NewsItem]) -> list[NewsCluster]:
        """Group articles reporting the same underlying event.

        This is the cascade's stage 1. Returns clusters rather than bare
        items so a future novelty stage (stage 2) can score whole events
        against a rolling buffer without re-deriving the grouping — see
        ``src/data/news_dedup.py`` for the full cascade note.
        """
        return cluster_news(items)

    def _deduplicate(self, items: list[NewsItem]) -> list[NewsItem]:
        """Collapse syndicated copies of one event down to one item each.

        Returns the cluster representatives, each stamped with
        ``collapsed_count`` / ``source_count`` so downstream consumers can
        see how widely a story was carried without mistaking that breadth
        for independent corroboration.

        The old implementation dropped duplicates via word-Jaccard > 0.7 on
        titles and discarded the count entirely. That threshold was measured
        against real cached headlines and does not separate: several genuine
        duplicates scored below distinct same-day events. See
        ``src/data/news_dedup.py`` for the replacement method and its
        calibration.
        """
        clusters = self.cluster_news(items)
        representatives: list[NewsItem] = []
        for cluster in clusters:
            rep = cluster.representative
            rep.collapsed_count = cluster.collapsed_count
            rep.source_count = cluster.source_count
            representatives.append(rep)
        collapsed = len(items) - len(representatives)
        if collapsed:
            logger.info(
                "news dedup: %d article(s) collapsed into %d event(s) "
                "(from %d raw)",
                collapsed, len(representatives), len(items),
            )
        return representatives

    def tag_symbol_mentions(self, items: list[NewsItem], universe: list[str]) -> dict[str, list[NewsItem]]:
        """Tag which news items mention symbols from the universe. Uses word-boundary matching."""
        import re
        # Short symbols (1-3 chars) are prone to false positives; require word boundaries
        patterns: dict[str, re.Pattern] = {}
        for s in universe:
            sym = s.upper()
            patterns[sym] = re.compile(r'\b' + re.escape(sym) + r'\b')
        result: dict[str, list[NewsItem]] = {}
        for item in items:
            text = f"{item.title} {item.summary}".upper()
            for sym, pat in patterns.items():
                if pat.search(text):
                    result.setdefault(sym, []).append(item)
        return result

    def format_for_prompt(self, items: list[NewsItem], max_items: int = 50) -> str:
        """Format news items into a text block for the LLM prompt."""
        if not items:
            return "No recent news available."

        limited = items[:max_items]
        lines = []
        for item in limited:
            time_str = item.published.strftime("%Y-%m-%d %H:%M UTC") if item.published else "unknown"
            # Syndication breadth is rendered explicitly. Without it, the
            # analyst either sees N copies of one story (and reads them as N
            # confirmations) or sees one copy with no idea how widely it was
            # carried. Naming the count, and naming what it does NOT mean, is
            # the whole point of the dedup stage.
            breadth = ""
            collapsed = getattr(item, "collapsed_count", 1) or 1
            if collapsed > 1:
                sources = getattr(item, "source_count", 1) or 1
                breadth = (
                    f" [carried by {sources} outlet{'s' if sources != 1 else ''}"
                    f", {collapsed} articles - syndication breadth, "
                    f"NOT independent corroboration]"
                )
            lines.append(f"[{item.source}] ({time_str}) {item.title}{breadth}")
            if item.summary:
                lines.append(f"  > {item.summary}")

        return "\n".join(lines)
