import sys
from pathlib import Path

import pytest

# Add src to path so tests can import from src.*
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    """Every test runs in its own tmp cwd so stores with relative data_dir defaults
    (NewsStore/MacroStore → 'data/news', 'data/macro') don't write to the real repo
    during the test suite.

    Agent PROMPT_PATH values are computed from __file__ (absolute), so they're
    unaffected. Tests that need real data/config paths should use explicit absolute
    paths or dedicated fixtures.

    Also disables outbound HTTP by default: `requests.get` raises unless a test
    explicitly re-patches it. This keeps the suite hermetic now that
    `cost_table.estimate_cost` does an on-demand LiteLLM lookup for unfamiliar
    models — without this, a test exercising a not-yet-priced model would make a
    real network call (slow + flaky in CI). Tests that want specific fetch
    behaviour (the refresh_pricing tests) override `requests.get` themselves, and
    that override wins within the test body.
    """
    monkeypatch.chdir(tmp_path)

    import requests

    def _no_network(*args, **kwargs):
        raise requests.ConnectionError(
            "outbound HTTP disabled in tests (conftest autouse); "
            "patch requests.get in the test if you need it"
        )

    monkeypatch.setattr(requests, "get", _no_network)

    # Clear OPENAI_BASE_URL / OPENAI_CA_BUNDLE so a developer who `source .env`'d
    # a relay endpoint into their shell doesn't change client-construction
    # assertions. Tests that exercise relay routing set them via monkeypatch.
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_CA_BUNDLE", raising=False)

    # Most unit tests exercise an agent in isolation with a fully mocked SDK
    # and no real provider request. Production defaults fail closed when an
    # agent lacks the persistent cost circuit; focused breaker tests override
    # this flag to verify that boundary explicitly.
    from src.agents.base import BaseAgent
    monkeypatch.setattr(BaseAgent, "_allow_unmetered_for_tests", True)

    # The alert-channel watchdog writes its durable check history to an
    # ABSOLUTE path (project-root/data/quant_agent.db) — sessions are
    # started by systemd from any working directory, so it cannot be
    # relative and the chdir above does not contain it. Every session now
    # calls it, so without this redirect an ordinary `main.main()` test
    # would create and append to the developer's real database. Tests that
    # care about the contents point it at their own file.
    from src import alert_watchdog
    monkeypatch.setattr(
        alert_watchdog, "DB_PATH", tmp_path / "watchdog" / "quant_agent.db",
    )
