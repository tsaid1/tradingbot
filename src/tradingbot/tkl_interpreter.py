"""TKL-aware deterministic signal interpreter with point-in-time context (Sprint 1.2A).

Advisory only. It turns one ``SourcePost`` into zero or more ``TradeAction`` children plus one
``Classification`` per clause. It authorizes nothing: every action goes through ``validator.py``
via the Sprint 1.1 replay coordinator, which also owns sizing and the journal.

Rules
  * A deterministic grammar for the publisher's house style. Tests use synthetic wording only.
  * Present tense authorizes; past tense reconciles; conditions hold; commentary, cancellations
    and position summaries never trade. Tense is decided per clause, not per post.
  * References to existing positions are resolved against a point-in-time view of the publisher's
    book: only posts published at or before the current post are visible, so later messages can
    never leak into earlier decisions.
  * Spot, index and futures levels (S&P 500, gold, natural gas, ...) are recorded as source series
    with no orderable instrument; the gateway holds them. Only an explicitly named ticker becomes
    an instrument.
  * An optional structured-output AI backend may be plugged in, but paid feed content is never
    sent to it unless the caller asserts that usage permission exists.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Literal, Protocol

from pydantic import Field

from tradingbot.event_models import ParsedPost, PriceBasis, ReplayResult, Timing, TradeAction
from tradingbot.event_replay import PaperEventReplay
from tradingbot.models import Action, AssetClass, Instrument, PositionSide, Quote, StrictModel
from tradingbot.source_events import SourcePost
from tradingbot.validator import gate_source


# ----------------------------------------------------------------------------- vocabulary

class MessageClass(str, Enum):
    OPEN = "OPEN"
    ADD = "ADD"
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"
    MODIFY = "MODIFY"
    CANCEL = "CANCEL"
    CONDITIONAL = "CONDITIONAL"
    COMMENTARY = "COMMENTARY"
    RETROSPECTIVE = "RETROSPECTIVE"
    POSITION_SUMMARY = "POSITION_SUMMARY"
    # A fresh, past-tense announcement that the publisher has just exited a position the follower
    # still holds. Becomes a CURRENT reduce-only CLOSE/REDUCE candidate for the gateway. Recaps and
    # stop-triggered reports stay RETROSPECTIVE and only reconcile.
    EXIT_ANNOUNCEMENT = "EXIT_ANNOUNCEMENT"


class Tense(str, Enum):
    PRESENT = "PRESENT"
    PAST = "PAST"
    FUTURE = "FUTURE"


class PriceRole(str, Enum):
    ENTRY = "ENTRY"
    STOP = "STOP"
    TARGET = "TARGET"
    EXIT = "EXIT"
    TRIGGER = "TRIGGER"
    REFERENCE = "REFERENCE"
    PNL_PERCENT = "PNL_PERCENT"


class PriceMention(StrictModel):
    role: PriceRole
    value: str


class Classification(StrictModel):
    action_id: str
    message_class: MessageClass
    tense: Tense
    underlying: Action = Action.NO_TRADE          # the action a conditional or retrospective refers to
    asset: str | None = None                      # ticker or source-series code
    side: PositionSide | None = None
    prices: list[PriceMention] = Field(default_factory=list)
    close_fraction: str | None = None
    idea_reference: str | None = None
    exit_kind: Literal["announcement", "recap", "stop_triggered"] | None = None
    clause: str
    sentence: int = 0                             # index of the sentence the clause came from
    ambiguities: list[str] = Field(default_factory=list)


class InterpretedPost(StrictModel):
    source_id: str
    post_id: str
    revision: int
    published_at: datetime
    parsed: ParsedPost
    classifications: list[Classification] = Field(default_factory=list)
    backend: Literal["deterministic", "ai"] = "deterministic"


class StructuredBackend(Protocol):
    def parse(self, text: str, image: bytes | None = None) -> ParsedPost: ...


# ----------------------------------------------------------------------------- lexicon

def _r(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


KNOWN_ETFS = frozenset(
    "TLT SPY QQQ GLD SLV IWM USO UNG HYG LQD TBT TMF GDX GDXJ DIA XLE XLF XLK SQQQ TQQQ VXX UVXY "
    "EEM FXI KRE SMH ARKK IEF SHY BITO IBIT".split()
)
# Source series that are never orderable as such. Longest names first so "s&p 500" wins over "s&p".
SERIES: list[tuple[str, str]] = [
    ("s&p 500", "SPX"), ("s&p500", "SPX"), ("the s&p", "SPX"), ("s&p", "SPX"), ("spx", "SPX"),
    ("nasdaq 100", "NDX"), ("nasdaq", "NDX"), ("ndx", "NDX"), ("dow jones", "DJI"), ("the dow", "DJI"),
    ("dow", "DJI"), ("russell 2000", "RTY"), ("russell", "RTY"), ("natural gas", "NATGAS"),
    ("nat gas", "NATGAS"), ("natgas", "NATGAS"), ("crude oil", "WTI"), ("wti", "WTI"), ("brent", "BRENT"),
    ("oil", "WTI"), ("gold", "GOLD"), ("silver", "SILVER"), ("copper", "COPPER"), ("bitcoin", "BTC"),
    ("btc", "BTC"), ("ethereum", "ETH"), ("10-year yield", "US10Y"), ("10-year", "US10Y"),
    ("10 year", "US10Y"), ("treasury yields", "US10Y"), ("vix", "VIX"), ("dollar index", "DXY"),
    ("dxy", "DXY"), ("us dollar", "DXY"),
]
SERIES_RES = [(re.compile(r"(?<![\w$])" + re.escape(name) + r"(?!\w)", re.IGNORECASE), code) for name, code in SERIES]
SERIES_ALT = "|".join(re.escape(name) for name, _ in SERIES)
TICKER_RE = re.compile(r"\$([A-Z]{1,5})\b")
BARE_ETF_RE = re.compile(r"(?<![\w$])(" + "|".join(sorted(KNOWN_ETFS)) + r")(?!\w)")
ASSET_TOKEN = rf"(?:\$?(?-i:[A-Z]{{1,5}})\b|{SERIES_ALT})"

FRACTIONS: list[tuple[re.Pattern[str], str]] = [
    (_r(r"\b(?:one[- ]|a )?half\b"), "0.5"),
    (_r(r"\b(?:one[- ]|a )third\b"), "0.333333"),
    (_r(r"\btwo[- ]thirds\b"), "0.666667"),
    (_r(r"\b(?:one[- ]|a )quarter\b"), "0.25"),
    (_r(r"\bthree[- ]quarters\b"), "0.75"),
]
PCT_OF_RE = _r(r"(\d{1,3})\s?%\s+of\b")
PARTIAL_RE = _r(r"\b(?:partial|some|part of|portion)\b")

# Verb lexicon: first match decides the base class and tense. Order matters.
CANCEL_RE = _r(r"\b(?:cancel(?:l)?(?:ing|ed)?|no longer (?:looking|planning|intending) to|scrapping|scrapped|"
               r"withdrawing|withdrawn|calling off|called off|abandon(?:ing|ed)|shelving)\b")
VERBS: list[tuple[re.Pattern[str], MessageClass, Tense]] = [
    (_r(r"\bwe (?:have |had )?(?:raised|moved|lowered|tightened|trailed|adjusted) (?:our |the )?(?:stop|target)"),
     MessageClass.MODIFY, Tense.PAST),
    (_r(r"\b(?:raising|moving|lowering|tightening|trailing|adjusting|lifting) (?:our |the )?"
        r"(?:stop|target|stop[- ]loss|take[- ]profit)|"
        r"\b(?:stop|target|stop[- ]loss)(?: is| has been)? (?:now )?(?:raised|moved|lowered|tightened|set|adjusted|trailed)"
        r"(?: up| down)? to\b|\bnew (?:stop|target)\b|\b(?:stop|target) (?:is )?now (?:at )?\$?\d"),
     MessageClass.MODIFY, Tense.PRESENT),
    (_r(r"\b(?:took|taken|booked|locked in) (?:partial |some )?profits?\b|\btrimmed\b|"
        r"\bsold (?:half|a third|one third|a quarter|two thirds|\d+%)|\breduced (?:our|the)\b"),
     MessageClass.REDUCE, Tense.PAST),
    (_r(r"\b(?:taking (?:partial |some )?profits?|booking (?:partial |some )?profits?|trimming|reducing|"
        r"scaling out|lightening up|selling (?:half|a third|one third|a quarter|two thirds|\d+%)|"
        r"closing (?:half|a third|one third|two thirds|a quarter|\d+% of)|"
        r"exiting (?:half|a third|one third|two thirds|a quarter|\d+% of))\b"),
     MessageClass.REDUCE, Tense.PRESENT),
    (_r(r"\b(?:added (?:to|more)|we added|doubled(?: down)?|increased our)\b"), MessageClass.ADD, Tense.PAST),
    (_r(r"\b(?:adding (?:to|more|here|a second|another)|doubling(?: down)?|increasing (?:our|the)|scaling into|"
        r"adding (?:a |an )?(?:second|third|new) tranche)\b"), MessageClass.ADD, Tense.PRESENT),
    (_r(r"\b(?:(?:have |has |had |were |was |got )?(?:closed(?: out)?|exited|stopped out|covered)|"
        r"took full profits|we are out of)\b"), MessageClass.CLOSE, Tense.PAST),
    (_r(r"\b(?:closing(?: out)?|exiting|covering|taking full profits|cutting (?:our |the )?(?:position|losses)|"
        r"closing the (?:rest|remaining|remainder|entire|full))\b"), MessageClass.CLOSE, Tense.PRESENT),
    (_r(r"\b(?:we (?:went|got) (?:long|short)|we entered|we bought|we shorted|entered (?:a |an )?(?:new )?(?:long|short)|"
        r"opened (?:a |an )?(?:new )?(?:long|short)|we are (?:long|short) [^.]{0,40}?\bfrom\b)"),
     MessageClass.OPEN, Tense.PAST),
    (_r(r"\b(?:we are now (?:long|short)|now (?:long|short)|going (?:long|short)|"
        r"entering (?:a |an )?(?:new )?(?:long|short)|opening (?:a |an )?(?:new )?(?:long|short)|"
        r"initiating (?:a |an )?(?:new )?(?:long|short)|we are buying|we are shorting|"
        rf"buying {ASSET_TOKEN}|shorting {ASSET_TOKEN}|new (?:long|short)(?: position)?\b|"
        rf"(?:long|short) {ASSET_TOKEN}[^.]{{0,30}}?\b(?:at|@)\b)"),
     MessageClass.OPEN, Tense.PRESENT),
]
INFINITIVE_RE = _r(r"\b(?:will|would|we'll|plan to|intend to|look(?:ing)? to) (?:look to )?"
                   r"(?P<verb>exit|close|cover|add|buy|sell|go long|go short|short|take profits|re-enter|stop out)\b")
INFINITIVE_ACTION = {
    "exit": Action.CLOSE, "close": Action.CLOSE, "cover": Action.CLOSE, "stop out": Action.CLOSE,
    "add": Action.ADD, "buy": Action.OPEN, "go long": Action.OPEN, "sell": Action.CLOSE, "short": Action.OPEN,
    "go short": Action.OPEN, "take profits": Action.REDUCE, "re-enter": Action.OPEN,
}
CONDITIONAL_RE = _r(r"\b(?:if|should|once|unless|provided that|as long as|in the event)\b|"
                    r"\bon (?:a |any )?(?:break|close|move|daily close|confirmed break)\b")
COMMENTARY_RE = _r(r"\b(?:remain|remains|remaining (?:long|short)|still (?:long|short)|continue to|expect|expecting|"
                   r"could|may|might|likely|watching|looking for|eyeing|no change|our view|we think|we believe|"
                   r"would not be surprised|setting up|setup)\b")
SUMMARY_RE = _r(r"^\s*(?:current|open|our|active) positions?\b|\bposition (?:summary|update)\b|"
                r"\bportfolio (?:update|summary)\b|^\s*positions?\s*:")
SUMMARY_LINE_RE = _r(r"^\s*(?:\d+[.)]\s*|[-•*]\s*)?(?:long|short)\s+\$?[A-Za-z&0-9 .]{1,30}?\s*(?:\(|-|–|:|from|entry|@|at)\s*\$?\d")
STRONG_OPEN_RE = _r(r"\b(?:we are now (?:long|short)|now (?:long|short)|going (?:long|short)|"
                    r"entering (?:a |an )?(?:new )?(?:long|short)|opening (?:a |an )?(?:new )?(?:long|short)|"
                    r"initiating (?:a |an )?(?:new )?(?:long|short)|we are buying|we are shorting|"
                    rf"buying {ASSET_TOKEN}|shorting {ASSET_TOKEN}|new (?:long|short)(?: position)?\b)")
STOP_TRIGGERED_RE = _r(r"\b(?:stopped out|stop(?:[- ]loss)? (?:was|got|has been|is|were) (?:hit|triggered|taken out|run|elected)|"
                       r"hit our stop|our stop (?:was )?(?:hit|triggered)|got stopped|triggered our stop|stop triggered|"
                       r"stopped us out|took us out)\b")
RECAP_RE = _r(r"\b(?:yesterday|last (?:week|month|year|session|night)|earlier (?:today|this (?:week|month|year))|"
              r"this (?:week|month|year)|on (?:monday|tuesday|wednesday|thursday|friday)|"
              r"in (?:january|february|march|april|may|june|july|august|september|october|november|december)|"
              r"\d+ (?:days?|weeks?|months?) ago|recap|review|summary|times|average|so far|year[- ]to[- ]date|ytd|"
              r"track record|results?|performance)\b")
# A stop reported as triggered is a past exit too; it must never read as commentary.
_close_past_index = next(i for i, (_rx, cls, tense) in enumerate(VERBS)
                         if cls == MessageClass.CLOSE and tense == Tense.PAST)
VERBS.insert(_close_past_index, (STOP_TRIGGERED_RE, MessageClass.CLOSE, Tense.PAST))
SIDE_LONG_RE = _r(r"\b(?:long|buy|buying|bought)\b")
SIDE_SHORT_RE = _r(r"\b(?:short|shorting|shorted|covering|covered|cover)\b")

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z$\"'(])|\n+|;\s*")
CONJUNCTION = re.compile(r",?\s+and\s+|,\s+then\s+|,\s+")
PNL_RE = re.compile(r"(?<![\d.])([+-]?\d+(?:\.\d+)?)\s?%(?!\s+of\b)", re.IGNORECASE)
DATE_RE = _r(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s*(?:\d{4})?|"
             r"\b(?:in|since|of|by|during|late|early|mid|this|last) (?:19|20)\d{2}\b")
PRICE_RE = re.compile(r"(?<![\w.$])\$?\s?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?!\s*%)(?![\d,])")
STOP_WORD_RE = _r(r"\b(?:stop|stop[- ]loss|sl)\b")
TARGET_WORD_RE = _r(r"\b(?:target|take[- ]profit|tp|objective)\b")
TRIGGER_WORD_RE = _r(r"\b(?:below|above|under|over|breaks?|breaking|break of|through|closes?|cross(?:es|ing)?)\b")
AT_WORD_RE = _r(r"(?:\b(?:at|here at|near|around|from|entry(?: at| of)?|entered at|price of|to)\b|@|:)\s*\$?\s*$")
WINDOW = 28
LEVEL_LINE_RE = _r(r"^\s*(?:stop(?:[- ]loss)?|target|take[- ]profit|tp|sl)\b[^a-z0-9$]{0,6}\$?\s?\d")
_LEVEL_HOSTS = {MessageClass.OPEN, MessageClass.ADD, MessageClass.MODIFY, MessageClass.CONDITIONAL}


# ----------------------------------------------------------------------------- point-in-time context

@dataclass
class IdeaView:
    """The publisher's position as the interpreter understood it. Not the execution ledger."""
    idea_id: str
    source_id: str
    asset: str
    side: PositionSide
    opened_at: datetime
    closed_at: datetime | None = None
    stop_price: str | None = None
    target_price: str | None = None

    def is_open_as_of(self, moment: datetime) -> bool:
        return self.opened_at <= moment and (self.closed_at is None or self.closed_at > moment)


