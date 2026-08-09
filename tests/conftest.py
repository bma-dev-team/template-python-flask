"""Test-session hooks shared by every build scaffolded from this template.

Two things: say out loud when an entire parametrised leg did not run, and put
back the per-test timeout that reporting a failure takes away.

**Why this exists.** Twice on the deposition build a whole verification leg was
silently absent from every number anyone quoted, and both times the number
looked healthy.

- CI pinned Python 3.11 while the local venv was 3.12, so fourteen review rounds
  on a migration guard had never run on the interpreter CI uses. Two guards were
  *erroring* rather than asserting.
- `TEST_DATABASE_URL` was unset locally, so 567 Postgres tests skipped and the
  session quoted "5 failed" all day. The real answer under CI's configuration
  was 7 failed, including a Postgres-only failure that would have reddened CI.

Same shape both times: the local default is a configuration CI does not use, the
gap is invisible because skips are quiet, and the total reads fine. **"567
skipped" is data; "the postgres leg did not run" is a finding.** A skip count at
the end of a run does not read as "you did not run half the matrix" -- it reads
as a footnote, and it was read as a footnote.

This makes the second sentence appear. It is deliberately generic: it knows
nothing about databases or interpreters, only that a parameter value appeared in
collection and every test carrying it was skipped.
"""
from __future__ import annotations

import re

import pytest

_PARAM_RE = re.compile(r"\[(.+)\]$")



@pytest.hookimpl(trylast=True)
def pytest_exception_interact(node, call, report):  # noqa: ARG001
    """Put back the per-test timeout that reporting a failure just took away.

    **The defect this closes, measured rather than reasoned.** `pytest-timeout`
    cancels the item's timer from this same hook -- it does so for the `--pdb`
    case, so a debugger session is not killed mid-inspection -- and never re-arms
    it. So the *first* failure in a test disarms `pyproject.toml`'s `timeout` for
    everything that comes after it, and what comes after it is **fixture
    teardown**.

    That is the wrong half to leave unbounded. Teardown is where a suite does its
    blocking work: closing sockets, joining threads, dropping schemas. On the
    build this came from, a socket test that failed for a stated reason then hung
    its run forever -- **one byte of output (`F`), no summary, no stack dump**,
    killed only by an outer wall clock. Before and after, same test, same box:

    ==================  ==============================================
    without this hook   `exit 124` (an outer timeout), 403 bytes of output
    with it             `exit 1` (a real failure), 8981 bytes with the report
    ==================  ==============================================

    **Any pytest suite with blocking fixture teardowns has this**, which is why it
    is in the template rather than in one build. It is not specific to sockets or
    to databases; it needs only a teardown that can block and a test that can
    fail, and the second is not optional.

    Re-armed at the full timeout rather than at whatever was left of it. The point
    is a bound, not an accounting: a test that has already failed deserves the
    same budget for its teardown as a passing one.

    **`--pdb` keeps its exemption**, which is the reason the plugin cancels here
    in the first place. This is `trylast`, so it does not run until pytest's own
    post-mortem hookimpl has returned, and it declines outright when a debugger is
    what the run is entering.

    **Deliberately narrow**: only a real test item, and only when the timer is the
    one installed around the whole runtest protocol, because that is the only case
    with a cancel on the other side of it. Anything else would leave a live timer
    that nothing turns off.

    One private API, `_get_item_settings`, is read here; the re-arm itself goes
    through the plugin's public `pytest_timeout_set_timer` hook. If a future
    plugin version moves that machinery, the `except` below means the cost is
    **the bound, not the failure report** -- the failure being reported right now
    matters more than the ceiling on what follows it.

    Proven by `tests/test_timeout_rearm.py`, which runs a failing test with a
    blocking teardown in a subprocess, with this hook and without it.
    """
    if node.config.getoption("usepdb", False):
        return
    if not isinstance(node, pytest.Item):
        return
    try:
        import pytest_timeout

        settings = pytest_timeout._get_item_settings(node)
        if not settings.timeout or settings.timeout <= 0 or settings.func_only is not False:
            return
        node.config.pluginmanager.hook.pytest_timeout_set_timer(item=node, settings=settings)
    except Exception:  # pragma: no cover - never break failure reporting
        pass


def _param_values(nodeid: str) -> list[str]:
    """Return the individual parameter values in a test id.

    ``tests/test_x.py::test_y[postgres]`` gives ``["postgres"]``.
    ``...[sqlite-3.11]`` gives ``["sqlite", "3.11"]``, because a leg is often
    one component of a multi-parameter id and skipping it is the same finding.
    """
    match = _PARAM_RE.search(nodeid)
    if not match:
        return []
    return [part for part in match.group(1).split("-") if part]


def pytest_terminal_summary(terminalreporter, exitstatus, config):  # noqa: ARG001
    """Name any parametrised leg that was collected but entirely skipped."""
    seen: dict[str, int] = {}
    skipped: dict[str, int] = {}
    for outcome in ("passed", "failed", "skipped", "error"):
        for report in terminalreporter.stats.get(outcome, []):
            nodeid = getattr(report, "nodeid", "")
            if not nodeid or getattr(report, "when", "call") != "call":
                # Setup-phase skips carry when="setup"; count them, since a leg
                # skipped by a fixture never reaches the call phase at all.
                if not (outcome == "skipped" and getattr(report, "when", "") == "setup"):
                    continue
            for value in _param_values(nodeid):
                seen[value] = seen.get(value, 0) + 1
                if outcome == "skipped":
                    skipped[value] = skipped.get(value, 0) + 1

    absent = sorted(v for v, total in seen.items() if skipped.get(v, 0) == total)
    if not absent:
        return

    terminalreporter.write_sep("=", "ENTIRE PARAMETRISED LEG DID NOT RUN", red=True)
    for value in absent:
        terminalreporter.write_line(
            f"  every test parametrised [{value}] was skipped ({seen[value]} tests). "
            f"This run did not verify that configuration."
        )
    terminalreporter.write_line(
        "  A green run here is not evidence about the leg above. If CI runs it, "
        "CI is testing something you did not."
    )
