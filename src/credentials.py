"""Credential delivery — prefer systemd's credentials directory, fall back to the environment.

WHY THIS EXISTS. Alpaca's `trade_updates` websocket authenticates with an
in-band websocket *message*, not an HTTP handshake header. The OneCLI gateway
(`docs/architecture/CREDENTIAL_DELIVERY_EVIDENCE.md`) injects headers into
outbound REST and therefore cannot reach that message, and the installed
`alpaca-py` stream is built on `websockets.legacy`, which has no proxy support
at all. So REST works with a placeholder credential while the socket can never
authenticate. The only way the socket can authenticate is for the desk process
to actually hold the real key — so the job is to deliver it safely rather than
withhold it.

WHAT THIS MODULE DOES. systemd can hand a service a secret as a file instead of
an environment variable. It materialises each credential as one read-only file
inside a per-unit directory on a tmpfs and tells the process where that is via
`CREDENTIALS_DIRECTORY`. Compared with today's plain-text `.env`, the value is
never in the process environment, so it does not appear in `/proc/<pid>/environ`,
is not inherited by every child process, and does not show up in a crash dump
that echoes the environment.

WHAT IT DELIBERATELY DOES NOT DO. It never logs, returns or formats a credential
value into any human-readable output. `describe_delivery()` reports names,
lengths and provenance only. The placeholder check reports a *reason*, never the
value that triggered it.

SCOPE OF THE IMPROVEMENT — STATED HONESTLY. On this box the credential file is
protected by file permissions, not by encryption at rest. systemd's
`LoadCredentialEncrypted=` cannot be used here: the desk runs as `systemd --user`
units under the `qamc` account, and decrypting a host-key credential requires
reading `/var/lib/systemd/credential.secret`, which is mode 0400 root. An
unprivileged user manager gets `Failed to determine local credential key:
Permission denied` and the unit fails at step CREDENTIALS. This was verified on
the box, and upstream systemd tracks it as an open limitation of user-scoped
credentials. The encrypted variant becomes available only if the desk's units
move to the system manager; the reader below is unchanged either way, because
both directives populate the same directory.
"""

import os
from pathlib import Path

# The variable systemd sets for a unit that declares LoadCredential= or
# LoadCredentialEncrypted=. Its absence is the normal, supported case: it means
# nobody wired credentials for this process, so the environment path applies.
CREDENTIALS_DIRECTORY_ENV = "CREDENTIALS_DIRECTORY"

# systemd credential name -> the environment variable name `config/settings.yaml`
# already interpolates. This mapping is the whole contract: a credential named
# `alpaca_api_key` in the unit lands wherever `${ALPACA_API_KEY}` appears, so
# nothing downstream of `load_config` changes shape or learns a new code path.
# Moving another secret onto this delivery path is one row here plus one line in
# the unit — no consumer changes.
CREDENTIAL_ENV_MAP: dict[str, str] = {
    "alpaca_api_key": "ALPACA_API_KEY",
    "alpaca_secret_key": "ALPACA_SECRET_KEY",
}


class CredentialDeliveryError(RuntimeError):
    """systemd said it delivered a credential and the credential is not usable.

    Raised only when there is positive evidence of a broken hand-off: the
    directory was advertised but is missing, or a credential file exists and is
    unreadable or blank. Never raised merely because credentials were not
    configured — that is the fallback case, not a failure.

    This is deliberately fatal. The failure it replaces is the expensive one:
    the desk quietly kept a placeholder, started normally, and only failed later
    at the broker, where the error looked like a broker problem.
    """


def _credentials_directory() -> Path | None:
    """The directory systemd advertised, or None when credentials are not wired."""
    raw = os.environ.get(CREDENTIALS_DIRECTORY_ENV, "").strip()
    if not raw:
        return None
    return Path(raw)


