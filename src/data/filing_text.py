"""Filing text extraction: SEC 10-Q / 10-K HTML to a compressed, high-signal text.

Pure functions of the file given; no provider, no network, no manifest.
"""

import logging
import re
from pathlib import Path

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


#: What a financial statement looks like, as text: dollar-prefixed amounts
#: ($1,234), bare comma-separated thousands (1,234,567), and parenthesized
#: negatives ((123)). Dense in income statements, balance sheets and cash
#: flow statements; near-absent in cover pages, tables of contents, XBRL
#: taxonomy boilerplate and the auditor's opinion letter.
#:
#: Defined once because two places need the SAME definition: the test for
#: whether extracted sections are worth keeping, and the search for the
#: densest region to fall back to. When those two disagreed, structured
#: extraction could pass a bar the fallback would have failed it on.
FINANCIAL_FIGURE_RE = re.compile(r"\$[\d,]+(?:\.\d+)?|\d{1,3}(?:,\d{3})+|\(\d{1,3}(?:,\d{3})*\)")


def extract_text(html_path: str, max_chars: int = 30000) -> str:
    """Extract high-signal sections from a SEC 10-Q / 10-K filing.

    A raw 10-K can be 200K+ chars; 70-80% is boilerplate the LLM doesn't
    need (properties listings, mine safety disclosures, legal notes,
    signatures, exhibit indices, XBRL footers). Dumping that to the
    earnings_analyst wastes ~30% of our total token budget and dilutes
    its attention away from what drives the investment call.

    This returns a compressed document with just:
    - Financial statements  (revenue / margins / EPS numbers)
    - MD&A                  (narrative on growth, segments, outlook)
    - Risk factors          (top risks management flagged)

    Falls back to truncated full-text when structured extraction
    can't locate any sections (non-standard filing layout).
    """
    raw = Path(html_path).read_bytes()
    soup = BeautifulSoup(raw, "html.parser")

    for tag in soup(["script", "style", "meta", "link"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Structured path
    sections = extract_key_sections(text)
    structured_output = ""
    if sections:
        parts: list[str] = []
        total = 0
        # Order: financials (hard numbers) → MD&A (narrative) → risks (tail)
        order = ("financial_statements", "mdna", "risk_factors")
        for label in order:
            body = sections.get(label)
            if not body:
                continue
            # Per-section cap — MD&A on a 10-K can run 40K+ on its own.
            if len(body) > 12000:
                body = body[:12000] + "\n[... section truncated ...]"
            header = label.replace("_", " ").upper()
            section_text = f"=== {header} ===\n{body}"
            if total + len(section_text) + 2 > max_chars:
                remaining = max_chars - total - 30  # 30 chars for tail marker
                if remaining > 2000:
                    parts.append(section_text[:remaining] + "\n[... truncated ...]")
                break
            parts.append(section_text)
            total += len(section_text) + 2
        if parts:
            structured_output = "\n\n".join(parts)

    # Accept the structured extraction only if it is BOTH long enough and
    # actually contains financial figures.
    #
    # Length alone was the test until 2026-08-28, and it silently gutted
    # the entire earnings evidence source. `extract_key_sections` matches
    # the phrase "financial statements", which also appears in the
    # auditor's opinion letter — "...the related notes (collectively
    # referred to as the financial statements)". That letter is prose, it
    # is several thousand characters long, and it therefore cleared a
    # 3,000-character bar comfortably. Clearing the bar SUPPRESSED the
    # density-seeking fallback below, which is the code that would have
    # found the real tables.
    #
    # Measured over the 68 filings cached on the production box: 56 of
    # them extracted fewer than 20 dollar amounts, and 12 — including
    # MSFT, AAPL, GOOGL, BAC, CVX and NFLX — extracted exactly ZERO. The
    # earnings analyst had never seen a single number for those names.
    #
    # A financial statement is defined by its figures, so that is what is
    # tested. Same pattern `_find_financial_dense_region` scores with, so
    # "dense enough to keep" and "dense enough to seek" mean one thing.
    MIN_STRUCTURED_SIZE = 3000
    MIN_STRUCTURED_FIGURES = 40
    figure_count = len(FINANCIAL_FIGURE_RE.findall(structured_output))
    if structured_output and len(structured_output) >= MIN_STRUCTURED_SIZE and figure_count >= MIN_STRUCTURED_FIGURES:
        logger.info(
            "Extracted %d section(s) from filing → %d chars, %d figures (down from %d)",
            len(sections),
            len(structured_output),
            figure_count,
            len(text),
        )
        return structured_output

    if structured_output and len(structured_output) >= MIN_STRUCTURED_SIZE:
        logger.warning(
            "Structured extraction produced %d chars but only %d financial "
            "figures (need %d) — this is narrative, not statements. "
            "Falling back to the density-seeking slice.",
            len(structured_output),
            figure_count,
            MIN_STRUCTURED_FIGURES,
        )

    # Fallback: truncated full text. The naive "first max_chars" slice
    # is wrong for iXBRL 10-Q filings — the front of the cleaned text is
    # typically cover page + TOC + XBRL boilerplate, and the actual
    # financial tables live 30-50% into the document. R6 log audit
    # found 58 cases where this fallback fed the earnings LLM nothing
    # but XBRL labels and got "data quality: CRITICAL" back. Instead,
    # find the densest $-amount / numeric-table region and slice
    # around it.
    if len(text) > max_chars:
        slice_start = find_financial_dense_region(text, max_chars)
        logger.info(
            "Structured extraction too sparse (%d chars); falling back to truncated full text "
            "(%d → %d chars, slice @ %d)",
            len(structured_output),
            len(text),
            max_chars,
            slice_start,
        )
        text = text[slice_start : slice_start + max_chars] + "\n\n[... truncated ...]"
    return text


def find_financial_dense_region(text: str, window: int) -> int:
    """Return the start index of the `window`-char slice with the
    highest density of financial-table content.

    Heuristic: count dollar-prefixed amounts (`$1,234`), bare
    comma-separated thousands (`1,234,567`), and parenthesized
    negatives (`(123)`). These patterns are dense in income
    statements, balance sheets, and cash flow statements; sparse
    in cover pages, TOCs, and XBRL taxonomy boilerplate.

    Returns 0 when text is shorter than window, or when no
    candidate slice is meaningfully denser than the head — in
    which case the original behavior (head slice) is preserved.
    """
    if len(text) <= window:
        return 0
    # Slide in 10 steps across the document; cheap, ~10 regex passes.
    pattern = FINANCIAL_FIGURE_RE
    step = max(window // 10, 1000)
    scores: list[tuple[int, int]] = []  # (count, start)
    for start in range(0, len(text) - window + 1, step):
        chunk = text[start : start + window]
        scores.append((len(pattern.findall(chunk)), start))
    if not scores:
        return 0
    best_count, best_start = max(scores, key=lambda x: x[0])
    head_count = scores[0][0]
    # Only relocate if the densest region is meaningfully richer than
    # the head — guards against the "all chunks are equally barren"
    # case where moving the slice doesn't help. 2× threshold tuned
    # against the audit's 58 affected filings: their head chunks had
    # ~5-15 matches while mid-document chunks had 50-300.
    if best_count >= 2 * max(head_count, 5):
        return best_start
    return 0


def extract_key_sections(text: str) -> dict[str, str]:
    """Locate financial / MD&A / risk-factor section bodies via regex.

    Filings typically carry a table of contents listing 'Item 1. ...',
    'Item 2. ...' near the top — those are pointers, not the section
    bodies themselves. We prefer matches beyond the first ~15K chars
    (past the TOC) when multiple matches exist. Body extends from the
    header to the next detected section/stop marker.
    """
    # Each entry: (label, pattern, strategy)
    # - "first":    the pattern matches a distinctive heading and the
    #               first occurrence is the real one.
    # - "skip_toc": the pattern matches a section title that DOES appear
    #               in a TOC — prefer the first occurrence past ~15K
    #               chars. Originally only used for mdna / risk_factors,
    #               but R6 log audit (May 2026) found 58 filings where
    #               financial_statements matched a TOC-style entry and
    #               produced a tiny (200-700 char) body — affected
    #               PG / SBUX / V / ABT / AMZN / CAT / COP / LLY 10-Qs
    #               whose internal "Index to Financial Statements"
    #               navigation listed "Consolidated Statements of
    #               Operations" before the real section started. Switched
    #               to skip_toc so we land on the actual table.
    patterns = [
        (
            "financial_statements",
            re.compile(r"(?im)(?:condensed\s+)?consolidated\s+statements?\s+of\s+(?:operations?|income|earnings)\b"),
            "skip_toc",
        ),
        (
            "mdna",
            re.compile(
                # [\u2019'] accepts both ASCII apostrophe and the curly
                # quote U+2019 that SEC HTML filings commonly use.
                r"(?im)^\s*(?:item\s*[27]\.?)\s*management[\u2019']?s?\s+discussion"
            ),
            "skip_toc",
        ),
        ("risk_factors", re.compile(r"(?im)^\s*(?:item\s*1a\.?)\s*risk\s+factors"), "skip_toc"),
    ]
    stop_pattern = re.compile(
        r"(?im)^\s*(?:item\s*\d+[a-z]?\.?\s|"
        r"signatures?\s*$|"
        r"exhibit\s+index|"
        r"part\s+(?:i|ii|iii|iv)\b)"
    )
    all_stops = sorted(m.start() for m in stop_pattern.finditer(text))

    found: dict[str, str] = {}
    for label, pat, strategy in patterns:
        matches = list(pat.finditer(text))
        if not matches:
            continue
        if strategy == "first":
            chosen = matches[0]
        else:  # skip_toc
            chosen = next(
                (m for m in matches if m.start() >= 15000),
                matches[-1],
            )
        body_start = chosen.end()
        # Next stop after (body_start + 200) — don't let the header's
        # own "Item X" mention terminate its own body.
        next_stop = None
        for stop in all_stops:
            if stop > body_start + 200:
                next_stop = stop
                break
        body = text[body_start:next_stop].strip() if next_stop else text[body_start:].strip()
        # Low threshold — 10-Q Risk Factors sections often read "No
        # material changes from 10-K" in ~200-400 chars, which is still
        # useful information (confirms no new risks flagged). Below 150
        # is almost certainly a false-positive match.
        if len(body) >= 150:
            found[label] = body
    return found
