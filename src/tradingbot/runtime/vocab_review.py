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
"""

from __future__ import annotations

import contextlib
import copy
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
_FAMILY_RE = re.compile(r"^[A-Z][A-Z0-9]{0,9}(\.OPT)?$")


class PrivatePathError(ValueError):
    """A private file would be readable by others or land inside a repository."""


class CandidateLexiconRefused(ConfigError):
    """A candidate (experimental) lexicon configured where the production lexicon belongs."""


# ------------------------------------------------------------------ private paths


def git_work_tree(path: Path) -> Path | None:
    """The git work tree holding ``path`` (a ``.git`` directory or file in it or an ancestor)."""
    for folder in (path, *path.parents):
        if (folder / ".git").exists():
            return folder
    return None


def check_private_path(path: str | Path, what: str) -> Path:
    """``path`` with its folders resolved (the last part is not followed), refused when it lies in
    this repository or any git work tree."""
    raw = Path(path).expanduser()
    target = Path(os.path.realpath(raw.parent)) / raw.name
    if target == REPO_ROOT or REPO_ROOT in target.parents:
        raise PrivatePathError(f"the {what} must lie outside the repository ({REPO_ROOT}): "
                               "private text never enters the repository tree")
    tree = git_work_tree(target.parent)
    if tree is not None:
        raise PrivatePathError(f"the {what} must lie outside any git work tree (found one at "
                               f"{tree}): private text must never be committable")
    return target


def _check_mode(mode: int, what: str) -> None:
    if stat.S_IMODE(mode) & 0o077:
        raise PrivatePathError(f"the {what} is group- or world-readable (mode "
                               f"{stat.S_IMODE(mode):o}); run chmod 600 on it")


class ReviewLog:
    """The service's append-only private review log. Refuses at construction a path inside the
    repository or a git work tree, a symbolic link, and an existing file others may read; every
    append re-checks the mode of the open file."""

    WHAT = "review log ([interpreter] review_log_path)"

    def __init__(self, path: str | Path):
        self.path = check_private_path(path, self.WHAT)
        if self.path.is_symlink():
            raise PrivatePathError(f"the {self.WHAT} may not be a symbolic link")
        if self.path.exists():
            if not self.path.is_file():
                raise PrivatePathError(f"the {self.WHAT} is not a regular file")
            _check_mode(self.path.stat().st_mode, self.WHAT)
        else:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)

    def append(self, record: dict) -> None:
        line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            _check_mode(os.fstat(fd).st_mode, self.WHAT)
            os.write(fd, line)
        finally:
            os.close(fd)


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
    entries, bad = [], 0
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
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
    sentences: list[tuple[str, str]] = field(default_factory=list)  # (role, sentence)


def normalise(token: str) -> str:
    return " ".join(str(token).lower().split())


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


def candidates(entries: list[dict], *, lexicon_path: str | Path | None = None) -> list[Candidate]:
    """The words to review, by (word, kind): held posts, holds, families, reason codes and the
    sentences that hold the word. A word already in the lexicon or marked a different market is
    left out. Sorted by held posts, then holds."""
    known = known_words(lexicon_path)
    found: dict[tuple[str, str], Candidate] = {}
    posts: dict[tuple[str, str], set] = {}
    for e in entries:
        seen = set()
        for t in e.get("tokens") or ():
            if not isinstance(t, dict) or not t.get("token"):
                continue
            key = (normalise(t["token"]), str(t.get("kind") or "UNKNOWN"))
            if key[0] in known or key in seen:
                continue
            seen.add(key)
            c = found.setdefault(key, Candidate(*key))
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
                if pair not in c.sentences:
                    c.sentences.append(pair)
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
    examples: list[tuple[str, str]] = field(default_factory=list)


def _proposal(raw: dict, entries: list[dict]) -> Proposal | None:
    token = raw.get("token")
    group = raw.get("group")
    if raw.get("schema") != PROPOSAL_SCHEMA or not isinstance(token, str) or not token.strip() \
            or group not in GROUPS:
        return None
    family = raw.get("family") if isinstance(raw.get("family"), str) else None
    evidence = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}
    cats = raw.get("categories") if isinstance(raw.get("categories"), dict) else {}
    measured = raw.get("measured")
    if isinstance(measured, dict) and measured and all(
            isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
            for k, v in measured.items()):
        measured = dict(measured)
    else:
        measured = None  # not measured (or not public counts)
    examples = []
    for ex in raw.get("examples") or ():
        if isinstance(ex, dict) and isinstance(ex.get("text"), str):
            examples.append((str(ex.get("role", "")), ex["text"]))
        elif isinstance(ex, dict) and (ex.get("source_id") or ex.get("post_id")):
            examples += _referenced(ex, normalise(token), entries)
    return Proposal(
        token=normalise(token), group=group, family=family,
        meaning=raw.get("meaning") if isinstance(raw.get("meaning"), str) else "",
        kind=raw.get("kind") if isinstance(raw.get("kind"), str) else None,
        posts=_count(evidence.get("posts")), holds=_count(evidence.get("holds")),
        families=sorted(str(f) for f in cats.get("families") or ()),
        reasons=sorted(str(r) for r in cats.get("reasons") or ()),
        measured=measured, examples=list(dict.fromkeys(examples)))


def _count(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


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


def read_proposals(path: str | Path, *, entries: list[dict] = (),
                   lexicon_path: str | Path | None = None) -> tuple[list[Proposal], int]:
    """A proposals file's proposals (words already decided left out) and how many lines could
    not be read. Example references are resolved against the review log ``entries``."""
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
        prop = _proposal(raw, list(entries)) if isinstance(raw, dict) else None
        if prop is None:
            bad += 1
        elif prop.token not in known and prop.token not in seen:
            seen.add(prop.token)
            out.append(prop)
    return out, bad


_INSTRUCTION_ROLES = {"LEVEL", "INSTRUCTION"}


def proposals_from_log(entries: list[dict], *, lexicon_path: str | Path | None = None
                       ) -> list[Proposal]:
    """A plain batch from the review log by kind: a proper noun is a non-market word; a market
    noun or instrument phrase seen in posts of one family is another name of that family; an
    unknown word of an instruction sentence is an instruction phrase; anything else is
    ambiguous. Meaning empty, measured change not measured."""
    out = []
    for c in candidates(entries, lexicon_path=lexicon_path):
        family = None
        roles = {r for r, _ in c.sentences}
        if c.kind == "PROPER_NOUN":
            group = NON_MARKET
        elif c.kind in ("MARKET_NOUN", "INSTRUMENT_PHRASE") and len(c.families) == 1:
            group, family = MARKET_NAME, c.families[0]
        elif c.kind == "UNKNOWN_WORD" and roles and roles <= _INSTRUCTION_ROLES:
            group = INSTRUCTION_PHRASE
        else:
            group = AMBIGUOUS
        out.append(Proposal(token=c.token, group=group, family=family, kind=c.kind,
                            posts=c.posts, holds=c.holds, families=list(c.families),
                            reasons=list(c.reasons), examples=list(c.sentences)))
    return out


# ------------------------------------------------------------------ the batch


@dataclass
class BatchItem:
    """The owner's verdict on one proposal: ``group`` and ``family`` as the owner left them."""
    token: str
    verdict: str
    group: str
    family: str | None = None
    meaning: str = ""


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


