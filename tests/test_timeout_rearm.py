"""The failing half of the timeout ceiling, proved rather than asserted.

`tests/conftest.py` re-arms the per-test timeout that `pytest-timeout` cancels
when it reports a failure. **This file exists because a guard's firing path is
the one its author never runs** -- building it means arranging the failure the
guard exists to catch, which is the expensive case, so guards ship tested on the
happy path and spend their whole value on the other one.

The hook came from a build where exactly that happened: a harness promising that
a stuck teardown would be "reported here, by name, instead of arriving as a
timeout with no summary" produced **one byte of output and then hung**. So this
test does not check that the hook is installed, or that it is shaped correctly.
It runs a failing test with a blocking teardown, in a subprocess, and looks at
what comes out.

**Both directions, because one is not evidence.** The probe shows the teardown is
bounded with the hook; the control shows it is *not* bounded without it. Without
the control this file would pass just as happily against a timeout that was never
disarmed in the first place, and would be reporting the plugin's behaviour rather
than ours.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The teardown blocks for this long; the ceiling is well under it. The gap is what
# separates "bounded" from "ran to completion", and both numbers are small so the
# whole file costs a few seconds.
TEARDOWN_BLOCKS_FOR = 6
CEILING = 2

# `signal` rather than the default: it raises inside the test process, so the
# subprocess still prints a summary we can read. The property under test -- that
# the timer was re-armed at all -- is the same either way, and the thread method
# would `os._exit` and leave nothing to assert against.
PYTEST_ARGS = [
    "-p", "no:cacheprovider",
    f"--timeout={CEILING}",
    "--timeout-method=signal",
    "-q",
]

MINI_SUITE = f"""
import time
import pytest


@pytest.fixture
def a_teardown_that_blocks():
    yield
    time.sleep({TEARDOWN_BLOCKS_FOR})


def test_that_fails_before_its_teardown_blocks(a_teardown_that_blocks):
    assert False, "the stated failure this test exists to report"
"""

# Imports the SHIPPED hook rather than a copy of it. A copy would drift from the
# thing that actually runs, and this file would keep passing while the real one
# rotted -- which is the same defect class the hook itself addresses.
CONFTEST_WITH_HOOK = f"""
import sys
sys.path.insert(0, {str(REPO_ROOT)!r})
from tests.conftest import pytest_exception_interact  # noqa: F401
"""

CONFTEST_WITHOUT_HOOK = "# deliberately empty: this is the control\n"


def _run_mini_suite(tmp_path: Path, conftest: str) -> tuple[str, float]:
    """Run the mini suite in its own directory and return (output, seconds)."""
    (tmp_path / "conftest.py").write_text(conftest)
    (tmp_path / "test_mini.py").write_text(MINI_SUITE)

    started = time.monotonic()
    finished = subprocess.run(
        [sys.executable, "-m", "pytest", *PYTEST_ARGS, "test_mini.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        # Generous: the control is *expected* to run long, and a hang here should
        # fail this test rather than hang the suite that is checking for hangs.
        timeout=TEARDOWN_BLOCKS_FOR * 5,
    )
    return finished.stdout + finished.stderr, time.monotonic() - started


def test_a_failing_test_does_not_disarm_the_ceiling_for_its_own_teardown(tmp_path):
    """The probe: with the hook, a blocking teardown after a failure is cut short."""
    output, seconds = _run_mini_suite(tmp_path, CONFTEST_WITH_HOOK)

    assert "Timeout" in output, (
        "the blocking teardown was never timed out, so the ceiling was still "
        f"disarmed when the failure was reported. Output was:\n{output}"
    )
    assert seconds < TEARDOWN_BLOCKS_FOR, (
        f"the run took {seconds:.1f}s against a {TEARDOWN_BLOCKS_FOR}s teardown "
        f"and a {CEILING}s ceiling, so the teardown ran to completion untimed"
    )


def test_without_the_hook_the_same_teardown_runs_untimed(tmp_path):
    """The control, and the reason the probe above means anything.

    If this ever starts failing, the plugin has changed its behaviour and the
    hook may have become unnecessary -- read it before deleting it, but do read
    it. A guard whose defect has been fixed upstream is dead weight that every
    build still pays to carry.
    """
    output, seconds = _run_mini_suite(tmp_path, CONFTEST_WITHOUT_HOOK)

    assert "Timeout" not in output, (
        "the teardown was bounded WITHOUT our hook, so the probe above is not "
        f"evidence about anything we ship. Output was:\n{output}"
    )
    assert seconds >= TEARDOWN_BLOCKS_FOR, (
        f"the run took only {seconds:.1f}s, so the teardown did not block as "
        "this control assumes and neither test here is measuring what it claims"
    )
