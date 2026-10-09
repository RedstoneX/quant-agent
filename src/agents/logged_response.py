"""Parse an agent's STORED response exactly as the live agent parsed it.

A row from the agent-response journal carries the model's raw text
(`full_response`). Re-reading it through `AgentResult.parse_json` means the
replay sees the same fenced / prose-wrapped JSON handling the live seat saw,
so a replayed verdict can never differ from the one that was acted on.

Moved byte-for-byte from `TradingPipeline._parse_logged_agent_response`
(2026-10-01) so readers of the journal need no pipeline to parse a row.
"""

from src.agents.base import AgentResult


def parse_logged_agent_response(row: dict):
    """Parse stored fenced/prose-wrapped JSON exactly as live agents do."""

    return AgentResult(
        raw_text=row.get("full_response") or "",
        tokens_used=0,
        model=row.get("model") or "",
    ).parse_json()
