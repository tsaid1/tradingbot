"""Single entry point; duplicate rejection precedes costly model inference."""
from __future__ import annotations

from tradingbot.interpreters import Interpreter
from tradingbot.models import Decision, MockPolicy, ProcessResult, Quote, SourceEvent
from tradingbot.storage import Journal


class SignalPipeline:
    def __init__(self, journal: Journal, interpreter: Interpreter, policy: MockPolicy):
        self.journal = journal
        self.interpreter = interpreter
        self.policy = policy

    def process(
        self, source: SourceEvent, quote: Quote | None, image: bytes | None = None
    ) -> ProcessResult:
        if not self.journal.reserve(source):
            return ProcessResult(
                source_id=source.source_id,
                decision=Decision(status="DUPLICATE", reasons=["SOURCE_ID_ALREADY_PROCESSED_OR_RESERVED"]),
            )
        try:
            candidate = self.interpreter.parse(source.text, image=image)
            # The journal obtains a writer lock and performs preflight again immediately
            # before committing any *fictional* order, preventing stale state assumptions.
            return self.journal.commit(source, candidate, quote, self.policy)
        except Exception as exc:
            self.journal.mark_error(source.source_id, type(exc).__name__ + ": " + str(exc))
            raise
