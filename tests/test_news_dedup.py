"""Tests for the news dedup stage (cascade stage 1).

The calibration cases at the bottom are lifted verbatim from real cached
headlines in the production news archive (``data/news/2026-08-*``). They are
pinned here so that any future change to the method or the threshold has to
face the same evidence the original choice was made against.

No network calls: everything constructs NewsItem objects directly.
"""

from datetime import datetime, timezone

import pytest

from src.data.news import NewsDataProvider, NewsItem
from src.data.news_dedup import (
    SIMILARITY_THRESHOLD,
    TITLE_ONLY_THRESHOLD,
    cluster_news,
    similarity,
    normalize_link,
    normalize_text,
    tokenize,
)

DAY = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def item(title, summary="", source="X", link="", published=DAY):
    return NewsItem(title=title, summary=summary, source=source,
                    published=published, link=link)


# === normalization helpers ===

def test_normalize_text_strips_case_punctuation_and_apostrophes():
    assert normalize_text("Trump's U.S. Deal — 'Done'!") == "trumps u.s. deal done"


def test_normalize_text_handles_empty_and_none_safely():
    assert normalize_text("") == ""
    assert normalize_text(None) == ""


def test_tokenize_drops_stopwords_and_single_chars():
    assert tokenize("The Fed is in a hurry") == ["fed", "hurry"]


def test_tokenize_keeps_figures_which_carry_event_identity():
    """Numbers and percentages are exactly the tokens that distinguish two
    events about one company, so they must survive tokenization."""
    toks = tokenize("Okta stock jumps 29% on $40 billion outlook")
    assert "29%" in toks
    assert "$40" in toks


def test_normalize_link_strips_tracking_and_fragment():
    base = "https://example.com/article/abc"
    assert normalize_link(base + "?utm_source=x") == base
    assert normalize_link(base + "#section-1") == base
    assert normalize_link(base + "/") == base
    assert normalize_link("HTTPS://Example.COM/article/abc") == base
    assert normalize_link("") == ""


# === exact duplicates ===

def test_exact_duplicate_titles_collapse_to_one_cluster():
    items = [
        item("Fed holds rates steady", source="CNBC"),
        item("Fed holds rates steady", source="BBC"),
        item("Oil slides on demand worries", source="NPR"),
    ]
    clusters = cluster_news(items)
    assert len(clusters) == 2
    fed = [c for c in clusters if "Fed" in c.representative.title][0]
    assert fed.collapsed_count == 2


def test_exact_duplicates_differing_only_by_punctuation_still_collapse():
    items = [
        item("Trump's tariff deal is 'done'", source="CNBC"),
        item("Trumps tariff deal is done", source="BBC"),
    ]
    assert len(cluster_news(items)) == 1


def test_same_url_across_outlets_collapses_regardless_of_headline():
    """A wire story republished under the same underlying URL is an exact
    duplicate even when the two outlets rewrote the headline past any
    plausible similarity threshold."""
    url = "https://apnews.com/article/fed-rate-pause-2026"
    items = [
        item("Fed pauses rate hikes amid soft data", source="Reuters",
             link=url + "?utm_source=reuters"),
        item("Markets cheer as central bank signals hold", source="CNBC",
             link=url + "?ref=cnbc"),
    ]
    clusters = cluster_news(items)
    assert len(clusters) == 1
    assert clusters[0].collapsed_count == 2
    assert set(clusters[0].sources) == {"Reuters", "CNBC"}


# === near duplicates with different headlines ===

def test_near_duplicate_different_headlines_collapse():
    """Real pair from 2026-08-27 BBC Business: the same shop-closure story
    filed twice under different headlines."""
    items = [
        item("Much-loved ice cream shop to close after 106 years",
             summary="The family business has served the town since 1920.",
             source="BBC Business"),
        item("'End of an era' for ice cream shop open 106 years",
             summary="Locals mourn the family business that has served the town since 1920.",
             source="BBC Business"),
    ]
    clusters = cluster_news(items)
    assert len(clusters) == 1, [c.representative.title for c in clusters]


