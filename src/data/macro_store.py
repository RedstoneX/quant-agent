"""Macro state persistence — yesterday's regime call for shift detection."""

import json
import logging
import os
from pathlib import Path

from src.models import SECTOR_DIRECTIONS, normalize_sector_stance
from src.util.time import et_today

logger = logging.getLogger(__name__)

# Statistical-release prints a regime call is actually based on.
# Daily market quotes (VIX, treasuries, dollar, OAS) reprint every
# session; treating those as expiry would invent churn and kill the
# cross-day remember path. CPI / unemployment / claims are the prints
# that can change under an unchanged regime label.
_SERIES_PRINT_FIELDS: tuple[tuple[str, str], ...] = (
    ("inflation", "headline_cpi_yoy"),
    ("inflation", "core_cpi_yoy"),
    ("inflation", "pce_yoy"),
    ("unemployment", "current"),
    ("jobless_claims", "current"),
)
_SERIES_PRINT_IDS = frozenset({
    "CPIAUCSL", "CPILFESL", "PCEPI", "UNRATE", "ICSA",
})


def series_prints_from_summary(summary, freshness=None) -> dict:
    """Fingerprint the actual FRED prints a macro call was based on.

    Values are the current readings. Observation dates come from
    ``SeriesFreshness.latest_observation`` when the provider recorded
    them this fetch. Empty on anything unreadable — never invented.
    """
    values: dict[str, float] = {}
    if isinstance(summary, dict):
        for group, field in _SERIES_PRINT_FIELDS:
            block = summary.get(group)
            if not isinstance(block, dict):
                continue
            raw = block.get(field)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                continue
            values[f"{group}.{field}"] = float(raw)
    observations: dict[str, str] = {}
    recorded = freshness if isinstance(freshness, dict) else {}
    for series_id, item in recorded.items():
        if str(series_id) not in _SERIES_PRINT_IDS:
            continue
        obs = getattr(item, "latest_observation", None)
        if obs is None:
            continue
        text = str(obs).strip()[:10]
        if len(text) == 10:
            observations[str(series_id)] = text
    return {"values": values, "observations": observations}


def series_prints_changed(stored, live) -> bool:
    """True when a live FRED fetch shows a different print than the snapshot.

    A missing live series is not a change (failed detector). A stored
    fingerprint with no values/observations cannot claim a change either
    — that would invent churn from an old snapshot that never recorded
    prints.
    """
    if not isinstance(stored, dict) or not isinstance(live, dict):
        return False
    stored_vals = stored.get("values") if isinstance(stored.get("values"), dict) else {}
    live_vals = live.get("values") if isinstance(live.get("values"), dict) else {}
    for key, stored_val in stored_vals.items():
        if key not in live_vals:
            continue
        try:
            if float(live_vals[key]) != float(stored_val):
                return True
        except (TypeError, ValueError):
            continue
    stored_obs = stored.get("observations") if isinstance(stored.get("observations"), dict) else {}
    live_obs = live.get("observations") if isinstance(live.get("observations"), dict) else {}
    for series_id, stored_date in stored_obs.items():
        live_date = str(live_obs.get(series_id) or "").strip()[:10]
        stored_text = str(stored_date or "").strip()[:10]
        if live_date and stored_text and live_date > stored_text:
            return True
    return False