@dataclass
class PendingIntent:
    intent_id: str
    asset: str
    action: Action
    trigger: str | None
    created_at: datetime
    cancelled_at: datetime | None = None

    def is_pending_as_of(self, moment: datetime) -> bool:
        return self.created_at <= moment and (self.cancelled_at is None or self.cancelled_at > moment)


@dataclass
class PointInTimeContext:
    """Append-only view of the publisher's book. Every lookup is filtered by `as_of`, so a post is
    always interpreted against exactly the state that existed when it was published, whatever
    has been ingested since."""
    ideas: dict[str, IdeaView] = field(default_factory=dict)
    intents: dict[str, PendingIntent] = field(default_factory=dict)
    observed_posts: set[tuple[str, int]] = field(default_factory=set)   # eligible posts that advanced the book
    seen_posts: set[tuple[str, int]] = field(default_factory=set)       # every post shown to observe()

    def has_other_revision(self, source_id: str, revision: int) -> bool:
        return any(sid == source_id and rev != revision for sid, rev in self.seen_posts)

    def open_ideas(self, asset: str, as_of: datetime, side: PositionSide | None = None) -> list[IdeaView]:
        return sorted(
            (i for i in self.ideas.values()
             if i.asset == asset and i.is_open_as_of(as_of) and (side is None or i.side == side)),
            key=lambda i: (i.opened_at, i.idea_id),
        )

    def pending_intents(self, asset: str, as_of: datetime) -> list[PendingIntent]:
        return sorted((p for p in self.intents.values() if p.asset == asset and p.is_pending_as_of(as_of)),
                      key=lambda p: (p.created_at, p.intent_id))


