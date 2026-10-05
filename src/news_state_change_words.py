"""Words too common in a headline to count as evidence a state change happened.

Lifted verbatim out of `src/agents/news_analyst.py`, which re-exports it.
"""
_STATE_CHANGE_STOPWORDS = frozenset({
    "from", "into", "with", "that", "this", "these", "those",
    "have", "been", "will", "would", "could", "should",
    "change", "state", "event", "today", "more", "less",
    "than", "some", "many", "much", "also", "very",
    "after", "before", "during", "while", "about", "against", "between",
    "said", "says", "reports", "reported", "according",
})