def load_systemd_credentials(env: dict[str, str] | None = None) -> dict[str, str]:
    """Read the mapped credentials systemd delivered.

    Returns a mapping of environment-variable name -> value, ready to be used in
    preference to `os.environ`. Returns an empty mapping when credentials are not
    configured, or when the directory exists but carries none of the names this
    desk knows about — a unit may load credentials for some other purpose, and
    that is not a failure.

    Raises CredentialDeliveryError when the hand-off is visibly broken.
    """
    directory = _credentials_directory()
    if directory is None:
        return {}

    if not directory.is_dir():
        raise CredentialDeliveryError(
            "The system said it had passed the trading credentials to the desk, but the "
            "place it said they were is not there. The desk is refusing to start rather "
            "than fall back to its placeholder key and fail later at the broker. "
            f"(Advertised location: {directory})"
        )

    resolved: dict[str, str] = {}
    for credential_name, env_name in sorted(CREDENTIAL_ENV_MAP.items()):
        path = directory / credential_name
        if not path.exists():
            # Not delivered. The environment path still applies for this one.
            continue
        try:
            raw = path.read_text()
        except OSError as exc:
            raise CredentialDeliveryError(
                f"The trading credential '{credential_name}' was passed to the desk but "
                "could not be read. The desk is refusing to start rather than fall back "
                "to its placeholder key and fail later at the broker. "
                f"(Reason: {exc.strerror or exc})"
            ) from exc

        # A trailing newline is what any ordinary editor or `printf` leaves
        # behind, and a header value must not carry one. Stripping surrounding
        # whitespace is the difference between working and a puzzling 401.
        value = raw.strip()
        if not value:
            raise CredentialDeliveryError(
                f"The trading credential '{credential_name}' was passed to the desk but "
                "it is empty. The desk is refusing to start rather than fall back to its "
                "placeholder key and fail later at the broker."
            )
        resolved[env_name] = value

    return resolved


# ---------------------------------------------------------------------------
# Placeholder detection
# ---------------------------------------------------------------------------
#
# WHY THIS IS SHAPE-BASED AND NOT FORMAT-BASED. The obvious check would be "does
# this look like a real Alpaca key" — a length and a prefix. Alpaca does not
# document either (checked against its own authentication documentation, which
# describes only how to send the credential, never its structure), so any length
# or prefix rule here would be a number invented to look diligent, and this desk
# does not ship those. Worse, it would be a rule that starts *rejecting real
# keys* the day Alpaca changes its issuing format.
#
# So the check runs the other way round: it fires only on positive evidence that
# a human typed a stand-in. Every rule below is either a word a person writes
# when they mean "fill this in later", or a structural impossibility for a
# credential that is sent verbatim as an HTTP header value. None of them carries
# a tunable threshold.
#
# THE LIMIT, STATED PLAINLY: this cannot detect a wrong-but-plausible key. A
# revoked, mistyped or other-account key passes every rule here. Only the broker
# can judge that, which is why the acceptance test for credential delivery is a
# once-only "the socket authenticated" observation and not this check.

# Words a person writes when they mean "replace me". Matched case-insensitively
# as substrings, so `placeholder-alpaca-key` and `PLACEHOLDER` both fire without
# this module ever hardcoding the literal value currently in `.env`.
_PLACEHOLDER_WORDS: tuple[str, ...] = (
    "placeholder",
    "changeme",
    "change-me",
    "change_me",
    "replaceme",
    "replace-me",
    "yourkey",
    "your-key",
    "your_key",
    "yoursecret",
    "your-secret",
    "your_secret",
    "dummy",
    "example",
    "sample",
    "notreal",
    "not-real",
    "fakekey",
    "fake-key",
    "todo",
    "fixme",
    "insert",
    "paste",
    "xxxx",
)