def test_near_duplicate_across_outlets_collapses():
    """Real pair from 2026-08-19 (BBC vs CNBC) — one tariff pause, two
    outlets, two headlines."""
    items = [
        item("Trump pauses new tariffs on Canada for three days, saying deal close",
             summary="The president said a deal with Ottawa was within reach.",
             source="BBC Business"),
        item("Trump pauses 50% scheduled tariffs on Canada for three days, "
             "announces 'deal' with Ottawa",
             summary="The president said the two sides were close to a deal.",
             source="CNBC Top News"),
    ]
    clusters = cluster_news(items)
    assert len(clusters) == 1
    assert clusters[0].source_count == 2


# === false-merge guards: distinct same-day events must stay separate ===

def test_two_distinct_same_day_events_about_one_company_stay_separate():
    """The failure mode that matters most. Both stories are about NVDA, on
    the same day, sharing the ticker and most topic words — but an earnings
    beat and an export-ban headline are two events and must not merge."""
    items = [
        item("Nvidia beats on Q2 earnings as data center revenue doubles",
             summary="Nvidia reported quarterly revenue above analyst estimates, "
                     "driven by data center demand.",
             source="CNBC Top News"),
        item("Nvidia shares slip as U.S. weighs new export curbs on China sales",
             summary="Washington is considering fresh restrictions on Nvidia chip "
                     "exports to China.",
             source="MarketWatch Top"),
    ]
    clusters = cluster_news(items)
    assert len(clusters) == 2, (
        "distinct same-day events about one company must not merge: "
        f"{[c.representative.title for c in clusters]}"
    )


def test_same_topic_different_speaker_does_not_merge():
    """Real pair from 2026-08-21 — the hardest measured false pair, scoring
    0.427. Same trade dispute, same day, but different speakers making
    different claims. It is the reason SIMILARITY_THRESHOLD sits at 0.45."""
    items = [
        item("Trump says U.S., Canada 'should be able' to reach trade deal as "
             "tariff deadline looms",
             summary="The president struck an optimistic tone on negotiations.",
             source="CNBC Top News"),
        item("Canada 'should fight' as US trade deadline looms, Manitoba premier says",
             summary="The provincial leader urged Ottawa to resist US pressure.",
             source="BBC Business"),
    ]
    assert len(cluster_news(items)) == 2


def test_two_fed_press_releases_on_one_day_stay_separate():
    """Real pair from 2026-08-20. Fed press-release headlines share heavy
    boilerplate ('Federal Reserve Board ...') and would collapse under a
    naive word-overlap rule, but an approval and an enforcement action are
    different events."""
    items = [
        item("Federal Reserve Board announces approval of application by "
             "First National Bancorp",
             source="Fed Press Releases"),
        item("Federal Reserve Board issues enforcement action with "
             "Cedar Valley Bank",
             source="Fed Press Releases"),
    ]
    assert len(cluster_news(items)) == 2


def test_complete_linkage_prevents_chaining_two_events_together():
    """Single-linkage clustering welds A and C together whenever some B
    resembles both. Complete linkage requires the candidate to match every
    existing member, so the bridge article cannot merge two distinct events."""
    a = item("Acme raises full-year profit guidance after strong quarter",
             summary="Acme lifted its outlook following a strong quarter.")
    bridge = item("Acme raises full-year profit guidance and names new chief "
                  "executive officer",
                  summary="Acme lifted its outlook and named a new chief executive.")
    c = item("Acme names new chief executive officer to lead turnaround",
             summary="Acme appointed a new chief executive to lead its turnaround.")
    clusters = cluster_news([a, bridge, c])
    reps = {cl.representative.title for cl in clusters}
    assert len(clusters) >= 2, reps
    # The guidance story and the CEO story must never share a cluster.
    for cl in clusters:
        titles = [m.title for m in cl.members]
        assert not (a.title in titles and c.title in titles)


# === collapsed count is preserved ===

def test_collapsed_count_and_sources_are_preserved():
    items = [
        item("Fed holds rates steady", source="CNBC"),
        item("Fed holds rates steady", source="BBC"),
        item("Fed holds rates steady", source="NPR"),
    ]
    (cluster,) = cluster_news(items)
    assert cluster.collapsed_count == 3
    assert cluster.source_count == 3
    assert cluster.sources == ["CNBC", "BBC", "NPR"]


