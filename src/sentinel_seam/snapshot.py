"""Outward state snapshot: a pure build from PASSED-IN state, an HMAC seal, and a file drop.

Three responsibilities, deliberately separable:

* `build_snapshot` — a pure function. Everything it reports is an argument;
  it never reaches into the pipeline, the broker or the database. The
  caller (the composition root, when wired) gathers the state.
* `sign_snapshot` / `verify_snapshot` — HMAC-SHA256 over the canonical
  JSON of the payload, standard library only. The key comes from
  configuration or the environment, never a literal. With NO key the
  snapshot is still produced and says so in plain words
  (`signature.scheme == "unsigned"`, `signature.value is None`), so a
  reader can never mistake an unsigned file for a sealed one.
* `SnapshotPublisher` — the standalone class that composes the two and
  writes the result to a local drop point. Every collaborator is a
  keyword-only constructor argument (docs/ARCHITECTURE.md section 3), so
  it is built and exercised without a TradingPipeline.

What is NOT here, on purpose: no network transport, no daemon, no
schedule, no reader. The later Sentinel build is a connection to this
file, not surgery on the desk.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
from importlib import metadata
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Bumped whenever the payload's shape changes incompatibly. A reader that
#: does not understand a version must refuse it rather than guess.
SNAPSHOT_SCHEMA_VERSION = 1

#: The environment variable the signing key is read from. The value is the
#: shared secret the Sentinel host also holds; it is never committed.
SIGNING_KEY_ENV_VAR = "QAMC_SNAPSHOT_SIGNING_KEY"

#: The code version is resolved from git, then package metadata, else this
#: literal — never a fabricated tag. A reader may refuse it.
UNKNOWN_VERSION = "unknown"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DIST_NAME = "quant-agent"

_SIGNATURE_FIELD = "signature"
_HMAC_SCHEME = "hmac-sha256"
_UNSIGNED_SCHEME = "unsigned"


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    """Deterministic encoding of the payload WITHOUT its signature field.

    Sorted keys, no whitespace, non-JSON values (datetimes, Decimals,
    Paths) rendered via `str` — the same bytes on every host, so a
    verifier on another machine reproduces them exactly.
    """
    body = {k: v for k, v in payload.items() if k != _SIGNATURE_FIELD}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=True).encode("utf-8")


def build_snapshot(
    *,
    heartbeat_at: datetime,
    desk_version: str,
    trading_state: Mapping[str, Any],
    expected_positions: list[Mapping[str, Any]],
    expected_protections: list[Mapping[str, Any]],
    risk_state: Mapping[str, Any],
    last_reconciliation: Mapping[str, Any] | None,
    recent_trades: list[Mapping[str, Any]] | None = None,
    cost_spent: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The unsigned snapshot, built only from what is passed in.

    The sections are the spec's list verbatim: heartbeat and version,
    trading state, the positions the desk believes it holds, the protection
    it believes covers each, risk state, last reconciliation, recent trades
    and cost spent. The last two are optional because the first cut of the
    wiring may not have them to hand; a reader sees `[]` / `{}`, never a
    missing key.
    """
    if heartbeat_at.tzinfo is None:
        raise ValueError("heartbeat_at must be timezone-aware")
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "heartbeat_at": heartbeat_at.astimezone(timezone.utc).isoformat(),
        "desk_version": str(desk_version),
        "trading_state": dict(trading_state),
        "expected_positions": [dict(p) for p in expected_positions],
        "expected_protections": [dict(p) for p in expected_protections],
        "risk_state": dict(risk_state),
        "last_reconciliation": dict(last_reconciliation or {}),
        "recent_trades": [dict(t) for t in (recent_trades or [])],
        "cost_spent": dict(cost_spent or {}),
    }


