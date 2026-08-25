#!/usr/bin/env python3
"""Read-only-provider preflight for the SEC Form 4 Smart Money source.

This performs no model call and no broker write. It refreshes the local SEC
cache, reads the deterministically surviving observations, and reports only
counts/provenance identifiers suitable for production commissioning.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import SmartMoneyConfig  # noqa: E402
from src.data.smart_money import SECForm4Provider  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config", type=Path,
        default=PROJECT_ROOT / "config" / "settings.yaml",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--max-filings", type=int,
        help="Bound this commissioning run below the configured refresh cap.",
    )
    parser.add_argument(
        "--deadline", type=float,
        help="Bound this commissioning run below the configured deadline.",
    )
    args = parser.parse_args()

    raw = yaml.safe_load(args.config.read_text()) or {}
    cfg = SmartMoneyConfig.model_validate(raw.get("smart_money") or {})
    provider = SECForm4Provider(
        search_url=cfg.search_url,
        archives_url=cfg.archives_url,
        data_dir=cfg.data_dir,
        user_agent=cfg.user_agent,
        request_timeout_s=cfg.request_timeout_s,
        refresh_deadline_s=args.deadline or cfg.refresh_deadline_s,
        requests_per_second=cfg.requests_per_second,
        lookback_days=cfg.lookback_days,
        max_filings_per_refresh=args.max_filings or cfg.max_filings_per_refresh,
        max_observations=cfg.max_observations,
        min_transaction_value_usd=cfg.min_transaction_value_usd,
        external_min_transaction_value_usd=cfg.external_min_transaction_value_usd,
        cluster_window_days=cfg.cluster_window_days,
        min_cluster_owners=cfg.min_cluster_owners,
    )
    refresh = provider.refresh()
    observations, provider_error = provider.fetch([])
    summary = {
        "refresh": refresh,
        "provider_error": provider_error,
        "surviving_observations": len(observations),
        "symbols": sorted({item.symbol for item in observations}),
        "admission_source_eligible_symbols": sorted({
            item.symbol for item in observations
            if bool(getattr(item, "admission_eligible", False))
        }),
        "accessions": sorted({
            str(getattr(item, "accession_number", ""))
            for item in observations if getattr(item, "accession_number", None)
        })[:20],
        "llm_calls": 0,
    }
    if args.json:
        print(json.dumps(summary, sort_keys=True))
    else:
        print("SEC Form 4 source preflight")
        for key, value in summary.items():
            print(f"{key}: {value}")
    return 1 if provider_error and not observations else 0


if __name__ == "__main__":
    raise SystemExit(main())
