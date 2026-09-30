"""`test_database_url` must isolate a run, and must never touch what it did not make.

The Postgres half runs real inner pytest sessions in subprocesses against a real
server, because the property is about separate processes sharing a server: an
in-process test would prove the helpers were written, not that two runs are
isolated. It needs `TEST_DATABASE_URL`, SQLAlchemy and a driver. It skips
without them locally, and FAILS without them in CI, because a skipped leg there
would ship the fixture unproven.

Learning L86; reviewed on bma-dev-team/template-python-flask PR #1.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import uuid

import pytest

import conftest as fixture_module
from conftest import (
    TestDatabaseRefused,
    checked_postgres_url,
    is_postgres_url,
    run_database_name,
    run_database_url,
)

REAL_CONFTEST = pathlib.Path(__file__).resolve().parent / "conftest.py"


def _require(module: str):
    """Import a module the Postgres leg needs; skip locally, fail in CI."""
    try:
        return __import__(module)
    except ImportError:
        if os.environ.get("CI"):
            pytest.fail(f"{module} is not installed in CI, so this leg would not run")
        pytest.skip(f"{module} is not installed")


# --- pure: naming and URL shape ---------------------------------------------


def test_two_runs_get_two_names_with_the_base_as_prefix():
    a = run_database_name("appdb", fixture_module.new_run_token())
    b = run_database_name("appdb", fixture_module.new_run_token())
    assert a != b
    assert a.startswith("appdb_run_") and b.startswith("appdb_run_")


def test_a_long_base_is_cut_to_63_bytes_and_keeps_the_token_whole():
    token = "12345_abcdef"
    name = run_database_name("x" * 80, token)
    assert len(name.encode("utf-8")) <= 63
    assert name.endswith("_run_" + token)


def test_the_byte_bound_does_not_split_a_multibyte_character():
    token = "1_abcdef"
    name = run_database_name("é" * 40, token)  # 2 bytes each
    assert len(name.encode("utf-8")) <= 63
    name.encode("utf-8").decode("utf-8")  # still valid UTF-8
    assert name.endswith("_run_" + token)


@pytest.mark.parametrize(
    "raw", ["sqlite:///x.db", "mysql://u@h/db", "", "not a url"]
)
def test_a_non_postgres_url_is_not_treated_as_postgres(raw):
    assert not is_postgres_url(raw)


def test_only_the_database_changes_in_the_run_url():
    _require("sqlalchemy")
    url = checked_postgres_url(
        "postgresql+psycopg://u:p%40ss@db.local:6543/appdb?sslmode=require&application_name=t"
    )
    run = run_database_url(url, "appdb_run_1_abcdef")
    assert (run.username, run.password, run.host, run.port) == ("u", "p@ss", "db.local", 6543)
    assert dict(run.query) == {"sslmode": "require", "application_name": "t"}
    assert run.database == "appdb_run_1_abcdef"


@pytest.mark.parametrize("key", ["dbname", "database", "service", "host", "hostaddr", "port", "DBNAME"])
def test_a_query_key_that_selects_the_destination_is_refused(key):
    _require("sqlalchemy")
    with pytest.raises(TestDatabaseRefused, match=key):
        checked_postgres_url(f"postgresql://u@h/appdb?{key}=other")


def test_dbname_equal_to_the_path_is_still_refused():
    # The round 1 case: harmless-looking, and it wins over the substituted path.
    _require("sqlalchemy")
    with pytest.raises(TestDatabaseRefused, match="dbname"):
        checked_postgres_url("postgresql+psycopg://u@h/appdb?dbname=appdb")


@pytest.mark.parametrize("raw", ["postgresql://u@h/appdb?sslmode=disable",
                                 "postgresql://u@h/appdb?application_name=x&connect_timeout=5",
                                 "postgres://u@h/appdb"])
def test_unrelated_settings_are_accepted(raw):
    _require("sqlalchemy")
    assert checked_postgres_url(raw).database == "appdb"


@pytest.mark.parametrize("raw", ["postgresql://u@h/", "postgresql://u@h", "postgresql+asyncpg://u@h/appdb",
                                 "postgresql+pg8000://u@h/appdb"])
def test_no_path_database_or_an_unchecked_driver_is_refused(raw):
    _require("sqlalchemy")
    with pytest.raises(TestDatabaseRefused):
        checked_postgres_url(raw)


@pytest.mark.parametrize("scheme", ["postgresql+psycopg", "postgresql+psycopg2"])
def test_the_effective_driver_arguments_name_only_the_run_database(scheme):
    # The URL text is not the evidence; what the driver is handed is.
    _require("sqlalchemy")
    url = checked_postgres_url(f"{scheme}://u:p@h:5432/appdb?sslmode=disable")
    run = run_database_url(url, "appdb_run_1_abcdef")
    _, kwargs = run.get_dialect()().create_connect_args(run)
    assert kwargs["dbname"] == "appdb_run_1_abcdef"
    assert "database" not in kwargs and "service" not in kwargs
    assert kwargs["sslmode"] == "disable"


def test_the_drop_refuses_a_name_this_process_did_not_create():
    # Shaped exactly like a run database, and still refused: the guard is an
    # exact record, not a pattern. This half needs no server; the refusal
    # happens before any connection.
    with pytest.raises(ValueError, match="did not create"):
        fixture_module.drop_run_database(None, "appdb_run_1_abcdef")


# --- inner sessions ----------------------------------------------------------


def _inner(tmp_path: pathlib.Path, body: str, name: str = "inner") -> pathlib.Path:
    """A throwaway project holding this template's real conftest and one test file."""
    root = tmp_path / name
    root.mkdir()
    (root / "conftest.py").write_text(REAL_CONFTEST.read_text())
    (root / "test_inner.py").write_text(body)
    return root


