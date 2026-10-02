"""Standalone cost-circuit pieces: each class is built from explicit keyword-only
collaborators and runs with no LLMCostCircuitBreaker (or pipeline) behind it.
Bodies moved verbatim from the src/cost_circuit/breaker_*.py mixins, which keep
thin same-named shims that build the object per call.
"""
