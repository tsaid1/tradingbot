"""Durable experiment submission journal (SDK-free).

One append-only journal per experiment: ``<state_dir>/ibkr_paper_orders/<experiment>.submissions``,
plus a small head anchor ``<experiment>.submissions.head``.

* **Created once, by hand.** ``init_journal`` (run by the owner through
  ``scripts/ibkr_paper_init_journal.py``, which opens no socket) creates it with a header line and
  refuses if it already exists. The order session NEVER creates it: a missing journal makes the
  count unknown (``SUBMISSION_COUNT_UNKNOWN``), never a fresh budget.
* **Header** (line 0): the experiment name, the masked account, the creation time and the digest
  of the experiment's caps. A header that does not match the session's experiment, account and
  caps makes the count unknown.
* **Submission lines** (line n >= 1): one per ``placeOrder`` (entries, children, amendments), with
  the order id, the wire ``orderRef``, kind, side, quantity, order type, prices and parent id,
  written and ``fsync``'d BEFORE the order goes to the wire.
* **Chained.** Every line carries ``seq`` (its line number) and ``prev`` (the previous line's hash)
  and ``hash`` (SHA-256 of the line's canonical JSON without ``hash``). An edited, removed,
  reordered or torn line breaks the chain. The head anchor holds the last ``seq`` and ``hash``
  (rewritten atomically after each append), so cutting lines off the end is detected too. Any
  break makes the count unknown and everything is refused until a human looks.
* **Locked.** The journal is held under an exclusive ``flock`` for the life of the session.

What this cannot prevent: someone who deletes the journal AND its head and initialises a new one.
The order session then compares the journal with the orders the broker itself shows for the
experiment (``orderRef`` prefix) and refuses when the broker shows more; once the broker's history
has rolled off (the current day at IBKR) nothing in software can tell. The owner controls the
state directory.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from tradingbot.ibkr_orders.caps import ExperimentCaps

JOURNAL_VERSION = 2
GENESIS = "0" * 64
JOURNAL_DIR = "ibkr_paper_orders"


class CounterError(RuntimeError):
    pass


class CounterLocked(CounterError):
    """Another session holds this experiment's counter."""


class CounterCorrupt(CounterError):
    """The journal does not verify exactly: the count is unknown."""


class CounterMissing(CounterCorrupt):
    """No journal: the count is unknown (never a fresh budget). The owner initialises it once."""


class JournalExists(CounterError):
    """``init_journal`` refuses to overwrite or re-create an experiment's journal."""


class CapReached(CounterError):
    """The experiment's submission cap is used up: nothing more is sent."""


def caps_digest(caps: ExperimentCaps) -> str:
    def norm(v):
        if isinstance(v, frozenset):
            return sorted(v)
        if isinstance(v, Decimal):
            return str(v)
        return v

    body = {k: norm(v) for k, v in asdict(caps).items()}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
                          ).hexdigest()


def _canonical(rec: dict) -> bytes:
    return json.dumps(rec, sort_keys=True, separators=(",", ":")).encode()


def _seal(rec: dict) -> dict:
    return {**rec, "hash": hashlib.sha256(_canonical(rec)).hexdigest()}


def journal_paths(state_dir: Path, experiment: str) -> tuple[Path, Path]:
    d = Path(state_dir) / JOURNAL_DIR
    return d / f"{experiment}.submissions", d / f"{experiment}.submissions.head"


def _fsync_dir(path: Path) -> None:
    dfd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _check_dir(d: Path) -> None:
    st = os.lstat(d)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise CounterError(f"{d} must be a real directory owned by the current user")


