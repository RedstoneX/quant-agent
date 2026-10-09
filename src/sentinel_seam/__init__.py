"""The Sentinel's OUTWARD seam: the desk publishes a signed state snapshot; nothing reads back in.

Specified in docs/FUTURE.md ("The outward snapshot — the one seam that
serves everything"). The inbound seam is the kill-switch FLAG the broker
layer already reads (RiskConfig.kill_switch_path); this package is only
the outward half. Imports nothing from src: it is a pure stdlib piece.
"""

from src.sentinel_seam.snapshot import (
    SNAPSHOT_SCHEMA_VERSION,
    SIGNING_KEY_ENV_VAR,
    UNKNOWN_VERSION,
    SnapshotPublisher,
    build_snapshot,
    desk_code_version,
    scrub_snapshot,
    sign_snapshot,
    signing_key_from_environ,
    verify_snapshot,
)

__all__ = [
    "SNAPSHOT_SCHEMA_VERSION",
    "SIGNING_KEY_ENV_VAR",
    "UNKNOWN_VERSION",
    "SnapshotPublisher",
    "build_snapshot",
    "desk_code_version",
    "scrub_snapshot",
    "sign_snapshot",
    "signing_key_from_environ",
    "verify_snapshot",
]
