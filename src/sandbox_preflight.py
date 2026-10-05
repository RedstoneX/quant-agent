"""Refuse to start a sandbox session that could touch production.

WHY THIS EXISTS. `docs/MARKET_HOURS_BACKLOG.md` ("PLAN — pointing a FULL daily
session at the disposable paper account") lists the hazards a sandbox session
cannot undo by resetting the paper account: an owner-facing Telegram message
sent with the desk's own bot token, writes landing in the production database
because every writer resolves its path from the checkout it runs from, and the
desk's own paper key pair being reused so the real desk's orders are cancelled.
The plan recorded them as warnings. A warning is not a control, so this module
turns each one into a refusal that happens BEFORE anything is constructed.

WHAT IT DOES NOT DO. It never relaxes anything. The paper-only lock in
`AlpacaConfig` is read here and re-asserted, never bypassed; this module can
only refuse to start a session that the rest of the desk would have allowed.

HOW THE ACCOUNT IS PINNED. The sandbox key pair is identified by an ALLOW-LIST
of one: `QAMC_SANDBOX_ALPACA_KEY_SHA256` must be the SHA-256 of the
`ALPACA_API_KEY` the process holds. A deny-list of known-bad keys would pass
any key nobody thought to list, including a freshly rotated production one. An
allow-list refuses every key except the one the operator deliberately pinned,
so the production key and the desk's own paper key are both refused without
either ever being named in this repo. The pinned value is a one-way digest
supplied from the environment; no key, secret, token or account id is committed.
"""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

#: Default location of the production checkout. The desk runs as the `qamc`
#: account and everything it writes resolves relative to its own checkout, so
#: a sandbox session standing anywhere underneath this is writing production
#: data no matter which credentials it holds. Overridable (and extendable, as
#: a colon-separated list) via `QAMC_PRODUCTION_CHECKOUT` for a box where the
#: desk lives elsewhere; it can only ever add refusals.
DEFAULT_PRODUCTION_CHECKOUTS = ("/home/qamc",)

#: Written by `scripts/sandbox_session.sh` into the sandbox's own `data/`.
#: Its absence beside an existing database is the signal that the database
#: was not created by a sandbox run, which is the production-database case.
SANDBOX_MARKER_NAME = "SANDBOX_ACCOUNT"

#: The spellings `src/notifier/transport.py` actually honours. Anything else,
#: INCLUDING "0" and "false", leaves Telegram pushes ON, so the sandbox must
#: insist on one of these three rather than on "the variable is set".
TELEGRAM_DISABLED_SPELLINGS = frozenset({"1", "true", "yes"})

#: Telegram credentials that must not be present at all. `TELEGRAM_DISABLED`
#: alone is one env var away from being lost; an inherited token with no
#: chat id (or vice versa) is already a silent no-op, but both together plus
#: a dropped mute is an owner-facing message. Requiring both to be absent
#: means the process is incapable of addressing the owner, not merely muted.
TELEGRAM_CREDENTIAL_VARS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")

#: The pinned digest of the disposable account's key. Hex, lower case, 64 chars.
SANDBOX_KEY_PIN_VAR = "QAMC_SANDBOX_ALPACA_KEY_SHA256"


class SandboxRefusal(RuntimeError):
    """The sandbox is pointed at something it must not touch.

    Deliberately fatal and deliberately not downgradable to a warning. Every
    condition this names is non-recoverable by resetting the paper account:
    a message the owner already read, a row already written into the
    production database, or a cancelled order on the desk's real paper
    account. There is no state in which continuing is better than stopping.
    """


@dataclass(frozen=True)
class SandboxEnvironment:
    """Everything the checks read, captured so a test can supply it."""

    checkout: Path
    env: dict[str, str]


def _production_checkouts(env: dict[str, str]) -> tuple[Path, ...]:
    """Paths a sandbox session must never stand inside."""
    raw = env.get("QAMC_PRODUCTION_CHECKOUT", "").strip()
    if not raw:
        configured = DEFAULT_PRODUCTION_CHECKOUTS
    else:
        configured = tuple(part for part in raw.split(":") if part.strip())
    return tuple(Path(part).expanduser() for part in configured)


def _is_within(child: Path, parent: Path) -> bool:
    """True when `child` is `parent` or sits underneath it."""
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def check_not_production_checkout(environment: SandboxEnvironment) -> None:
    """Refuse when the session would run from the production checkout."""
    checkout = environment.checkout.resolve()
    for production in _production_checkouts(environment.env):
        if not _is_within(checkout, production):
            continue
        raise SandboxRefusal(
            "Refusing to start: this sandbox session is standing inside the "
            "production checkout, where every writer resolves its data path "
            "from the checkout it runs from, so sandbox trades would be "
            f"written into the production database. (Checkout: {checkout}; "
            f"production: {production}.) Make a separate checkout with its "
            "own empty data directory and run from there."
        )


def check_home_is_not_production(environment: SandboxEnvironment) -> None:
    """Refuse when the home directory is the production account's.

    The session lock and last-run markers are anchored to the home directory,
    not to the checkout, so a process whose home is production's would write
    them into production even from a separate checkout.
    """
    home = Path(environment.env.get("HOME") or Path.home()).resolve()
    for production in _production_checkouts(environment.env):
        if _is_within(home, production):
            raise SandboxRefusal(
                "Refusing to start: this process's home directory is inside "
                "the production account, and the session lock and last-run "
                f"markers live under the home directory. (Home: {home}; "
                f"production: {production}.) Run as a different account."
            )


