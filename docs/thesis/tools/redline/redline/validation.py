"""Run the docx skill's validate.py on a revised document and capture its verdict.

The script path defaults to the synced docx skill and can be overridden with the
REDLINE_VALIDATE_SCRIPT environment variable. validate.py imports defusedxml; when the
interpreter running this tool lacks it, REDLINE_VALIDATE_PYTHONPATH names a directory that holds
it and is prepended to PYTHONPATH for the validator only.
"""

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_VALIDATE_SCRIPT = Path(
    "/Users/heed/.claude-heed/skills/synced/bb1ffb1c-c0ed-41fc-82cf-12382800d193_"
    "a1c0a5c7-2101-4fec-b27c-c9163ee7b4ec/docx/scripts/office/validate.py"
)
TIMEOUT_SECONDS = 600


@dataclass(frozen=True)
class ValidationResult:
    """Whether validate.py ran, whether it passed, and what it printed."""

    ran: bool
    passed: bool
    exit_code: int | None
    command: str
    output: str


def validate_script_path() -> Path:
    """Return the validate.py to use: REDLINE_VALIDATE_SCRIPT or the skill default."""
    return Path(os.environ.get("REDLINE_VALIDATE_SCRIPT", DEFAULT_VALIDATE_SCRIPT))


def run_validate(output_docx: Path, original_docx: Path, author: str, script: Path | None = None) -> ValidationResult:
    """Run validate.py OUT --original IN --author NAME and report the outcome."""
    script = Path(script) if script is not None else validate_script_path()
    arguments = [sys.executable, str(script), str(output_docx), "--original", str(original_docx), "--author", author]
    command = shlex.join(arguments)
    if not script.is_file():
        return ValidationResult(False, False, None, command, f"validate.py not found at {script}")
    completed = subprocess.run(
        arguments, capture_output=True, text=True, env=_validator_environment(), timeout=TIMEOUT_SECONDS
    )
    output = (completed.stdout + completed.stderr).strip()
    return ValidationResult(True, completed.returncode == 0, completed.returncode, command, output)


def _validator_environment() -> dict[str, str]:
    """Copy the environment, prepending REDLINE_VALIDATE_PYTHONPATH to PYTHONPATH when set."""
    environment = dict(os.environ)
    extra = environment.get("REDLINE_VALIDATE_PYTHONPATH")
    if extra:
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = extra if not existing else f"{extra}{os.pathsep}{existing}"
    return environment
