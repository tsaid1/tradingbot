"""The owner's vocabulary review: a PRIVATE log of vocabulary holds, proposals, and consolidated
owner-approved batches applied to the private lexicon. See docs/RUNTIME.md, "Vocabulary review
(owner only)".

The entry rule fails closed: an entry holds when its post holds a word the public lists do not
know. Only the owner may read the publisher's words, so this module never prints or logs them;
it hands them only to its caller (the owner's local review page) and writes them only to the
owner's private files, each outside the repository and every git work tree, mode 0600:

* ``ReviewLog``: the service's writer for ``[interpreter] review_log_path``.
* ``read_review_log``, ``candidates``: the log aggregated by (word, kind).
* Proposals (``PROPOSAL_SCHEMA``, one JSON object per line): ``read_proposals`` reads a proposals
  file (written by the owner's local proposer, which may add measured effects), and
  ``proposals_from_log`` builds a plain batch from the review log when there is none.
* ``apply_batch``: the owner's consolidated approval. Accepted proposals go to their group's
  list in the PRODUCTION lexicon (``[interpreter] lexicon_path``) as ONE change: each one is
  checked by the runtime's own loader, the whole result is checked again, then written atomically
  with mode 0600 and one timestamped backup; ``undo_last`` restores it. Every other section of the
  file is kept as it was. "Keep holding" words are recorded in a private decisions file beside
  the lexicon and are not offered again.
* ``write_candidate``: an EXPERIMENT. The production lexicon plus selected proposals, written to a
  separate private file marked ``CANDIDATE_KEY``; the service refuses to load it, and only an
  offline replay may, with an explicit opt-in. The production file is never touched.

Nothing here decides a trade: a word takes effect only when the service (or a replay) restarts
and loads the lexicon through its own validators.

The page binds an approval to what the owner reviewed: ``batch_digest`` covers the final table
(word, verdict, group, family, meaning) and the production lexicon's bytes, and ``apply_reviewed``
recomputes the batch from the current files at click time and refuses any difference.

The review log is the ONE file that holds the publisher's sentences: proposals, candidates, the
decisions file and backups carry words and public codes only; a plain batch refers to its examples
by (source id, action id) and the page loads at most ``MAX_EXAMPLES`` sentences per proposal from
the log at render time. ``ReviewLog`` never appends the same hold twice, and
``truncate_review_log`` is the owner's way to bound it.

Note: the private evidence rides on ``MappingDecision`` as a pydantic private attribute. ``pickle``,
``copy.deepcopy``, ``model_copy`` and ``==`` on a decision DO carry or compare it; no code under
``src/`` does any of these to a decision (tests/test_vocab_review_fixes.py scans for it).
"""

from __future__ import annotations

import contextlib
import copy
import errno
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from tradingbot.runtime.config import ConfigError
from tradingbot.runtime.mapping import _VOCABULARY_DETAILS, vocabulary_hold

# The public reason codes of holds a lexicon word can resolve (``mapping.vocabulary_hold`` decides
# with the detail). Anything else (an unattributed action sentence, an additive ellipsis, a stop
# refusal, long-only) is never offered for review.
VOCABULARY_REASON_CODES = frozenset(_VOCABULARY_DETAILS)

REPO_ROOT = Path(__file__).resolve().parents[3]

# The owner's four proposal groups and the lexicon list each one writes (entry_anchor section).
MARKET_NAME = "MARKET_NAME"  # market names and instrument aliases -> family_aliases[family]
NON_MARKET = "NON_MARKET"  # non-market words mistaken for instruments -> non_market_words
INSTRUCTION_PHRASE = "INSTRUCTION_PHRASE"  # publisher-specific instruction phrases -> context_words
AMBIGUOUS = "AMBIGUOUS"  # needs contextual interpretation: no list until the owner picks one
GROUPS = {
    MARKET_NAME: "Market names and instrument aliases",
    NON_MARKET: "Non-market words frequently mistaken for instruments",
    INSTRUCTION_PHRASE: "Publisher-specific instruction phrases",
    AMBIGUOUS: "Ambiguous expressions requiring contextual interpretation",
}
GROUP_LIST = {MARKET_NAME: "family_aliases", NON_MARKET: "non_market_words",
              INSTRUCTION_PHRASE: "context_words"}
# The owner's verdict on one proposal in a batch.
ACCEPT = "ACCEPT"  # into its group's list
REJECT = "REJECT"  # not now: nothing written, offered again
KEEP_HOLDING = "KEEP_HOLDING"  # recorded beside the lexicon: posts keep holding, not offered again
PENDING = "PENDING"  # undecided: nothing written
VERDICTS = (PENDING, ACCEPT, REJECT, KEEP_HOLDING)
PROPOSAL_SCHEMA = "tradingbot.vocab_proposal/1"
# Marks a candidate (experimental) lexicon: the service's loader refuses such a file.
CANDIDATE_KEY = "candidate_lexicon"
CANDIDATE_SUFFIX = ".candidate.json"
# Marks a state directory used by a candidate replay: production never runs there.
CANDIDATE_RUN_MARKER = "CANDIDATE_RUN"
_FAMILY_RE = re.compile(r"^[A-Z][A-Z0-9]{0,9}(\.OPT)?$")
# At most this many example sentences per proposal are loaded for display.
MAX_EXAMPLES = 5
# A word that held a post through the mention-with-context rule was seen next to a price or a
# trading word: it may BE another market. Proposed as ambiguous; this warning comes from the
# review log's evidence, never from a proposals file.
MAY_BE_MARKET = "seen with a price or a trading word; may be another market"


class PrivatePathError(ValueError):
    """A private file would be readable by others or land inside a repository."""


class CandidateLexiconRefused(ConfigError):
    """A candidate (experimental) lexicon configured where the production lexicon belongs."""


# ------------------------------------------------------------------ private paths


def git_work_tree(path: Path) -> Path | None:
    """The git work tree holding ``path`` (a ``.git`` directory or file in it or an ancestor, or
    ``path`` inside a ``.git`` directory). A work tree whose git directory lives elsewhere with no
    ``.git`` entry (``core.worktree``) cannot be detected."""
    for folder in (path, *path.parents):
        if (folder / ".git").exists() or folder.name == ".git":
            return folder
    return None


def bare_repository(path: Path) -> Path | None:
    """A bare git repository holding ``path``: an ancestor with ``HEAD``, ``objects/``, ``refs/``."""
    for folder in (path, *path.parents):
        if (folder / "HEAD").is_file() and (folder / "objects").is_dir() \
                and (folder / "refs").is_dir():
            return folder
    return None