def check_data_dir_is_local(environment: SandboxEnvironment, data_dir: Path) -> None:
    """Refuse when the data directory escapes the sandbox checkout.

    Resolved, so a symlink from the sandbox's own `data/` into the production
    one is caught. That symlink is the failure this check exists for: the
    checkout looks separate and every write still lands in production.
    """
    checkout = environment.checkout.resolve()
    resolved = data_dir.resolve()
    if _is_within(resolved, checkout):
        return
    raise SandboxRefusal(
        "Refusing to start: the sandbox's data directory resolves to a "
        "location outside its own checkout, so its writes would leave the "
        f"sandbox. (Data directory: {resolved}; checkout: {checkout}.) "
        "Remove the symlink or redirection and use the checkout's own data "
        "directory."
    )


def check_database_is_the_sandbox_one(data_dir: Path, db_path: Path) -> None:
    """Refuse when a database exists that no sandbox run created.

    A fresh sandbox checkout has an empty `data/`. A database already sitting
    there without the marker file is somebody else's history — in the worst
    case a copy of production — and appending a sandbox session to it makes
    the two indistinguishable afterwards.
    """
    resolved_db = db_path.resolve()
    if not resolved_db.exists():
        return
    marker = (data_dir / SANDBOX_MARKER_NAME).resolve()
    if marker.exists():
        return
    raise SandboxRefusal(
        "Refusing to start: a database already exists in this data directory "
        "and it was not created by a sandbox run, so it may be production "
        f"history. (Database: {resolved_db}; expected marker: {marker}.) "
        "Start from an empty data directory."
    )


def check_owner_channel_is_incapable(environment: SandboxEnvironment) -> None:
    """Refuse unless the process cannot address the owner at all."""
    env = environment.env
    present = tuple(name for name in TELEGRAM_CREDENTIAL_VARS if env.get(name, "").strip())
    if present:
        raise SandboxRefusal(
            "Refusing to start: this sandbox session inherited the owner's "
            "messaging credentials, so it could send the owner a message he "
            "would reasonably read as the real desk. (Inherited: "
            f"{', '.join(present)}.) Unset them for the sandbox process; a "
            "mute switch is not enough, because losing the switch restores "
            "the ability to message him."
        )
    mute = env.get("TELEGRAM_DISABLED", "").strip().lower()
    if mute in TELEGRAM_DISABLED_SPELLINGS:
        return
    raise SandboxRefusal(
        "Refusing to start: the sandbox does not have the owner-facing "
        "channel switched off. TELEGRAM_DISABLED must be one of "
        f"{sorted(TELEGRAM_DISABLED_SPELLINGS)} — the notifier ignores every "
        f"other value, including '0' and 'false'. (Got: '{mute}'.)"
    )


def check_account_key_is_the_pinned_sandbox_one(environment: SandboxEnvironment) -> None:
    """Refuse unless the Alpaca key is the one the operator pinned."""
    env = environment.env
    pin = env.get(SANDBOX_KEY_PIN_VAR, "").strip().lower()
    key = env.get("ALPACA_API_KEY", "").strip()
    if not pin:
        raise SandboxRefusal(
            "Refusing to start: the disposable account's key is not pinned, "
            f"so the session cannot tell it apart from the production key or "
            f"the desk's own paper key. Set {SANDBOX_KEY_PIN_VAR} to the "
            "SHA-256 of the sandbox ALPACA_API_KEY (see "
            "config/sandbox.env.example)."
        )
    if not key:
        raise SandboxRefusal(
            "Refusing to start: no ALPACA_API_KEY is present, so there is "
            "nothing to check the sandbox pin against."
        )
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    if digest == pin:
        return
    raise SandboxRefusal(
        "Refusing to start: the Alpaca key this process holds is NOT the "
        "pinned disposable sandbox key. It may be the production key or the "
        "desk's own paper key, and trading either from here would cancel the "
        "real desk's orders. Pin only the disposable account's key in "
        f"{SANDBOX_KEY_PIN_VAR}."
    )


def check_paper_lock_still_holds(paper: bool, base_url: str) -> None:
    """Re-assert the existing paper-only lock; never relax it."""
    if paper is not True:
        raise SandboxRefusal(
            "Refusing to start: the configuration is not paper-only. The "
            "sandbox never reaches live trading."
        )
    if "paper-api.alpaca.markets" in base_url:
        return
    raise SandboxRefusal(
        "Refusing to start: the broker base URL is not Alpaca's paper host. "
        f"(Got: {base_url}.)"
    )


def run_preflight(checkout: Path, env: dict[str, str] | None = None) -> None:
    """Run every refusal. Returns None, or raises `SandboxRefusal`.

    Ordered cheapest and most catastrophic first: where the process is
    standing, then whether it can speak to the owner, then which account it
    holds. Loading the config is last because it is the only step that reads
    a file and because it is pointless if the earlier answers are wrong.
    """
    from src.config import load_config

    environment = SandboxEnvironment(checkout=checkout, env=dict(env or os.environ))
    check_not_production_checkout(environment)
    check_home_is_not_production(environment)
    check_owner_channel_is_incapable(environment)
    check_account_key_is_the_pinned_sandbox_one(environment)
    config = load_config(checkout / "config" / "settings.yaml")
    check_paper_lock_still_holds(config.alpaca.paper, config.alpaca.base_url)
    db_path = checkout / config.storage.db_path
    data_dir = db_path.parent
    check_data_dir_is_local(environment, data_dir)
    check_database_is_the_sandbox_one(data_dir, db_path)


def main() -> int:
    """Exit 0 when the sandbox is safe to run, 1 with the reason when it is not."""
    checkout = Path(__file__).resolve().parent.parent
    try:
        run_preflight(checkout)
    except SandboxRefusal as refusal:
        print(f"SANDBOX PREFLIGHT REFUSED\n{refusal}")
        return 1
    print(f"SANDBOX PREFLIGHT PASSED for checkout {checkout}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
