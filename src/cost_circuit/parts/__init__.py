"""Standalone cost-circuit pieces: each class is built from explicit keyword-only
collaborators and runs with no LLMCostCircuitBreaker (or pipeline) behind it.
Bodies moved verbatim from the src/cost_circuit/breaker_*.py mixins.
AlertFormats, EpisodeWording, CircuitState and QuotaHolds are HELD by
LLMCostCircuitBreaker (built once, delegated to; their shim modules are gone);
the other seven mixins still keep thin same-named shims built per call.
"""
