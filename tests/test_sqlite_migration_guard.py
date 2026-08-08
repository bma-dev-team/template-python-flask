"""Tests for the SQLite batch-migration foreign-key guard.

Ported from the deposition build, where the guard took fourteen rounds and six
shipped defects. These are the schema-agnostic forms: they build their own two
tables rather than borrowing a product schema, which is what lets them travel.
The originating build's reuse notes are explicit that a test which needs its app
fixture cannot come here, because this template has no models, no migrations and
no app factory with tables.

**Why they build the shape instead of borrowing one.** WITHOUT ROWID, the
self-referential foreign key and both rowid-renumbering cases were all found by
constructing the shape deliberately. A test that borrows the standard fixture
can only ever confirm the standard case.

SQLAlchemy is a test-only dependency here. The guard itself imports nothing but
the standard library; see app/sqlite_migration_guard.py.
"""
import gc
import importlib.util
import os
import sqlite3
from collections import Counter

import pytest

sa = pytest.importorskip(
    "sqlalchemy",
    reason="SQLAlchemy is a test-only dependency of the migration guard; "
           "CI installs it. If this skips in CI the guard is unproven, which is "
           "worse than absent -- see test_the_guard_tests_actually_ran.",
)
from sqlalchemy import create_engine          # noqa: E402
from sqlalchemy.pool import StaticPool        # noqa: E402

from app.sqlite_migration_guard import sqlite_foreign_keys_suspended  # noqa: E402


def _pooled_connection_count(engine=None) -> int:
    """How many idle connections the pool can be holding, from the pool itself.

    Read rather than hardcoded, because the number the check below needs is
    whatever `pool_size` happens to be -- and `config.py` already special-cases
    Postgres pool tuning, so a change here is plausible. A literal that agreed
    with the pool today would degrade to a probabilistic sample the day it did
    not, silently and in the safe-looking direction.

    Fails rather than skips if the engine does not pool at all: with a `NullPool`
    every `connect()` is a fresh connection the listener has already armed, so
    the check below would pass without being able to observe anything.
    """
    assert engine is not None, (
        "pass an engine explicitly. The originating build defaulted this to its\n"
        "Flask-SQLAlchemy global; there is no app here, and a silent fallback is\n"
        "how these tests would quietly re-acquire a dependency on one."
    )
    size = getattr(engine.pool, "size", None)
    assert callable(size) and size() > 0, (
        f"{type(engine.pool).__name__} does not hold idle connections, so a "
        f"leaked pragma cannot be observed and this check would pass vacuously"
    )
    return size()

def _foreign_key_enforcement(engine=None) -> set[int]:
    """`PRAGMA foreign_keys`, on every connection the pool is currently holding.

    Held open together rather than read one at a time. The pragma is
    per-connection and the pool hands back whichever one it likes, so reading a
    single connection answers "*some* connection is enforcing" when the question
    is "is *any* connection disarmed" -- and it answers it differently from run to
    run. One leaked connection is a live defect: whoever checks it out next runs
    unprotected. So they all have to say the same thing.

    Exhaustive rather than a sample: it holds `pool_size` connections at once,
    which drains everything idle before the pool starts opening new ones.

    (Found the hard way: the first version of this helper read one connection,
    and the seeded rollback test below passed with the bug still present.)
    """
    assert engine is not None, (
        "pass an engine explicitly. The originating build defaulted this to its\n"
        "Flask-SQLAlchemy global; there is no app here, and a silent fallback is\n"
        "how these tests would quietly re-acquire a dependency on one."
    )
    held = []
    try:
        held = [engine.connect() for _ in range(_pooled_connection_count(engine))]
        return {
            connection.exec_driver_sql("PRAGMA foreign_keys").scalar()
            for connection in held
        }
    finally:
        for connection in held:
            connection.close()

# How many statements one clean pass through the guard issues. Asserted, not
# documentation: the walk below can only fail a statement it sees, so a guard
# that grows or loses one has to come back here and say so deliberately.
GUARD_STATEMENTS = 6

