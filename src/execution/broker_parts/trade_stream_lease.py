"""The account-wide trade_updates lease, split out of ``trade_stream``.

Same behaviour as before the split; the only change is that every broad
catch-all now records a full traceback plus a counted row through
``record_guarded_pass`` (rows need the broker the lease was built for; with
none lent, the traceback is still logged and the row is skipped).
"""
from __future__ import annotations

from pathlib import Path
import fcntl
import logging
import os

from src.sentinel.guarded import record_guarded_pass

logger = logging.getLogger(__name__)
_W = "trade_stream.lease"



def _swallowed(owner, name: str, exc: BaseException, effect: str) -> None:
    record_guarded_pass(owner, f"{_W}.{name}", exc, log=logger,
                        context={"effect": effect})


class _TradeUpdatesLease:
    """Account-wide exclusive right to open Alpaca's trade_updates websocket.

    flock is released when the fd closes, including process death, so a
    killed job cannot wedge the slot. The pid written into the file is
    diagnostic only — ownership is the lock, not the text. Not a new
    service, not IPC, not a second trading-memory system.
    """

    def __init__(self, path: Path, owner=None):
        self.path = Path(path)
        self._fh = None
        self._owner = owner

    def acquire(self, *, blocking: bool = False) -> bool:
        if self._fh is not None:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.path, "a+")
        except Exception as exc:
            _swallowed(self._owner, "open", exc, "no socket opened; REST fills")
            return False
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(fh.fileno(), flags)
        except BlockingIOError:
            fh.close()
            return False
        except Exception as exc:
            _swallowed(self._owner, "flock", exc, "no socket opened; REST fills")
            try:
                fh.close()
            except Exception as exc2:
                _swallowed(self._owner, "close_after_flock", exc2, "fd lingers")
            return False
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"{os.getpid()}\n")
            fh.flush()
        except Exception as exc:
            _swallowed(self._owner, "write_pid", exc, "pid text missing; lock held")
        else:
            record_guarded_pass(self._owner, f"{_W}.write_pid")
        self._fh = fh
        return True

    def release(self) -> None:
        fh = self._fh
        self._fh = None
        if fh is None:
            return
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception as exc:
            _swallowed(self._owner, "unlock", exc, "lock freed on close")
        try:
            fh.close()
        except Exception as exc:
            _swallowed(self._owner, "close", exc, "fd lingers until gc")

    def held(self) -> bool:
        return self._fh is not None
