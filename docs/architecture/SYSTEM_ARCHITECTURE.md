# QAMC System Architecture — Current Boundaries

```text
Operator browser / iPad
        |
        v
Mission Control presentation layer
        |
        | current read-only seam
        v
Accepted Stage-2 API
        |
        v
quant-agent authoritative core
 specialists → Portfolio Manager → AI Risk Manager
             → deterministic risk/sizing/execution
             → Alpaca Paper

canonical AI/trade evidence → existing quant-agent storage / agent_logs
```

## Authority / failure domains

- **quant-agent core** owns scheduling, trading decisions, canonical records, memory/reflection, Meta Reflector, deterministic risk/execution, and broker interaction.
- **Stage-2 API** is a separate read-only adapter. It is not imported by the trading process and can fail without affecting trading.
- **Mission Control** is operator presentation. Its post-Stage-2 implementation architecture is provisional during Discovery R1.
- **Forensic observability is native** to QAMC records (`agent_logs`, `run_id` / `decision_id`, trade records, replay tooling); no external observability service is required.

## Runtime posture

The intended permanent runtime remains a small Linux VPS/server with independently restartable trading and read-side/UI processes where useful. Private/Tailscale access is preferred initially. Distributed infrastructure is not a default.

## Scope boundary

This architecture does not authorize writable Mission Control operations or live trading. The previous future Sentinel/live-money concept is preserved under `docs/reference/future/` and is not relevant to current implementation unless explicitly authorized later.
