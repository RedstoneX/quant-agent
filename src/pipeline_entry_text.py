"""Plain-language reprice endings, moved out of ``pipeline_entry_orders`` unchanged."""

#: Plain-language endings for the single-shot reprice, keyed by the
#: `repeg_outcome` written into the entry spec. Read by the end-of-session
#: cancel alert so the owner is told what WAS and WAS NOT tried, in words —
#: never colour or an emoji standing alone; severity must survive being
#: read as plain text.
_REPEG_OUTCOME_TEXT = {
    "disabled": "automatic repricing is switched off (execution.repeg_enabled)",
    "no_room": "it was already sitting at the slippage ceiling, so there was "
               "no legal price left to reprice to",
    "not_at_exchange": "the exchange had not acknowledged it within the "
                       "window, so a reprice was NOT attempted (Alpaca "
                       "rejects a replace on an order that has not reached "
                       "the exchange)",
    "market_within_limit": "the market was at or below the limit, so it "
                           "should have filled without a reprice",
    "replaced": "it was repriced ONCE to a market-crossing price",
    "replace_rejected": "one reprice was attempted and the broker refused it",
    "replace_unknown": "one reprice was attempted and its outcome could not "
                       "be read back from the broker",
    "wal_refused": "a reprice was NOT attempted because its recovery record "
                   "could not be written first",
    "wait_failed": "a reprice was NOT attempted because the order's status "
                   "could not be read",
    "quote_unavailable": "a reprice was NOT attempted because no quote was "
                         "available",
    "unpriced": "a reprice was NOT attempted (no reference or limit price)",
}
