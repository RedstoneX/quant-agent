"""VERBATIM copy of the two log bodies as they stood in
``src/portfolio_constructor/entry_stop/resolver.py`` at commit 8858100a, the
commit immediately BEFORE #1295 moved them into
``src/portfolio_constructor/absolute_floor_record.py``.

This file is a frozen historical record, not live code: it is never imported
or executed, only parsed. It exists because the move's faithfulness must stay
provable AFTER the move merges -- comparing the moved bodies against
``origin/main`` was green only while the move was unmerged and went red the
moment trunk became the moved version. Nothing here may ever be edited; a
change to the recorder that alters either body must fail
``tests/test_absolute_floor_record.py``.
"""

logger.info(
    "Constructor: %s %s stop $%.2f kept [%s] — it "
    "sits at the computed structural level $%.2f "
    "(%.2f ATRs %s the $%.2f entry). The %.2f x ATR "
    "noise band would have moved it to $%.2f, which "
    "is not a level anyone is defending, so the band "
    "does not apply.",
    side_label,
    symbol,
    stop_loss,
    STOP_RULE_LEVEL_HONOURED,
    level,
    abs(entry_price - stop_loss) / atr,
    side_word,
    entry_price,
    multiple,
    band_edge,
)

logger.info(
    "Constructor: %s %s stop $%.2f → $%.2f [%s] — it "
    "sits at the computed structural level $%.2f, "
    "which is real, but only %.2f ATRs from the "
    "$%.2f entry. A stop inside one ordinary day's "
    "range is a coin flip, so it is moved out to the "
    "%.2f x ATR floor — not to the %.2f x ATR noise "
    "band, which the level exempts it from.",
    side_label,
    symbol,
    stop_loss,
    honoured,
    STOP_RULE_ABSOLUTE_FLOOR,
    level,
    abs(entry_price - stop_loss) / atr,
    entry_price,
    floor_multiple,
    multiple,
)