# Cloud-synced roots (macOS): a private file there is copied to a provider. Not refused (the owner
# may choose it) but warned on the page and alerted at start, by class only.
_SYNCED = (
    ("Library/CloudStorage", "CLOUD_STORAGE"),
    ("Library/Mobile Documents", "ICLOUD_DRIVE"),
    ("Dropbox", "DROPBOX"),
    ("OneDrive", "ONEDRIVE"),
    ("Google Drive", "GOOGLE_DRIVE"),
    ("Box", "BOX"),
    ("Documents", "POSSIBLY_ICLOUD_DESKTOP_DOCUMENTS"),
    ("Desktop", "POSSIBLY_ICLOUD_DESKTOP_DOCUMENTS"),
)


def git_tree_class(path: str | Path) -> str | None:
    """``OTHER_GIT_WORK_TREE`` when ``path`` lies in a git work tree (one ``check_private_path``
    allowed: not this program's): the file could be committed there by mistake. A constant,
    never the path."""
    p = Path(path).expanduser()
    return "OTHER_GIT_WORK_TREE" if git_work_tree(
        Path(os.path.realpath(p.parent))) is not None else None


def synced_folder_class(path: str | Path) -> str | None:
    """The class of cloud-synced folder holding ``path`` (a constant, never the path), or None."""
    p = Path(path).expanduser()
    target = Path(os.path.realpath(p.parent)) / p.name
    home = Path(os.path.realpath(Path.home()))
    try:
        rel = target.relative_to(home)
    except ValueError:
        return None
    first = rel.parts[0] if rel.parts else ""
    for root, cls in _SYNCED:
        parts = root.split("/")
        if len(parts) == 2 and rel.parts[:2] == tuple(parts):
            return cls
        if len(parts) == 1 and (first == root or first.startswith(f"{root} - ")
                                or (root in ("Dropbox", "OneDrive") and first.startswith(root))):
            return cls
    return None


def common_git_dir(tree: Path) -> Path | None:
    """The common git directory of the work tree ``tree`` (its ``.git`` directory, or for a linked
    worktree the main repository's, through ``.git`` -> ``gitdir`` -> ``commondir``), read from
    the files only. None when it cannot be read."""
    dot = tree / ".git"
    try:
        if dot.is_dir():
            git_dir = dot
        elif dot.is_file():
            text = dot.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir:"):
                return None
            git_dir = Path(text.removeprefix("gitdir:").strip())
            git_dir = git_dir if git_dir.is_absolute() else tree / git_dir
        else:
            return None
        common = git_dir / "commondir"
        if common.is_file():
            c = Path(common.read_text(encoding="utf-8").strip())
            git_dir = c if c.is_absolute() else git_dir / c
    except OSError:
        return None
    return Path(os.path.realpath(git_dir))


def code_git_dir() -> Path | None:
    """The common git directory of the checkout this program runs from (main checkout or linked
    worktree), or None when it cannot be determined (an installed package, no git, or a git
    directory that does not exist): then every git work tree is refused."""
    code = common_git_dir(REPO_ROOT)
    return code if code is not None and code.is_dir() else None


