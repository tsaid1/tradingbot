"""The hosted reviewer site imports the adjudication core and nothing of the trading system."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

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


def test_the_platform_is_told_the_framework_and_the_entry_it_names_exists():
    """Without ``framework`` the platform builds no function for a project that was not created
    from this folder: it serves the static files and answers 404 for every page of the app (seen
    on the first previews). ``maxDuration`` bounds a request that waits on the mail server."""
    hosted = Path(__file__).resolve().parents[1] / "hosted"
    config = json.loads((hosted / "vercel.json").read_text())
    assert config["framework"] == "flask"
    assert set(config["functions"]) == {"wsgi.py"} and (hosted / "wsgi.py").is_file()
    assert 15 < config["functions"]["wsgi.py"]["maxDuration"] <= 60
    assert (hosted / "public" / "static").is_dir()  # served by the platform, not by Flask
