"""Reading back the seat stances recorded for one decision.

A plain function over plain collaborators — an open sqlite connection and
the process lock — so the conviction ledger can read stances without
owning, importing or reaching back into the `Database` object. The WRITE
side stays where its only caller expects it; this is the read.
"""

import logging

logger = logging.getLogger(__name__)

SEAT_STANCE_KIND = "seat_stance"


def read_seat_stances(conn, lock, *, decision_id: str, symbol: str | None = None) -> list:
    """Reconstruct `SeatStance` objects for one decision (optionally one
    symbol). Malformed rows are skipped with a warning rather than taking
    down the read — same tolerance as every other evidence reader here."""
    from src.conviction_ledger import SeatStance

    if not decision_id:
        return []
    sql = "SELECT symbol, evidence_json FROM specialist_evidence WHERE decision_id = ? AND kind = ?"
    params: list = [decision_id, SEAT_STANCE_KIND]
    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol.strip().upper())
    sql += " ORDER BY id"
    with lock:
        rows = conn.execute(sql, tuple(params)).fetchall()
    import json as _json

    out = []
    for row in rows:
        try:
            data = _json.loads(row["evidence_json"])
            out.append(
                SeatStance(
                    seat=data.get("seat") or "",
                    symbol=data.get("symbol") or row["symbol"] or "",
                    stance=data.get("stance") or "",
                    conviction=data.get("conviction") or "medium",
                    nominated=bool(data.get("nominated")),
                    observation=data.get("observation") or "",
                )
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Skipping malformed seat_stance row: %s", e)
    return out