def _env(url: str | None, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "TEST_DATABASE_URL"}
    if url is not None:
        env["TEST_DATABASE_URL"] = url
    env.update(extra)
    return env


def _start(root: pathlib.Path, env: dict) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rA", str(root)],
        cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def _run(root: pathlib.Path, env: dict) -> tuple[int, str]:
    proc = _start(root, env)
    out, _ = proc.communicate(timeout=240)
    return proc.returncode, out


def test_unset_yields_none_and_imports_no_database_library(tmp_path):
    root = _inner(tmp_path, """
import sys

def test_it(test_database_url):
    assert test_database_url is None
    assert "sqlalchemy" not in sys.modules
""")
    code, out = _run(root, _env(None))
    assert code == 0, out


# --- the Postgres leg --------------------------------------------------------


@pytest.fixture(scope="module")
def pg():
    """The server under test, and a sentinel table in its base database."""
    raw = os.environ.get("TEST_DATABASE_URL")
    if not raw:
        if os.environ.get("CI"):
            pytest.fail("TEST_DATABASE_URL is not set in CI, so the Postgres leg would not run")
        pytest.skip("TEST_DATABASE_URL is not set; the Postgres leg did not run")
    _require("sqlalchemy")
    from sqlalchemy import create_engine, text

    url = checked_postgres_url(raw)
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    sentinel = f"l86_sentinel_{uuid.uuid4().hex[:8]}"
    with engine.connect() as conn:
        conn.execute(text(f'CREATE TABLE "{sentinel}" (v text)'))
        conn.execute(text(f"INSERT INTO \"{sentinel}\" VALUES ('untouched')"))

    class Server:
        base_url = raw
        base = url.database

        def databases(self) -> set[str]:
            with engine.connect() as conn:
                return {r[0] for r in conn.execute(text("SELECT datname FROM pg_database"))}

        def run_databases(self) -> set[str]:
            return {d for d in self.databases() if d.startswith(self.base + "_run_")}

        def sentinel_intact(self) -> bool:
            with engine.connect() as conn:
                return conn.execute(text(f'SELECT v FROM "{sentinel}"')).scalar() == "untouched"

        def execute(self, sql: str) -> None:
            with engine.connect() as conn:
                conn.execute(text(sql))

    yield Server()
    with engine.connect() as conn:
        conn.execute(text(f'DROP TABLE IF EXISTS "{sentinel}"'))
    engine.dispose()


RECORD_NAME = """
import os, pathlib
from sqlalchemy import create_engine, text

def _current(url):
    engine = create_engine(url)
    with engine.connect() as conn:
        name = conn.execute(text("SELECT current_database()")).scalar()
    engine.dispose()
    pathlib.Path(os.environ["L86_RECORD"]).write_text(name)
    return name
"""


def test_the_run_database_exists_during_the_run_and_is_gone_after(pg, tmp_path):
    record = tmp_path / "name.txt"
    root = _inner(tmp_path, RECORD_NAME + """
def test_it(test_database_url):
    assert _current(test_database_url).startswith(os.environ["L86_BASE"] + "_run_")
""")
    code, out = _run(root, _env(pg.base_url, L86_RECORD=str(record), L86_BASE=pg.base))
    assert code == 0, out
    name = record.read_text()
    assert name not in pg.databases()
    assert pg.sentinel_intact()


def test_a_failing_run_still_drops_its_database(pg, tmp_path):
    record = tmp_path / "name.txt"
    root = _inner(tmp_path, RECORD_NAME + """
def test_it(test_database_url):
    _current(test_database_url)
    assert False, "this inner test fails on purpose"
""")
    code, out = _run(root, _env(pg.base_url, L86_RECORD=str(record)))
    assert code == 1, out
    assert record.read_text() not in pg.databases()
    assert pg.sentinel_intact()


