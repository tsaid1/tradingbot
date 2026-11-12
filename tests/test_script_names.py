"""Scripts are run as ``python scripts/<name>.py``, which puts ``scripts/`` first on ``sys.path``.

A script named like a standard-library module (for example ``operator.py``) then shadows that module
for every script in the folder: ``enum`` imports ``operator`` and the interpreter fails before any
project code runs. Whether that bites depends on what the interpreter imported at start-up, so it can
pass on one machine and fail on another. Keep script names collision-free.
"""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
# Third-party packages this project or its dashboard can import.
OTHER_PACKAGES = {"watchdog", "streamlit", "pydantic", "openai", "dotenv", "ibapi", "pytest"}


def test_no_script_shadows_a_standard_library_or_dependency_module():
    names = {p.stem for p in SCRIPTS.glob("*.py")}
    clashes = sorted(names & (set(sys.stdlib_module_names) | OTHER_PACKAGES))
    assert clashes == [], f"rename these scripts: {clashes}"