def _identity(path: Path) -> tuple[int, int] | None:
    """(device, inode) of ``path``, None when it does not exist. Any other error propagates (the
    caller refuses)."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return (st.st_dev, st.st_ino)


def _code_identities(code: Path) -> set[tuple[int, int]]:
    """File identities of this program's repository: the checkout it runs from, the common git
    directory, the main checkout and every linked worktree recorded under ``worktrees/``."""
    places = [Path(REPO_ROOT), code]
    if code.name == ".git":
        places.append(code.parent)  # the main checkout
    worktrees = code / "worktrees"
    if worktrees.is_dir():
        for d in worktrees.iterdir():
            places.append(d)
            gitdir = d / "gitdir"
            if gitdir.is_file():
                places.append(Path(gitdir.read_text(encoding="utf-8").strip()).parent)
    return {i for i in map(_identity, places) if i is not None}


def check_private_path(path: str | Path, what: str) -> Path:
    """``path`` with its folders resolved (the last part is not followed). Refused inside this
    program's repository (the checkout it runs from, its main checkout, every linked worktree of
    it and its git directory), inside a ``.git`` directory and inside a bare repository; when this
    program's repository cannot be determined, inside any git work tree (fail closed). A path
    inside ANY OTHER git work tree is allowed (the Product Owner's decision) and warned by
    ``git_tree_class``.

    "Inside this program's repository" is decided by FILE IDENTITY (device and inode) of every
    existing enclosing directory, never by comparing strings, so a case or Unicode-normalisation
    variant or a symlinked parent cannot slip past on a file system that folds them. String
    checks remain as an extra refusal. Any error while checking refuses."""
    try:
        return _check_private_path(path, what)
    except PrivatePathError:
        raise
    except (OSError, ValueError) as exc:
        raise PrivatePathError(f"the {what} could not be checked against this program's "
                               f"repository ({type(exc).__name__}); refused") from None


def _check_private_path(path: str | Path, what: str) -> Path:
    raw = Path(path).expanduser()
    target = Path(os.path.realpath(raw.parent)) / raw.name
    root = Path(os.path.realpath(REPO_ROOT))
    if target == root or root in target.parents:
        raise PrivatePathError(f"the {what} must lie outside this program's repository ({root}): "
                               "private text never enters the repository tree")
    if any(part.lower() == ".git" for part in target.parent.parts):
        raise PrivatePathError(f"the {what} may not lie inside a git directory")
    code = code_git_dir()
    if code is not None:
        ours = _code_identities(code)
        for folder in (target.parent, *target.parent.parents):
            if _identity(folder) in ours:
                raise PrivatePathError(
                    f"the {what} must lie outside this program's repository: {folder} is (the "
                    "same directory as) its checkout, a work tree of it or its git directory")
    tree = git_work_tree(target.parent)
    if tree is not None:
        if code is None:
            raise PrivatePathError(
                f"the {what} lies in a git work tree ({tree}) and this program's repository "
                "cannot be determined (no git checkout), so every git work tree is refused")
        theirs = common_git_dir(tree)
        if theirs is None or _identity(theirs) is None or _identity(theirs) == _identity(code):
            raise PrivatePathError(
                f"the {what} must lie outside this program's repository: {tree} is a work tree "
                "of it (or its git directory cannot be read)")
    bare = bare_repository(target.parent)
    if bare is not None:
        raise PrivatePathError(f"the {what} must lie outside any bare git repository (found one "
                               f"at {bare})")
    return target


def _check_mode(mode: int, what: str) -> None:
    if stat.S_IMODE(mode) & 0o077:
        raise PrivatePathError(f"the {what} is group- or world-readable (mode "
                               f"{stat.S_IMODE(mode):o}); run chmod 600 on it")


class PrivateDir:
    """Open-then-verify for writes into one private folder (review S: no time-of-check /
    time-of-use gap). The folder passes ``check_private_path`` and is held open as a directory
    descriptor; every file is opened RELATIVE to that descriptor with ``O_NOFOLLOW`` and checked
    by ``fstat`` (a regular file, link count 1, not group- or world-readable). ``verify`` re-runs
    the private-path check on the resolved path and requires the folder at that path to be the one
    held (device and inode): a folder swapped for a symlink, moved or replaced is refused."""

    def __init__(self, path: str | Path, what: str, *, create: bool = False):
        self.raw, self.what = Path(path).expanduser(), what
        self.target = check_private_path(self.raw, what)
        if create:
            self.target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.fd = os.open(self.target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            self.verify()
        except BaseException:
            self.close()
            raise

    @property
    def name(self) -> str:
        return self.target.name

    def verify(self) -> None:
        if self.fd is None:
            raise PrivatePathError(f"the {self.what} is closed")
        again = check_private_path(self.raw, self.what)
        st = os.fstat(self.fd)
        if again != self.target or _identity(again.parent) != (st.st_dev, st.st_ino):
            raise PrivatePathError(f"the folder of the {self.what} changed since it was checked "
                                   "(moved, replaced or swapped for a link); refused")

    def check_open(self, fd: int) -> None:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PrivatePathError(f"the {self.what} is not a regular file")
        if st.st_nlink != 1:
            raise PrivatePathError(f"the {self.what} is hard-linked elsewhere (link count "
                                   f"{st.st_nlink}); refused")
        _check_mode(st.st_mode, self.what)

    def open(self, name: str, flags: int) -> int:
        """Open ``name`` in the held folder (never through a link), verified before and after."""
        self.verify()
        fd = os.open(name, flags | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            self.check_open(fd)
            self.verify()
        except BaseException:
            os.close(fd)
            raise
        return fd

    def check_existing(self, name: str) -> None:
        """An existing file about to be replaced may not be a link or hard-linked elsewhere."""
        try:
            st = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise PrivatePathError(f"the {self.what} is not a regular file")
        if st.st_nlink != 1:
            raise PrivatePathError(f"the {self.what} is hard-linked elsewhere (link count "
                                   f"{st.st_nlink}); refused")

    def create(self, name: str, data: bytes) -> None:
        """A new file ``name`` (never an existing one), written and flushed."""
        fd = self.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)

    def write_atomic(self, data: bytes) -> None:
        """Replace this folder's target file with ``data``: a new temporary file, verified, then
        renamed over it within the held folder."""
        self.check_existing(self.name)
        tmp = f".{self.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}"
        self.create(tmp, data)
        try:
            self.verify()
            self.check_existing(self.name)
            os.replace(tmp, self.name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=self.fd)
            raise
        with contextlib.suppress(OSError):
            os.fsync(self.fd)

    def unlink(self, name: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name, dir_fd=self.fd)

    def close(self) -> None:
        if getattr(self, "fd", None) is not None:
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        self.close()


class ReviewLog:
    """The service's append-only private review log. Refuses at construction a path inside this
    program's repository (``check_private_path``), a symbolic link, a file that is hard-linked
    elsewhere and an existing file others may read. Its folder is held open from start
    (``PrivateDir``): every append opens the file relative to it, never through a link, and
    re-checks the path, the folder's identity, the link count and the mode."""

    WHAT = "review log ([interpreter] review_log_path)"

    def __init__(self, path: str | Path):
        self.dir = PrivateDir(path, self.WHAT, create=True)
        self.path = self.dir.target
        self._seen: set[tuple] = set()
        try:
            try:
                fd = self.dir.open(self.dir.name, os.O_RDONLY)
            except FileNotFoundError:
                self.dir.create(self.dir.name, b"")
            else:
                with os.fdopen(fd, "rb") as f:
                    text = f.read().decode("utf-8", "replace")
                self._seen = {record_key(e) for e in _parse_log(text)[0]}
        except OSError as exc:
            self.dir.close()
            if exc.errno == errno.ELOOP:
                raise PrivatePathError(f"the {self.WHAT} may not be a symbolic link") from None
            raise
        except BaseException:
            self.dir.close()
            raise

    def append(self, record: dict) -> bool:
        """Append one hold, unless the same hold (post, revision, action, reason code and
        detail) is already in the log: a replay or a restart never duplicates it. The key is not
        the words: the earlier line of the same hold already carries them. True when appended."""
        key = record_key(record)
        if key in self._seen:
            return False
        line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        fd = self.dir.open(self.dir.name, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
        self._seen.add(key)
        return True

    def close(self) -> None:
        self.dir.close()


def record_key(record: dict) -> tuple:
    """One hold's identity in the review log (identifiers and public codes only)."""
    return (str(record.get("source_id") or record.get("post_id")), record.get("revision"),
            record.get("action_id"), record.get("reason_code"),
            tuple(record.get("reason_detail") or ()))


def truncate_review_log(path: str | Path, keep_last: int) -> int:
    """The owner's way to bound the review log: keep only its last ``keep_last`` lines (atomic,
    mode 0600). Returns how many lines were removed. Run it while the service is stopped."""
    target = check_private_path(path, ReviewLog.WHAT)
    if not target.is_file():
        return 0
    lines = target.read_bytes().splitlines(keepends=True)
    keep = lines[-keep_last:] if keep_last > 0 else []
    if len(keep) == len(lines):
        return 0
    _write_atomic(target, b"".join(keep))
    return len(lines) - len(keep)


def review_record(*, at: datetime, post_id: str, source_id: str, revision: int, action_id: str,
                  family: str | None, reason: str, evidence) -> dict:
    """One private review-log line: the public reason code and detail, and privately the words
    (with their kinds) and the sentences involved."""
    code, *detail = reason.split(",")
    return {
        "at": at.isoformat(), "post_id": post_id, "source_id": source_id, "revision": revision,
        "action_id": action_id, "family": family, "reason_code": code, "reason_detail": detail,
        "tokens": [{"token": t, "kind": k} for t, k in evidence.tokens],
        "sentences": [{"role": r, "text": s} for r, s in evidence.sentences],
    }


# ------------------------------------------------------------------ reading and aggregating


def read_review_log(path: str | Path) -> tuple[list[dict], int]:
    """The log's entries and how many lines could not be read. A missing log is empty."""
    p = Path(path).expanduser()
    if not p.exists():
        return [], 0
    return _parse_log(p.read_text(encoding="utf-8", errors="replace"))


def _parse_log(text: str) -> tuple[list[dict], int]:
    entries, bad = [], 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(e, dict) and isinstance(e.get("tokens"), list):
            entries.append(e)
        else:
            bad += 1
    return entries, bad


@dataclass
class Candidate:
    token: str  # normalised: lower case
    kind: str
    holds: int = 0  # review-log lines (held actions) the word is involved in
    posts: int = 0  # distinct held posts
    families: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    sentences: list[tuple[str, str]] = field(default_factory=list)  # at most MAX_EXAMPLES
    refs: list[dict] = field(default_factory=list)  # {source_id, action_id}, at most MAX_EXAMPLES
    may_be_market: bool = False  # held a post through the mention-with-context rule


def normalise(token: str) -> str:
    return " ".join(str(token).lower().split())


_KEY_EDGE = "()[]{}.,;:!?\"'\u2018\u2019"


def word_key(token: str) -> str:
    """"The same word", as the anchor rule and the mention detector compare it: lower case, edge
    punctuation and a leading ``#``, ``$`` or ``@`` removed, a possessive ``'s`` removed,
    whitespace collapsed. A plural is a different word (the detector does not fold it)."""
    out = []
    for w in str(token).replace("\u2019", "'").split():
        w = w.strip(_KEY_EDGE).lstrip("#$@").strip(_KEY_EDGE).lower()
        w = w.removesuffix("'s")
        if w:
            out.append(w)
    return " ".join(out)


def word_parts(token: str) -> set[str]:
    """The words of a token or phrase, also split on ``/``, ``.`` and ``-`` (the detector reads
    "EUR/USD" part by part, and each word of a phrase becomes understood)."""
    return {p for p in re.split(r"[\s/.\-]+", word_key(token)) if p}


def is_warned(token: str, warned: set[str]) -> bool:
    """Is any part of ``token`` a word the review log saw with a price or a trading word?"""
    return bool(word_parts(token) & {p for w in warned for p in word_parts(w)})


def known_words(lexicon_path: str | Path | None) -> set[str]:
    """Words already decided: every phrase of the lexicon's entry_anchor lists and every word
    recorded as a different market. Empty when there is no readable lexicon."""
    if lexicon_path is None:
        return set()
    out = set(decided_words(lexicon_path))
    with contextlib.suppress(OSError, ValueError):
        section = json.loads(Path(lexicon_path).read_text(encoding="utf-8")).get("entry_anchor")
        if isinstance(section, dict):
            for key in ("present_markers", "context_words", "non_market_words"):
                out |= {normalise(w) for w in section.get(key) or () if isinstance(w, str)}
            for names in (section.get("family_aliases") or {}).values():
                out |= {normalise(w) for w in names or () if isinstance(w, str)}
    return out


def _mention_with_context(entry: dict) -> bool:
    """A review-log line of a mention hold that needed a trading context (or one logged before
    the rule detail existed): the word was seen with a price or a trading word."""
    if entry.get("reason_code") != "ENTRY_ANCHOR_POST_UNRESOLVED_MARKET":
        return False
    detail = [str(d) for d in entry.get("reason_detail") or ()]
    return "RULE:MENTION_WITH_CONTEXT" in detail or not any(d.startswith("RULE:") for d in detail)


def market_warning_words(entries: list[dict]) -> set[str]:
    """The words the review log saw in a mention-with-context hold."""
    return {word_key(t["token"]) for e in entries if _mention_with_context(e)
            for t in e.get("tokens") or () if isinstance(t, dict) and t.get("token")}


def candidates(entries: list[dict], *, lexicon_path: str | Path | None = None) -> list[Candidate]:
    """The words to review, by (word, kind): held posts, holds, families, reason codes and the
    sentences that hold the word. A word already in the lexicon or marked a different market is
    left out. Sorted by held posts, then holds."""
    known = known_words(lexicon_path)
    found: dict[tuple[str, str], Candidate] = {}
    posts: dict[tuple[str, str], set] = {}
    for e in entries:
        if not vocabulary_hold(str(e.get("reason_code") or ""), e.get("reason_detail")):
            continue  # no lexicon word may remove this hold: never proposed
        seen = set()
        for t in e.get("tokens") or ():
            if not isinstance(t, dict) or not t.get("token"):
                continue
            key = (word_key(t["token"]), str(t.get("kind") or "UNKNOWN"))
            if key[0] in known or key in seen:
                continue
            seen.add(key)
            c = found.setdefault(key, Candidate(*key))
            c.may_be_market = c.may_be_market or _mention_with_context(e)
            c.holds += 1
            posts.setdefault(key, set()).add(e.get("source_id") or e.get("post_id"))
            if e.get("family") and e["family"] not in c.families:
                c.families.append(e["family"])
            if e.get("reason_code") and e["reason_code"] not in c.reasons:
                c.reasons.append(e["reason_code"])
            pattern = re.compile(rf"(?<!\w){re.escape(key[0])}(?!\w)", re.IGNORECASE)
            texts = [(s.get("role", ""), s.get("text", "")) for s in e.get("sentences") or ()
                     if isinstance(s, dict)]
            for pair in [p for p in texts if pattern.search(p[1])] or texts:
                if pair not in c.sentences and len(c.sentences) < MAX_EXAMPLES:
                    c.sentences.append(pair)
            ref = {"source_id": str(e.get("source_id") or e.get("post_id")),
                   "action_id": e.get("action_id")}
            if ref not in c.refs and len(c.refs) < MAX_EXAMPLES:
                c.refs.append(ref)
    for key, c in found.items():
        c.posts = len(posts[key])
        c.families.sort()
        c.reasons.sort()
    return sorted(found.values(), key=lambda c: (-c.posts, -c.holds, c.token, c.kind))


# ------------------------------------------------------------------ proposals


@dataclass
class Proposal:
    """One proposed lexicon addition (a ``PROPOSAL_SCHEMA`` line). Public fields: ``group``,
    ``family``, the evidence counts, ``families`` and ``reasons`` (public codes), ``measured``
    (public counts from a local evaluation, or None: not measured). PRIVATE: ``token``,
    ``meaning`` and ``examples`` (role, sentence) for display to the owner only."""
    token: str
    group: str
    family: str | None = None
    meaning: str = ""
    kind: str | None = None
    posts: int = 0
    holds: int = 0
    families: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    measured: dict[str, int] | None = None
    # Inline example text a proposals file supplied (shown, never copied into another file).
    examples: list[tuple[str, str]] = field(default_factory=list)
    # References into the review log ({source_id[, action_id]}), resolved at display time.
    example_refs: list[dict] = field(default_factory=list)
    # From the review log's evidence only (``market_warning_words``), never from the file.
    market_warning: bool = False


def _proposal(raw: dict) -> Proposal | None:
    token = raw.get("token")
    group = raw.get("group")
    if raw.get("schema") != PROPOSAL_SCHEMA or not isinstance(token, str) or not token.strip() \
            or group not in GROUPS:
        return None
    family = raw.get("family")
    if family is not None and not (isinstance(family, str) and _FAMILY_RE.match(family)):
        return None
    cited = (raw.get("categories") or {}).get("reasons") if isinstance(
        raw.get("categories"), dict) else None
    if cited and not any(str(r) in VOCABULARY_REASON_CODES for r in cited):
        return None  # cites only holds no lexicon word may remove
    evidence = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}
    cats = raw.get("categories") if isinstance(raw.get("categories"), dict) else {}
    measured = raw.get("measured")
    if isinstance(measured, dict) and measured and all(
            isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
            for k, v in measured.items()):
        measured = dict(measured)
    else:
        measured = None  # not measured (or not public counts)
    examples, refs = [], []
    for ex in raw.get("examples") or ():
        if isinstance(ex, dict) and isinstance(ex.get("text"), str):
            if len(examples) < MAX_EXAMPLES:
                examples.append((str(ex.get("role", "")), ex["text"]))
        elif isinstance(ex, dict) and (ex.get("source_id") or ex.get("post_id")):
            ref = {"source_id": str(ex.get("source_id") or ex.get("post_id"))}
            if isinstance(ex.get("action_id"), str):
                ref["action_id"] = ex["action_id"]
            if len(refs) < MAX_EXAMPLES:
                refs.append(ref)
    return Proposal(
        token=word_key(token), group=group, family=family,
        meaning=raw.get("meaning") if isinstance(raw.get("meaning"), str) else "",
        kind=raw.get("kind") if isinstance(raw.get("kind"), str) else None,
        posts=_count(evidence.get("posts")), holds=_count(evidence.get("holds")),
        families=sorted(str(f) for f in cats.get("families") or ()),
        reasons=sorted(str(r) for r in cats.get("reasons") or ()),
        measured=measured, examples=list(dict.fromkeys(examples)), example_refs=refs)


def _count(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def example_sentences(p: Proposal, entries: list[dict], limit: int = MAX_EXAMPLES
                      ) -> list[tuple[str, str]]:
    """The sentences to show for ``p`` (display only, loaded at render time): its inline examples,
    then the review-log sentences its references point to, at most ``limit``."""
    out = list(p.examples)
    for ref in p.example_refs:
        out += _referenced(ref, p.token, entries)
    return list(dict.fromkeys(out))[:limit]


def _referenced(ref: dict, token: str, entries: list[dict]) -> list[tuple[str, str]]:
    """An example given by reference: the review-log sentences of that post holding the word."""
    want = ref.get("source_id") or ref.get("post_id")
    pattern = re.compile(rf"(?<!\w){re.escape(token)}(?!\w)", re.IGNORECASE)
    out = []
    for e in entries:
        if want not in (e.get("source_id"), e.get("post_id")):
            continue
        if ref.get("action_id") and ref["action_id"] != e.get("action_id"):
            continue
        out += [(s.get("role", ""), s.get("text", "")) for s in e.get("sentences") or ()
                if isinstance(s, dict) and pattern.search(s.get("text", ""))]
    return out


def default_proposals_path(review_log_path: str | Path) -> Path:
    p = Path(review_log_path).expanduser()
    return p.with_name(f"{p.stem}.proposals.jsonl")


def read_proposals(path: str | Path, *, lexicon_path: str | Path | None = None
                   ) -> tuple[list[Proposal], int]:
    """A proposals file's proposals (words already decided left out) and how many lines could
    not be read. Example references stay references (``example_sentences`` resolves them)."""
    p = Path(path).expanduser()
    if not p.exists():
        return [], 0
    known = known_words(lexicon_path)
    out, bad, seen = [], 0, set()
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        prop = _proposal(raw) if isinstance(raw, dict) else None
        if prop is None:
            bad += 1
        elif prop.token not in known and prop.token not in seen:
            seen.add(prop.token)
            out.append(prop)
    return out, bad


_INSTRUCTION_ROLES = {"LEVEL", "INSTRUCTION"}


def proposals_from_log(entries: list[dict], *, lexicon_path: str | Path | None = None
                       ) -> list[Proposal]:
    """A plain batch from the review log, ONE proposal per word (``word_key``) with all its kinds.
    A word the log saw with a price or a trading word (a mention-with-context hold), in any form,
    is ambiguous and warned whatever its kinds. Otherwise by kind: a proper noun is a non-market
    word; a market noun or instrument phrase seen in posts of one family is another name of that
    family; an unknown word of an instruction sentence is an instruction phrase; several kinds or
    anything else is ambiguous. Meaning empty, measured change not measured."""
    warned = market_warning_words(entries)
    by_word: dict[str, list[Candidate]] = {}
    for c in candidates(entries, lexicon_path=lexicon_path):
        by_word.setdefault(c.token, []).append(c)
    posts: dict[str, set] = {}
    for e in entries:
        if vocabulary_hold(str(e.get("reason_code") or ""), e.get("reason_detail")):
            for t in e.get("tokens") or ():
                if isinstance(t, dict) and t.get("token"):
                    posts.setdefault(word_key(t["token"]), set()).add(
                        e.get("source_id") or e.get("post_id"))
    out = []
    for token, cs in by_word.items():
        kinds = sorted({c.kind for c in cs})
        families = sorted({f for c in cs for f in c.families})
        roles = {r for c in cs for r, _ in c.sentences}
        warn = is_warned(token, warned) or any(c.may_be_market for c in cs)
        family = None
        if warn or len(kinds) > 1:
            group = AMBIGUOUS
        elif kinds == ["PROPER_NOUN"]:
            group = NON_MARKET
        elif kinds[0] in ("MARKET_NOUN", "INSTRUMENT_PHRASE") and len(families) == 1:
            group, family = MARKET_NAME, families[0]
        elif kinds == ["UNKNOWN_WORD"] and roles and roles <= _INSTRUCTION_ROLES:
            group = INSTRUCTION_PHRASE
        else:
            group = AMBIGUOUS
        refs = []
        for c in cs:
            refs += [r for r in c.refs if r not in refs]
        out.append(Proposal(token=token, group=group, family=family, kind=", ".join(kinds),
                            posts=len(posts.get(token, ())) or max(c.posts for c in cs),
                            holds=sum(c.holds for c in cs), families=families,
                            reasons=sorted({r for c in cs for r in c.reasons}),
                            example_refs=refs[:MAX_EXAMPLES], market_warning=warn))
    return sorted(out, key=lambda p: (-p.posts, -p.holds, p.token))


# ------------------------------------------------------------------ the batch


@dataclass
class BatchItem:
    """The owner's verdict on one proposal: ``group`` and ``family`` as the owner left them."""
    token: str
    verdict: str
    group: str
    family: str | None = None
    meaning: str = ""
    may_be_market: bool = False  # the review log saw it with a price or a trading word


@dataclass
class BatchResult:
    # WRITTEN (the lexicon changed), RECORDED (only keep-holding words), NOTHING (no change),
    # REFUSED (nothing written), RESTORED, NOTHING_TO_UNDO
    status: str
    message: str
    added: dict[str, list[str]] = field(default_factory=dict)  # list name -> words
    refused: list[tuple[str, str]] = field(default_factory=list)  # (word, loader's reason)
    unchanged: list[str] = field(default_factory=list)
    kept_holding: list[str] = field(default_factory=list)
    backup: Path | None = None
    path: Path | None = None


def proposal_id(p: Proposal) -> str:
    """A stable id of a proposal's identity (word, kind, group, family): the page keys every
    widget by it, never by list position. Holds no private text."""
    ident = json.dumps([p.token, p.kind, p.group, p.family], ensure_ascii=False)
    return hashlib.sha256(ident.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Verdict:
    """The owner's choice for one proposal, as the page holds it."""
    verdict: str = PENDING
    group: str | None = None
    family: str | None = None
    meaning: str = ""


def load_batch(review_log_path, lexicon_path, proposals_path) -> tuple[list[Proposal], list[dict],
                                                                       str, int]:
    """The proposals the page shows, from the current files: (proposals, review-log entries,
    source ("file" or "log"), unreadable lines)."""
    entries, bad = read_review_log(review_log_path)
    path = Path(proposals_path).expanduser()
    if path.exists():
        props, bad_props = read_proposals(path, lexicon_path=lexicon_path)
        warned = market_warning_words(entries)  # the evidence decides, never the file
        for p in props:
            p.market_warning = is_warned(p.token, warned)
            if p.market_warning:  # the owner, never the file, moves it to a list
                p.group, p.family = AMBIGUOUS, None
        return props, entries, "file", bad + bad_props
    return proposals_from_log(entries, lexicon_path=lexicon_path), entries, "log", bad


def batch_items(proposals: list[Proposal], verdicts: dict[str, Verdict]) -> list[BatchItem]:
    """Each shown proposal with the owner's verdict (undecided when the owner chose nothing)."""
    out = []
    for p in proposals:
        v = verdicts.get(proposal_id(p)) or Verdict()
        group = v.group if v.group in GROUPS else p.group
        family = (v.family or p.family) if group == MARKET_NAME else None
        out.append(BatchItem(p.token, v.verdict, group, family, v.meaning,
                             p.market_warning))
    return out


def final_table(items: list[BatchItem]) -> list[dict[str, str]]:
    """The final batch the owner confirms: every accepted or keep-holding word, the exact list it
    is written to and its effect in plain words."""
    rows = []
    for i in items:
        if i.verdict == ACCEPT and i.group in GROUP_LIST:
            where = GROUP_LIST[i.group] + (f".{i.family}" if i.group == MARKET_NAME else "")
            effect = "removes a hold: posts containing this word may become executable"
        elif i.verdict == ACCEPT:
            where, effect = "-", "refused: choose a list first"
        elif i.verdict == KEEP_HOLDING:
            where, effect = "review-decisions (keep holding)", "keeps holding"
        else:
            continue
        rows.append({"word": normalise(i.token), "list": f"entry_anchor.{where}"
                     if i.verdict == ACCEPT and i.group in GROUP_LIST else where,
                     "family": i.family or "", "effect": effect, "meaning": i.meaning,
                     "warning": MAY_BE_MARKET if i.may_be_market and i.verdict == ACCEPT
                     else ""})
    return sorted(rows, key=lambda r: (r["list"], r["word"]))


def batch_digest(items: list[BatchItem], lexicon_path: str | Path) -> str:
    """A digest of the final table and of the production lexicon's current bytes."""
    try:
        lex = Path(lexicon_path).expanduser().read_bytes()
    except OSError:
        lex = b""
    rows = [[normalise(i.token), i.verdict, i.group, i.family or "", i.meaning,
             bool(i.may_be_market)] for i in items if i.verdict in (ACCEPT, KEEP_HOLDING)]
    blob = json.dumps([sorted(rows), hashlib.sha256(lex).hexdigest()], ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def apply_reviewed(lexicon_path, review_log_path, proposals_path, verdicts: dict[str, Verdict],
                   expected_digest: str | None, *, now: datetime | None = None) -> BatchResult:
    """Apply exactly the batch the owner reviewed: recompute it from the current files and refuse
    when its digest is not the one shown (a changed proposals file, review log or lexicon)."""
    props, *_ = load_batch(review_log_path, lexicon_path, proposals_path)
    items = batch_items(props, verdicts)
    if not expected_digest or batch_digest(items, lexicon_path) != expected_digest:
        return BatchResult("REFUSED", "the batch changed since you reviewed it (the proposals, "
                                      "the review log or the lexicon changed); nothing written. "
                                      "Review the final table again.")
    return apply_batch(lexicon_path, items, now=now)


def decisions_path(lexicon_path: str | Path) -> Path:
    """The private decisions file beside the lexicon: keep-holding words and the batch history
    (with the owner's meanings)."""
    p = Path(lexicon_path).expanduser()
    return p.with_name(f"{p.stem}.review-decisions.json")


def _read_decisions(lexicon_path) -> dict:
    try:
        raw = json.loads(decisions_path(lexicon_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def decided_words(lexicon_path: str | Path) -> set[str]:
    return {normalise(d["token"]) for d in _read_decisions(lexicon_path).get("keep_holding") or ()
            if isinstance(d, dict) and d.get("token")}


def backups(lexicon_path: str | Path) -> list[Path]:
    """The lexicon's backups, oldest first (their names sort by time)."""
    p = Path(lexicon_path).expanduser()
    return sorted(p.parent.glob(f"{_glob_escape(p.name)}.bak-*"))


def _glob_escape(name: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", name)


def _write_atomic(path: Path, data: bytes, what: str = "private file") -> None:
    """Replace a private file atomically, open-then-verify (``PrivateDir.write_atomic``)."""
    with PrivateDir(path, what) as d:
        d.write_atomic(data)


def loader_problem(path: Path, *, allow_candidate: bool = False) -> str | None:
    """Why the runtime's own lexicon loader refuses ``path``, or None when it loads."""
    # The real loader, not a copy: its raw validation errors are for the owner's page only.
    from tradingbot.runtime.service import read_lexicons

    try:
        read_lexicons(path, allow_candidate=allow_candidate)
    except ValidationError as exc:
        return "; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors())
    except CandidateLexiconRefused:
        return "it is a candidate lexicon, not the production lexicon"
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return f"the lexicon loader refuses the file ({type(exc).__name__})"
    return None


def _serialise(raw: dict) -> bytes:
    return (json.dumps(raw, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _read_production(lexicon_path) -> tuple[Path, bytes, dict] | str:
    """The production lexicon (path, bytes, parsed), or why it cannot be used."""
    try:
        path = check_private_path(lexicon_path, "private lexicon")
    except PrivatePathError as exc:
        return str(exc)
    if not path.is_file():
        return (f"the private lexicon {path} does not exist: create it with the content {{}} and "
                "run chmod 600 on it first")
    try:
        before = path.read_bytes()
        raw = json.loads(before.decode("utf-8"))
    except (OSError, ValueError):
        return "the private lexicon is not readable JSON; nothing written"
    if not isinstance(raw, dict) or not isinstance(raw.get("entry_anchor", {}), dict):
        return "the private lexicon is not a JSON object with an entry_anchor object"
    if CANDIDATE_KEY in raw:
        return "the configured lexicon is a candidate lexicon, not the production lexicon"
    return path, before, raw


def _add(raw: dict, item: BatchItem) -> tuple[str, str] | str:
    """Add ``item`` to ``raw`` in place: (list name, word) written, or why not (no change)."""
    word = normalise(item.token)
    if item.group not in GROUP_LIST:
        return "an ambiguous expression needs a list chosen by the owner before it is accepted"
    section = raw.setdefault("entry_anchor", {})
    if item.group == MARKET_NAME:
        if not (item.family and _FAMILY_RE.match(item.family)):
            return "a market name needs the family it names"
        aliases = section.setdefault("family_aliases", {})
        if not isinstance(aliases, dict):
            return "entry_anchor.family_aliases is not an object"
        target, where = aliases.setdefault(item.family, []), f"family_aliases.{item.family}"
    else:
        where = GROUP_LIST[item.group]
        target = section.setdefault(where, [])
    if not isinstance(target, list):
        return f"entry_anchor.{where} is not a list"
    if word in (normalise(w) for w in target if isinstance(w, str)):
        return "UNCHANGED"
    target.append(word)
    return where, word


def _check(path: Path, raw: dict, *, allow_candidate: bool = False) -> str | None:
    """The loader's verdict on ``raw``, from a temporary private file beside ``path``."""
    tmp = f".{path.name}.check-{os.getpid()}-{os.urandom(4).hex()}"
    try:
        with PrivateDir(path, "private lexicon") as d:
            d.create(tmp, _serialise(raw))
            try:
                return loader_problem(d.target.parent / tmp, allow_candidate=allow_candidate)
            finally:
                d.unlink(tmp)
    except (OSError, PrivatePathError) as exc:
        return f"could not write beside the lexicon ({type(exc).__name__})"


def _build(path: Path, raw: dict, items: list[BatchItem], result: BatchResult,
           *, allow_candidate: bool = False) -> dict:
    """``raw`` plus every accepted item the loader accepts, one at a time; refusals recorded."""
    new = copy.deepcopy(raw)
    for item in items:
        if item.verdict != ACCEPT:
            continue
        trial = copy.deepcopy(new)
        added = _add(trial, item)
        if added == "UNCHANGED":
            result.unchanged.append(normalise(item.token))
            continue
        if isinstance(added, str):
            result.refused.append((normalise(item.token), added))
            continue
        problem = _check(path, trial, allow_candidate=allow_candidate)
        if problem is not None:
            result.refused.append((normalise(item.token), problem))
            continue
        new = trial
        result.added.setdefault(added[0], []).append(added[1])
    return new


def apply_batch(lexicon_path: str | Path, items: list[BatchItem], *,
                now: datetime | None = None) -> BatchResult:
    """The owner's consolidated approval: ONE validated, atomic change of the production lexicon
    with ONE backup (see the module docstring). Refused proposals are listed with the loader's
    reason and left out; pending and rejected ones write nothing."""
    now = now or datetime.now(UTC)
    found = _read_production(lexicon_path)
    if isinstance(found, str):
        return BatchResult("REFUSED", found)
    path, before, raw = found
    result = BatchResult("NOTHING", "", path=path)
    new = _build(path, raw, items, result)
    keep = [normalise(i.token) for i in items if i.verdict == KEEP_HOLDING]
    batch = now.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    if result.added:
        problem = _check(path, new)  # the whole result, once more
        if problem is not None:
            return BatchResult("REFUSED", f"the batch as a whole is refused by the loader: "
                                          f"{problem}; nothing written", refused=result.refused)
        backup = path.with_name(f"{path.name}.bak-{batch}")
        n = 1
        while backup.exists():
            backup, n = path.with_name(f"{path.name}.bak-{batch}-{n}"), n + 1
        try:
            with PrivateDir(path, "private lexicon") as d:
                d.check_existing(d.name)
                d.create(backup.name, before)
                try:
                    d.write_atomic(_serialise(new))
                except BaseException:
                    d.unlink(backup.name)
                    raise
        except (OSError, PrivatePathError) as exc:
            return BatchResult("REFUSED", f"the lexicon was not replaced ({type(exc).__name__}); "
                                          "it is unchanged", refused=result.refused)
        result.backup, result.status = backup, "WRITTEN"
    recorded = True
    if result.added or keep:
        recorded = _record_batch(path, batch, items, result, keep, now)
        if result.status != "WRITTEN":
            result.status = "RECORDED" if recorded else "REFUSED"
    result.kept_holding = keep if recorded else []
    n_added = sum(len(v) for v in result.added.values())
    result.message = (f"{n_added} word(s) added, {len(result.kept_holding)} kept holding, "
                      f"{len(result.refused)} refused, {len(result.unchanged)} already present. "
                      "A new word takes effect when the service (or a replay) is restarted.")
    if not recorded:
        result.message += (" The decisions file could not be written: keep-holding words were "
                           "not recorded" + (", and undo of this batch is not available."
                                             if result.status == "WRITTEN" else "."))
    return result


def _record_batch(path, batch, items, result, keep, now) -> bool:
    """Keep-holding words, the applied words (with the owner's meanings) and the sha256 of the
    lexicon written, beside the lexicon. After the lexicon write: the lexicon is the authority.
    False when the file could not be written."""
    raw = _read_decisions(path)
    if result.backup is not None:
        raw.setdefault("writes", []).append({
            "batch": batch, "backup": result.backup.name, "at": now.isoformat(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    written = {w for words in result.added.values() for w in words}
    meanings = {normalise(i.token): i.meaning for i in items}
    held = raw.setdefault("keep_holding", [])
    have = {normalise(d.get("token", "")) for d in held if isinstance(d, dict)}
    held += [{"token": w, "at": now.isoformat(), "batch": batch,
              "meaning": meanings.get(w, "")} for w in keep if w not in have]
    raw.setdefault("applied", []).extend(
        {"token": w, "list": where, "meaning": meanings.get(w, ""), "at": now.isoformat(),
         "batch": batch, "backup": result.backup.name if result.backup else None}
        for where, words in result.added.items() for w in words if w in written)
    try:
        _write_atomic(decisions_path(path), _serialise(raw), "decisions file")
    except (OSError, PrivatePathError):
        return False
    return True


def undo_last(lexicon_path: str | Path) -> BatchResult:
    """Restore the lexicon from its latest backup (validated by the loader first), remove that
    backup, and drop the batch's records from the decisions file."""
    try:
        path = check_private_path(lexicon_path, "private lexicon")
    except PrivatePathError as exc:
        return BatchResult("REFUSED", str(exc))
    found = backups(path)
    if not found:
        return BatchResult("NOTHING_TO_UNDO", "No backup to restore.")
    latest = found[-1]
    raw = _read_decisions(path)
    wrote = [w for w in raw.get("writes") or () if isinstance(w, dict)
             and w.get("backup") == latest.name]
    current = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    if not wrote or wrote[-1].get("sha256") != current:
        return BatchResult("REFUSED", f"the lexicon is not the file this tool last wrote (it was "
                                      f"edited by hand, or the write was not recorded): undo "
                                      f"would discard that edit. Restore {latest.name} by hand "
                                      "if that is what you want.", backup=latest, path=path)
    problem = loader_problem(latest)
    if problem is not None:
        return BatchResult("REFUSED", f"the latest backup {latest.name} does not load: {problem}")
    try:
        _write_atomic(path, latest.read_bytes(), "private lexicon")
        latest.unlink()
    except (OSError, PrivatePathError) as exc:
        return BatchResult("REFUSED", f"the backup was not restored ({type(exc).__name__})")
    batches = {d.get("batch") for d in raw.get("writes") or ()
               if isinstance(d, dict) and d.get("backup") == latest.name}
    if batches:
        for key in ("applied", "keep_holding", "writes"):
            raw[key] = [d for d in raw.get(key) or ()
                        if not (isinstance(d, dict) and d.get("batch") in batches)]
        with contextlib.suppress(OSError, PrivatePathError):
            _write_atomic(decisions_path(path), _serialise(raw), "decisions file")
    return BatchResult("RESTORED", f"Restored {latest.name}. It takes effect when the service (or "
                                   "a replay) is restarted.", backup=latest, path=path)


# ------------------------------------------------------------------ experiments


def default_candidate_path(lexicon_path: str | Path) -> Path:
    p = Path(lexicon_path).expanduser()
    return p.with_name(f"{p.stem}.candidate.json")


def write_candidate(lexicon_path: str | Path, items: list[BatchItem], out_path: str | Path, *,
                    now: datetime | None = None) -> BatchResult:
    """An EXPERIMENT: the production lexicon plus the accepted ``items``, validated by the same
    loader, written to ``out_path`` (private, never the production file) with the candidate
    marker. The production lexicon is only read. The service refuses to load the result; an
    offline replay may, with ``--allow-candidate-lexicon``."""
    now = now or datetime.now(UTC)
    found = _read_production(lexicon_path)
    if isinstance(found, str):
        return BatchResult("REFUSED", found)
    path, before, raw = found
    try:
        out = check_private_path(out_path, "candidate lexicon")
    except PrivatePathError as exc:
        return BatchResult("REFUSED", str(exc))
    if out == path or out.name.startswith(f"{path.name}.bak-") or \
            out == decisions_path(path) or not out.name.endswith(CANDIDATE_SUFFIX):
        return BatchResult("REFUSED", f"the candidate lexicon must be its own file ending in "
                                      f"{CANDIDATE_SUFFIX}, never the production lexicon, a "
                                      "backup, the decisions file or the review log")
    if out.exists():
        try:
            marked = CANDIDATE_KEY in json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            marked = False
        if not marked or out.is_symlink():
            return BatchResult("REFUSED", "an existing file without the candidate marker is "
                                          "never overwritten")
    result = BatchResult("NOTHING", "", path=out)
    new = _build(path, raw, items, result, allow_candidate=True)
    new[CANDIDATE_KEY] = {
        "marker": "CANDIDATE: unapproved vocabulary proposals, not the production lexicon",
        "created_at": now.isoformat(), "base": path.name,
        "base_sha256": hashlib.sha256(before).hexdigest(),
        "added": sum(len(v) for v in result.added.values()),
    }
    problem = _check(out, new, allow_candidate=True)
    if problem is not None:
        return BatchResult("REFUSED", f"the candidate is refused by the loader: {problem}",
                           refused=result.refused)
    try:
        _write_atomic(out, _serialise(new), "candidate lexicon")
    except (OSError, PrivatePathError) as exc:
        return BatchResult("REFUSED", f"the candidate was not written ({type(exc).__name__})")
    result.status = "WRITTEN"
    result.message = (f"Candidate written with {new[CANDIDATE_KEY]['added']} proposal(s); the "
                      "production lexicon is unchanged. Only an offline replay with "
                      "--allow-candidate-lexicon loads it.")
    return result