def test_singleton_cluster_reports_count_of_one():
    (cluster,) = cluster_news([item("A lone headline nobody else carried")])
    assert cluster.collapsed_count == 1
    assert cluster.source_count == 1


def test_cluster_reports_earliest_published():
    early = datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)
    late = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)
    items = [
        item("Fed holds rates steady", source="CNBC", published=late),
        item("Fed holds rates steady", source="BBC", published=early),
    ]
    (cluster,) = cluster_news(items)
    assert cluster.earliest_published == early


def test_provider_deduplicate_stamps_counts_onto_representatives():
    provider = NewsDataProvider()
    items = [
        item("Fed holds rates steady", source="CNBC"),
        item("Fed holds rates steady", source="BBC"),
        item("Oil slides on demand worries", source="NPR"),
    ]
    deduped = provider._deduplicate(items)
    assert len(deduped) == 2
    by_title = {i.title: i for i in deduped}
    assert by_title["Fed holds rates steady"].collapsed_count == 2
    assert by_title["Fed holds rates steady"].source_count == 2
    assert by_title["Oil slides on demand worries"].collapsed_count == 1


def test_format_for_prompt_states_breadth_and_disclaims_corroboration():
    """The count has to reach the model, and it has to reach it labelled.
    An unlabelled '(x5)' would invite exactly the misreading being fixed."""
    provider = NewsDataProvider()
    it = item("Fed holds rates steady", source="CNBC")
    it.collapsed_count = 5
    it.source_count = 4
    text = provider.format_for_prompt([it])
    assert "4 outlets" in text
    assert "5 articles" in text
    assert "NOT independent corroboration" in text


def test_format_for_prompt_stays_quiet_for_singletons():
    provider = NewsDataProvider()
    text = provider.format_for_prompt([item("Fed holds rates steady")])
    assert "carried by" not in text


# === representative selection ===

def test_representative_is_the_most_informative_member():
    items = [
        item("Fed holds", summary="", source="A"),
        item("Fed holds", summary="The Federal Open Market Committee left the "
                                  "target range unchanged at 4.25-4.5%.", source="B"),
    ]
    (cluster,) = cluster_news(items)
    assert cluster.representative.source == "B"


def test_representative_tie_breaks_to_the_earliest_report():
    early = datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)
    late = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)
    items = [
        item("Fed holds rates steady", source="Late", published=late),
        item("Fed holds rates steady", source="Early", published=early),
    ]
    (cluster,) = cluster_news(items)
    assert cluster.representative.source == "Early"


# === structural / seam ===

def test_novelty_seam_is_present_and_unset():
    """Stage 2 (novelty scoring) is deliberately not built. The field exists
    so the next stage has somewhere to write without reshaping this one."""
    (cluster,) = cluster_news([item("Anything at all")])
    assert cluster.novelty is None


def test_empty_and_single_item_inputs_are_handled():
    assert cluster_news([]) == []
    assert len(cluster_news([item("Only one")])) == 1


def test_clustering_is_deterministic():
    items = [
        item("Fed holds rates steady", source="CNBC"),
        item("Fed holds rates steady", source="BBC"),
        item("Oil slides on demand worries", source="NPR"),
        item("Nvidia beats on Q2 earnings", source="MW"),
    ]
    first = [[m.source for m in c.members] for c in cluster_news(items)]
    for _ in range(5):
        assert [[m.source for m in c.members] for c in cluster_news(items)] == first


def test_input_order_does_not_change_the_number_of_events():
    items = [
        item("Fed holds rates steady", source="CNBC"),
        item("Oil slides on demand worries", source="NPR"),
        item("Fed holds rates steady", source="BBC"),
    ]
    assert len(cluster_news(items)) == len(cluster_news(list(reversed(items)))) == 2


# === threshold calibration, pinned against real archived headlines ===

# Labels. True = same event, must merge. False = different events, must NOT
# merge. KNOWN_MISS = same event, deliberately NOT merged: catching it would
# require a threshold low enough to also merge genuinely distinct stories, so
# it is the price paid for zero false merges. Recorded rather than hidden — a
# future method that turns a KNOWN_MISS into True while every False row still
# passes is a real improvement, and should be re-labelled then.
KNOWN_MISS = "known_miss"

