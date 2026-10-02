"""Notification settings (moved out of src/config/__init__.py; re-exported there)."""

from pydantic import BaseModel, field_validator


class NotificationsConfig(BaseModel):
    """Where Telegram alerts point the operator back into Mission Control.

    The operator reads these on his phone. He got a BUY CRM alert whose
    rationale read "...strong heavy accumulation volume" and just stopped
    there mid-sentence, with no way to see the rest or jump into the
    dashboard for the full picture. `mission_control_url` is the tap-through
    target `TelegramNotifier.send()` appends as an HTML link to relevant
    alerts (see src/notifier.py, src/trader_feed.py). An empty string
    disables the link entirely — never emit a broken one instead.

    Defaults to the tailnet address Tailscale Serve exposes for the qamc
    API (`ovh-vps.wallaby-bowfin.ts.net`, proxying tailnet-only port 443 to
    the API on 127.0.0.1:8800), which mounts the cockpit
    (`app.mount("/cockpit", ...)` in src/api/server.py). Unreachable from
    the public internet, matching Mission Control's "private, read-only,
    non-critical to trading" posture.
    """

    risk_only: bool = False
    """Per-category Telegram mute: only money-at-risk messages are delivered, operational
    ones are dropped and recorded (status `filtered`). Declared here so the feature
    registry fails the build if the switch sits in an unchosen state; the
    `TELEGRAM_RISK_ONLY` env var overrides it at runtime (src/notifier/category.py)."""

    mission_control_url: str = "https://ovh-vps.wallaby-bowfin.ts.net/cockpit/"
    """Base URL Telegram alerts link to. Empty string = no link. Must be
    http(s) when non-empty — the value lands inside an href="..." attribute,
    and rejecting other schemes here (e.g. an accidental "javascript:") is
    cheaper than relying on Telegram's client-side handling of it."""

    @field_validator("mission_control_url")
    @classmethod
    def _validate_scheme(cls, v: str) -> str:
        v = v.strip()
        if v and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError(
                "notifications.mission_control_url must be http:// or "
                "https:// (or empty, to disable the link) — got: " + v
            )
        return v
