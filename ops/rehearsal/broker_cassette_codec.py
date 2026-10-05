"""Private deterministic codec for broker-cassette values and identifiers."""

from __future__ import annotations

import re
from dataclasses import fields as dataclass_fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from uuid import UUID


SCHEMA = "qamc-broker-cassette-v1"
_TOKEN_RE = re.compile(r"^<QAMC:(?P<category>[a-z_]+):(?P<number>[0-9]{4})>$")
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_ACCOUNT_RE = re.compile(r"\bPA[A-Z0-9]{8,}\b")


def _category_for(field: str | None, context: str) -> str | None:
    key = (field or "").casefold()
    lowered = context.casefold()
    explicit = {
        "account_number": "account",
        "account_id": "account_id",
        "client_order_id": "client_order_id",
        "activity_id": "activity_id",
        "order_id": "order_id",
        "broker_order_id": "order_id",
        "replaced_by": "order_id",
        "replaces": "order_id",
        "asset_id": "asset_id",
        "trade_id": "trade_id",
    }
    if key in explicit:
        return explicit[key]
    if key == "id":
        if "account" in lowered:
            return "account_id"
        if "activit" in lowered:
            return "activity_id"
        if "order" in lowered or "submit" in lowered or "cancel" in lowered:
            return "order_id"
        if "asset" in lowered:
            return "asset_id"
        return "broker_id"
    # Do not absorb full-session metadata such as run_id or decision_id into
    # the broker namespace. Unknown IDs are broker IDs only when the field
    # says so explicitly; UUID values still tokenize independently below.
    if key.startswith("broker_") and key.endswith("_id"):
        return "broker_id"
    # Context supplies the semantic category only for an unlabelled scalar
    # (for example get_order_by_id(UUID(...))).  A labelled field such as
    # ``symbol`` inside an order response is not itself an order identifier.
    if field is not None:
        return None
    if "account" in lowered:
        return "account_id"
    if "activit" in lowered:
        return "activity_id"
    if "order" in lowered or "submit" in lowered or "cancel" in lowered:
        return "order_id"
    if "asset" in lowered:
        return "asset_id"
    return None


class IdentifierTokenizer:
    """Assign stable, opaque identifiers in first-observed order."""

    def __init__(self):
        # Text alone is not an identifier's identity. Alpaca uses both integer
        # and string IDs, and the same representation can occur in unrelated
        # namespaces (an order ``7`` is not asset ``7``).
        self._raw_to_token: dict[
            tuple[str, str, str], tuple[str, str]
        ] = {}
        self._counts: dict[str, int] = {}

    @staticmethod
    def _raw_key(raw: Any, category: str) -> tuple[str, str, str]:
        raw_type = f"{type(raw).__module__}.{type(raw).__qualname__}"
        return category, raw_type, repr(raw)

    def token(self, raw: Any, category: str) -> str:
        text = str(raw)
        if _TOKEN_RE.fullmatch(text):
            self.observe(text)
            return text
        key = self._raw_key(raw, category)
        existing = self._raw_to_token.get(key)
        if existing is not None:
            return existing[1]
        number = self._counts.get(category, 0) + 1
        self._counts[category] = number
        value = f"<QAMC:{category}:{number:04d}>"
        self._raw_to_token[key] = (text, value)
        return value

    def observe(self, value: str) -> None:
        match = _TOKEN_RE.fullmatch(value)
        if match:
            category = match.group("category")
            self._counts[category] = max(
                self._counts.get(category, 0), int(match.group("number"))
            )

    def sanitize_text(self, value: str, context: str) -> str:
        sanitized = value
        context_category = _category_for(None, context) or "broker_id"
        # Replace identifiers already seen in structured values first.
        observed_by_text: dict[str, list[tuple[str, str]]] = {}
        for (category, _raw_type, _raw_repr), (raw_text, token) in (
            self._raw_to_token.items()
        ):
            observed_by_text.setdefault(raw_text, []).append((category, token))
        for raw_text, candidates in sorted(
            observed_by_text.items(), key=lambda item: len(item[0]), reverse=True
        ):
            matching = {
                token for category, token in candidates
                if category == context_category
            }
            all_tokens = {token for _category, token in candidates}
            if len(matching) == 1:
                replacement = next(iter(matching))
            elif len(all_tokens) == 1:
                replacement = next(iter(all_tokens))
            else:
                # Error text has lost the raw Python type/category. When more
                # than one identifier renders identically, tokenize it in the
                # error's namespace rather than guessing or leaking linkage.
                replacement = self.token(raw_text, context_category)
            sanitized = sanitized.replace(raw_text, replacement)

        sanitized = _UUID_RE.sub(
            lambda match: self.token(match.group(0), context_category), sanitized
        )
        sanitized = _ACCOUNT_RE.sub(
            lambda match: self.token(match.group(0), "account"), sanitized
        )
        return sanitized