# ----------------------------------------------------------------------------- interpreter

@dataclass
class _Clause:
    text: str
    sentence: int


class TklInterpreter:
    def __init__(self, context: PointInTimeContext | None = None, backend: StructuredBackend | None = None,
                 *, external_processing_permitted: bool = False):
        self.context = context or PointInTimeContext()
        self.backend = backend
        self.external_processing_permitted = external_processing_permitted

    # ------------------------------------------------------------------ public API

    def interpret(self, post: SourcePost) -> InterpretedPost:
        """Pure: reads the context as of `post.published_at`, never writes it."""
        if self.backend is not None:
            return self._interpret_with_backend(post)
        classifications = self._classify(post)
        actions = [self._to_trade_action(c) for c in classifications]
        return InterpretedPost(source_id=post.source_id, post_id=post.post_id, revision=post.revision,
                               published_at=post.published_at, parsed=ParsedPost(actions=actions),
                               classifications=classifications)

    def is_source_eligible(self, post: SourcePost) -> bool:
        """The canonical provenance rule, reused from the gateway: a post that `gate_source` would
        hold or block (edit, unverified author, repost, truncated text, timestamp faults) can neither
        advance the publisher-book context nor turn a past-tense exit into an order candidate."""
        is_edit = self.context.has_other_revision(post.source_id, post.revision)
        return gate_source(post.to_replay_source(), post.received_at, is_edit=is_edit) is None

    def observe(self, post: SourcePost, interpreted: InterpretedPost) -> None:
        """Advance the publisher-book view. Only source-eligible first revisions count; edits,
        reposts, truncated and unverified posts are interpreted but never change the context."""
        key = (post.source_id, post.revision)
        self.context.seen_posts.add(key)
        if key in self.context.observed_posts or not self.is_source_eligible(post):
            return
        self.context.observed_posts.add(key)
        when = post.published_at
        for c in interpreted.classifications:
            if not c.asset:
                continue
            is_open = c.message_class == MessageClass.OPEN and c.tense == Tense.PRESENT
            is_stated = c.message_class == MessageClass.RETROSPECTIVE and c.underlying == Action.OPEN
            if (is_open or is_stated) and c.side is not None:
                if is_stated and self.context.open_ideas(c.asset, when, c.side):
                    continue   # "we are long X from Y" restates a position already in view
                idea_id = f"{post.source_id}:{c.action_id}"
                self.context.ideas[idea_id] = IdeaView(
                    idea_id=idea_id, source_id=post.source_id, asset=c.asset, side=c.side, opened_at=when,
                    stop_price=_price(c, PriceRole.STOP), target_price=_price(c, PriceRole.TARGET))
            elif c.message_class in (MessageClass.CLOSE, MessageClass.RETROSPECTIVE, MessageClass.EXIT_ANNOUNCEMENT) \
                    and c.idea_reference:
                idea = self.context.ideas.get(c.idea_reference)
                if idea is not None and c.underlying == Action.CLOSE and idea.closed_at is None:
                    idea.closed_at = when
            elif c.message_class == MessageClass.MODIFY and c.idea_reference and c.tense == Tense.PRESENT:
                idea = self.context.ideas.get(c.idea_reference)
                if idea is not None:
                    idea.stop_price = _price(c, PriceRole.STOP) or idea.stop_price
                    idea.target_price = _price(c, PriceRole.TARGET) or idea.target_price
            elif c.message_class == MessageClass.CONDITIONAL:
                intent_id = f"{post.source_id}:{c.action_id}"
                self.context.intents[intent_id] = PendingIntent(
                    intent_id=intent_id, asset=c.asset, action=c.underlying,
                    trigger=_price(c, PriceRole.TRIGGER), created_at=when)
            elif c.message_class == MessageClass.CANCEL:
                for intent in self.context.pending_intents(c.asset, when):
                    intent.cancelled_at = when

    @property
    def observed_posts(self) -> set[tuple[str, int]]:
        return self.context.observed_posts

    def ingest(self, post: SourcePost) -> InterpretedPost:
        interpreted = self.interpret(post)
        self.observe(post, interpreted)
        return interpreted

    # ------------------------------------------------------------------ AI backend (gated)

    def _interpret_with_backend(self, post: SourcePost) -> InterpretedPost:
        assert self.backend is not None
        if post.is_paid_content and not self.external_processing_permitted:
            raise PermissionError(
                "External AI processing of paid feed content is disabled until usage permission is "
                "confirmed (publisher terms; spec decision D-07). Pass external_processing_permitted=True "
                "only once the product owner has recorded that permission."
            )
        parsed = self.backend.parse(post.text)
        classifications = [
            Classification(action_id=a.action_id, message_class=_class_from_action(a), tense=_tense_from_timing(a.timing),
                           underlying=a.action, asset=a.source_market_symbol, side=a.position_side,
                           close_fraction=a.close_fraction, idea_reference=a.idea_reference,
                           clause="[ai backend]", ambiguities=list(a.ambiguities))
            for a in parsed.actions
        ]
        return InterpretedPost(source_id=post.source_id, post_id=post.post_id, revision=post.revision,
                               published_at=post.published_at, parsed=parsed, classifications=classifications,
                               backend="ai")

    # ------------------------------------------------------------------ deterministic grammar

    def _classify(self, post: SourcePost) -> list[Classification]:
        text = _normalise(post.text)
        if not text.strip():
            return []
        eligible = self.is_source_eligible(post)
        summary_mode = self._is_summary(text)
        out: list[Classification] = []
        summary_parts: list[str] = []
        carried_asset: str | None = None
        for clause in self._clauses(text):
            if summary_mode and _is_summary_clause(clause.text):
                summary_parts.append(clause.text)
                continue
            if out and LEVEL_LINE_RE.match(clause.text) and out[-1].message_class in _LEVEL_HOSTS:
                # "Stop: $86. Target: $95." belong to the instruction before them, not to a new clause.
                roles = {PriceRole.STOP, PriceRole.TARGET}
                out[-1].prices.extend(m for m in _prices(clause.text, None, conditional=False) if m.role in roles)
                continue
            c = self._classify_clause(clause, post, len(out) + 1, carried_asset, eligible)
            if c is None:
                continue
            if summary_mode and c.message_class == MessageClass.COMMENTARY:
                summary_parts.append(clause.text)   # descriptive lines of a summary post
                continue
            if c.asset:
                carried_asset = c.asset
            out.append(c)
        if summary_parts:
            joined = " ".join(summary_parts)
            out.append(Classification(action_id=f"a{len(out) + 1}", message_class=MessageClass.POSITION_SUMMARY,
                                      tense=Tense.PRESENT, clause=joined[:200],
                                      prices=_prices(joined, None, conditional=False)))
        return out

    @staticmethod
    def _is_summary(text: str) -> bool:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if SUMMARY_RE.search(text):
            return True
        return sum(1 for ln in lines if SUMMARY_LINE_RE.search(ln)) >= 2

    @staticmethod
    def _clauses(text: str) -> list[_Clause]:
        clauses: list[_Clause] = []
        for s_index, sentence in enumerate(p for p in SENTENCE_SPLIT.split(text) if p and p.strip()):
            for part in _split_conjunctions(sentence.strip()):
                if part.strip():
                    clauses.append(_Clause(part.strip(), s_index))
        return clauses

    def _classify_clause(self, clause: _Clause, post: SourcePost, index: int,
                         carried: str | None, eligible: bool) -> Classification | None:
        text = clause.text
        action_id = f"a{index}"
        ambiguities: list[str] = []
        asset, _ticker, asset_ambiguity = _asset(text)
        if asset is None and carried is not None:
            asset = carried   # "Closing our S&P short and going long at 6,700": the second clause inherits
        if asset_ambiguity:
            ambiguities.append(asset_ambiguity)

        verb = next(((cls, tense, m) for rx, cls, tense in VERBS if (m := rx.search(text))), None)
        cancel = CANCEL_RE.search(text) is not None
        conditional = CONDITIONAL_RE.search(text) is not None
        infinitive = INFINITIVE_RE.search(text)
        commentary_words = COMMENTARY_RE.search(text) is not None

        if cancel:
            message_class, tense, underlying = MessageClass.CANCEL, Tense.PRESENT, Action.NO_TRADE
        elif conditional and (verb is not None or infinitive is not None):
            message_class, tense = MessageClass.CONDITIONAL, Tense.FUTURE
            if infinitive is not None:
                underlying = INFINITIVE_ACTION[infinitive.group("verb").lower()]
            else:
                underlying = _ACTION_FOR_CLASS.get(verb[0], Action.NO_TRADE)
        elif verb is None or (verb[0] == MessageClass.OPEN and commentary_words):
            if verb is None and infinitive is not None and not commentary_words:
                message_class, tense = MessageClass.CONDITIONAL, Tense.FUTURE
                underlying = INFINITIVE_ACTION[infinitive.group("verb").lower()]
                ambiguities.append("FUTURE_INTENT_WITHOUT_EXPLICIT_CONDITION")
            else:
                message_class, tense, underlying = MessageClass.COMMENTARY, Tense.PRESENT, Action.NO_TRADE
        else:
            base, tense, _ = verb
            underlying = _ACTION_FOR_CLASS[base]
            message_class = MessageClass.RETROSPECTIVE if tense == Tense.PAST else base

        fraction = _fraction(text)
        if underlying == Action.REDUCE and fraction is None and not PARTIAL_RE.search(text) \
                and message_class != MessageClass.CONDITIONAL:
            underlying = Action.CLOSE          # "took profits on X" with no partial marker reads as a full exit
            if message_class == MessageClass.REDUCE:
                message_class = MessageClass.CLOSE
        if underlying == Action.CLOSE and fraction is not None:
            underlying = Action.REDUCE
            if message_class == MessageClass.CLOSE:
                message_class = MessageClass.REDUCE

        side = _side(text, underlying)
        prices = _prices(text, underlying, conditional=(message_class == MessageClass.CONDITIONAL))
        actionable = message_class in (MessageClass.OPEN, MessageClass.ADD, MessageClass.REDUCE, MessageClass.CLOSE,
                                       MessageClass.MODIFY, MessageClass.CONDITIONAL, MessageClass.RETROSPECTIVE)
        if actionable and asset is None:
            ambiguities.append("ASSET_UNRESOLVED")

        idea_reference = None
        matches: list[IdeaView] = []
        if asset is not None and underlying in (Action.ADD, Action.REDUCE, Action.CLOSE, Action.MODIFY) \
                and message_class != MessageClass.CONDITIONAL:
            matches = self.context.open_ideas(asset, post.published_at, side)
            if len(matches) == 1:
                idea_reference = matches[0].idea_id
                side = side or matches[0].side
            elif not matches:
                if message_class != MessageClass.RETROSPECTIVE:
                    ambiguities.append("NO_OPEN_IDEA_IN_CONTEXT")
            else:
                ambiguities.append("MULTIPLE_OPEN_IDEAS_IN_CONTEXT")

        exit_kind = None
        if message_class == MessageClass.RETROSPECTIVE and underlying in (Action.CLOSE, Action.REDUCE):
            if STOP_TRIGGERED_RE.search(text):
                exit_kind = "stop_triggered"          # broker-side stop may already have fired: reconcile
            elif RECAP_RE.search(text):
                exit_kind = "recap"
            elif eligible and matches:
                # Fresh announcement of a completed publisher exit against a position the follower
                # still holds: a reduce-only candidate for the canonical gateway. Several candidate
                # positions keep the ambiguity, which the gateway turns into HOLD.
                message_class, exit_kind = MessageClass.EXIT_ANNOUNCEMENT, "announcement"
                ambiguities = [a for a in ambiguities if a != "NO_OPEN_IDEA_IN_CONTEXT"]
        if asset is not None and message_class == MessageClass.OPEN and side is not None \
                and self.context.open_ideas(asset, post.published_at, side):
            ambiguities.append("EXISTING_OPEN_IDEA_SAME_SIDE_IN_CONTEXT")
        if message_class == MessageClass.OPEN and side is None:
            ambiguities.append("SIDE_UNCLEAR")
        if message_class == MessageClass.OPEN and STRONG_OPEN_RE.search(text) is None:
            ambiguities.append("HEADLINE_POSITION_LINE_WITHOUT_ENTRY_VERB")   # "Long $TLT at 88" alone: entry or summary?
        if message_class == MessageClass.OPEN and verb is not None and re.search(r"\bwe are (?:long|short)\b", text, re.IGNORECASE) \
                and not re.search(r"\bnow\b", text, re.IGNORECASE):
            ambiguities.append("POSITION_STATEMENT_OR_ENTRY")

        return Classification(action_id=action_id, message_class=message_class, tense=tense, underlying=underlying,
                              asset=asset, side=side, prices=prices, close_fraction=fraction,
                              idea_reference=idea_reference, exit_kind=exit_kind, clause=text[:200],
                              sentence=clause.sentence, ambiguities=[a for a in dict.fromkeys(ambiguities) if a])

    def _to_trade_action(self, c: Classification) -> TradeAction:
        ticker = c.asset if c.asset and c.asset not in _SERIES_CODES else None
        instrument = None
        basis = PriceBasis.UNKNOWN
        if ticker:
            instrument = Instrument(asset_class=AssetClass.ETF if ticker in KNOWN_ETFS else AssetClass.EQUITY, symbol=ticker)
            basis = PriceBasis.TRADED_CONTRACT
        elif c.asset:
            basis = PriceBasis.SOURCE_SERIES
        timing = {
            MessageClass.CONDITIONAL: Timing.CONDITIONAL, MessageClass.RETROSPECTIVE: Timing.HISTORICAL,
            MessageClass.COMMENTARY: Timing.COMMENTARY, MessageClass.POSITION_SUMMARY: Timing.COMMENTARY,
        }.get(c.message_class, Timing.CURRENT)   # EXIT_ANNOUNCEMENT is CURRENT: the gateway decides
        action = c.underlying if c.message_class not in (MessageClass.COMMENTARY, MessageClass.POSITION_SUMMARY,
                                                         MessageClass.CANCEL) else Action.NO_TRADE
        explicit = c.message_class in (MessageClass.OPEN, MessageClass.ADD, MessageClass.REDUCE, MessageClass.CLOSE,
                                       MessageClass.MODIFY, MessageClass.CONDITIONAL, MessageClass.RETROSPECTIVE,
                                       MessageClass.EXIT_ANNOUNCEMENT) and c.asset is not None
        entry = _price(c, PriceRole.ENTRY) if action in (Action.OPEN, Action.ADD) else None
        reason = f"{c.message_class.value}/{c.tense.value}" + (f"/{c.exit_kind}" if c.exit_kind else "")
        return TradeAction(
            action_id=c.action_id, action=action, timing=timing, instrument=instrument, position_side=c.side,
            idea_reference=c.idea_reference if action != Action.OPEN else None,
            atomic_group=None, price_basis=basis, source_market_symbol=c.asset,
            entry_reference=entry, stop_price=_price(c, PriceRole.STOP) if action in (Action.OPEN, Action.MODIFY) else None,
            target_price=_price(c, PriceRole.TARGET) if action in (Action.OPEN, Action.MODIFY) else None,
            close_fraction=c.close_fraction if action == Action.REDUCE else None,
            explicit_instruction=explicit, evidence=[c.clause] if explicit else [],
            ambiguities=list(c.ambiguities), reason=reason,
        )


