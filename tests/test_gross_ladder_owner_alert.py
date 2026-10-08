"""Board item 182: the owner alert stays put and the ladder sentence names the real rung.

Moved verbatim out of `tests/test_gross_exposure_ladder.py` so that file stops growing.
"""

BASE_X = 2.0


def test_the_owner_alert_stays_put_and_the_sentence_names_the_real_rung():
    """Board item 182, after the change was reverted.

    A first draft replaced `GROSS_LADDER_ALERT_PCT` with
    `min(threshold for threshold, _ in GROSS_LADDER)`, arguing that freezing
    the alert would mean reaching a new floor in silence. That is false of
    this code. The trigger is `drawdown <= GROSS_LADDER_ALERT_PCT`, which is
    MONOTONE: once the drawdown passes the threshold the alert is true at
    every deeper drawdown too. Freezing it at -20 alerts from -20 onward
    INCLUDING at a hypothetical -30 rung; tying it to the deepest rung moves
    the alert to -30 and buys silence across -20% to -30%, a band in which
    the ladder is cutting the book to its floor multiple. The change made the
    desk quieter, so it was reverted.

    What was really wrong was the SENTENCE: with a deeper rung present, a
    -22% message claiming "the desk is at its most de-levered setting" is
    untrue. This test pins both halves — the trigger does not move, and the
    prose names the rung the ladder is actually on.
    """
    import src.risk.gross_ladder as rules_mod
    from src.risk.rules import GROSS_LADDER_ALERT_PCT, resolve_gross_ceiling

    # SOURCED 2026-09-30, not inferred off the table and not a round number
    # of this desk's own choosing: MiFID Org Regulation Article 62(1), as
    # COBS 16A.4.3UK, requires the client be told at a 10% depreciation.
    assert GROSS_LADDER_ALERT_PCT == -10.0
    assert GROSS_LADDER_ALERT_PCT != min(t for t, _ in rules_mod.GROSS_LADDER)

    assert resolve_gross_ceiling(-10.0, base_x=BASE_X).alert_owner
    assert resolve_gross_ceiling(-25.0, base_x=BASE_X).alert_owner
    assert not resolve_gross_ceiling(-9.9, base_x=BASE_X).alert_owner

    # At today's table the deepest rung IS the alert, so the sentence may
    # claim the floor.
    at_floor = resolve_gross_ceiling(-22.0, base_x=BASE_X).reason
    assert "most de-levered setting" in at_floor

    # Add a DEEPER rung and both properties must hold: the owner is still
    # told at -22 (no silent band), and the sentence stops claiming the floor
    # while naming the rung actually in force and the deeper one below it.
    deeper = rules_mod.GROSS_LADDER + ((-30.0, 0.25),)
    original = rules_mod.GROSS_LADDER
    try:
        rules_mod.GROSS_LADDER = deeper
        mid = rules_mod.resolve_gross_ceiling(-22.0, base_x=BASE_X)
        assert mid.alert_owner, "freezing the alert must not create silence"
        assert "NOT its most de-levered setting" in mid.reason
        assert "-20% rung" in mid.reason  # the rung in force, not the alert
        assert "30%" in mid.reason
        floor = rules_mod.resolve_gross_ceiling(-31.0, base_x=BASE_X)
        assert floor.alert_owner
        assert "deepest 30% rung" in floor.reason
        assert "NOT its most de-levered" not in floor.reason
    finally:
        rules_mod.GROSS_LADDER = original