# Each entry is (title_a, summary_a, title_b, summary_b, label), copied
# verbatim from the production news archive. Title AND summary are both kept
# because the summary carries real discriminative power — the same pairs with
# summaries stripped are measurably harder to separate, which is why
# TITLE_ONLY_THRESHOLD exists.
#
# The `False` rows are the false-merge risks that set the threshold floor. If a
# future change breaks one of these, the change is not an improvement — it is a
# regression against measured data.
_CALIBRATION_PAIRS = [
    (
        "Luigi Mangione pleads guilty in federal case related to UnitedHealthcare CEO killing",
        "Mangione's lawyers argue his federal plea bars his prosecution for murder in state court for Brian Thompson's slaying due to New York's double jeopardy law",
        "Luigi Mangione pleads guilty in federal court to stalking health-insurance CEO",
        "Luigi Mangione, who is accused of killing a UnitedHealthcare executive, pled guilty to federal stalking charges ahead of a high-profile state murder trial in New York set to begin next month.",
        True,
    ),
    (
        "Why is Selena Gomez being sued?",
        "BBC journalist Ana Guerra-Moore looks at why investors who backed Wondermind Global are claiming the pop star failed to fulfil promises.",
        "Selena Gomez sued for alleged fraud over mental health company",
        "Investors say the actor and singer did not take an \"active role\" in the company as promised.",
        KNOWN_MISS,
    ),
    (
        "Trump pauses new tariffs on Canada for three days, saying deal close",
        "The US president says the two sides are finalising documents to avert import taxes targeting a range of Canadian goods.",
        "Trump pauses 50% scheduled tariffs on Canada for three days, announces 'deal' with Ottawa",
        "Trump has already imposed a variety of tariffs on Canada and its specific exports, including metals, lumber and auto parts.",
        True,
    ),
    (
        "Trump says U.S. and Canada reached deal to delay 50% U.S. tariffs on Canadian imports",
        "President Trump said he was delaying the 50% U.S. tariffs on Canadian imports after the two countries reached a last-minute deal less than two hours before the sanctions were to go into effect.",
        "Trump pauses 50% scheduled tariffs on Canada for three days, announces 'deal' with Ottawa",
        "Trump has already imposed a variety of tariffs on Canada and its specific exports, including metals, lumber and auto parts.",
        KNOWN_MISS,
    ),
    (
        "The $40 trillion national debt may already be costing you",
        "The $40 trillion national debt isn't just a number in Washington. It can make borrowing more expensive for everyday Americans, and every realistic plan to bring it down involves tradeoffs.",
        "U.S. debt tops $40 trillion",
        "The federal debt is growing even faster than expected. Just paying interest on the $40 trillion balance is now the government's second-largest expense, outpacing everything but Social Security.",
        True,
    ),
    (
        "3 things to know about the $40 trillion federal debt",
        "The U.S. federal debt hit a record $40 trillion this week. The debt has doubled since 2017, and just paying interest on the accumulated debt now costs the government more than $1 trillion a year.",
        "U.S. debt tops $40 trillion",
        "The federal debt is growing even faster than expected. Just paying interest on the $40 trillion balance is now the government's second-largest expense, outpacing everything but Social Security.",
        True,
    ),
    (
        "Former JPMorgan exec takes on Social Security advisory role",
        "Matt Zames will take on an unpaid adviser position at the Social Security Administration, according to reports.",
        "Trump admin taps former JPMorgan Chase exec Matt Zames to advise Social Security agency",
        "Matt Zames is taking an unpaid position to help his former JPMorgan colleague Frank Bisignano tackle modernization of the SSA, CNBC has learned.",
        True,
    ),
    (
        "Canada unveils retaliatory tariffs on about $20 billion of U.S. goods",
        "Canadian trade negotiators left the U.S. last week after failing to clinch a trade deal that would stop President Donald Trump's new tariffs from taking effect.",
        "Canada announces retaliatory tariffs on the U.S. as the countries' trade fight deepens",
        "The measures come after President Trump imposed 50% tariffs on many Canadian goods and threatened more after trade talks broke down on Friday.",
        True,
    ),
    (
        "Household energy bills to hit three-year high as Ofgem announces 4% rise from October",
        "A household using a typical amount of gas and electricity will pay £60 a year more, regulator Ofgem says.",
        "Energy prices to rise to three-year high",
        "Energy bills for millions of households are expected to rise this winter to the highest level for three years",
        True,
    ),
    (
        "Much-loved ice cream shop to close after 106 years",
        "The family-run shop has been a fixture in Brislington, Bristol since the 1920s.",
        "'End of an era' for ice cream shop open 106 years",
        "The Tarr family say they are \"devastated\" to announce they will have to close due to rising costs.",
        True,
    ),
    (
        "Yields decline on CNBC report Treasury could use General Account to fund buybacks",
        "Treasury yields fell Monday as investors await Federal Reserve Chair Kevin Warsh's keynote speech at Jackson Hole later this week.",
        "Bessent could tap near $1 trillion Treasury General Account to fund bond buybacks, sources said",
        "Using the TGA would provide the Treasury with considerable firepower to influence long-term bond yields.",
        KNOWN_MISS,
    ),
    (
        "What's happening to UK interest rates and what does it mean for mortgages?",
        "The interest rate set by the Bank of England affects mortgage, loan and savings rates for millions.",
        "What is happening to UK prices?",
        "UK inflation dropped slightly in June 2026, but higher energy costs are expected to push it back up.",
        False,
    ),
    (
        "Trump says U.S., Canada 'should be able' to reach trade deal as tariff deadline looms",
        "Trump had already postponed the 50% tariffs on a range of Canadian goods so Washington and Ottawa could finalize a tentative deal.",
        "Canada 'should fight' as US trade deadline looms, Manitoba premier says",
        "Premier Wab Kinew's comments signal a tough battle for Prime Minister Mark Carney to get buy-in from Canadians on a possible trade deal with the US.",
        False,
    ),
    (
        "Berkshire Hathaway doubles down on the U.S. housing market with a fresh bet on this stock",
        "Berkshire Hathaway has upped its bet on the U.S. housing market, and also boosted its stakes in Alphabet and Delta Air Lines.",
        "Berkshire Hathaway boosts Alphabet to a top three holding, ups Delta and housing bets",
        "Berkshire owned about 106 million shares in the parent of Google, worth $37.9 billion at the end of June, according to a regulatory filing released Friday.",
        False,
    ),
    (
        "Federal Reserve Board announces approval of application by National Westminster Bank Plc",
        "Federal Reserve Board announces approval of application by National Westminster Bank Plc",
        "Federal Reserve Board issues enforcement action with SouthPoint Bancshares, Inc. and announces termination of enforcement action with Deutsche Bank AG, DB USA Corporation, and Deutsche Bank AG New York Branch",
        "Federal Reserve Board issues enforcement action with SouthPoint Bancshares, Inc. and announces termination of enforcement action with Deutsche Bank AG, DB USA Corporation, and Deutsche Bank AG New York Branch",
        False,
    ),
    (
        "Nvidia earnings live updates: Q2 results, memory prices and AI outlook",
        "Follow Nvidia’s Q2 2027 earnings for data center results, guidance, AI demand and NVDA stock reaction in real time.",
        "S&P 500 is little changed after PCE report shows sticky inflation; Nvidia earnings on deck: Live updates",
        "The S&amp;P 500 moved slightly lower on Wednesday after the latest personal consumption expenditures price index reading revealed that inflation remains elevated.",
        False,
    ),
    (
        "Fed Chairman Kevin Warsh delivers his key Jackson Hole speech Friday. Here's what to expect",
        "Prior Fed chairs have used the speech as an opportunity to lay out broad policy frameworks and intentions.",
        "The stakes are high as Kevin Warsh is set to give his first major speech as Fed Chair",
        "The stakes are high as Fed Chair Kevin Warsh prepares to give a speech on Friday at the annual gathering of economists and central bankers in Jackson Hole, Wyoming.",
        False,
    ),
    (
        "Why bitcoin has more room to run after biggest 3-day gain since 2023, according to analysts",
        "Bitcoin's outsized rebound last week could prove more than just a short-lived relief rally.",
        "Crypto extends gains after biggest 3-day rally since 2023",
        "Bitcoin and crypto stocks extended their rally after the flagship cryptocurrency broke out of its trading range.",
        False,
    ),
]


