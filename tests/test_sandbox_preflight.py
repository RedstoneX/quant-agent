"""The sandbox refuses production, and cannot message the owner.

Each test names one non-recoverable hazard from the plan in
docs/MARKET_HOURS_BACKLOG.md and proves the refusal fires, rather than
proving a warning is logged. A test that only asserted "no exception on the
happy path" would pass with every check deleted, so every check here has a
paired positive case.
"""

import hashlib
from pathlib import Path

import pytest

from src.sandbox_preflight import (
    SANDBOX_MARKER_NAME,
    SandboxEnvironment,
    SandboxRefusal,
    check_account_key_is_the_pinned_sandbox_one,
    check_data_dir_is_local,
    check_database_is_the_sandbox_one,
    check_home_is_not_production,
    check_not_production_checkout,
    check_owner_channel_is_incapable,
    check_paper_lock_still_holds,
)

SANDBOX_KEY = "PK_DISPOSABLE_SANDBOX_KEY"
SANDBOX_PIN = hashlib.sha256(SANDBOX_KEY.encode("utf-8")).hexdigest()


def _clean_env(**overrides: str) -> dict[str, str]:
    """An environment that passes every check, before the test spoils one."""
    env = {
        "TELEGRAM_DISABLED": "1",
        "ALPACA_API_KEY": SANDBOX_KEY,
        "QAMC_SANDBOX_ALPACA_KEY_SHA256": SANDBOX_PIN,
    }
    env.update(overrides)
    return env


def _environment(tmp_path: Path, **overrides: str) -> SandboxEnvironment:
    return SandboxEnvironment(checkout=tmp_path, env=_clean_env(**overrides))


def test_refuses_to_run_from_the_production_checkout(tmp_path: Path) -> None:
    production = tmp_path / "qamc"
    checkout = production / "quant-agent"
    checkout.mkdir(parents=True)
    environment = SandboxEnvironment(
        checkout=checkout,
        env=_clean_env(QAMC_PRODUCTION_CHECKOUT=str(production)),
    )
    with pytest.raises(SandboxRefusal, match="production checkout"):
        check_not_production_checkout(environment)


def test_allows_a_checkout_outside_production(tmp_path: Path) -> None:
    production = tmp_path / "qamc"
    production.mkdir()
    checkout = tmp_path / "sandbox-checkout"
    checkout.mkdir()
    environment = SandboxEnvironment(
        checkout=checkout,
        env=_clean_env(QAMC_PRODUCTION_CHECKOUT=str(production)),
    )
    check_not_production_checkout(environment)


def test_refuses_a_data_dir_symlinked_into_production(tmp_path: Path) -> None:
    production_data = tmp_path / "qamc" / "data"
    production_data.mkdir(parents=True)
    checkout = tmp_path / "sandbox-checkout"
    checkout.mkdir()
    local_data = checkout / "data"
    local_data.symlink_to(production_data)
    with pytest.raises(SandboxRefusal, match="outside its own checkout"):
        check_data_dir_is_local(_environment(checkout), local_data)


def test_allows_the_checkouts_own_data_dir(tmp_path: Path) -> None:
    local_data = tmp_path / "data"
    local_data.mkdir()
    check_data_dir_is_local(_environment(tmp_path), local_data)