# pysqlite issues these itself around DML. They are not the guard's statements
# and nobody can fail them, so they are excluded when checking that the walk saw
# everything SQLite actually ran.
_DRIVER_TRANSACTION_CONTROL = {"BEGIN", "COMMIT", "ROLLBACK"}

def _statements_sqlite_actually_ran(traced):
    return [
        sql for sql in traced
        # `.rstrip(";")` because `executescript` traces `'BEGIN;'` while a plain
        # `execute` traces `'BEGIN '`. Nothing issues the former today; matching
        # only the bare token would make this a false RED the day something does.
        if sql.strip().split()[0].upper().rstrip(";") not in _DRIVER_TRANSACTION_CONTROL
    ]

def _guard_run_with_failure_at(index, body, when="before", error=RuntimeError,
                               engine=None):
    """One pass through the guard with its `index`-th driver statement raising.

    Returns the statements the pass issued. `index` of -1 injects nothing, which
    is how the statement count for a given body is discovered.

    `when` is the half of this that matters. "before" raises instead of running
    the statement, which models a connection that was already gone. "after" runs
    it and *then* raises, which models the statement taking effect and the
    failure arriving on the way back -- a cursor that dies during fetch, a driver
    error after the pragma has applied.

    Only "after" can catch a suspension left outside the try/finally, because
    only "after" produces the state that makes it dangerous: enforcement actually
    off, and an exception on a path with no restore. Injecting "before" leaves
    enforcement on, so the connection is safe and the missing restore is
    invisible -- which is exactly how a statement gets moved out of the protected
    block and nothing goes red.

    `error` is the exception injected. `except Exception` in the guard does not
    cover a `BaseException`, so the walk runs with both.

    Returns `(issued, ran)`. `issued` is what came through `exec_driver_sql`, the
    only route this can fail. `ran` is what SQLite itself saw, via its trace
    callback, which is blind to *how* a statement was issued. The two must match
    on an uninjected pass, and that comparison is the point: a statement issued
    through a raw DBAPI cursor -- the form `_enforce_sqlite_foreign_keys` uses in
    this same module -- is invisible to the shadow and visible to the trace. This
    walk once missed exactly that, and the whole suite stayed green with the
    suspension moved back outside the protected block.

    The two lists are not identical in general: the trace reports SQL as SQLite
    received it, so an `executemany` of three parameter sets appears as one
    `issued` entry and three `ran` entries, and a parameterised statement is
    traced with its parameters substituted. Both make `ran` longer than `issued`,
    which fails this comparison rather than passing it -- safe, but the next
    person to see that RED should know it may mean "the guard started batching",
    not "the guard started evading". The guard issues only literal pragmas today,
    so the lists match exactly.
    """
    assert engine is not None, (
        "pass an engine explicitly. The originating build defaulted this to its\n"
        "Flask-SQLAlchemy global; there is no app here, and a silent fallback is\n"
        "how these tests would quietly re-acquire a dependency on one."
    )
    with engine.connect() as connection:
        raw = connection.connection.dbapi_connection
        ran = []
        raw.set_trace_callback(ran.append)

        real = connection.exec_driver_sql
        issued = []

        def failing(sql, *args, **kwargs):
            issued.append(sql)
            hit = len(issued) - 1 == index
            if hit and when == "before":
                raise error(f"injected failure before statement {index}: {sql}")
            result = real(sql, *args, **kwargs)
            if hit:
                raise error(f"injected failure after statement {index}: {sql}")
            return result

        connection.exec_driver_sql = failing
        try:
            with sqlite_foreign_keys_suspended(connection):
                body(connection)
        except BaseException:
            pass  # what the guard raises is another test's business
        finally:
            connection.exec_driver_sql = real
            try:
                raw.set_trace_callback(None)
            except Exception:
                pass  # the connection was discarded, which takes the callback with it
            # A discarded connection is out of the pool but not necessarily
            # closed, and SQLite holds its write lock until it is. Closed here
            # rather than left to garbage collection so the next iteration --
            # and the fixture teardown -- are not racing a lock this test made.
            # Only when it was discarded: a connection still in the pool is not
            # this test's to close.
            if connection.invalidated:
                try:
                    raw.close()
                except Exception:
                    pass
                # And one connection is not `raw` at all -- an interrupt on the
                # exit check leaves a second SQLite handle reachable only from
                # the traceback, which keeps the write lock until it is
                # collected. Measured: `raw.close()` alone clears the read-back
                # case and not this one.
                gc.collect()
    return issued, _statements_sqlite_actually_ran(ran)

