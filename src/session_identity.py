"""Which account a session is — ONE identity for REST and for the fill socket.

THE DEFECT THIS CLOSES. Alpaca's `trade_updates` websocket authenticates with
an in-band message, so the local credential gateway — which rewrites outbound
REST headers and nothing else — cannot reach it. REST therefore transacts as
whichever account the gateway substitutes for the host being called, while the
socket transacts as whatever key pair the *process* happens to hold. Those two
were resolved from different places, so one session could place orders on a
rehearsal account while receiving the production account's fills. The second
half is the dangerous one: a sandbox session could react to real activity.

THE RULE. A session has exactly one identity. It names itself in the
environment (`QAMC_SESSION_IDENTITY`, defaulting to the desk), and every
credential it presents — REST and socket alike — is resolved for that name.
The delivery directory is *derived from the name* rather than read from one
fixed variable, so a session that is not the desk can never pick up the desk's
delivered pair by inheriting an environment variable it never asked for.

WHY NOT A SANDBOX BRANCH. An `if rehearsal:` arm, or a wrapper callers are
expected to use, is skipped by the next caller who does not know it exists —
and the caller who skips it is exactly the one holding the wrong account. The
identity follows the session instead, through the one resolver every consumer
already goes through.

NOTHING IS STORED. The directory variable is computed from the identity name
at call time, the checkout root is read from this file's own position (see
`src.data_paths`), and no path, list or count is written down anywhere here.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from src.data_paths import repo_root

#: The identity a process has when it does not say otherwise. The production
#: desk declares nothing, so its behaviour is unchanged by this module.
DESK_IDENTITY = "desk"

#: How a session names itself. One variable, read at call time.
IDENTITY_ENV = "QAMC_SESSION_IDENTITY"

#: The variable systemd sets for the desk's own unit. Every other identity
#: gets a name derived from it — see `credentials_directory_var`.
DESK_CREDENTIALS_DIRECTORY_ENV = "CREDENTIALS_DIRECTORY"


class IdentitySplit(RuntimeError):
    """A session was about to act as two different accounts at once.

    Deliberately fatal. The failure it replaces is silent: the session keeps
    running, its observations are contaminated by another account's events,
    and — the part that matters — it can react to that other account's
    activity. There is no safe degraded mode for "we are not sure whose
    account this is", so the only honest response is to refuse.
    """


def session_identity(env: dict | None = None) -> str:
    """The name of the account this session is acting as.

    Lower-cased and stripped so `Rehearsal`, ` rehearsal ` and `rehearsal`
    cannot resolve to three different credential directories.
    """
    raw = (env if env is not None else os.environ).get(IDENTITY_ENV, "")
    return str(raw).strip().lower() or DESK_IDENTITY


def credentials_directory_var(identity: str) -> str:
    """The environment variable that advertises THIS identity's credentials.

    Derived from the name, never looked up in a table: the desk keeps the
    variable systemd already sets, and any other identity must be handed its
    own, so an inherited `CREDENTIALS_DIRECTORY` belonging to the desk unit is
    invisible to a session that is not the desk.
    """
    name = str(identity).strip().lower() or DESK_IDENTITY
    if name == DESK_IDENTITY:
        return DESK_CREDENTIALS_DIRECTORY_ENV
    suffix = "".join(ch if ch.isalnum() else "_" for ch in name).upper()
    return f"{DESK_CREDENTIALS_DIRECTORY_ENV}_{suffix}"


def fingerprint(value: object) -> str:
    """A non-revealing stand-in for a credential, safe in any log or report.

    A one-way digest truncated to twelve hex characters: enough to say "these
    two are the same pair" or "these two differ", and not enough to be any
    part of the credential itself. Nothing here ever returns, logs or formats
    a credential value, not even a prefix of one.
    """
    text = "" if value is None else str(value)
    if not text:
        return "absent"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def writes_outside_own_checkout(cwd: Path | None = None) -> bool:
    """True when the process writes its data somewhere other than its own tree.

    The pipeline's caches (`data/news`, `data/checkpoints`, ...) resolve
    against the working directory, which is how the rehearsal sandbox
    redirects them. So a working directory that is not the checkout root means
    this session's data is landing in somebody else's directory — the signal
    that it is not the plain desk session, whatever it calls itself.
    """
    here = Path(cwd).resolve() if cwd is not None else Path.cwd().resolve()
    return here != repo_root()


def assert_identity_whole(
    *,
    rest_key: object,
    rest_secret: object,
    socket_key: object,
    socket_secret: object,
    env: dict | None = None,
    cwd: Path | None = None,
) -> str:
    """Refuse a session whose REST and socket identities are not the same one.

    Three refusals, every one of them computed here and now from the live
    environment, the live working directory and the pairs actually in hand:

      1. The pair the socket will present differs from the pair REST is using.
         That is the split this module exists to close.
      2. The session calls itself the desk while writing its data outside the
         checkout it is running from — a sandbox tree with production's
         identity, which is the shape the hazard took.
      3. The session is not the desk but no credential directory was
         advertised under its own name, while the desk's was: it would
         otherwise inherit the desk's pair by accident.

    Returns a one-line, value-free description for the caller to log.
    """
    environment = env if env is not None else os.environ
    identity = session_identity(environment)

    rest_mark = f"{fingerprint(rest_key)}/{fingerprint(rest_secret)}"
    socket_mark = f"{fingerprint(socket_key)}/{fingerprint(socket_secret)}"
    # Nothing to compare against is not agreement and not disagreement. A real
    # session — desk or sandbox — always carries its own pair, because that is
    # what `load_config` interpolated from; a process holding none at all is a
    # harness, and inventing a verdict from an absent value would be a made-up
    # answer. Checks 2 and 3 below still apply to it in full.
    rest_present = rest_mark != "absent/absent"
    if rest_present and rest_mark != socket_mark:
        raise IdentitySplit(
            "this session would trade on one account and listen to another: "
            f"its REST credentials ({rest_mark}) are not the credentials the "
            f"fill socket is about to present ({socket_mark}). Refusing to "
            "open the socket — fills from the wrong account would both "
            "corrupt this session's observations and let it react to another "
            f"account's activity. (Session identity: {identity}.)"
        )

    outside = writes_outside_own_checkout(cwd)
    if identity == DESK_IDENTITY and outside:
        raise IdentitySplit(
            "this session is writing its data outside the checkout it is "
            "running from, which means it is a sandbox session, yet it is "
            f"presenting the {DESK_IDENTITY} identity and would receive the "
            f"{DESK_IDENTITY} account's fills. Refusing to open the socket. "
            f"Set {IDENTITY_ENV} to the account this session really is."
        )

    if identity != DESK_IDENTITY:
        own_var = credentials_directory_var(identity)
        if not str(environment.get(own_var, "")).strip():
            if str(environment.get(DESK_CREDENTIALS_DIRECTORY_ENV, "")).strip():
                raise IdentitySplit(
                    f"this session calls itself '{identity}' but no "
                    f"credentials were delivered under {own_var}, while the "
                    f"{DESK_IDENTITY} unit's "
                    f"{DESK_CREDENTIALS_DIRECTORY_ENV} is present. Refusing "
                    "to open the socket rather than inherit the "
                    f"{DESK_IDENTITY} account's key pair by accident."
                )

    where = "its own checkout" if not outside else "a sandbox tree"
    return (
        f"session identity '{identity}' is whole: REST and the fill socket "
        f"both present {rest_mark}, writing to {where}"
    )


def checked_socket_identity(broker: object) -> str:
    """Refuse, at the moment of opening, a socket that is not this session's.

    The one call site is the `trade_updates` stream's `start`. It is on the
    open path itself rather than in a wrapper or a sandbox-only arm, so there
    is nothing for a future caller to forget: the socket cannot be opened
    without passing through it.
    """
    from src.credentials import session_broker_credentials

    rest_key, rest_secret = session_broker_credentials()
    return assert_identity_whole(
        rest_key=rest_key,
        rest_secret=rest_secret,
        socket_key=getattr(broker, "api_key", None),
        socket_secret=getattr(broker, "secret_key", None),
    )
def _credential_fingerprint(credential: str | None) -> str:
    """Length + first two characters of a key. NEVER the value, never a secret.

    Enough to tell a real Alpaca key (26 chars, `PK`/`AK` prefix) from the
    29-character literal containing the word `placeholder` that the process
    actually held until 2026-09-18 — which is the single fact that would
    have ended this investigation on day one. Two characters cannot
    identify an account and cannot be replayed.
    """
    if not credential:
        return "absent (empty)"
    text = str(credential)
    return f"length {len(text)}, starts '{text[:2]}'"