def _is_summary_clause(text: str) -> bool:
    """Header or listed-position line of a summary post. An explicit instruction ("We are now short
    $SPY at 685", "Closing our $TLT long") never matches, so it survives inside a summary post."""
    return SUMMARY_RE.search(text) is not None or SUMMARY_LINE_RE.match(text) is not None


# ----------------------------------------------------------------------------- replay runner

@dataclass
class ReplayStep:
    post: SourcePost
    interpreted: InterpretedPost
    result: ReplayResult


QuoteProvider = Callable[[SourcePost, datetime], dict[str, Quote]]


def replay_posts(posts: list[SourcePost], coordinator: PaperEventReplay, interpreter: TklInterpreter,
                 quotes_for: QuoteProvider, *, lag_seconds: float = 0.0) -> list[ReplayStep]:
    """Point-in-time replay in availability order (receipt for live posts, edit time for later
    revisions, publication otherwise): interpret, authorize and fill through the Sprint 1.1
    coordinator, then advance the context. `now` is the real receipt time for live posts and
    `available_at + lag_seconds` for archive or synthetic posts (archive receipt times are not
    latencies, see docs/research/G0_EVENT_SCHEMA.md)."""
    steps: list[ReplayStep] = []
    for post in sorted(posts, key=lambda p: (p.available_at, p.revision, p.post_id)):
        interpreted = interpreter.interpret(post)
        now = post.received_at if post.is_live else post.available_at + timedelta(seconds=lag_seconds)
        parsed = group_linked_actions(interpreted)
        result = coordinator.process(post.to_replay_source(), parsed, quotes_for(post, now), now=now)
        interpreter.observe(post, interpreted)
        steps.append(ReplayStep(post=post, interpreted=interpreted, result=result))
    return steps


