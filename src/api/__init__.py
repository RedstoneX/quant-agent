"""QAMC Mission Control API — Stage 2, thin read-only adapter.

Read-only HTTP API over quant-agent's existing canonical state (SQLite +
broker-live reads). Runs as a separate process from trading; trading has
no import-time or runtime dependency on this package. See
docs/architecture/MISSION_CONTROL_API.md for the endpoint contract and
docs/MILESTONES.md's Stage 2 section for the Checkpoint C safety
verification (a docs/CHECKPOINT_C_ACCEPTANCE.md record is added once the
operator accepts it, mirroring docs/CHECKPOINT_B_ACCEPTANCE.md for Stage 1).

Hard invariant: nothing under `src/api/` may import `src.execution.broker`'s
write methods, `src.pipeline`, `src.pipeline_stages`, `src.risk.*`, or
`src.storage.db.Database`'s write methods. This package only ever performs
reads — its own dedicated read-only SQLite connection and the pre-existing
read-only `AlpacaBroker` methods (`get_account`, `get_positions`).
"""
