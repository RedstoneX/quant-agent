# Safety Boundaries

1. Paper trading only; live trading is not authorized.
2. Deterministic Python risk and broker-side protection are final authority.
3. AI Risk Manager is an advisory/challenge layer, not a replacement for hard rules.
4. Risk-system uncertainty/failure must fail closed.
5. Dashboard/API/AgentLens failure cannot remove stops or other broker-side protection.
6. Mission Control cannot issue broker orders directly or override deterministic rejection.
7. Provider/model/prompt changes cannot alter protected hard-risk code/ceilings.
8. Meta Reflector proposals require human approval initially; protected risk agents/behavior stay outside automatic evolution.
9. Any later writable risk configuration must be server-validated, range-limited and audited; protected ceilings remain non-editable.
10. Emergency controls must map to real quant-agent/Alpaca lifecycle semantics discovered during implementation, not frontend-invented behavior.