def group_linked_actions(interpreted: InterpretedPost) -> ParsedPost:
    """Fill-capable actions (CURRENT timing) from the same sentence on the same asset form one
    atomic group (ADD + MODIFY, CLOSE + OPEN reversal). Different assets stay independent, and
    historical, conditional or commentary clauses never join a group: they cannot fill, so
    grouping them would only turn their RECONCILE / HOLD into an atomic rejection of the rest."""
    by_key: dict[tuple[int, str], list[str]] = {}
    actions = []
    for a in interpreted.parsed.actions:
        c = next(x for x in interpreted.classifications if x.action_id == a.action_id)
        if a.action != Action.NO_TRADE and a.timing == Timing.CURRENT and c.asset:
            by_key.setdefault((c.sentence, c.asset), []).append(a.action_id)
    groups = {aid: f"s{k[0]}-{k[1]}" for k, ids in by_key.items() if len(ids) > 1 for aid in ids}
    for a in interpreted.parsed.actions:
        actions.append(a.model_copy(update={"atomic_group": groups.get(a.action_id)}) if a.action_id in groups else a)
    return ParsedPost(actions=actions, summary=interpreted.parsed.summary)


# ----------------------------------------------------------------------------- helpers

_ACTION_FOR_CLASS = {
    MessageClass.OPEN: Action.OPEN, MessageClass.ADD: Action.ADD, MessageClass.REDUCE: Action.REDUCE,
    MessageClass.CLOSE: Action.CLOSE, MessageClass.MODIFY: Action.MODIFY,
}
_SERIES_CODES = frozenset(code for _, code in SERIES)


