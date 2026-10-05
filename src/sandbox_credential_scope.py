"""The credential names a sandbox session may hold, and the ones it must not.

A sandbox session is a full trading day against a disposable paper account.
It is explicitly safe to break, so it must not hold anything that reaches
outside that account: the owner's messaging channel, a model provider the run
never routes to, or repository access.

The allow-list below was derived from what the run actually reads, not from
what production holds:

* `config/settings.yaml` routes every seat to either `google` (the analyst,
  evening and reflector seats) or `openrouter` (portfolio manager, risk,
  position reviewer, and both failover routes). Nothing routes to `anthropic`,
  `openai` or `deepseek` directly; OpenAI is only reached through OpenRouter.
* `ApiKeysConfig` insists on a FRED key (free data, no spend) and the broker
  pair, which the preflight separately pins to the disposable account.

Only NAMES appear here. No value is read, returned or logged by this module.
"""

from __future__ import annotations

SANDBOX_NEEDS: frozenset[str] = frozenset({
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "FRED_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
})

#: Names the sandbox must not hold, each with the reason it has no use for it.
SANDBOX_FORBIDDEN: dict[str, str] = {
    "TELEGRAM_BOT_TOKEN": "owner-notification channel",
    "TELEGRAM_CHAT_ID": "owner-notification channel",
    "ANTHROPIC_API_KEY": "no seat routes to Anthropic directly",
    "OPENAI_API_KEY": "OpenAI is only reached through OpenRouter",
    "DEEPSEEK_API_KEY": "no seat routes to DeepSeek",
    "GH_TOKEN": "repository access is not used by a trading day",
    "GITHUB_TOKEN": "repository access is not used by a trading day",
    "CREDENTIALS_DIRECTORY": "production systemd credential hand-off",
}


def credential_scope_violations(env: dict[str, str]) -> list[str]:
    """Names in `env` that the sandbox must not hold, with the reason. Never values."""
    return [
        f"{name} ({reason})"
        for name, reason in SANDBOX_FORBIDDEN.items()
        if env.get(name, "").strip()
    ]