@pytest.mark.parametrize("title_a,summary_a,title_b,summary_b,label",
                         _CALIBRATION_PAIRS)
def test_calibration_against_real_archived_headlines(
    title_a, summary_a, title_b, summary_b, label
):
    """Every pair here is real: two articles that actually appeared in the
    same day's fetch. The label is a hand judgement of whether they describe
    one underlying event, and what this stage is expected to do about it."""
    pair = [
        item(title_a, summary=summary_a, source="A"),
        item(title_b, summary=summary_b, source="B"),
    ]
    merged = len(cluster_news(pair)) == 1
    expected = (label is True)
    verdict = {True: "expected merge", False: "FALSE MERGE",
               KNOWN_MISS: "known miss regressed"}[label]
    assert merged is expected, (
        f"{verdict} (score {similarity(pair[0], pair[1]):.3f}): "
        f"{title_a!r} vs {title_b!r}"
    )


def test_no_false_merges_anywhere_in_the_calibration_set():
    """The precision guarantee, stated once as a whole-set property rather
    than only case by case."""
    for title_a, summary_a, title_b, summary_b, label in _CALIBRATION_PAIRS:
        if label is not False:
            continue
        pair = [item(title_a, summary=summary_a), item(title_b, summary=summary_b)]
        assert len(cluster_news(pair)) == 2, title_a


