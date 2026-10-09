"""Classifier: did Alpaca refuse a new-order POST? Moved verbatim out of order_desk."""

from __future__ import annotations


def _is_terminal_submission_rejection(exc: BaseException) -> bool:
    """True when Alpaca's OWN answer to a NEW-ORDER POST says it was refused.

    SEPARATE from `_is_terminal_broker_rejection` on purpose. That one is the
    retry classifier for STOP PLACEMENT (board item 129): its job is only to
    decide whether another attempt is worth making, and a false "terminal"
    there costs at worst an alert two seconds early. This one decides whether
    `submit_order` SWALLOWS the failure and hands the caller a
    `rejected_by_broker` result instead of raising — so a false positive here
    means the desk records an order as refused while the broker may actually
    be holding it. The two questions are not the same question, and the code
    set was inherited rather than re-checked when #786 reused it.

    Re-checked 2026-09-30 against Alpaca's own published documentation for
    the create-order endpoint:

      * https://docs.alpaca.markets/reference/postorder — the endpoint's own
        reference lists exactly three responses: 200 (the created order),
        403 ("Buying power or shares is not sufficient.") and 422 ("Input
        parameters are not recognized."). 404 is NOT among them; neither
        is 400.
      * https://alpaca.markets/learn/how-to-fix-common-trading-api-errors-at-alpaca
        — Alpaca's own troubleshooting guide for this API lists 422 for
        order-parameter errors and 403 for account/risk-control refusals,
        and names 400 only in a FUNDING flow, never for POST /v2/orders.

    So 422 is the one code both sources establish as "this order submission
    was rejected", and it is the only one that short-circuits here.

      * 404 is DROPPED. Nothing fetched shows Alpaca returning it for a
        creation POST; where 404 does appear in this API it means an
        addressed resource was not found (looking up / cancelling /
        replacing an order by id), which on a submission would read far more
        like "created, then not found" than "definitely rejected" — the
        dangerous direction, because treating a LIVE order as rejected leaves
        real exposure the desk believes it does not have. It is not kept on
        inheritance alone.
      * 400 is DROPPED for the same reason: not documented for this endpoint
        by either source above.
      * 403 is deliberately NOT ADDED even though it IS documented here. It
        is a different failure (buying power / shortability / PDT), the
        callers' existing exception paths already handle it, and widening
        what `submit_order` swallows is not what this classifier is for.

    A code that is not established simply keeps the pre-#786 behaviour: the
    exception propagates and the caller's own recovery runs.
    """
    return getattr(exc, "status_code", None) == 422
