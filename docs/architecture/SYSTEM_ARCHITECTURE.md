# QAMC System Architecture

```text
Browser / iPad
    |
    v
QAMC Mission Control (React/Vite/Tailwind)
    |
    | REST + WebSocket/SSE as justified
    v
Thin QAMC API / read-model layer
    |
    v
quant-agent authoritative core
 specialist agents
       -> Portfolio Manager
       -> AI Risk Manager
       -> deterministic risk/sizing/execution gate
       -> Alpaca Paper

quant-agent AI activity --best effort/redacted--> AgentLens (optional sidecar)
```

## Authority boundaries
- **quant-agent** owns trading decisions, scheduling, canonical records, memory/reflection and Meta Reflector.
- **QAMC API** exposes state and later narrow validated controls. It does not become a second engine.
- **Mission Control** is presentation/operator UX, never direct broker execution.
- **AgentLens** is forensic observability only.

## Runtime
Initial production target: a small Linux VPS/server with trading, API/UI and optional AgentLens as separately restartable services. Preserve upstream systemd model where practical. No Kubernetes/Redis/Kafka/etc. by default.

Initial remote access: private/Tailscale preferred. `here.now` may publish frontend previews during development but is not a trading dependency.