def _class_from_action(a: TradeAction) -> MessageClass:
    if a.timing == Timing.HISTORICAL:
        return MessageClass.RETROSPECTIVE
    if a.timing == Timing.CONDITIONAL:
        return MessageClass.CONDITIONAL
    if a.timing in (Timing.COMMENTARY, Timing.UNCERTAIN) or a.action == Action.NO_TRADE:
        return MessageClass.COMMENTARY
    return MessageClass(a.action.value)


def _tense_from_timing(timing: Timing) -> Tense:
    return {Timing.HISTORICAL: Tense.PAST, Timing.CONDITIONAL: Tense.FUTURE}.get(timing, Tense.PRESENT)


def _normalise(text: str) -> str:
    return (text.replace("’", "'").replace("“", '"').replace("”", '"')
            .replace("—", " - ").replace("–", "-").replace(" ", " "))


def _split_conjunctions(sentence: str) -> list[str]:
    """Cut a sentence before a second action verb so each clause carries one instruction."""
    positions = sorted({m.start() for rx, _, _ in VERBS for m in rx.finditer(sentence)}
                       | {m.start() for m in INFINITIVE_RE.finditer(sentence)}
                       | {m.start() for m in CANCEL_RE.finditer(sentence)})
    if len(positions) < 2:
        return [sentence]
    first, second = positions[0], positions[1]
    cut = None
    for m in CONJUNCTION.finditer(sentence, first + 1, second + 1):
        cut = m
    if cut is None or cut.start() <= first:
        return [sentence]
    head, tail = sentence[:cut.start()].strip(), sentence[cut.end():].strip()
    return [head, *_split_conjunctions(tail)] if head and tail else [sentence]


