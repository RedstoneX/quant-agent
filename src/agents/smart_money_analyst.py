import json
import hashlib
import logging
import os
import threading
from pathlib import Path

from pydantic import ValidationError

from src.agents.base import AgentResult, BaseAgent
from src.models import SmartMoneyFinding, SmartMoneyObservation

logger = logging.getLogger(__name__)
PROMPT_PATH = Path(__file__).parent.parent.parent / "config" / "prompts" / "smart_money_analyst.md"
DEFAULT_SYNTHESIS_CACHE = Path("data/smart_money/synthesis_cache.json")
_CACHE_LOCK = threading.Lock()


class SmartMoneyAnalystAgent(BaseAgent):
    def __init__(self, *args, synthesis_cache_path: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.synthesis_cache_path = Path(
            synthesis_cache_path or DEFAULT_SYNTHESIS_CACHE
        )

    @property
    def name(self) -> str: return "smart_money_analyst"

    @property
    def system_prompt(self) -> str:
        return PROMPT_PATH.read_text() if PROMPT_PATH.exists() else "Synthesize smart-money evidence as JSON."

    def build_user_message(self, **kwargs) -> str:
        observations = kwargs.get("observations", [])
        return "Validated source observations:\n" + json.dumps(
            [o.model_dump(mode="json") for o in observations], indent=2,
        )

    def _cache_path(self) -> Path:
        return Path(getattr(self, "synthesis_cache_path", DEFAULT_SYNTHESIS_CACHE))

    @staticmethod
    def _evidence_hash(observations: list[SmartMoneyObservation]) -> str:
        # Hash source facts and deterministic eligibility, not fields that
        # naturally change as the same filing ages or moves into a run-scoped
        # trading context. Otherwise unchanged evidence would burn a fresh
        # model call every day merely because disclosure_age_days advanced.
        stable_fields = (
            "symbol", "stream", "actor", "actor_cik", "actor_roles",
            "joint_owner_ciks", "direction", "transaction_date",
            "disclosure_date", "accepted_at", "source_url",
            "accession_number", "filing_form", "transaction_code",
            "transaction_row", "security_title", "shares",
            "price_per_share", "transaction_value_usd",
            "post_transaction_shares", "ownership_nature", "amendment",
            "late_filing", "is_10b5_1", "listed_exchange",
            "admission_eligible", "transient_admission_eligible",
        )
        rows = []
        for observation in observations:
            dumped = observation.model_dump(mode="json")
            rows.append({field: dumped.get(field) for field in stable_fields})
        rows.sort(key=lambda row: (
            str(row.get("symbol", "")),
            str(row.get("accession_number", "")),
            str(row.get("transaction_row", "")),
            str(row.get("actor_cik", "")),
        ))
        encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _load_cache(self) -> dict:
        path = self._cache_path()
        try:
            payload = json.loads(path.read_text())
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_cache(self, payload: dict) -> None:
        path = self._cache_path()
        with _CACHE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
            os.replace(tmp, path)

    @staticmethod
    def _stance_matches_source(
        stance: str, observations: list[SmartMoneyObservation],
    ) -> bool:
        directions = {
            item.direction for item in observations
            if item.direction in {"buy", "sell"}
        }
        if directions == {"buy"}:
            return stance in {"bullish", "neutral"}
        if directions == {"sell"}:
            return stance in {"bearish", "neutral"}
        if directions == {"buy", "sell"}:
            return stance in {"mixed", "neutral"}
        return stance == "neutral"

    def _parse_findings(
        self,
        raw_findings: list,
        observations: list[SmartMoneyObservation],
        evidence_hash: str,
    ) -> tuple[list[SmartMoneyFinding], int]:
        by_symbol: dict[str, list[SmartMoneyObservation]] = {}
        for item in observations:
            by_symbol.setdefault(item.symbol, []).append(item)
        findings: list[SmartMoneyFinding] = []
        invalid = 0
        for raw in raw_findings:
            try:
                symbol = str(raw.get("symbol", "")).upper()
                source_rows = by_symbol.get(symbol, [])
                stance = str(raw.get("stance", "")).lower()
                if not source_rows or not self._stance_matches_source(stance, source_rows):
                    logger.warning(
                        "Dropping direction-incompatible smart-money finding: "
                        "%s stance=%s", symbol, stance,
                    )
                    invalid += 1
                    continue
                normalized = dict(raw)
                normalized["observations"] = [o.model_dump() for o in source_rows]
                normalized["evidence_hash"] = evidence_hash
                finding = SmartMoneyFinding(**normalized)
                findings.append(finding)
            except (ValidationError, AttributeError, TypeError) as exc:
                logger.warning("Dropping invalid smart-money finding: %s", exc)
                invalid += 1
        return findings, invalid

    def analyze(self, observations: list[SmartMoneyObservation]) -> tuple[list[SmartMoneyFinding], AgentResult | None, str | None]:
        if not observations:
            return [], None, None
        evidence_hash = self._evidence_hash(observations)
        cached = self._load_cache().get(evidence_hash)
        if isinstance(cached, dict) and isinstance(cached.get("findings"), list):
            findings, invalid = self._parse_findings(
                cached["findings"], observations, evidence_hash,
            )
            if not invalid:
                raw_text = json.dumps(cached, sort_keys=True)
                return findings, AgentResult(
                    raw_text=raw_text,
                    tokens_used=0,
                    model=getattr(self, "model", "cached"),
                    user_message="[cached evidence hash]",
                    provider_requests=0,
                ), None
        result = self.run(observations=observations)
        parsed = result.parse_json()
        if not isinstance(parsed, dict) or not isinstance(parsed.get("findings"), list):
            return [], result, "analysis_parse_error"
        findings, invalid = self._parse_findings(
            parsed["findings"], observations, evidence_hash,
        )
        if invalid and not findings:
            return [], result, "analysis_schema_error"
        if not invalid:
            # Cache only fully validated synthesis. Source facts are reattached
            # from current observations on every cache hit.
            compact = {
                "findings": [{
                    "symbol": finding.symbol,
                    "stance": finding.stance,
                    "economic_role": finding.economic_role,
                    "summary": finding.summary,
                    "why_now": finding.why_now,
                } for finding in findings]
            }
            self._save_cache({**self._load_cache(), evidence_hash: compact})
        return findings, result, "analysis_partial_schema_error" if invalid else None