def _atomic_write(path: Path, data: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(data)
    os.replace(str(tmp), str(path))


# MacroAnalysis.sector_guidance is a LIST of {sector, stance, reason} where
# stance ∈ overweight|neutral|underweight. Every consumer of the persisted
# state (`_missed_ops_macro_sector_map`, thesis-health, the PM's evidence
# registry) wants a DICT keyed by sector with bullish|neutral|bearish values.
# Convert once, on write, through the shared map in `src.models`.


def _normalize_sector_guidance(raw) -> dict[str, str]:
    """[{sector, stance, reason}, ...] → {sector: bullish|neutral|bearish}.

    Tolerates the already-normalized dict shape (idempotent) and drops
    anything unrecognized. Never raises — a malformed guidance block must
    not take down the macro save.
    """
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        for sector, direction in raw.items():
            if isinstance(direction, str) and direction in SECTOR_DIRECTIONS:
                out[str(sector)] = direction
        return out
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        sector = item.get("sector")
        direction = normalize_sector_stance(item.get("stance"))
        if sector and direction:
            out[str(sector)] = direction
    return out


class MacroStore:
    def __init__(self, data_dir: str = "data/macro"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.last_state_path = self.data_dir / "last_state.json"
        self.history_path = self.data_dir / "history.json"

    def load_last_state(self) -> dict | None:
        """Return the most recently persisted macro state, or None on first run / corrupt file."""
        if not self.last_state_path.exists():
            return None
        try:
            return json.loads(self.last_state_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load last macro state: %s", e)
            return None

    def save_last_state(self, analysis: dict, series_prints: dict | None = None) -> None:
        """Persist the shift-relevant subset PLUS the live fields needed
        to re-validate as MacroAnalysis on a later tick.

        2026-09-16: dropping `reasoning_chain` made every intraday Phase 13
        re-parse a ValidationError (`Field required`) even when morning's
        call was valid. Compact dict `sector_guidance` stays for existing
        readers (thesis-health, missed-ops). The live list (with reasons)
        is stored as `sector_guidance_rows` so we do not invent reasons
        when rehydrating. We do not invent a chain if the caller never
        had one.

        ``series_prints`` is the FRED value/timestamp fingerprint the
        regime call actually saw. Expiry later compares live prints
        against this, not the regime label string.
        """
        if not isinstance(analysis, dict):
            return
        sg_raw = analysis.get("sector_guidance")
        snapshot = {
            "date": str(et_today()),
            "regime": analysis.get("regime"),
            "confidence": analysis.get("confidence"),
            "equity_outlook": analysis.get("equity_outlook"),
            "summary": analysis.get("summary"),
            "position_guidance": analysis.get("position_guidance"),
            "sector_guidance": _normalize_sector_guidance(sg_raw),
            "regime_shift": analysis.get("regime_shift"),
            "shift_reason": analysis.get("shift_reason") or "",
            "key_observations": analysis.get("key_observations") or [],
            "risk_factors": analysis.get("risk_factors") or [],
            "bull_triggers": analysis.get("bull_triggers") or [],
            "bear_triggers": analysis.get("bear_triggers") or [],
            "alignment_with_news": analysis.get("alignment_with_news") or "",
            # Board item 119: how much of the FRED set this regime call was
            # actually formed on. This snapshot is what midday/close/intra
            # read back as `carried_from_morning`, what later DAYS read back
            # as `remembered`, and what feeds the PM's 7-day regime
            # trajectory — so a verdict formed on holes must not be
            # laundered into a complete-looking one by being written here
            # without the stamp. Defaults to "unknown" rather than
            # "complete" for the same reason as on the model: an unstamped
            # caller makes no claim, and absence is not proof of coverage.
            "coverage_state": analysis.get("coverage_state") or "unknown",
            "coverage_note": analysis.get("coverage_note") or "",
        }
        if isinstance(series_prints, dict) and (
            series_prints.get("values") or series_prints.get("observations")
        ):
            snapshot["series_prints"] = {
                "values": dict(series_prints.get("values") or {}),
                "observations": dict(series_prints.get("observations") or {}),
            }
        chain = analysis.get("reasoning_chain")
        if isinstance(chain, dict) and chain:
            snapshot["reasoning_chain"] = chain
        if isinstance(sg_raw, list):
            snapshot["sector_guidance_rows"] = sg_raw
        elif isinstance(sg_raw, dict):
            # Already compact; rows can be rebuilt mechanically without
            # invented reasons (empty reason).
            snapshot["sector_guidance_rows"] = [
                {
                    "sector": sector,
                    "stance": (
                        "overweight" if direction == "bullish"
                        else "underweight" if direction == "bearish"
                        else "neutral"
                    ),
                    "reason": "",
                }
                for sector, direction in sg_raw.items()
                if isinstance(direction, str)
            ]
        _atomic_write(self.last_state_path, json.dumps(snapshot, indent=2, ensure_ascii=False))
        logger.info("Saved macro last state → %s (regime=%s)",
                    self.last_state_path, snapshot.get("regime"))
        # Append to history so future PM runs can see the 7-day regime trajectory.
        self._append_history(snapshot)

    def _append_history(self, snapshot: dict, keep_days: int = 21) -> None:
        """Maintain a rolling list (latest last). Dedup by date (today overwrites)."""
        history: list[dict] = []
        if self.history_path.exists():
            try:
                history = json.loads(self.history_path.read_text()) or []
            except (json.JSONDecodeError, OSError):
                history = []
        today = snapshot.get("date")
        history = [h for h in history if h.get("date") != today]
        history.append(snapshot)
        history = sorted(history, key=lambda x: x.get("date", ""))[-keep_days:]
        _atomic_write(self.history_path, json.dumps(history, indent=2, ensure_ascii=False))

    def load_history(self, days: int = 7) -> list[dict]:
        """Return the most recent `days` macro-state snapshots, oldest first."""
        if not self.history_path.exists():
            # Legacy fallback: at least surface today's last_state as a 1-element history
            last = self.load_last_state()
            return [last] if last else []
        try:
            history = json.loads(self.history_path.read_text()) or []
        except (json.JSONDecodeError, OSError):
            return []
        return history[-days:]