def test_a_connection_left_open_does_not_keep_the_database(pg, tmp_path):
    record = tmp_path / "name.txt"
    root = _inner(tmp_path, RECORD_NAME + """
LEFT_OPEN = []

def test_it(test_database_url):
    _current(test_database_url)
    conn = create_engine(test_database_url).connect()
    conn.execute(text("SELECT 1"))
    LEFT_OPEN.append(conn)  # never closed
""")
    code, out = _run(root, _env(pg.base_url, L86_RECORD=str(record)))
    assert code == 0, out
    assert record.read_text() not in pg.databases()


def test_dbname_in_the_query_is_refused_before_anything_is_created(pg, tmp_path):
    before = pg.run_databases()
    root = _inner(tmp_path, """
def test_it(test_database_url):
    raise AssertionError("must not be reached")
""")
    sep = "&" if "?" in pg.base_url else "?"
    code, out = _run(root, _env(f"{pg.base_url}{sep}dbname={pg.base}"))
    assert code != 0, out
    assert "dbname" in out and "must not be reached" not in out, out
    assert pg.run_databases() == before
    assert pg.sentinel_intact()


def test_an_unrelated_query_setting_still_reaches_the_run_database(pg, tmp_path):
    record = tmp_path / "name.txt"
    root = _inner(tmp_path, RECORD_NAME + """
def test_it(test_database_url):
    assert _current(test_database_url).startswith(os.environ["L86_BASE"] + "_run_")
""")
    sep = "&" if "?" in pg.base_url else "?"
    code, out = _run(root, _env(f"{pg.base_url}{sep}application_name=l86",
                                L86_RECORD=str(record), L86_BASE=pg.base))
    assert code == 0, out
    assert record.read_text() not in pg.databases()


def test_the_identity_check_refuses_a_url_that_lands_elsewhere(pg, monkeypatch):
    # Simulate a selector the refusal list does not know: the run URL still
    # points at the base database. The fixture must refuse to hand it out and
    # must drop what it created.
    before = pg.run_databases()
    monkeypatch.setattr(fixture_module, "run_database_url", lambda url, name: url)
    with pytest.raises(RuntimeError, match="refusing to hand it out"):
        fixture_module.create_run_database(checked_postgres_url(pg.base_url))
    assert pg.run_databases() == before
    assert pg.sentinel_intact()


CHURN = """
import os, time
from sqlalchemy import create_engine, text

def test_churn(test_database_url):
    url = test_database_url if os.environ["L86_MODE"] == "fixture" else os.environ["L86_SHARED"]
    tag = os.environ["L86_TAG"]
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    deadline = time.monotonic() + 4
    seen_foreign = set()
    with engine.connect() as conn:
        while time.monotonic() < deadline:
            # What every build fixture does: throw the schema away and rebuild it.
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
            conn.execute(text(f"CREATE TABLE marker_{tag} (v int)"))
            time.sleep(0.02)
            tables = {r[0] for r in conn.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))}
            if tables != {f"marker_{tag}"}:
                seen_foreign.add(tuple(sorted(tables)))
    engine.dispose()
    assert not seen_foreign, f"another run's schema reached this one: {sorted(seen_foreign)[:3]}"
"""


def _churn_pair(pg, tmp_path, mode: str, base_url: str, shared: str = "") -> list[tuple[int, str]]:
    procs = []
    for tag in ("a", "b"):
        root = _inner(tmp_path, CHURN, name=f"{mode}_{tag}")
        procs.append(_start(root, _env(base_url, L86_MODE=mode, L86_TAG=tag, L86_SHARED=shared)))
    return [(p.wait(timeout=240), p.stdout.read()) for p in procs]


@pytest.mark.parametrize("query", ["", "application_name=l86"])
def test_two_concurrent_runs_do_not_see_each_others_schema(pg, tmp_path, query):
    """The L86 incident: two runs rebuilding their schema at the same time."""
    url = pg.base_url
    if query:
        url += ("&" if "?" in url else "?") + query
    results = _churn_pair(pg, tmp_path, "fixture", url)
    for code, out in results:
        assert code == 0, out
    assert pg.sentinel_intact()


def test_control_the_same_pair_on_one_shared_database_collides(pg, tmp_path):
    """Without this, the test above could pass because the churn never overlaps."""
    from sqlalchemy.engine import make_url

    shared = f"{pg.base}_ctl_{uuid.uuid4().hex[:6]}"
    pg.execute(f'CREATE DATABASE "{shared}"')
    try:
        shared_url = make_url(pg.base_url).set(database=shared).render_as_string(hide_password=False)
        results = _churn_pair(pg, tmp_path, "shared", pg.base_url, shared=shared_url)
        assert any(code != 0 for code, _ in results), [out for _, out in results]
    finally:
        pg.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{shared}' AND pid <> pg_backend_pid()"
        )
        pg.execute(f'DROP DATABASE IF EXISTS "{shared}"')
