"""Standalone protection parts lifted verbatim out of `src/pipeline_protection.py`.

Each imports the broker seam (`src.execution`) lazily inside its bodies, exactly as it did
before the move; the builders and `ProtectionMixin` stay in `src/pipeline_protection.py`.
"""
