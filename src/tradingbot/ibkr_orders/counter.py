"""Durable experiment submission counter (SDK-free).

One append-only journal per experiment: ``<state_dir>/ibkr_paper_orders/<experiment>.submissions``.
Each ``placeOrder`` is recorded as one JSON line, written and ``fsync``'d BEFORE the order goes to
the wire, so a restart can never reset the count. The used count is the number of lines. Anything
that does not parse exactly (a torn or edited line, a gap in the sequence, another experiment's
line) makes the count unknown: order mode refuses until a human looks.

The journal is held under an exclusive ``flock`` for the life of the session, so two processes can
never both spend the same budget. The lock is released by the OS when the process dies.
"""
from __future__ import annotations

import fcntl
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path


class CounterError(RuntimeError):
    pass


class CounterLocked(CounterError):
    """Another session holds this experiment's counter."""


class CounterCorrupt(CounterError):
    """The journal does not parse exactly: the count is unknown."""


class CapReached(CounterError):
    """The experiment's submission cap is used up: nothing more is sent."""


class SubmissionCounter:
    def __init__(self, state_dir: Path, experiment: str, max_submissions: int) -> None:
        self.dir = Path(state_dir) / "ibkr_paper_orders"
        self.path = self.dir / f"{experiment}.submissions"
        self.experiment = experiment
        self.max_submissions = max_submissions
        self._fd: int | None = None
        self._used: int | None = None
        self._max_order_id = 0

    # ------------------------------------------------------------------ lifecycle
    def open(self) -> None:
        if self._fd is not None:
            return
        self.dir.mkdir(mode=0o700, exist_ok=True)
        st = os.lstat(self.dir)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise CounterError(f"{self.dir} must be a real directory owned by the current user")
        created = not self.path.exists()
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
                     0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise CounterLocked(f"{self.path} is held by another order session") from None
        try:
            if created:
                self._fsync_dir()
            self._used, self._max_order_id = self._parse(fd)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def close(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def _fsync_dir(self) -> None:
        dfd = os.open(self.dir, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    def _parse(self, fd: int) -> tuple[int, int]:
        size = os.fstat(fd).st_size
        raw = os.pread(fd, size, 0) if size else b""
        if raw and not raw.endswith(b"\n"):
            raise CounterCorrupt(f"{self.path}: last line is torn; the count is unknown")
        used, max_id = 0, 0
        for n, line in enumerate(raw.splitlines(), start=1):
            try:
                rec = json.loads(line)
                ok = (isinstance(rec, dict) and rec.get("n") == n
                      and rec.get("experiment") == self.experiment
                      and isinstance(rec.get("order_id"), int))
            except (ValueError, UnicodeDecodeError):
                ok = False
            if not ok:
                raise CounterCorrupt(f"{self.path}: line {n} does not parse; the count is unknown")
            used = n
            max_id = max(max_id, rec["order_id"])
        return used, max_id

    # ------------------------------------------------------------------ use
    @property
    def used(self) -> int | None:
        return self._used if self._fd is not None else None

    @property
    def max_order_id(self) -> int:
        """Highest order id ever recorded (new ids are always above it: never reused)."""
        return self._max_order_id

    def record(self, *, order_id: int, order_ref: str, kind: str) -> int:
        """Append and fsync one submission BEFORE it is sent. Returns the new used count."""
        if self._fd is None or self._used is None:
            raise CounterError("counter is not open")
        if self._used >= self.max_submissions:
            raise CapReached(f"{self._used} of {self.max_submissions} submissions used")
        n = self._used + 1
        line = json.dumps({"n": n, "experiment": self.experiment, "order_id": order_id,
                           "order_ref": order_ref, "kind": kind,
                           "at": datetime.now(UTC).isoformat(timespec="milliseconds")},
                          sort_keys=True).encode() + b"\n"
        written = os.write(self._fd, line)
        if written != len(line):
            self._used = None  # torn: unknown from now on
            raise CounterCorrupt("short write to the submission journal")
        os.fsync(self._fd)
        self._used = n
        self._max_order_id = max(self._max_order_id, order_id)
        return n
