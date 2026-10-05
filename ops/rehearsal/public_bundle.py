"""Fail-closed promotion gate for public rehearsal recordings."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping


_TOKEN_RE = re.compile(r"^<QAMC:[a-z_]+:[0-9]{4}>$")
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_ACCOUNT_RE = re.compile(r"\bPA[A-Z0-9]{8,}\b")
_CREDENTIAL_QUERY_RE = re.compile(
    r"[?&](?:api[_-]?key|token|access[_-]?token|key|secret)=", re.IGNORECASE
)
_CREDENTIAL_USERINFO_RE = re.compile(r"https?://[^/@\s:]+:[^/@\s]+@", re.IGNORECASE)
_CREDENTIAL_HEADER_KEYS = {
    "authorization",
    "proxy-authorization",
    "apca-api-key-id",
    "apca-api-secret-key",
    "x-api-key",
    "x-goog-api-key",
}
_PRODUCTION_PATH_MARKERS = (
    "/home/qamc/",
    "/var/lib/qamc/",
    "/run/qamc/",
    "/etc/systemd/system/quant-agent",
)
_EXPLICIT_IDENTIFIER_KEYS = {
    "id",
    "broker_id",
    "account_number",
    "account_id",
    "order_id",
    "broker_order_id",
    "client_order_id",
    "activity_id",
    "asset_id",
    "trade_id",
    "replaced_by",
    "replaces",
}
_BROKER_IDENTIFIER_KEY_RE = re.compile(r"^broker(?:_[a-z0-9]+)*_id$")


class PublicBundleRejected(RuntimeError):
    """An in-memory recording contains data unsafe for the public repository."""


def _encoded(payload: Any) -> bytes:
    return (
        json.dumps(
            payload,
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _identifier_key(key: str) -> bool:
    lowered = key.casefold()
    # This gate owns broker identity, not every identifier in a future
    # full-session recording. Replay metadata such as run_id, decision_id and
    # session_id is intentionally preserved; an unqualified UUID is still
    # rejected by the content scan above.
    return (
        lowered in _EXPLICIT_IDENTIFIER_KEYS
        or bool(_BROKER_IDENTIFIER_KEY_RE.fullmatch(lowered))
    )


def _walk(value: Any, *, path: str = "$"):
    yield path, None, value
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise PublicBundleRejected("unsafe non-string mapping key")
            child = f"{path}.{key}"
            yield child, key, item
            yield from _walk(item, path=child)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _walk(item, path=f"{path}[{index}]")


def _all_identifier_values_are_tokens(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return bool(_TOKEN_RE.fullmatch(value))
    if isinstance(value, (list, tuple)):
        return all(_all_identifier_values_are_tokens(item) for item in value)
    return False


def _all_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _all_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _all_strings(item)


def assert_public_safe(
    payload: Any,
    *,
    secrets: Iterable[str] = (),
    account_ids: Iterable[str] = (),
) -> bytes:
    """Return canonical JSON only if every public-safety check passes.

    Rejection messages deliberately identify only the class and structural
    location of a problem.  They never echo the private value.
    """

    encoded = _encoded(payload)
    text = encoded.decode("utf-8")
    in_memory_strings = tuple(_all_strings(payload))
    needle_groups = (
        ("supplied secret", secrets),
        ("supplied account", account_ids),
    )
    for label, needles in needle_groups:
        if any(
            str(needle)
            and any(str(needle) in candidate for candidate in in_memory_strings)
            for needle in needles
        ):
            raise PublicBundleRejected(f"public bundle contains an exact {label} value")
    if _UUID_RE.search(text):
        raise PublicBundleRejected("public bundle contains a raw UUID")
    if _ACCOUNT_RE.search(text):
        raise PublicBundleRejected("public bundle contains a raw broker account number")
    if _CREDENTIAL_QUERY_RE.search(text) or _CREDENTIAL_USERINFO_RE.search(text):
        raise PublicBundleRejected("public bundle contains a credential-bearing URL")
    if any(marker in text for marker in _PRODUCTION_PATH_MARKERS):
        raise PublicBundleRejected("public bundle contains a production path")

    for path, key, value in _walk(payload):
        if key is not None and key.casefold() in _CREDENTIAL_HEADER_KEYS:
            raise PublicBundleRejected(
                f"public bundle contains a credential header at {path}"
            )
        if (
            key is not None
            and _identifier_key(key)
            and not _all_identifier_values_are_tokens(value)
        ):
            raise PublicBundleRejected(
                f"public bundle contains an untokenized identifier at {path}"
            )
    return encoded


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def promote_public_bundle(
    payload: Any,
    destination: Path | str,
    *,
    secrets: Iterable[str] = (),
    account_ids: Iterable[str] = (),
    repository_root: Path | str | None = None,
    staging_dir: Path | str | None = None,
) -> Path:
    """Stage first, scan in memory, then atomically promote a safe bundle.

    The staging location is fixed beneath ``ops/rehearsal/captures.local``;
    that directory is ignored by git.  Existing destinations are refused so
    capture cannot silently overwrite a reviewed public fixture.
    """

    root = Path(repository_root or Path(__file__).resolve().parents[2]).resolve()
    ignored_root = (root / "ops" / "rehearsal" / "captures.local").resolve()
    stage_value = Path(staging_dir or ignored_root)
    stage = (
        root / stage_value if not stage_value.is_absolute() else stage_value
    ).resolve()
    target_value = Path(destination)
    target = (
        root / target_value if not target_value.is_absolute() else target_value
    ).resolve()
    if not _inside(stage, ignored_root):
        raise ValueError("staging directory must be inside ops/rehearsal/captures.local")
    if not _inside(target, root) or _inside(target, ignored_root):
        raise ValueError("destination must be a non-staging path inside the repository")
    if target.suffix != ".json":
        raise ValueError("public broker bundles must be JSON files")
    if target.exists():
        raise FileExistsError("public bundle destination already exists")

    stage.mkdir(parents=True, exist_ok=True)
    raw = _encoded(payload)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="broker-capture-", suffix=".json", dir=stage
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())

        safe = assert_public_safe(
            payload, secrets=secrets, account_ids=account_ids
        )
        if safe != raw:
            raise PublicBundleRejected("public bundle changed during safety validation")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(temporary, 0o644)
        # Hard-link promotion is atomic and refuses an existing target, closing
        # the exists-check race without a destructive overwrite.
        os.link(temporary, target)
        temporary.unlink()
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return target
    finally:
        temporary.unlink(missing_ok=True)
