"""Standalone cost-circuit pieces: each class is built from explicit keyword-only
collaborators and runs with no LLMCostCircuitBreaker (or pipeline) behind it.
Bodies moved verbatim from the former src/cost_circuit/breaker_*.py mixins.
All eleven are HELD by LLMCostCircuitBreaker (built once in `_hold_parts`,
delegated to by same-named methods); no shim module or mixin remains.
"""