def _a_schema_built_from_nothing(path):
    """An engine and a seeded parent/child pair, with no app and no migrations.

    The whole point of this helper is what it does *not* touch: no `create_app`,
    no `app.models`, no `migrations/`, no revision identifiers, no
    `tests.conftest`. It is the shape every test in this file would need in order
    to travel to `template-python-flask`, which has none of those things.
    """
    engine = create_engine(f"sqlite:///{path}")

    # The guard's contract is "enforcement was ON, hold it off, put it back".
    # It does not turn enforcement on, and nothing in this template does either:
    # the originating build registered this listener in app/extensions.py, and
    # SQLite's own default is OFF. So the tests establish the precondition, and
    # in doing so they prove what the guard's docstring asserts -- that the two
    # halves are one policy. Without this the guard restores nothing, because
    # there was nothing to restore, and every assertion below reads {0, 1}.
    #
    # This is also the canonical form the SQLITE-FK conventions check demands,
    # and a build wiring up the guard needs BOTH halves, not just this file.
    @sa.event.listens_for(engine, "connect")
    def _enforce_sqlite_foreign_keys(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    with engine.connect() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE parent (id INTEGER PRIMARY KEY, label TEXT)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE child (id INTEGER PRIMARY KEY, label TEXT, "
            "parent_id INTEGER REFERENCES parent(id) ON DELETE SET NULL)"
        )
        connection.exec_driver_sql("INSERT INTO parent (id, label) VALUES (1, 'kept')")
        connection.exec_driver_sql(
            "INSERT INTO child (id, label, parent_id) VALUES (1, 'attributed', 1)"
        )
        connection.commit()
    return engine

def test_the_invariant_holds_on_a_schema_the_guard_has_never_seen(tmp_path):
    """ac-01. The per-statement walk, standing on its own.

    This is the same proof as
    `test_no_statement_in_the_guard_can_leave_the_pool_disarmed`, built on two
    tables created here rather than on this build's migrated schema -- and it is
    the reference form for porting, because `template-python-flask` has no
    `app/models`, no `migrations/` and no app factory to re-fixture against. A
    test that needs `migrated_app` cannot travel; this one is a straight copy.

    It is the better test on one axis and not on another, which is worth being
    precise about. On schema shape it is better: the tables are chosen to
    exercise the guard -- a parent, a child, an `ON DELETE SET NULL` between them
    -- rather than inherited from whatever this product happens to store, and
    `WITHOUT ROWID`, the self-referential foreign key and both renumbering cases
    were all found that way. On integration it is weaker: it hand-writes the
    move-and-copy, so unlike the migration-driven tests it no longer pins that
    Alembic's batch mode still rebuilds tables the way this guard assumes. That
    assumption is pinned by the (b) tests, which is part of why they must ship
    as stubs a build actually instantiates rather than be dropped.

    Both halves of the walk are kept: every statement failed in turn, before and
    after, with an `Exception` and with a `BaseException`.
    """
    engine = _a_schema_built_from_nothing(tmp_path / "agnostic.db")
    try:
        def leaves_a_transaction_open(connection):
            connection.exec_driver_sql(
                "INSERT INTO child (id, label, parent_id) VALUES (2, 'mid_run', 1)"
            )

        def commits_a_violation(connection):
            connection.exec_driver_sql(
                "INSERT INTO child (id, label, parent_id) VALUES (3, 'orphan', 987654)"
            )
            connection.commit()

        bodies = {
            "a clean run": lambda _connection: None,
            "a run that leaves a transaction open": leaves_a_transaction_open,
            "a run that committed a violation": commits_a_violation,
        }

        for label, body in bodies.items():
            issued, ran = _guard_run_with_failure_at(-1, body, engine=engine)
            assert _foreign_key_enforcement(engine) == {1}, (
                f"{label}: the pool was already disarmed with nothing injected"
            )
            assert issued == ran, (
                f"{label}: SQLite ran {ran} but the walk can only fail {issued}"
            )
            if label == "a clean run":
                assert len(issued) == GUARD_STATEMENTS

            for error in (RuntimeError, KeyboardInterrupt):
                for when in ("before", "after"):
                    for index in range(len(issued)):
                        _guard_run_with_failure_at(index, body, when, error, engine)
                        assert _foreign_key_enforcement(engine) == {1}, (
                            f"{label}: failing {when} statement {index} "
                            f"({issued[index]!r}) with {error.__name__} returned a "
                            f"connection to the pool with enforcement off"
                        )
    finally:
        engine.dispose()

def test_a_rebuild_does_not_strip_the_child_rows_on_a_schema_built_here(tmp_path):
    """ac-01. The headline data-loss path, without this build's migrations.

    `test_rolling_the_title_column_back_does_not_unname_the_testimony` proves
    this through a real `flask db downgrade` on revision `a32ef7817b9d`. The
    proof does not depend on either: what matters is that a batch-style rebuild
    of a foreign-key parent, under the guard, leaves the children pointing where
    they pointed. Written here so the port has the invariant without the
    revision.
    """
    engine = _a_schema_built_from_nothing(tmp_path / "rebuild.db")
    try:
        with engine.connect() as connection:
            with sqlite_foreign_keys_suspended(connection):
                # `batch_alter_table('parent') as batch_op: drop_column('label')`
                connection.exec_driver_sql("CREATE TABLE _tmp_parent (id INTEGER PRIMARY KEY)")
                connection.exec_driver_sql(
                    "INSERT INTO _tmp_parent (id) SELECT id FROM parent"
                )
                connection.exec_driver_sql("DROP TABLE parent")
                connection.exec_driver_sql("ALTER TABLE _tmp_parent RENAME TO parent")
                connection.commit()

        with engine.connect() as connection:
            surviving = connection.exec_driver_sql(
                "SELECT id, label, parent_id FROM child"
            ).fetchall()
        assert [tuple(row) for row in surviving] == [(1, "attributed", 1)], (
            "the rebuild fired ON DELETE SET NULL and stripped the child's parent"
        )
        assert _foreign_key_enforcement(engine) == {1}
    finally:
        engine.dispose()


def test_the_guard_tests_actually_ran():
    """Fail loudly in CI if the guard's tests were skipped rather than run.

    Everything above `importorskip`s SQLAlchemy, which is right for a laptop
    that has never installed it and dangerous everywhere else: a skipped suite
    and a passing suite are the same colour. The guard would then ship into
    every build from this template, be asserted PRESENT by the
    SQLITE-BATCH-MIGRATION conventions check, and have been proven by nothing.

    That is strictly worse than shipping no guard, because a documented one
    nobody has exercised gets trusted.

    So: locally this skips, and in CI it fails if the dependency is absent.
    """
    if not os.environ.get("CI"):
        pytest.skip("local run; CI is where the guard has to be proven")
    assert importlib.util.find_spec("sqlalchemy") is not None, (
        "SQLAlchemy is missing in CI, so every test in this file skipped and the "
        "migration guard shipped unproven. Add it to the CI install step."
    )