class Codec:
    """Encode SDK values to JSON-safe shapes and revive replay values."""

    def __init__(self, tokenizer: IdentifierTokenizer):
        self._tokenizer = tokenizer

    def encode(self, value: Any, *, context: str, field: str | None = None) -> Any:
        category = _category_for(field, context)
        if isinstance(value, UUID):
            return self._tokenizer.token(value, category or "broker_id")
        # Alpaca's enums inherit from str. Tag them before the str branch so a
        # JSON round trip and an in-memory replay have identical call shapes.
        if isinstance(value, Enum):
            return {
                "__qamc_type__": "enum",
                "type": f"{type(value).__module__}.{type(value).__qualname__}",
                "value": self.encode(value.value, context=context),
            }
        # Alpaca Trade.id is an integer market-print identifier. Treat numeric
        # values in explicitly identified fields like string/UUID broker IDs.
        if (
            category is not None
            and isinstance(value, int)
            and not isinstance(value, bool)
        ):
            return self._tokenizer.token(value, category)
        if isinstance(value, str):
            if _TOKEN_RE.fullmatch(value):
                self._tokenizer.observe(value)
                return value
            if category is not None:
                return self._tokenizer.token(value, category)
            if _UUID_RE.fullmatch(value):
                inferred = _category_for(None, context) or "broker_id"
                return self._tokenizer.token(value, inferred)
            if _ACCOUNT_RE.fullmatch(value):
                return self._tokenizer.token(value, "account")
            return value
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, datetime):
            return {"__qamc_type__": "datetime", "value": value.isoformat()}
        if isinstance(value, date):
            return {"__qamc_type__": "date", "value": value.isoformat()}
        if isinstance(value, Decimal):
            return {"__qamc_type__": "decimal", "value": str(value)}
        if isinstance(value, Path):
            return {"__qamc_type__": "path", "value": str(value)}
        if isinstance(value, Mapping):
            if not all(isinstance(key, str) for key in value):
                raise TypeError("broker cassette mappings require string keys")
            keys = {key.casefold() for key in value}
            mapping_context = context
            if "account_number" in keys:
                mapping_context += ".account"
            elif "activity_type" in keys:
                mapping_context += ".activity"
            elif "client_order_id" in keys:
                mapping_context += ".order"
            elif "asset_class" in keys:
                mapping_context += ".asset"
            return {
                key: self.encode(item, context=mapping_context, field=key)
                for key, item in sorted(value.items())
            }
        if isinstance(value, list):
            return [self.encode(item, context=context, field=field) for item in value]
        if isinstance(value, tuple):
            return {
                "__qamc_type__": "tuple",
                "items": [
                    self.encode(item, context=context, field=field)
                    for item in value
                ],
            }
        if isinstance(value, (set, frozenset)):
            encoded = [
                self.encode(item, context=context, field=field) for item in value
            ]
            return {"__qamc_type__": "set", "items": sorted(encoded, key=repr)}

        if hasattr(value, "model_dump"):
            dumped = value.model_dump(mode="python")
            if not isinstance(dumped, Mapping):
                raise TypeError("broker cassette model_dump values must be mappings")
            # Pydantic recursively converts nested Alpaca models to dicts. Read
            # top-level fields from the live object to retain nested objects.
            fields = {}
            for name, flattened in dumped.items():
                try:
                    fields[name] = getattr(value, name)
                except (AttributeError, TypeError):
                    fields[name] = flattened
        elif is_dataclass(value) and not isinstance(value, type):
            # ``asdict`` recursively flattens nested dataclasses too; preserve
            # their object boundaries by reading each field directly.
            fields = {
                item.name: getattr(value, item.name)
                for item in dataclass_fields(value)
            }
        elif hasattr(value, "__dict__"):
            fields = vars(value)
        else:
            raise TypeError(
                f"unsupported broker cassette value type: {type(value).__name__}"
            )
        return {
            "__qamc_type__": "object",
            "type": f"{type(value).__module__}.{type(value).__qualname__}",
            "fields": self.encode(fields, context=context),
        }

    def decode(self, value: Any) -> Any:
        if isinstance(value, str):
            self._tokenizer.observe(value)
            return value
        if isinstance(value, list):
            return [self.decode(item) for item in value]
        if not isinstance(value, dict):
            return value
        tag = value.get("__qamc_type__")
        if tag == "object":
            fields = self.decode(value["fields"])
            return SimpleNamespace(**fields)
        if tag == "tuple":
            return tuple(self.decode(item) for item in value["items"])
        if tag == "set":
            return set(self.decode(item) for item in value["items"])
        if tag == "enum":
            return self.decode(value["value"])
        if tag == "datetime":
            return datetime.fromisoformat(value["value"])
        if tag == "date":
            return date.fromisoformat(value["value"])
        if tag == "decimal":
            return Decimal(value["value"])
        if tag == "path":
            return Path(value["value"])
        return {key: self.decode(item) for key, item in value.items()}
