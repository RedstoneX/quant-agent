import json
import logging
from pathlib import Path

from pydantic import ValidationError

from src.agents.base import AgentResult, BaseAgent
from src.models import SmartMoneyFinding, SmartMoneyObservation

logger = logging.getLogger(__name__)
PROMPT_PATH = Path(__file__).parent.parent.parent / "config" / "prompts" / "smart_money_analyst.md"


class SmartMoneyAnalystAgent(BaseAgent):
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

    def analyze(self, observations: list[SmartMoneyObservation]) -> tuple[list[SmartMoneyFinding], AgentResult | None, str | None]:
        if not observations:
            return [], None, None
        result = self.run(observations=observations)
        parsed = result.parse_json()
        if not isinstance(parsed, dict) or not isinstance(parsed.get("findings"), list):
            return [], result, "analysis_parse_error"
        raw_findings = parsed["findings"]
        by_symbol: dict[str, list[SmartMoneyObservation]] = {}
        for item in observations:
            by_symbol.setdefault(item.symbol, []).append(item)
        findings: list[SmartMoneyFinding] = []
        invalid = 0
        for raw in raw_findings if isinstance(raw_findings, list) else []:
            try:
                symbol = str(raw.get("symbol", "")).upper()
                # The LLM may synthesize, but never author source facts.
                raw["observations"] = [o.model_dump() for o in by_symbol.get(symbol, [])]
                finding = SmartMoneyFinding(**raw)
                if finding.observations:
                    findings.append(finding)
            except (ValidationError, AttributeError) as exc:
                logger.warning("Dropping invalid smart-money finding: %s", exc)
                invalid += 1
        if invalid and not findings:
            return [], result, "analysis_schema_error"
        return findings, result, "analysis_partial_schema_error" if invalid else None
