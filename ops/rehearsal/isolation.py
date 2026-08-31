"""Sandbox construction for a full-session rehearsal.

A rehearsal runs the REAL trading pipeline. That is the whole point — the
failures this repo actually ships are interactions between real components,
not component bugs, and 2,516 mocked unit tests have never once caught one.
But "real pipeline" and "must not touch production" are only compatible if
isolation is *structural*: enforced by construction and then asserted, never
merely intended.

Three independent walls, each asserted before a session is allowed to start:

  1. **Filesystem.** The scratch tree is built by `Sandbox.prepare`, which
     copies production's SQLite database with ``VACUUM INTO`` (a consistent
     snapshot taken through SQLite's own reader — never ``cp`` on a file
     another process may be mid-write on) and copies the on-disk caches.
     `assert_isolated` then re-derives every path the pipeline will use and
     refuses to proceed unless each one resolves inside the sandbox root.

     Two of those paths are NOT configurable. `MacroStore`, `NewsStore`,
     `TechStore`, `EarningsDataProvider`, `CompanyProfileStore` and
     `src.decision_checkpoint` all hardcode relative defaults (``data/news``,
     ``data/checkpoints``, ...) and are constructed with no arguments in
     `TradingPipeline.__init__`. The only lever that redirects them is the
     process working directory, so `Sandbox.activate` chdirs and
     `assert_isolated` checks the cwd is the sandbox root. This is a real
     constraint of the code under test, not a shortcut.

  2. **Network.** `no_network()` replaces the socket-level connect calls for
     the duration of the rehearsal, so a provider SDK, yfinance, FRED, an RSS
     feed or the Alpaca REST client cannot reach anything off-box no matter
     which layer tries. Loopback is left alone (nothing in the session uses
     it, but blocking it would break unrelated tooling). This is what makes
     "cannot reach production's broker account" structural rather than a
     promise about credentials.

  3. **Credentials.** The rehearsal config carries sentinel API keys, so even
     if walls 1 and 2 both failed, the Alpaca client would be authenticating
     as nobody. `assert_isolated` checks the sentinels are in place.

Finally `ProductionWitness` stats the production database before and after and
raises if a single byte moved. That is the belt to the three suspenders: it
cannot prevent damage, but it makes silent damage impossible.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Written into the rehearsal config in place of real credentials. Chosen to be
# obviously non-functional and greppable in any log line that leaks one.
SENTINEL_KEY = "REHEARSAL-NOT-A-REAL-KEY"

# Never copied into the sandbox: SQLite sidecars (the VACUUM INTO copy is a
# standalone, fully-checkpointed database and a stale -wal/-shm beside it is
# actively harmful), operator backups, and the evening replay archive, which is
# large and is read by no session path.
DEFAULT_DATA_EXCLUDES = (
    "quant_agent.db",
    "quant_agent.db-wal",
    "quant_agent.db-shm",
    "evening_replays",
)


class IsolationError(RuntimeError):
    """A rehearsal was about to run without provable isolation."""


class NetworkBlocked(OSError):
    """A rehearsal tried to open an off-box socket."""


@dataclass
class Sandbox:
    """A prepared scratch tree that a rehearsal session may write to freely."""

    root: Path
    db_path: Path
    data_dir: Path
    source_db: Path
    source_data_dir: Path | None = None
    copied_bytes: int = 0
    notes: list[str] = field(default_factory=list)

    # ---------------------------------------------------------------- build

    @classmethod
    def prepare(
        cls,
        source_db: str | Path,
        root: str | Path,
        *,
        source_data_dir: str | Path | None = None,
        data_excludes: tuple[str, ...] = DEFAULT_DATA_EXCLUDES,
        sudo_user: str | None = None,
    ) -> "Sandbox":
        """Build a scratch tree from a production database + data directory.

        `sudo_user` exists because on this deployment the production database
        is owned by the `qamc` service account and is not readable by the
        account that runs rehearsals. When set, the ``VACUUM INTO`` is executed
        as that user via ``sudo -n -u``; the snapshot it writes is then
        chowned into the sandbox. The source is opened read-only either way.
        """
        source_db = Path(source_db).resolve()
        root = Path(root).resolve()
        data_dir = root / "data"
        root.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = data_dir / "quant_agent.db"

        notes: list[str] = []
        if sudo_user:
            _vacuum_into_as_user(source_db, db_path, sudo_user)
            notes.append(
                f"database snapshot taken as user '{sudo_user}' "
                f"(production database is not readable by this account)"
            )
        else:
            vacuum_into(source_db, db_path)

        copied = db_path.stat().st_size
        if source_data_dir is not None:
            source_data_dir = Path(source_data_dir).resolve()
            copied += _copy_data_tree(
                source_data_dir, data_dir, data_excludes, sudo_user=sudo_user,
            )
            notes.append(
                f"on-disk caches copied from {source_data_dir} "
                f"(excluded: {', '.join(data_excludes)})"
            )
        else:
            notes.append(
                "no data directory copied — earnings/news/macro/profile caches "
                "are empty in this rehearsal and every provider that would "
                "normally read them degrades"
            )

        return cls(
            root=root,
            db_path=db_path,
            data_dir=data_dir,
            source_db=source_db,
            source_data_dir=source_data_dir,
            copied_bytes=copied,
            notes=notes,
        )

    # ------------------------------------------------------------- activate

    @contextmanager
    def activate(self):
        """Chdir into the sandbox for the duration of the block.

        The pipeline's disk-backed stores resolve relative paths against the
        process cwd (see the module docstring), so this is the only lever that
        redirects them. Restored unconditionally.
        """
        previous = Path.cwd()
        os.chdir(self.root)
        try:
            yield self
        finally:
            os.chdir(previous)

    def contains(self, path: str | Path) -> bool:
        """True when `path` resolves inside this sandbox."""
        try:
            resolved = Path(path).resolve()
        except (OSError, ValueError):
            return False
        return resolved == self.root or self.root in resolved.parents


# ------------------------------------------------------------------ copying


def vacuum_into(source_db: str | Path, dest: str | Path) -> Path:
    """Snapshot a live SQLite database into `dest` via ``VACUUM INTO``.

    ``VACUUM INTO`` runs inside a read transaction, so the copy is a single
    consistent point-in-time image even while another process is writing —
    which production is, on a 30-minute intra-check timer. Copying the file
    with ``cp`` instead can capture a torn page set or miss the WAL entirely.

    The source is opened with ``mode=ro`` in a URI so the connection itself is
    incapable of writing to production.
    """
    source_db = Path(source_db)
    dest = Path(dest)
    if dest.exists():
        dest.unlink()
    uri = f"file:{source_db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("VACUUM INTO ?", (str(dest),))
    finally:
        conn.close()
    return dest


def _vacuum_into_as_user(source_db: Path, dest: Path, user: str) -> Path:
    """``VACUUM INTO`` executed as another POSIX account, then handed back.

    The snapshot lands in a world-readable staging path the target user can
    write and this account can read, and is then moved into the sandbox.
    """
    import subprocess
    import tempfile

    staging = Path(tempfile.gettempdir()) / f"rehearsal-snapshot-{os.getpid()}.db"
    if staging.exists():
        staging.unlink()
    script = (
        "import sqlite3, sys\n"
        "src, dst = sys.argv[1], sys.argv[2]\n"
        "c = sqlite3.connect('file:' + src + '?mode=ro', uri=True)\n"
        "c.execute('VACUUM INTO ?', (dst,))\n"
        "c.close()\n"
    )
    result = subprocess.run(
        ["sudo", "-n", "-u", user, "python3", "-c", script,
         str(source_db), str(staging)],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        raise IsolationError(
            f"could not snapshot {source_db} as user '{user}': "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    subprocess.run(
        ["sudo", "-n", "-u", user, "chmod", "0644", str(staging)],
        capture_output=True, text=True, timeout=60,
    )
    shutil.copy2(staging, dest)
    subprocess.run(
        ["sudo", "-n", "-u", user, "rm", "-f", str(staging)],
        capture_output=True, text=True, timeout=60,
    )
    return dest


def _copy_data_tree(
    source: Path, dest: Path, excludes: tuple[str, ...],
    *, sudo_user: str | None = None,
) -> int:
    """Copy the on-disk cache tree into the sandbox. Returns bytes copied.

    Copies rather than links: a hardlink or symlink would let an in-place
    write inside the session reach production's inode, which is exactly the
    thing this module exists to make impossible.
    """
    if sudo_user:
        return _copy_data_tree_as_user(source, dest, excludes, sudo_user)
    total = 0
    for entry in sorted(source.iterdir()):
        if entry.name in excludes or entry.name.startswith("quant_agent.db"):
            continue
        target = dest / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, dirs_exist_ok=True, copy_function=shutil.copy2)
            total += sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
        elif entry.is_file():
            shutil.copy2(entry, target)
            total += target.stat().st_size
    return total


def _copy_data_tree_as_user(
    source: Path, dest: Path, excludes: tuple[str, ...], user: str,
) -> int:
    """Copy a data tree owned by another account into the sandbox.

    ``cp -a`` runs as the owning user and writes into the sandbox, then
    ownership is handed back to us. Timestamps are preserved (``-a``) because
    at least one consumer — the OpenRouter pricing cache — treats file mtime
    as a freshness signal, so a copy that reset every mtime to "now" would be
    silently declaring every cache in the tree current.

    That preserved mtime is the *starting* point, not the final one, for the
    pricing cache specifically: `ops/rehearsal/runner.py` then stamps it with
    the age the rehearsal declared (`pricing_cache_age_hours`) inside the
    sandbox, so a rehearsal's verdict does not depend on when a paid session
    last happened to refresh production's copy. See that module's docstring.
    Nothing here or there ever writes to the production file.
    """
    import subprocess

    exclude_args: list[str] = []
    for name in excludes:
        exclude_args.append(f"--exclude={name}")
    exclude_args.append("--exclude=quant_agent.db*")
    # Copy as ROOT, not as `user`. The destination is created by the invoking
    # account (typically `ubuntu`), so an rsync running as `qamc` cannot write
    # into it — it can read the source but not the target, which fails with a
    # wall of per-directory permission errors. Root can read the production
    # tree and write the sandbox, and the ownership is corrected immediately
    # below so nothing is left root-owned. The production tree is only ever
    # the rsync SOURCE, so this cannot write to it.
    result = subprocess.run(
        ["sudo", "-n", "rsync", "-a", *exclude_args,
         f"{source}/", f"{dest}/"],
        capture_output=True, text=True, timeout=900,
    )
    if result.returncode != 0:
        raise IsolationError(
            f"could not copy {source} as user '{user}': "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    # Hand the sandbox back to the invoking account. Done as root because the
    # tree is root-owned for the moment between the rsync above and this call.
    subprocess.run(
        ["sudo", "-n", "chown", "-R",
         f"{os.getuid()}:{os.getgid()}", str(dest)],
        capture_output=True, text=True, timeout=300,
    )
    subprocess.run(
        ["chmod", "-R", "u+rwX", str(dest)],
        capture_output=True, text=True, timeout=300,
    )
    return sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())


# --------------------------------------------------------------- assertions


def assert_isolated(sandbox: Sandbox, config, *, production_db: str | Path | None = None) -> list[str]:
    """Refuse to run unless isolation is provable. Returns the checks passed.

    Raises `IsolationError` on the first failure. Called by the runner
    immediately before the session starts and again after the pipeline is
    constructed, because construction is where the database handle and the
    broker client are actually created.
    """
    checks: list[str] = []
    root = sandbox.root

    resolved_db = _resolved_db_path(config)
    if not sandbox.contains(resolved_db):
        raise IsolationError(
            f"storage.db_path resolves to {resolved_db}, which is outside the "
            f"rehearsal sandbox {root}"
        )
    checks.append(f"database writes land in {resolved_db}")

    if production_db is not None:
        prod = Path(production_db).resolve()
        if resolved_db == prod:
            raise IsolationError(
                f"storage.db_path IS the production database ({prod})"
            )
        if sandbox.contains(prod):
            raise IsolationError(
                f"the production database {prod} is inside the sandbox root "
                f"{root}; the sandbox would be writing to production"
            )
        checks.append(f"production database {prod} is outside the sandbox")

    cwd = Path.cwd().resolve()
    if cwd != root:
        raise IsolationError(
            f"working directory is {cwd}, not the sandbox root {root}; the "
            f"pipeline's hardcoded relative cache paths (data/news, "
            f"data/checkpoints, ...) would resolve against production"
        )
    checks.append(f"working directory is the sandbox root {root}")

    smart_money_dir = getattr(getattr(config, "smart_money", None), "data_dir", None)
    if smart_money_dir is not None:
        resolved_sm = (root / smart_money_dir).resolve() if not Path(smart_money_dir).is_absolute() else Path(smart_money_dir).resolve()
        if not sandbox.contains(resolved_sm):
            raise IsolationError(
                f"smart_money.data_dir resolves to {resolved_sm}, outside {root}"
            )
        checks.append(f"smart-money cache writes land in {resolved_sm}")

    keys = getattr(config, "api_keys", None)
    for field_name in ("alpaca_key", "alpaca_secret"):
        value = getattr(keys, field_name, None)
        if value != SENTINEL_KEY:
            raise IsolationError(
                f"api_keys.{field_name} is not the rehearsal sentinel; a real "
                f"broker credential must never be present in a rehearsal config"
            )
    checks.append("broker credentials are non-functional sentinels")

    return checks


def assert_broker_is_stubbed(broker) -> str:
    """Refuse to run unless the pipeline's broker cannot reach an account.

    Checked against the constructed pipeline, not the config, because the
    broker is built inside `TradingPipeline.__init__` and this is the only
    place the actual object can be inspected.
    """
    from ops.rehearsal.broker import RehearsalDataClient, RehearsalTradingClient

    client = getattr(broker, "client", None)
    data_client = getattr(broker, "_data_client", None)
    if not isinstance(client, RehearsalTradingClient):
        raise IsolationError(
            f"broker.client is {type(client).__name__}, not a rehearsal stub — "
            f"this session could submit real orders"
        )
    if data_client is not None and not isinstance(data_client, RehearsalDataClient):
        raise IsolationError(
            f"broker._data_client is {type(data_client).__name__}, not a "
            f"rehearsal stub"
        )
    if getattr(broker, "api_key", None) != SENTINEL_KEY:
        raise IsolationError("broker is holding a non-sentinel API key")
    return "broker transport is a rehearsal stub holding sentinel credentials"


def _resolved_db_path(config) -> Path:
    """Re-derive the path `TradingPipeline.__init__` will open.

    Mirrors `src/pipeline.py`: a relative `storage.db_path` is resolved
    against the repository root, NOT the cwd. A rehearsal must therefore pass
    an absolute path, and this check is what proves it did.
    """
    raw = getattr(getattr(config, "storage", None), "db_path", "")
    if raw == ":memory:":
        return Path(":memory:")
    path = Path(raw)
    if not path.is_absolute():
        import src.pipeline as pipeline_module

        path = Path(pipeline_module.__file__).resolve().parent.parent / path
    return path.resolve()


# ------------------------------------------------------------ network wall


_REAL_SOCKET_CONNECT = socket.socket.connect
_REAL_SOCKET_CONNECT_EX = socket.socket.connect_ex
_REAL_CREATE_CONNECTION = socket.create_connection


def _is_loopback(address) -> bool:
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if not isinstance(host, str):
        return False
    return host in ("127.0.0.1", "::1", "localhost", "")


@contextmanager
def no_network(record: list[str] | None = None):
    """Block every off-box socket connection for the duration of the block.

    Installed at the socket layer deliberately: patching `requests.get`, or an
    SDK's transport, or an environment variable only blocks the paths you
    thought of. Anthropic, OpenAI, OpenRouter, Alpaca, yfinance, FRED and
    feedparser all reach the network eventually through this one call.

    Anything blocked is appended to `record`, so the report can say which
    component tried and the operator can judge whether the resulting
    degradation invalidates the rehearsal.
    """
    blocked = record if record is not None else []

    def _blocked(address, what: str):
        target = f"{address[0]}:{address[1]}" if isinstance(address, tuple) and len(address) > 1 else str(address)
        message = f"{what} to {target}"
        if message not in blocked:
            blocked.append(message)
        raise NetworkBlocked(
            f"rehearsal blocked an outbound connection ({message}); a rehearsal "
            f"is offline by construction — no provider call, no market data "
            f"fetch and no broker request may leave this process"
        )

    def guarded_connect(self, address):
        if _is_loopback(address):
            return _REAL_SOCKET_CONNECT(self, address)
        _blocked(address, "socket.connect")

    def guarded_connect_ex(self, address):
        if _is_loopback(address):
            return _REAL_SOCKET_CONNECT_EX(self, address)
        _blocked(address, "socket.connect_ex")

    def guarded_create_connection(address, *args, **kwargs):
        if _is_loopback(address):
            return _REAL_CREATE_CONNECTION(address, *args, **kwargs)
        _blocked(address, "socket.create_connection")

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.create_connection = guarded_create_connection
    try:
        yield blocked
    finally:
        socket.socket.connect = _REAL_SOCKET_CONNECT
        socket.socket.connect_ex = _REAL_SOCKET_CONNECT_EX
        socket.create_connection = _REAL_CREATE_CONNECTION


# --------------------------------------------------------- production guard


@dataclass
class ProductionWitness:
    """Records production's database fingerprint and proves it never moved."""

    path: Path
    before: tuple[int, int, int] | None = None

    @classmethod
    def watch(cls, path: str | Path | None, *, sudo_user: str | None = None) -> "ProductionWitness":
        witness = cls(path=Path(path)) if path else cls(path=Path())
        if path:
            witness.before = _fingerprint(Path(path), sudo_user=sudo_user)
        return witness

    def assert_untouched(self, *, sudo_user: str | None = None) -> str:
        if self.before is None:
            return "no production database was named; nothing to witness"
        after = _fingerprint(self.path, sudo_user=sudo_user)
        if after != self.before:
            raise IsolationError(
                f"the production database {self.path} CHANGED during the "
                f"rehearsal (size/mtime/inode {self.before} -> {after}). Treat "
                f"this rehearsal's result as void and investigate immediately."
            )
        return f"production database {self.path} is byte-identical (unchanged)"


def _fingerprint(path: Path, *, sudo_user: str | None = None) -> tuple[int, int, int]:
    if sudo_user:
        import subprocess

        result = subprocess.run(
            ["sudo", "-n", "-u", sudo_user, "stat", "-c", "%s %Y %i", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            raise IsolationError(
                f"could not stat {path} as '{sudo_user}': {result.stderr.strip()}"
            )
        size, mtime, inode = result.stdout.split()
        return (int(size), int(mtime), int(inode))
    stat = path.stat()
    return (stat.st_size, int(stat.st_mtime), stat.st_ino)