def _asset(text: str) -> tuple[str | None, str | None, str | None]:
    """(asset code, ticker or None, ambiguity or None)."""
    tickers = list(dict.fromkeys(TICKER_RE.findall(text)))
    bare = [t for t in dict.fromkeys(BARE_ETF_RE.findall(text)) if t not in tickers]
    tickers += bare
    series: list[str] = []
    for rx, code in SERIES_RES:
        if rx.search(text) and code not in series:
            series.append(code)
    if len(tickers) > 1:
        return tickers[0], tickers[0], "MULTIPLE_TICKERS_IN_CLAUSE"
    if tickers and series:
        return tickers[0], tickers[0], "MIXED_SERIES_AND_TICKER"
    if tickers:
        return tickers[0], tickers[0], None
    if len(series) > 1:
        return series[0], None, "MULTIPLE_SERIES_IN_CLAUSE"
    if series:
        return series[0], None, None
    return None, None, None


def _side(text: str, underlying: Action) -> PositionSide | None:
    long_ = SIDE_LONG_RE.search(text) is not None
    short = SIDE_SHORT_RE.search(text) is not None
    if underlying in (Action.CLOSE, Action.REDUCE) and re.search(r"\bcover(?:ing|ed)?\b", text, re.IGNORECASE):
        return PositionSide.SHORT
    if long_ and not short:
        return PositionSide.LONG
    if short and not long_:
        return PositionSide.SHORT
    return None