def test_recall_on_the_calibration_set_does_not_silently_regress():
    """Recall is deliberately below 100%. Pinning the count means a future
    change that quietly trades away real merges gets noticed — and a change
    that improves recall fails loudly enough to be re-baselined on purpose."""
    hits = 0
    for title_a, summary_a, title_b, summary_b, label in _CALIBRATION_PAIRS:
        if label is False:
            continue
        pair = [item(title_a, summary=summary_a), item(title_b, summary=summary_b)]
        hits += len(cluster_news(pair)) == 1
    assert hits == 8, (
        f"measured recall changed: {hits}/11 same-event pairs merged, expected 8"
    )


def test_threshold_sits_above_the_measured_false_pair_ceiling():
    """Documents the numbers rather than trusting a comment. With summaries
    present the worst measured false pair scored 0.429; on titles alone it
    scored 0.503, which is why the two thresholds differ."""
    assert SIMILARITY_THRESHOLD > 0.429
    assert TITLE_ONLY_THRESHOLD > 0.503
    pair = [
        item("Trump says U.S., Canada 'should be able' to reach trade deal as "
             "tariff deadline looms",
             summary="The president struck an optimistic tone on negotiations.",
             source="A"),
        item("Canada 'should fight' as US trade deadline looms, Manitoba "
             "premier says",
             summary="The provincial leader urged Ottawa to resist US pressure.",
             source="B"),
    ]
    assert len(cluster_news(pair)) == 2
    # ... and demonstrate the failure the threshold is protecting against.
    assert len(cluster_news(pair, threshold=0.20)) == 1


def test_headline_only_pairs_must_clear_the_stricter_bar():
    """A pair with no summary on either side has strictly less evidence, so
    it needs more similarity. This pair scores 0.503 on titles alone — enough
    to clear SIMILARITY_THRESHOLD, not enough to clear TITLE_ONLY_THRESHOLD."""
    titles = (
        "Trump says U.S., Canada 'should be able' to reach trade deal as "
        "tariff deadline looms",
        "Canada 'should fight' as US trade deadline looms, Manitoba premier says",
    )
    bare = [item(t, summary="", source=s) for t, s in zip(titles, "AB")]
    assert SIMILARITY_THRESHOLD < similarity(bare[0], bare[1]) < TITLE_ONLY_THRESHOLD
    assert len(cluster_news(bare)) == 2, "headline-only pair merged below the stricter bar"


def test_one_summary_present_is_enough_to_use_the_normal_bar():
    """The stricter bar applies only when NEITHER side has a summary. One
    summary is still evidence."""
    a = item("Household energy bills to hit three-year high as Ofgem announces "
             "4% rise from October",
             summary="Regulator confirms the price cap will rise in October.",
             source="A")
    b = item("Energy prices to rise to three-year high", summary="", source="B")
    assert len(cluster_news([a, b])) == 1
