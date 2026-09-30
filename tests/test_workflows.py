"""The workflows' install steps, and the retry script they go through.

An install that fails on a PyPI blip fails the whole job — for trade-bot, a run
that places nothing. Every `pip install` goes through scripts/retry.sh, and the
script is run here for real rather than read.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
RETRY = (ROOT / "scripts" / "retry.sh").as_posix()


def test_every_pip_install_in_every_workflow_is_retried():
    installs = []
    for path in WORKFLOWS:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "pip install" in line:
                installs.append((path.name, line.strip()))

    assert installs, "no install steps found — has the layout changed?"
    bare = [(name, line) for name, line in installs
            if not re.match(r"- run: bash scripts/retry\.sh pip install ", line)]
    assert not bare, f"install steps without the retry: {bare}"


# --------------------------------------------------------------------------
# The script itself. Each attempt appends a line to a file, so the test counts
# the attempts that actually ran; `fail_first` is how many of them fail.
# --------------------------------------------------------------------------

BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not installed")


def _retry(tmp_path, *, fail_first, exit_code=1):
    cmd = (f'echo x >> attempts; '
           f'[ "$(wc -l < attempts)" -gt {fail_first} ] || exit {exit_code}')
    result = subprocess.run(
        [BASH, RETRY, BASH, "-c", cmd],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
        env={**os.environ, "RETRY_DELAY": "0"},
    )
    attempts = (tmp_path / "attempts").read_text().count("x")
    return result, attempts


@needs_bash
def test_a_command_that_works_runs_once(tmp_path):
    result, attempts = _retry(tmp_path, fail_first=0)
    assert result.returncode == 0
    assert attempts == 1
    assert result.stderr == ""


@needs_bash
def test_a_blip_is_retried_until_it_works(tmp_path):
    result, attempts = _retry(tmp_path, fail_first=2)
    assert result.returncode == 0
    assert attempts == 3
    assert result.stderr.count("::warning::") == 2


@needs_bash
def test_a_real_failure_stops_after_three_and_keeps_its_exit_code(tmp_path):
    result, attempts = _retry(tmp_path, fail_first=99, exit_code=7)
    assert result.returncode == 7
    assert attempts == 3
    assert "::error::" in result.stderr