def _write_private(path: Path, data: bytes) -> Path:
    """A new temporary file beside ``path`` with mode 0600, flushed to disk."""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    return tmp


def _sync_dir(folder: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _replace(tmp: Path, path: Path) -> None:
    try:
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _sync_dir(path.parent)


def _write_atomic(path: Path, data: bytes) -> None:
    _replace(_write_private(path, data), path)
    os.chmod(path, 0o600)


def loader_problem(path: Path, *, allow_candidate: bool = False) -> str | None:
    """Why the runtime's own lexicon loader refuses ``path``, or None when it loads."""
    from tradingbot.runtime.service import load_lexicons  # the real loader, not a copy

    try:
        load_lexicons(path, allow_candidate=allow_candidate)
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
    try:
        tmp = _write_private(path, _serialise(raw))
    except OSError as exc:
        return f"could not write beside the lexicon ({type(exc).__name__})"
    try:
        return loader_problem(tmp, allow_candidate=allow_candidate)
    finally:
        tmp.unlink(missing_ok=True)


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
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, before)
                os.fsync(fd)
            finally:
                os.close(fd)
            _write_atomic(path, _serialise(new))
        except OSError as exc:
            backup.unlink(missing_ok=True)
            return BatchResult("REFUSED", f"the lexicon was not replaced ({type(exc).__name__}); "
                                          "it is unchanged", refused=result.refused)
        result.backup, result.status = backup, "WRITTEN"
    if result.added or keep:
        _record_batch(path, batch, items, result, keep, now)
        if result.status != "WRITTEN":
            result.status = "RECORDED"
    result.kept_holding = keep if result.status in ("WRITTEN", "RECORDED") else []
    n_added = sum(len(v) for v in result.added.values())
    result.message = (f"{n_added} word(s) added, {len(result.kept_holding)} kept holding, "
                      f"{len(result.refused)} refused, {len(result.unchanged)} already present. "
                      "A new word takes effect when the service (or a replay) is restarted.")
    return result


def _record_batch(path, batch, items, result, keep, now) -> None:
    """Keep-holding words and the applied words (with the owner's meanings) beside the lexicon.
    Best effort after the lexicon write: the lexicon is the authority."""
    raw = _read_decisions(path)
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
    with contextlib.suppress(OSError):
        _write_atomic(decisions_path(path), _serialise(raw))


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
    problem = loader_problem(latest)
    if problem is not None:
        return BatchResult("REFUSED", f"the latest backup {latest.name} does not load: {problem}")
    try:
        _write_atomic(path, latest.read_bytes())
        latest.unlink()
    except OSError as exc:
        return BatchResult("REFUSED", f"the backup was not restored ({type(exc).__name__})")
    raw = _read_decisions(path)
    batches = {d.get("batch") for d in raw.get("applied") or ()
               if isinstance(d, dict) and d.get("backup") == latest.name}
    if batches:
        for key in ("applied", "keep_holding"):
            raw[key] = [d for d in raw.get(key) or ()
                        if not (isinstance(d, dict) and d.get("batch") in batches)]
        with contextlib.suppress(OSError):
            _write_atomic(decisions_path(path), _serialise(raw))
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
    if out == path or out.name.startswith(f"{path.name}.bak-"):
        return BatchResult("REFUSED", "the candidate lexicon must be a separate file, never the "
                                      "production lexicon or one of its backups")
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
        _write_atomic(out, _serialise(new))
    except OSError as exc:
        return BatchResult("REFUSED", f"the candidate was not written ({type(exc).__name__})")
    result.status = "WRITTEN"
    result.message = (f"Candidate written with {new[CANDIDATE_KEY]['added']} proposal(s); the "
                      "production lexicon is unchanged. Only an offline replay with "
                      "--allow-candidate-lexicon loads it.")
    return result
