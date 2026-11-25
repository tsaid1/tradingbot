"""The hosted reviewer site imports the adjudication core and nothing of the trading system."""
from __future__ import annotations

import subprocess
import sys

ALLOWED = {"tradingbot", "tradingbot.runtime", "tradingbot.runtime.adjudication",
           "tradingbot.runtime.adjudication_rubric", "tradingbot.runtime.vocab_recommend",
           "tradingbot.runtime.vocab_review", "tradingbot.runtime.errors"}


def _loaded(statement: str) -> set[str]:
    code = (f"import sys\n{statement}\n"
            "print('\\n'.join(sorted(m for m in sys.modules if m.split('.')[0] == 'tradingbot')))")
    out = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)
    return set(out.stdout.split())


def test_the_adjudication_core_loads_no_model_risk_config_or_service_module():
    assert _loaded("import tradingbot.runtime.adjudication") <= ALLOWED


def test_config_error_is_one_class_wherever_it_is_imported_from():
    from tradingbot.runtime import config, errors, vocab_review

    assert config.ConfigError is errors.ConfigError
    assert issubclass(vocab_review.CandidateLexiconRefused, config.ConfigError)