def _fraction(text: str) -> str | None:
    m = PCT_OF_RE.search(text)
    if m:
        pct = int(m.group(1))
        return str(pct / 100) if 0 < pct < 100 else None
    for rx, value in FRACTIONS:
        if rx.search(text):
            return value
    return None


def _prices(text: str, underlying: Action | None, *, conditional: bool) -> list[PriceMention]:
    mentions: list[PriceMention] = []
    work = text
    for m in PNL_RE.finditer(work):
        mentions.append(PriceMention(role=PriceRole.PNL_PERCENT, value=m.group(1)))
    work = PNL_RE.sub(lambda m: " " * len(m.group(0)), work)
    work = PCT_OF_RE.sub(lambda m: " " * len(m.group(0)), work)
    work = DATE_RE.sub(lambda m: " " * len(m.group(0)), work)
    for rx, _ in SERIES_RES:   # "S&P 500" is a name, not a level
        work = rx.sub(lambda m: " " * len(m.group(0)), work)
    for m in PRICE_RE.finditer(work):
        value = m.group(1).replace(",", "")
        window = work[max(0, m.start() - WINDOW):m.start()].lower()
        if STOP_WORD_RE.search(window):
            role = PriceRole.STOP
        elif TARGET_WORD_RE.search(window):
            role = PriceRole.TARGET
        elif conditional and TRIGGER_WORD_RE.search(window):
            role = PriceRole.TRIGGER
        elif AT_WORD_RE.search(window):
            if conditional:
                role = PriceRole.TRIGGER
            elif underlying in (Action.OPEN, Action.ADD):
                role = PriceRole.ENTRY
            elif underlying in (Action.CLOSE, Action.REDUCE):
                role = PriceRole.EXIT
            else:
                role = PriceRole.REFERENCE
        else:
            role = PriceRole.REFERENCE
        mentions.append(PriceMention(role=role, value=value))
    return mentions


def _price(c: Classification, role: PriceRole) -> str | None:
    return next((p.value for p in c.prices if p.role == role), None)
