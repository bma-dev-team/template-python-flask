"""Test-session hooks shared by every build scaffolded from this template.

Currently one thing: say out loud when an entire parametrised leg did not run.

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

_PARAM_RE = re.compile(r"\[(.+)\]$")


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
