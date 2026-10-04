"""Deployed-value reader for the number ledger, lifted verbatim out of
`src.number_sources` so that module stays under its size ceiling."""

from __future__ import annotations

import ast
from pathlib import Path

import yaml

from src.feature_flags import config_modules

REPO_ROOT = Path(__file__).resolve().parent.parent


def _appconfig_sections(root: Path) -> dict[str, str]:
    """`{ConfigClassName: settings.yaml section}` read from `AppConfig`.

    Read rather than hardcoded: the mapping IS the field name on `AppConfig`,
    so a renamed section cannot desynchronise this check from the loader.
    """
    trees = [ast.parse(p.read_text(encoding="utf-8")) for p in config_modules(root)]
    for node in (n for t in trees for n in ast.walk(t)):
        if not isinstance(node, ast.ClassDef) or node.name != "AppConfig":
            continue
        out: dict[str, str] = {}
        for body_node in node.body:
            if (
                isinstance(body_node, ast.AnnAssign)
                and isinstance(body_node.annotation, ast.Name)
                and isinstance(body_node.target, ast.Name)
            ):
                out[body_node.annotation.id] = body_node.target.id
        return out
    return {}


def deployed_values(root: Path | None = None) -> dict[str, float]:
    """`{site_id: deployed value}` for every ledger site `settings.yaml` sets.

    THE BLIND SPOT THIS CLOSES. The ledger pins the CODE DEFAULT. For a
    `src.config.*Config.<field>` site the deployed value comes from
    `config/settings.yaml`, so `risk.max_position_risk_pct: 5` could be edited
    to `10` with the gate entirely silent. Measured 2026-09-18: 52 sites route
    this way. They all currently agree with their defaults — which is exactly
    why this is cheap to start enforcing now.
    """
    base = root or REPO_ROOT
    settings = base / "config" / "settings.yaml"
    if not settings.is_file():
        raise FileNotFoundError(str(settings))
    sections = _appconfig_sections(base)
    raw = yaml.safe_load(settings.read_text(encoding="utf-8")) or {}
    out: dict[str, float] = {}
    for class_name, section in sections.items():
        block = raw.get(section)
        if not isinstance(block, dict):
            continue
        for key, value in block.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            out[f"src.config.{class_name}.{key}"] = float(value)
    return out
