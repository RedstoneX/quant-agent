#!/usr/bin/env python3
"""Re-verify `_PRICING_OPENROUTER` against OpenRouter's live catalog.

    python ops/model_policy/verify_pricing.py          # check, exit 1 on drift
    python ops/model_policy/verify_pricing.py --json

Every row in `src/cost_table.py:_PRICING_OPENROUTER` is a hand-copied rate,
and a hand-copied rate goes stale silently — the failure mode is a cost
report that looks confident and is wrong, which is worse than "$?.??". This
makes the table's provenance a one-command check rather than a claim in a
comment.

Exit code 0 when every pinned rate matches the catalog, 1 on any drift or
missing model. Reads a public endpoint; no credential involved.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.cost_table import _OPENROUTER_PRICING_URL, _PRICING_OPENROUTER  # noqa: E402

# Rates are quoted to 6 decimals per token; compare at per-million scale with
# a tolerance that absorbs float round-trip but nothing economically real.
TOLERANCE = 1e-6


def fetch_catalog() -> dict[str, dict[str, float]]:
    opener = urllib.request.build_opener()
    with opener.open(_OPENROUTER_PRICING_URL, timeout=30) as resp:
        payload = json.loads(resp.read().decode())
    out: dict[str, dict[str, float]] = {}
    for entry in payload.get("data") or []:
        pricing = entry.get("pricing") or {}
        try:
            out[entry["id"]] = {
                "input": float(pricing["prompt"]) * 1e6,
                "output": float(pricing["completion"]) * 1e6,
            }
        except (KeyError, TypeError, ValueError):
            continue
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    catalog = fetch_catalog()
    rows = []
    drift = 0
    for model, pinned in sorted(_PRICING_OPENROUTER.items()):
        live = catalog.get(model)
        if live is None:
            rows.append({"model": model, "status": "MISSING",
                         "pinned": pinned, "live": None})
            drift += 1
            continue
        ok = (
            abs(live["input"] - pinned["input"]) <= TOLERANCE
            and abs(live["output"] - pinned["output"]) <= TOLERANCE
        )
        rows.append({"model": model, "status": "OK" if ok else "DRIFT",
                     "pinned": pinned, "live": live})
        if not ok:
            drift += 1

    if args.json:
        print(json.dumps({"catalog_models": len(catalog), "drift": drift,
                          "rows": rows}, indent=2))
    else:
        for r in rows:
            live = r["live"]
            live_txt = (
                "not in catalog" if live is None
                else f"in=${live['input']:.3f}/M out=${live['output']:.3f}/M"
            )
            print(f"  {r['status']:<7} {r['model']:<34} "
                  f"pinned in=${r['pinned']['input']:.3f}/M "
                  f"out=${r['pinned']['output']:.3f}/M | catalog {live_txt}")
        print(f"\n{len(rows) - drift}/{len(rows)} pinned rates match "
              f"OpenRouter's catalog ({len(catalog)} models priced).")
        print("PRICING PROVENANCE: OK" if drift == 0
              else f"PRICING PROVENANCE: DRIFT on {drift} model(s)")
    return 1 if drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
