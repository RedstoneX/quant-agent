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
    "Reuters Business": "https://www.reutersagency.com/feed/?best-topics=business-finance",
    "CNBC Top News": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "CNBC Economy": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
    "MarketWatch Top": "https://feeds.marketwatch.com/marketwatch/topstories/",
    "MarketWatch Markets": "https://feeds.marketwatch.com/marketwatch/marketpulse/",
    # Macro / Policy / Politics
    "AP Business": "https://rsshub.app/apnews/topics/business",
    "BBC Business": "https://feeds.bbci.co.uk/news/business/rss.xml",
    "NPR Economy": "https://feeds.npr.org/1017/rss.xml",
    # Fed / Treasury
    "Fed Press Releases": "https://www.federalreserve.gov/feeds/press_all.xml",
}

USER_AGENT = "Mozilla/5.0 (quant-agent/0.1)"
FETCH_TIMEOUT = 10


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

    def fetch_news(self, lookback_hours_override: int | None = None) -> list[NewsItem]:
        """Fetch recent news from all RSS feeds.

        Default lookback is 24h, fine for Tue-Fri morning runs. On Monday
        morning the previous trading day was Friday, so a 24h window
        misses ~72h of weekend news (Fed pressers, geopolitical events,
        earnings pre-announcements all routinely land on weekends). The
        Monday-aware path: if today is Monday, automatically extend the
        lookback to cover the gap. The caller can also override via
        `lookback_hours_override` for hand-tuning / replay scenarios.
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

        for source_name, url in self.feeds.items():
            try:
                items = self._fetch_feed(source_name, url, cutoff)
                all_items.extend(items)
            except Exception as e:
                logger.warning("Failed to fetch %s: %s", source_name, e)

        # Deduplicate by title similarity and sort by time (newest first)
        deduped = self._deduplicate(all_items)
        deduped.sort(key=lambda x: x.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

        logger.info("Fetched %d news items from %d sources (after dedup from %d)",
                     len(deduped), len(self.feeds), len(all_items))
        return deduped

    def _fetch_feed(self, source_name: str, url: str, cutoff: datetime) -> list[NewsItem]:
        """Fetch and parse a single RSS feed."""
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=FETCH_TIMEOUT) as resp:
                raw = resp.read()
        except Exception as e:
            logger.warning("Feed %s fetch failed: %s", source_name, e)
            return []

        feed = feedparser.parse(raw)

        if feed.bozo and not feed.entries:
            logger.warning("Feed %s returned no entries: %s", source_name, feed.bozo_exception)
            return []

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
