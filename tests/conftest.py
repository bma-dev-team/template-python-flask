"""Test-session hooks shared by every build scaffolded from this template.

Three things: say out loud when an entire parametrised leg did not run, put
back the per-test timeout that reporting a failure takes away, and give every
test run a Postgres database of its own (`test_database_url`, at the bottom).

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

import os
import re
import secrets

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


# --- a Postgres database per test run (learning L86) -----------------------
#
# **Why this exists.** On the deposition build two sessions on one machine
# shared one `TEST_DATABASE_URL`. One ran the full suite, whose fixtures
# `drop_all()` / `create_all()` every few seconds; the other ran `flask
# bootstrap` against the same database. For 45 minutes each session's failures
# (missing relations, a deadlock in `drop_all`, a vanished `session` table)
# looked like defects in its own code. A rule that says "use your own database"
# depends on every build remembering it; this makes it the default.
#
# **How a build uses it:** take the database from the `test_database_url`
# fixture, never from `os.environ`. `TEST_DATABASE_URL` then names a server and
# a base database to connect through, not the database the tests write to.
#
# Reviewed on bma-dev-team/template-python-flask PR #1 (ERW, tier M, Level 2).

TEST_DATABASE_URL_ENV = "TEST_DATABASE_URL"

# The drivers whose connection arguments were checked (see _DESTINATION_KEYS).
_POSTGRES_SCHEMES = {"postgresql", "postgres", "postgresql+psycopg", "postgresql+psycopg2"}

# Query keys that choose the destination by a route other than the URL path.
# SQLAlchemy's psycopg dialects build the connection arguments from the path and
# then apply `opts.update(url.query)` (`create_connect_args`), so a retained
# `?dbname=base` silently overrides the per-run database and the run is back on
# the shared one. `service` selects through pg_service.conf; `host`, `hostaddr`
# and `port` in the query are libpq multi-host selectors. The identity check in
# `create_run_database` is the backstop for anything this list misses.
_DESTINATION_KEYS = ("dbname", "database", "service", "host", "hostaddr", "port")

# Postgres truncates identifiers longer than this silently (NAMEDATALEN - 1).
_IDENTIFIER_MAX_BYTES = 63

# Names this process created. The drop refuses anything else, including a name
# that merely looks like a run database: it never matches by pattern.
_created_databases: set[str] = set()


class TestDatabaseRefused(ValueError):
    """`TEST_DATABASE_URL` has a shape this fixture will not create a database for."""

    __test__ = False  # not a test class, despite the name


def _scheme(raw: str) -> str:
    return raw.split("://", 1)[0].lower() if "://" in raw else ""


def is_postgres_url(raw: str) -> bool:
    """Whether a URL is Postgres at all, supported driver or not. Stdlib only."""
    return _scheme(raw).split("+", 1)[0] in ("postgresql", "postgres")


def checked_postgres_url(raw: str):
    """Parse a Postgres `TEST_DATABASE_URL`, refusing any shape that could select
    its database by a route other than the path. Nothing has been created yet."""
    from sqlalchemy.engine import make_url

    scheme = _scheme(raw)
    if scheme not in _POSTGRES_SCHEMES:
        raise TestDatabaseRefused(
            f"{TEST_DATABASE_URL_ENV} uses {scheme!r}; supported: "
            f"{', '.join(sorted(_POSTGRES_SCHEMES))}"
        )
    url = make_url(raw)
    if url.drivername == "postgres":
        # SQLAlchemy 2 has no dialect called `postgres`; it is the same database.
        url = url.set(drivername="postgresql")
    if not url.database:
        raise TestDatabaseRefused(
            f"{TEST_DATABASE_URL_ENV} names no database in its path; "
            "it must name the base database to connect through"
        )
    selectors = sorted(k for k in url.query if k.lower() in _DESTINATION_KEYS)
    if selectors:
        raise TestDatabaseRefused(
            f"{TEST_DATABASE_URL_ENV} selects its destination through the query "
            f"key(s) {selectors}, which would override the per-run database; "
            "name the database in the URL path and remove them (for a Unix "
            "socket, leave the host empty, as in postgresql://user@/dbname, and "
            "set PGHOST if the socket directory is not the default)"
        )
    return url


def new_run_token() -> str:
    """Different for every run, including two runs on one branch at once."""
    return f"{os.getpid()}_{secrets.token_hex(3)}"


def run_database_name(base: str, token: str) -> str:
    """`<base>_run_<token>`, cut to 63 bytes of UTF-8 with the token kept whole."""
    suffix = f"_run_{token}"
    room = _IDENTIFIER_MAX_BYTES - len(suffix.encode("utf-8"))
    head = base.encode("utf-8")[:room].decode("utf-8", "ignore")
    return head + suffix


def run_database_url(url, name: str):
    """The same connection with only the path database replaced."""
    return url.set(database=name)


def create_run_database(url):
    """Create this run's database and return `(run_url, name)`.

    The destination is OBSERVED before anything is handed out: a connection
    through the run URL must report the created database as current. On any
    failure after CREATE, the database this call made is dropped again.
    """
    from sqlalchemy import create_engine, text

    name = run_database_name(url.database, new_run_token())
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            quoted = admin.dialect.identifier_preparer.quote_identifier(name)
            conn.execute(text(f"CREATE DATABASE {quoted}"))
        _created_databases.add(name)  # only after CREATE succeeded
    finally:
        admin.dispose()

    try:
        run_url = run_database_url(url, name)
        probe = create_engine(run_url)
        try:
            with probe.connect() as conn:
                current = conn.execute(text("SELECT current_database()")).scalar()
        finally:
            probe.dispose()
        if current != name:
            raise RuntimeError(
                f"the per-run URL connected to {current!r}, not the database "
                f"created for this run ({name!r}); refusing to hand it out"
            )
    except BaseException:
        drop_run_database(url, name)
        raise
    return run_url, name


def drop_run_database(url, name: str) -> None:
    """Drop a database this process created, and nothing else."""
    if name not in _created_databases:
        raise ValueError(f"refusing to drop {name!r}: this process did not create it")
    from sqlalchemy import create_engine, text

    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            # DROP DATABASE fails while anyone is connected; a test that left a
            # connection open would otherwise leave the database behind.
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
            quoted = admin.dialect.identifier_preparer.quote_identifier(name)
            conn.execute(text(f"DROP DATABASE IF EXISTS {quoted}"))
        _created_databases.discard(name)
    finally:
        admin.dispose()


@pytest.fixture(scope="session")
def test_database_url():
    """This run's own database URL, or None when `TEST_DATABASE_URL` is unset.

    Loads `.env` before reading the variable: reading `os.environ` first is how
    the deposition build twice skipped its whole Postgres leg on a machine where
    it was configured. A non-Postgres URL is returned unchanged. Under
    pytest-xdist every worker is its own session and gets its own database.
    A run killed outright (SIGKILL, a runner timeout) leaves its
    `<base>_run_*` database behind; it is never reused, because every run
    derives a new name.
    """
    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv(usecwd=True))
    raw = os.environ.get(TEST_DATABASE_URL_ENV)
    if not raw:
        yield None
        return
    if not is_postgres_url(raw):
        yield raw
        return
    url = checked_postgres_url(raw)
    run_url, name = create_run_database(url)
    try:
        yield run_url.render_as_string(hide_password=False)
    finally:
        try:
            drop_run_database(url, name)
        except Exception as exc:  # the results stand; say what was left
            import warnings

            warnings.warn(f"per-run test database {name!r} was left behind: {exc}")
