# QAMC session trades and intraday chart verification

Captured on 2026-08-21 UTC against implementation commit `36276b574bb18149135973edea4206dae9945c32`.

The desktop (1440×1200) and iPad (820×1180) captures verify the Morning session remains pinned after clicking the executed MRVL BUY, the chart switches to MRVL, and the `5m Today` timeframe renders the relevant BUY marker. The chart visibly distinguishes the live price (`$237.07`) from the previous close (`$251.01`) and retains quote freshness text.

Browser verification used a local GET-only fixture/proxy: read-only session and execution data came from the existing branch preview, while deterministic MRVL price bars and quote data reproduced the reported live-versus-history reconciliation case. Both viewports completed with zero console or page errors.

Verification results:

- Backend: 2,030 passed.
- Frontend: 55 passed.
- Production frontend build: passed.