def test_refuses_a_database_no_sandbox_run_created(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "quant_agent.db"
    db_path.write_bytes(b"")
    with pytest.raises(SandboxRefusal, match="may be production"):
        check_database_is_the_sandbox_one(data_dir, db_path)


def test_allows_a_marked_sandbox_database(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "quant_agent.db"
    db_path.write_bytes(b"")
    (data_dir / SANDBOX_MARKER_NAME).write_text("sandbox")
    check_database_is_the_sandbox_one(data_dir, db_path)


def test_allows_an_empty_data_dir(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    check_database_is_the_sandbox_one(data_dir, data_dir / "quant_agent.db")


@pytest.mark.parametrize("variable", ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"])
def test_refuses_an_inherited_owner_messaging_credential(
    tmp_path: Path, variable: str
) -> None:
    environment = _environment(tmp_path, **{variable: "inherited-from-the-desk"})
    with pytest.raises(SandboxRefusal, match="messaging credentials"):
        check_owner_channel_is_incapable(environment)


@pytest.mark.parametrize("spelling", ["0", "false", "off", ""])
def test_refuses_a_mute_spelling_the_notifier_ignores(
    tmp_path: Path, spelling: str
) -> None:
    environment = _environment(tmp_path, TELEGRAM_DISABLED=spelling)
    with pytest.raises(SandboxRefusal, match="TELEGRAM_DISABLED"):
        check_owner_channel_is_incapable(environment)


@pytest.mark.parametrize("spelling", ["1", "true", "YES"])
def test_accepts_the_mute_spellings_the_notifier_honours(
    tmp_path: Path, spelling: str
) -> None:
    check_owner_channel_is_incapable(_environment(tmp_path, TELEGRAM_DISABLED=spelling))


def test_refuses_a_key_that_is_not_the_pinned_sandbox_one(tmp_path: Path) -> None:
    environment = _environment(tmp_path, ALPACA_API_KEY="PK_SOME_OTHER_ACCOUNT")
    with pytest.raises(SandboxRefusal, match="NOT the pinned"):
        check_account_key_is_the_pinned_sandbox_one(environment)


def test_refuses_when_no_sandbox_key_is_pinned(tmp_path: Path) -> None:
    environment = _environment(tmp_path, QAMC_SANDBOX_ALPACA_KEY_SHA256="")
    with pytest.raises(SandboxRefusal, match="not pinned"):
        check_account_key_is_the_pinned_sandbox_one(environment)


def test_accepts_the_pinned_sandbox_key(tmp_path: Path) -> None:
    check_account_key_is_the_pinned_sandbox_one(_environment(tmp_path))


def test_paper_lock_is_re_asserted_not_relaxed() -> None:
    check_paper_lock_still_holds(True, "https://paper-api.alpaca.markets")
    with pytest.raises(SandboxRefusal, match="not paper-only"):
        check_paper_lock_still_holds(False, "https://paper-api.alpaca.markets")
    with pytest.raises(SandboxRefusal, match="not Alpaca's paper host"):
        check_paper_lock_still_holds(True, "https://api.alpaca.markets")


def test_no_credential_is_committed_in_the_sandbox_env_example() -> None:
    """The example file carries placeholders only."""
    repo_root = Path(__file__).resolve().parent.parent
    text = (repo_root / "config" / "sandbox.env.example").read_text(encoding="utf-8")
    assert "ALPACA_API_KEY=PK..." in text
    assert "QAMC_SANDBOX_ALPACA_KEY_SHA256=" + "0" * 64 in text
    assert "TELEGRAM_BOT_TOKEN=" not in text
    assert "TELEGRAM_CHAT_ID=" not in text


def test_the_wrapper_strips_the_owner_channel_before_sourcing() -> None:
    """The structural half of the guarantee lives in the wrapper, so pin it."""
    repo_root = Path(__file__).resolve().parent.parent
    script = (repo_root / "scripts" / "sandbox_session.sh").read_text(encoding="utf-8")
    assert script.count("unset TELEGRAM_BOT_TOKEN") == 2
    assert script.count("unset TELEGRAM_CHAT_ID") == 2
    assert "export TELEGRAM_DISABLED=1" in script
    assert "-m src.sandbox_preflight" in script


def test_refuses_a_home_directory_inside_production(tmp_path: Path) -> None:
    production = tmp_path / "qamc"
    production.mkdir()
    refused = SandboxEnvironment(
        checkout=tmp_path,
        env=_clean_env(QAMC_PRODUCTION_CHECKOUT=str(production), HOME=str(production)),
    )
    with pytest.raises(SandboxRefusal, match="home directory"):
        check_home_is_not_production(refused)
    allowed = SandboxEnvironment(
        checkout=tmp_path,
        env=_clean_env(QAMC_PRODUCTION_CHECKOUT=str(production), HOME=str(tmp_path / "other")),
    )
    check_home_is_not_production(allowed)


def test_refuses_a_model_provider_key_the_run_never_routes_to(tmp_path: Path) -> None:
    from src.sandbox_credential_scope import credential_scope_violations

    held = credential_scope_violations({"ANTHROPIC_API_KEY": "x", "OPENAI_API_KEY": "y", "GOOGLE_API_KEY": "z"})
    assert len(held) == 2
    assert all("GOOGLE" not in item for item in held)


def test_the_allow_list_and_the_forbidden_list_never_overlap() -> None:
    from src.sandbox_credential_scope import SANDBOX_FORBIDDEN, SANDBOX_NEEDS

    assert not SANDBOX_NEEDS & set(SANDBOX_FORBIDDEN)


def test_the_scope_check_never_echoes_a_value() -> None:
    from src.sandbox_credential_scope import credential_scope_violations

    assert "SECRETVALUE" not in "".join(credential_scope_violations({"OPENAI_API_KEY": "SECRETVALUE"}))
