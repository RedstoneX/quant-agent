"""The session's ladder-resolved gross-exposure ceiling (spec §11.2).

Lifted out of `src/pipeline_stages.py` so that `src/pipeline_sizing.py` (which
`src.pipeline_stages` imports) can read it without importing back up. This
module imports nothing from `src.pipeline_stages` or `src.pipeline_sizing`;
`src.pipeline_stages` re-exports the name so every import path is unchanged.
"""

from __future__ import annotations

from src.pipeline_stage_helpers import record_stage


def _session_gross_ceiling(pipeline, ctx):
    """Spec §11.2 — this session's ladder-resolved gross-exposure ceiling.

    The run preamble already resolved it from account state before any agent
    ran; this re-derives it so the resume lane (where the preamble did not
    run) sizes against a real ceiling too. Returns None on any failure — the
    constructor then falls back to the standing cap, which is still a
    ceiling. It never falls back to "no ceiling".
    """
    resolve = getattr(pipeline, "_resolve_gross_ceiling", None)
    if resolve is None:
        return None
    try:
        from src.risk.rules import GrossCeiling
        ceiling = resolve(ctx)
        return ceiling if isinstance(ceiling, GrossCeiling) else None
    except Exception as exc:  # noqa: BLE001
        record_stage(pipeline, "gross_ceiling", exc)
        return None