def desk_code_version(*, cwd: Path | None = None, run: Callable[..., Any] = subprocess.run) -> str:
    """The desk's code version: git short SHA, else package version, else "unknown".

    Every failure path lands on the literal `UNKNOWN_VERSION` so a reader
    sees an honest gap, never a guessed tag.
    """
    try:
        out = run(
            ["git", "-C", str(cwd or _REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        sha = (getattr(out, "stdout", "") or "").strip()
        if getattr(out, "returncode", 1) == 0 and sha:
            return sha
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    try:
        return metadata.version(_DIST_NAME)
    except metadata.PackageNotFoundError:
        return UNKNOWN_VERSION


# -- scrubbing ---------------------------------------------------------------
# docs/FUTURE.md: "The snapshot is scrubbed before it leaves. No account
# identifier, no key or token, no internal hostname or filesystem path, no
# provider credentials." Two rules: a KEY whose name says it holds one of
# those is redacted whole; a string VALUE that looks like one is redacted
# wherever it sits. Redaction markers are words, never empty strings, so a
# reader can tell "scrubbed" from "absent".
_SENSITIVE_KEY = re.compile(
    r"(account|acct|api_?key|secret|token|password|passwd|credential|"
    r"bearer|hostname|host_?name|home_?dir|cwd|file_?path|db_?path)",
    re.IGNORECASE,
)
_SENSITIVE_VALUE = [
    # Absolute or home-relative filesystem paths (POSIX and Windows).
    (
        re.compile(r"(?:^|(?<=\s))(?:~|/(?:home|Users|var|tmp|opt|etc|srv|mnt|root|data)|[A-Za-z]:\\)[^\s\"']*"),
        "[PATH REDACTED]",
    ),
    # Bearer tokens and provider key prefixes (Alpaca PK/AK/SK, OpenAI/Anthropic sk-).
    (re.compile(r"\b(?:Bearer\s+\S+|(?:PK|AK|SK)[A-Z0-9]{12,}|sk-[A-Za-z0-9_-]{8,})"), "[SECRET REDACTED]"),
    # Broker account numbers: 10-14 upper-case alphanumerics with digits AND
    # letters (a ticker is letters only, far shorter).
    (re.compile(r"\b(?=[A-Z0-9]{10,14}\b)(?=[A-Z0-9]*[0-9])(?=[A-Z0-9]*[A-Z])[A-Z0-9]{10,14}\b"), "[ACCOUNT REDACTED]"),
    # Long opaque secrets: 32+ chars of base64/hex with no spaces.
    (re.compile(r"\b[A-Za-z0-9+/=_-]{32,}\b"), "[SECRET REDACTED]"),
    # E-mail addresses and IPv4 addresses identify the owner or the host.
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[EMAIL REDACTED]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[IP REDACTED]"),
    # Internal hostnames.
    (re.compile(r"\b[a-z0-9.-]+\.(?:local|internal|lan)\b"), "[HOST REDACTED]"),
]


def _scrub_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: ("[REDACTED]" if _SENSITIVE_KEY.search(str(k)) else _scrub_value(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_value(v) for v in value]
    if isinstance(value, str):
        for pattern, marker in _SENSITIVE_VALUE:
            value = pattern.sub(marker, value)
        return value
    return value


def scrub_snapshot(payload: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of `payload` with account ids, secrets, paths and hostnames redacted.

    Pure: no I/O, no reference to the live environment. Applied BEFORE
    signing, so the seal covers what actually leaves.
    """
    return _scrub_value(payload)


def sign_snapshot(payload: Mapping[str, Any], *, key: bytes | None) -> dict[str, Any]:
    """Return a copy of `payload` carrying a `signature` block.

    With a key: `{"scheme": "hmac-sha256", "value": <hex>}`.
    Without one: `{"scheme": "unsigned", "value": None}` — produced, not
    refused, and unmistakably not a seal.
    """
    sealed = {k: v for k, v in payload.items() if k != _SIGNATURE_FIELD}
    if not key:
        sealed[_SIGNATURE_FIELD] = {"scheme": _UNSIGNED_SCHEME, "value": None}
        return sealed
    digest = hmac.new(key, _canonical_bytes(sealed), hashlib.sha256).hexdigest()
    sealed[_SIGNATURE_FIELD] = {"scheme": _HMAC_SCHEME, "value": digest}
    return sealed


def verify_snapshot(envelope: Mapping[str, Any], *, key: bytes) -> bool:
    """True only for an HMAC-sealed snapshot whose seal matches `key`.

    An unsigned snapshot, a missing or malformed block, an unknown scheme
    and a tampered body all return False. Constant-time comparison.
    """
    sig = envelope.get(_SIGNATURE_FIELD)
    if not isinstance(sig, Mapping) or sig.get("scheme") != _HMAC_SCHEME:
        return False
    claimed = sig.get("value")
    if not isinstance(claimed, str) or not key:
        return False
    expected = hmac.new(key, _canonical_bytes(envelope), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, claimed)


def signing_key_from_environ(environ: Mapping[str, str] | None = None) -> bytes | None:
    """The signing key from the environment, or None when it is not set.

    Whitespace is stripped because a pasted-in trailing newline has
    corrupted an environment secret on this box before.
    """
    env = os.environ if environ is None else environ
    raw = (env.get(SIGNING_KEY_ENV_VAR) or "").strip()
    return raw.encode("utf-8") if raw else None


class SnapshotPublisher:
    """Builds, signs and drops the snapshot. Built from explicit collaborators only.

    `state_reader` is a zero-argument callable returning the keyword
    arguments of `build_snapshot` except `heartbeat_at` and
    `desk_version` (the composition root supplies it; this class never
    reaches into anything). `clock` returns a timezone-aware now.
    `desk_version` is the code version (see `desk_code_version`; the
    composition root resolves it once). `output_path` is the local drop point; the write is atomic (tmp file
    then rename) so a reader never sees a half-written file.
    """

    def __init__(
        self,
        *,
        state_reader: Callable[[], Mapping[str, Any]],
        output_path: Path,
        desk_version: str,
        signing_key: bytes | None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._state_reader = state_reader
        self._output_path = Path(output_path)
        self._desk_version = desk_version
        self._signing_key = signing_key
        self._clock = clock

    @property
    def is_signed(self) -> bool:
        return bool(self._signing_key)

    def render(self) -> dict[str, Any]:
        """The sealed snapshot as a dict, without writing it."""
        state = dict(self._state_reader())
        payload = build_snapshot(heartbeat_at=self._clock(), desk_version=self._desk_version, **state)
        return sign_snapshot(scrub_snapshot(payload), key=self._signing_key)

    def publish(self) -> dict[str, Any]:
        """Render and atomically write the snapshot; returns what was written."""
        envelope = self.render()
        text = json.dumps(envelope, sort_keys=True, indent=2, default=str)
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._output_path.with_name(self._output_path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self._output_path)
        return envelope