def placeholder_reason(value: str) -> str | None:
    """Return a plain-English reason this value is obviously not a real key, else None.

    The reason never contains the value. Callers log the reason.
    """
    if not value:
        return "it is empty"

    lowered = value.lower()
    for word in _PLACEHOLDER_WORDS:
        if word in lowered:
            return f"it contains the word '{word}', which is what a fill-this-in stand-in looks like"

    # A credential is sent verbatim as an HTTP header value. Whitespace inside
    # one is not a valid credential under any issuing format; it is a copy-paste
    # accident or a sentence someone typed into the field.
    if any(character.isspace() for character in value):
        return "it contains a space or a line break, which a real key never does"

    # One character repeated. No threshold to choose — either the value carries
    # exactly one distinct character or it does not.
    if len(set(value)) == 1:
        return "it is the same character repeated"

    # A credential that is sent as a header value has to be printable ASCII.
    # Anything else is a mangled paste, not a key.
    if any(not (0x21 <= ord(character) <= 0x7E) for character in value):
        return "it contains characters that cannot appear in a real key"

    return None


def looks_like_placeholder(value: str) -> bool:
    """True when `value` is obviously a stand-in rather than a real credential."""
    return placeholder_reason(value) is not None


# ---------------------------------------------------------------------------
# Startup reporting
# ---------------------------------------------------------------------------

def describe_delivery(
    resolved: dict[str, str],
    api_keys: object,
) -> tuple[list[str], list[str]]:
    """Describe how the broker credentials arrived, and flag placeholder-shaped ones.

    Returns `(facts, problems)`. `facts` is safe to log verbatim: it names each
    credential, where it came from and how long it is, and never its value — the
    length alone is enough to tell a rotated key from an unchanged one without
    revealing anything usable. `problems` is the loud part: one plain-English
    line per credential that is obviously a stand-in.
    """
    facts: list[str] = []
    problems: list[str] = []

    # Field name on ApiKeysConfig -> the environment variable it interpolates
    # from, so provenance can be reported per credential.
    broker_fields = {
        "alpaca_key": "ALPACA_API_KEY",
        "alpaca_secret": "ALPACA_SECRET_KEY",
    }

    for field_name, env_name in broker_fields.items():
        value = getattr(api_keys, field_name, "") or ""
        source = "systemd credential" if env_name in resolved else "environment (.env)"
        facts.append(
            f"{field_name}: delivered via {source}, {len(value)} characters"
        )
        reason = placeholder_reason(value)
        if reason is not None:
            problems.append(
                f"The desk's {field_name} is not a real trading credential — {reason}. "
                "Anything that needs the broker to authenticate the desk itself will "
                "fail, including the live fill websocket."
            )

    return facts, problems


def report_startup_credentials(api_keys: object, *, logger, alert: bool = True) -> list[str]:
    """Log how the broker credentials arrived and shout about placeholder-shaped ones.

    THE FAILURE THIS EXISTS TO PREVENT. The most expensive failure in this
    project's history was nobody noticing the desk held a placeholder key for
    eight days, while five separate attempts optimised the timing of a handshake
    that could never have succeeded with the key it was presenting. Nothing said
    so at startup. This does, every single start, in the log the owner's alert
    path can reach.

    Returns the list of problems, so a caller may treat them as it likes. Logging
    and the owner alert are best-effort and never prevent startup: refusing to
    start because a *notification* failed would take the desk down for the wrong
    reason. A visibly broken credential hand-off is the fatal case, and that is
    raised earlier, in `load_config`.
    """
    resolved = load_systemd_credentials()
    facts, problems = describe_delivery(resolved, api_keys)

    for fact in facts:
        logger.info("credential delivery — %s", fact)

    for problem in problems:
        logger.error("PLACEHOLDER CREDENTIAL — %s", problem)

    if problems and alert:
        try:
            from src.notifier import send_owner_alert

            # One message per problem: the desk's standing alert rule is that
            # every failure gets its own Telegram message rather than being
            # folded into a run summary where it can be skimmed past.
            for problem in problems:
                send_owner_alert(f"Placeholder trading credential in use. {problem}")
        except Exception as exc:  # noqa: BLE001 - notification must never block startup
            logger.warning("could not alert the owner about a placeholder credential: %s", exc)

    return problems