def _write_head(head: Path, seq: int, digest: str) -> None:
    tmp = head.with_name(head.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, _canonical({"seq": seq, "hash": digest}) + b"\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, head)
    _fsync_dir(head.parent)


def init_journal(state_dir: Path, caps: ExperimentCaps, masked_account: str) -> Path:
    """Create the experiment's journal ONCE (header line + head anchor). Offline: no socket.
    Refuses if a journal (or its head) already exists."""
    path, head = journal_paths(state_dir, caps.name)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    _check_dir(path.parent)
    if head.exists() or os.path.lexists(head):
        raise JournalExists(f"{head} already exists; a journal is created once per experiment")
    header = _seal({"seq": 0, "type": "header", "version": JOURNAL_VERSION,
                    "experiment": caps.name, "account_masked": masked_account,
                    "created_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
                    "caps_digest": caps_digest(caps), "prev": GENESIS})
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                     0o600)
    except FileExistsError:
        raise JournalExists(f"{path} already exists; a journal is created once per experiment"
                            ) from None
    try:
        os.write(fd, _canonical(header) + b"\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(path.parent)
    _write_head(head, 0, header["hash"])
    return path


_SUBMISSION_FIELDS = {"seq": int, "type": str, "experiment": str, "order_id": int,
                      "parent_id": int, "order_ref": str, "kind": str, "action": str,
                      "quantity": str, "order_type": str, "limit_price": (str, type(None)),
                      "stop_price": (str, type(None)), "at": str, "prev": str, "hash": str}


class SubmissionCounter:
    def __init__(self, state_dir: Path, caps: ExperimentCaps, masked_account: str) -> None:
        self.path, self.head = journal_paths(state_dir, caps.name)
        self.dir = self.path.parent
        self.experiment = caps.name
        self.max_submissions = caps.max_submissions
        self._digest = caps_digest(caps)
        self._account = masked_account
        self._fd: int | None = None
        self._used: int | None = None
        self._last_hash = GENESIS
        self._entries: list[dict] = []
        self._max_order_id = 0
        self.header: dict | None = None

    # ------------------------------------------------------------------ lifecycle
    def open(self) -> None:
        """Lock and verify. Raises ``CounterMissing`` / ``CounterCorrupt`` (count unknown) or
        ``CounterLocked``. Never creates anything."""
        if self._fd is not None:
            return
        if not os.path.lexists(self.path):
            raise CounterMissing(f"{self.path} does not exist: the count is unknown (the owner "
                                 "creates the journal once with scripts/ibkr_paper_init_journal.py)")
        _check_dir(self.dir)
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise CounterCorrupt(f"{self.path} cannot be opened: {exc}") from None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise CounterLocked(f"{self.path} is held by another order session") from None
        try:
            self._parse(fd)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def close(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def _bad(self, n: int, why: str) -> CounterCorrupt:
        return CounterCorrupt(f"{self.path}: line {n}: {why}; the count is unknown")

    def _parse(self, fd: int) -> None:
        size = os.fstat(fd).st_size
        raw = os.pread(fd, size, 0) if size else b""
        if not raw.endswith(b"\n"):
            raise CounterCorrupt(f"{self.path}: empty or last line torn; the count is unknown")
        prev, entries, max_id = GENESIS, [], 0
        for n, line in enumerate(raw.splitlines()):
            try:
                rec = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                raise self._bad(n, "not JSON") from None
            if not isinstance(rec, dict) or rec.get("seq") != n or rec.get("prev") != prev:
                raise self._bad(n, "sequence or chain broken")
            body = {k: v for k, v in rec.items() if k != "hash"}
            if rec.get("hash") != hashlib.sha256(_canonical(body)).hexdigest():
                raise self._bad(n, "hash does not match")
            if n == 0:
                if (rec.get("type") != "header" or rec.get("version") != JOURNAL_VERSION
                        or rec.get("experiment") != self.experiment
                        or rec.get("account_masked") != self._account
                        or rec.get("caps_digest") != self._digest):
                    raise self._bad(0, "header does not match this experiment, account and caps")
                self.header = rec
            else:
                if (set(rec) != set(_SUBMISSION_FIELDS) or rec["type"] != "submission"
                        or rec["experiment"] != self.experiment
                        or not all(isinstance(rec[k], t) and not isinstance(rec[k], bool)
                                   for k, t in _SUBMISSION_FIELDS.items())):
                    raise self._bad(n, "not a submission record")
                entries.append(rec)
                max_id = max(max_id, rec["order_id"])
            prev = rec["hash"]
        last_seq = len(entries)
        try:
            anchor = json.loads(self.head.read_bytes())
        except (OSError, ValueError):
            raise CounterCorrupt(f"{self.head} missing or unreadable; the count is unknown"
                                 ) from None
        if not isinstance(anchor, dict):
            raise CounterCorrupt(f"{self.head} unreadable; the count is unknown")
        a_seq = anchor.get("seq")
        recs = [self.header, *entries]
        # the anchor is rewritten just after each append: it may trail by one after a crash
        if (not isinstance(a_seq, int) or a_seq not in (last_seq, last_seq - 1) or a_seq < 0
                or recs[a_seq]["hash"] != anchor.get("hash")):
            raise CounterCorrupt(f"{self.path}: does not reach the head anchor (seq {a_seq}, "
                                 f"journal {last_seq}): truncated or replaced; the count is "
                                 "unknown")
        self._entries, self._max_order_id = entries, max_id
        self._used, self._last_hash = last_seq, prev

    # ------------------------------------------------------------------ use
    @property
    def used(self) -> int | None:
        return self._used if self._fd is not None else None

    @property
    def max_order_id(self) -> int:
        """Highest order id ever recorded (new ids are always above it: never reused)."""
        return self._max_order_id

    @property
    def entries(self) -> tuple[dict, ...]:
        """Every submission line (verified), oldest first."""
        return tuple(self._entries)

    def record(self, *, order_id: int, order_ref: str, kind: str, action: str,
               quantity: Decimal, order_type: str, limit_price: Decimal | None,
               stop_price: Decimal | None, parent_id: int) -> int:
        """Append and fsync one submission BEFORE it is sent. Returns the new used count."""
        if self._fd is None or self._used is None:
            raise CounterError("counter is not open")
        if self._used >= self.max_submissions:
            raise CapReached(f"{self._used} of {self.max_submissions} submissions used")
        n = self._used + 1
        rec = _seal({"seq": n, "type": "submission", "experiment": self.experiment,
                     "order_id": int(order_id), "parent_id": int(parent_id),
                     "order_ref": order_ref, "kind": kind, "action": action,
                     "quantity": str(quantity), "order_type": order_type,
                     "limit_price": None if limit_price is None else str(limit_price),
                     "stop_price": None if stop_price is None else str(stop_price),
                     "at": datetime.now(UTC).isoformat(timespec="milliseconds"),
                     "prev": self._last_hash})
        line = _canonical(rec) + b"\n"
        written = os.write(self._fd, line)
        if written != len(line):
            self._used = None  # torn: unknown from now on
            raise CounterCorrupt("short write to the submission journal")
        os.fsync(self._fd)
        self._used, self._last_hash = n, rec["hash"]
        self._entries.append(rec)
        self._max_order_id = max(self._max_order_id, order_id)
        _write_head(self.head, n, rec["hash"])
        return n
