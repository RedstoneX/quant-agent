"""Stage 2 Checkpoint C — no API key/secret ever reaches an API response.

Two layers, matching the design in the Wave-0 safety review:

1. **Schema-level (static).** Every Pydantic response model in
   `src.api.schemas` is inspected for a field name that could plausibly
   carry a secret (`*key*`, `*secret*`, `*token*`, `*password*`). This is
   the structural guarantee: even before any endpoint is called, the
   response *shape* has nowhere to put a secret.
2. **Live (end-to-end).** A real `AppConfig` is loaded from a temp
   `settings.yaml` with distinctive sentinel values standing in for every
   configured secret (`ApiKeysConfig`'s seven fields), then every GET
   route is hit and its raw response body text is scanned for each
   sentinel string. Catches the case a schema-level check can't: a field
   that legitimately holds free text (e.g. `full_response`, an LLM's raw
   output) accidentally containing a secret because something upstream
   interpolated it in — which structurally can't happen here (agent
   prompts never contain API keys), but this test proves it rather than
   assuming it.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

import src.api.deps as deps
from src.api.server import app

_SECRET_FIELD_NAME_RE = re.compile(
    # `token(?!s)` excludes legitimate token-COUNT fields (`tokens_used`,
    # `input_tokens`, `output_tokens`) while still catching a real
    # credential-shaped name like `access_token` or `auth_token`.
    r"(api[_-]?key|secret|token(?!s)|password|credential)", re.IGNORECASE
)

_SETTINGS_YAML = """
api_keys:
  anthropic: "{anthropic}"
  openai: "{openai}"
  deepseek: "{deepseek}"
  openrouter: "{openrouter}"
  fred: "{fred}"
  alpaca_key: "{alpaca_key}"
  alpaca_secret: "{alpaca_secret}"
alpaca:
  base_url: "https://paper-api.alpaca.markets"
  paper: true
llm:
  tech_analyst_model: "gpt-5.5"
  news_analyst_model: "gpt-5.5"
  macro_analyst_model: "gpt-5.5"
  earnings_analyst_model: "gpt-5.5"
  portfolio_manager_model: "gpt-5.5"
  risk_manager_model: "gpt-5.5"
  position_reviewer_model: "gpt-5.5"
  evening_analyst_model: "gpt-5.5"
  meta_reflector_model: "gpt-5.5"
  max_tokens: 8192
risk:
  max_position_pct: 20
  max_total_position_pct: 90
  max_daily_loss_pct: 3
  max_sector_pct: 40
  require_stop_loss: true
trading:
  universe: ["SPY", "QQQ"]
  lookback_days: 120
  schedule:
    morning: "06:00"
    midday: "12:00"
    evening: "16:30"
storage:
  db_path: "test_no_secrets.db"
"""

_SENTINELS = {
    "anthropic": "SENTINEL-ANTHROPIC-KEY-9f3a1c",
    "openai": "SENTINEL-OPENAI-KEY-7b2e4d",
    "deepseek": "SENTINEL-DEEPSEEK-KEY-1a9c5f",
    "openrouter": "SENTINEL-OPENROUTER-KEY-6e8b2a",
    "fred": "SENTINEL-FRED-KEY-3d7f1e",
    "alpaca_key": "SENTINEL-ALPACA-KEY-4c6a9b",
    "alpaca_secret": "SENTINEL-ALPACA-SECRET-8e2d5c",
}


# ---------------------------------------------------------------------------
# 1. Schema-level: no response model field name is secret-shaped.
# ---------------------------------------------------------------------------

def test_no_response_schema_field_is_secret_shaped():
    import src.api.schemas as schemas_module

    checked_any = False
    for name in dir(schemas_module):
        obj = getattr(schemas_module, name)
        if not isinstance(obj, type):
            continue
        model_fields = getattr(obj, "model_fields", None)
        if model_fields is None:
            continue
        checked_any = True
        for field_name in model_fields:
            assert not _SECRET_FIELD_NAME_RE.search(field_name), (
                f"{obj.__name__}.{field_name} looks secret-shaped — response "
                f"models must never declare a field that could carry a key"
            )
    assert checked_any, "expected to find at least one Pydantic model in src.api.schemas"


# ---------------------------------------------------------------------------
# 2. Live: hit every GET route with sentinel-loaded config, scan bodies.
# ---------------------------------------------------------------------------

@pytest.fixture
def sentinel_config(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "settings.yaml"
    config_file.write_text(_SETTINGS_YAML.format(**_SENTINELS))
    deps.get_config.cache_clear()
    monkeypatch.setenv("QUANT_AGENT_API_CONFIG", str(config_file))
    yield
    deps.get_config.cache_clear()


def _all_get_paths() -> list[str]:
    paths = []
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", None)
        if path and "GET" in methods and "{" not in path:
            paths.append(path)
    return paths


def test_no_sentinel_secret_appears_in_any_response_body(sentinel_config):
    client = TestClient(app, raise_server_exceptions=False)
    checked_any = False
    for path in _all_get_paths():
        resp = client.get(path)
        checked_any = True
        body_text = resp.text
        for label, sentinel in _SENTINELS.items():
            assert sentinel not in body_text, (
                f"GET {path} leaked the {label} sentinel secret in its response body"
            )
    assert checked_any, "expected at least one GET route to check"


def test_agents_endpoint_never_leaks_configured_secrets(sentinel_config):
    """/agents is the route most likely to accidentally reach into config —
    it renders per-agent provider/model. Confirm explicitly it never touches
    `api_keys`."""
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/agents")
    assert resp.status_code == 200
    for sentinel in _SENTINELS.values():
        assert sentinel not in resp.text
