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

def _engine_with_enforcement(path):
    """A SQLite engine whose connections come up with foreign keys enforced.

    Extracted because every test here needs it and the reason is easy to lose:
    the guard restores enforcement, it never establishes it. SQLite's default is
    OFF and this template registers nothing, so without this the guard has
    nothing to put back and every assertion reads {0, 1}.

    This is the canonical form the SQLITE-FK conventions check asks builds for.
    """
    engine = create_engine(f"sqlite:///{path}")

    @sa.event.listens_for(engine, "connect")
    def _enforce_sqlite_foreign_keys(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


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


def test_the_baseline_diff_survives_a_rebuild_that_renumbers_rowids(tmp_path):
    """ac-01. The guard must not depend on a property of *this* schema.

    `foreign_key_check` reports `(table, rowid, parent, constraint)`. In this
    build every `id` is `INTEGER PRIMARY KEY`, which *is* the rowid, so a
    move-and-copy preserves it and a diff keyed on the whole row happens to work.
    That is a property of the schema, not of the guard, and this helper is going
    into the template for builds nobody has looked at yet.

    With a `TEXT PRIMARY KEY` and sparse rowids -- ordinary after any delete --
    the rebuild renumbers, so a diff keyed on rowid reports every pre-existing
    orphan as newly created. Every run. It never clears, and `flask bootstrap`
    calls `upgrade()`, so it is a permanently failing release command.

    The tables here are deliberately not this build's: nothing about the property
    involves `participant`, and a test bound to `TITLE_REVISION` could not have
    caught it.
    """
    engine = _engine_with_enforcement(tmp_path / "the_baseline_diff_survives_a.db")

    with engine.connect() as connection:
        connection.exec_driver_sql("CREATE TABLE t_parent (id TEXT PRIMARY KEY)")
        connection.exec_driver_sql(
            "CREATE TABLE t_child (id TEXT PRIMARY KEY, "
            "parent_id TEXT REFERENCES t_parent(id))"
        )
        connection.exec_driver_sql("INSERT INTO t_parent (id) VALUES ('p1')")
        for i in range(1, 6):
            connection.exec_driver_sql(
                f"INSERT INTO t_child (id, parent_id) VALUES ('c{i}', 'p1')"
            )
        # Sparse rowids, which is what any delete leaves behind.
        connection.exec_driver_sql("DELETE FROM t_child WHERE id IN ('c1','c2')")
        connection.commit()
        # A pre-existing orphan, written the only way one can exist.
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql("UPDATE t_child SET parent_id='GONE' WHERE id='c3'")
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        before = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    assert before, "the fixture failed to create a pre-existing violation"

    with engine.connect() as connection:
        # Alembic's batch rebuild: temp table carrying the constraints, copy,
        # DROP the original, rename.
        with sqlite_foreign_keys_suspended(connection):
            connection.exec_driver_sql(
                "CREATE TABLE _tmp_t_child (id TEXT PRIMARY KEY, "
                "parent_id TEXT REFERENCES t_parent(id))"
            )
            connection.exec_driver_sql(
                "INSERT INTO _tmp_t_child (id, parent_id) "
                "SELECT id, parent_id FROM t_child"
            )
            connection.exec_driver_sql("DROP TABLE t_child")
            connection.exec_driver_sql("ALTER TABLE _tmp_t_child RENAME TO t_child")
            connection.commit()

    # The rebuild renumbered the rowids, and the orphan is the same orphan.
    with engine.connect() as connection:
        after = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    assert [row[1] for row in before] != [row[1] for row in after], (
        "this test proves nothing unless the rebuild actually renumbered the "
        f"rowids; before={before} after={after}"
    )


def test_a_table_that_is_its_own_parent_survives_its_own_rebuild(tmp_path):
    """ac-01. The rebuild fires `ON DELETE` against the table being rebuilt.

    Every foreign key in this build points between two tables, so every test here
    exercises "rebuild A, watch B". A self-parent -- `parent_id` referencing the
    same table's `id` -- is a different execution shape, not just a different
    schema: the batch rebuild's implicit `DELETE FROM` fires `ON DELETE CASCADE`
    against rows *in the table currently being dropped*, mid-rebuild.

    Nothing in this build can produce that, and it is ordinary in the app types
    this helper is heading towards: categories, org charts, threaded comments,
    folder trees. Unguarded, dropping the table cascades the whole hierarchy away
    before the copy is renamed into place.
    """
    engine = _engine_with_enforcement(tmp_path / "a_table_that_is_its_own_pare.db")

    with engine.connect() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE node (id INTEGER PRIMARY KEY, label TEXT, "
            "parent_id INTEGER REFERENCES node(id) ON DELETE CASCADE)"
        )
        # root -> child -> grandchild, so a cascade has something to chain through
        connection.exec_driver_sql("INSERT INTO node (id, label, parent_id) VALUES (1,'root',NULL)")
        connection.exec_driver_sql("INSERT INTO node (id, label, parent_id) VALUES (2,'child',1)")
        connection.exec_driver_sql("INSERT INTO node (id, label, parent_id) VALUES (3,'grandchild',2)")
        connection.commit()

    with engine.connect() as connection:
        with sqlite_foreign_keys_suspended(connection):
            # Alembic's batch rebuild of `node`, carrying its own self-reference.
            connection.exec_driver_sql(
                "CREATE TABLE _tmp_node (id INTEGER PRIMARY KEY, label TEXT, "
                "parent_id INTEGER REFERENCES node(id) ON DELETE CASCADE)"
            )
            connection.exec_driver_sql(
                "INSERT INTO _tmp_node (id, label, parent_id) "
                "SELECT id, label, parent_id FROM node"
            )
            connection.exec_driver_sql("DROP TABLE node")
            connection.exec_driver_sql("ALTER TABLE _tmp_node RENAME TO node")
            connection.commit()

    with engine.connect() as connection:
        surviving = connection.exec_driver_sql(
            "SELECT id, label, parent_id FROM node ORDER BY id"
        ).fetchall()
    assert [tuple(row) for row in surviving] == [
        (1, "root", None), (2, "child", 1), (3, "grandchild", 2)
    ], "the self-referential cascade took the hierarchy with it"


def test_the_baseline_diff_survives_a_rebuild_that_renumbers_constraints(tmp_path):
    """ac-01. The constraint id renumbers too, for the same kind of reason.

    Dropping the rowid from the comparison key fixed one schema dependency and
    left another in the same tuple. `constraint` is SQLite's `fkid`: an index
    into `foreign_key_list`, assigned in reverse declaration order and
    renumbered whenever the list changes. A batch `drop_column` on an FK-bearing
    column is exactly that -- the surviving foreign keys shuffle down.

    So a pre-existing orphan keyed `('child','pa',1)` becomes `('child','pa',0)`
    and is reported as newly created: a deploy exiting 1 after the migration has
    already committed. Narrower than the rowid version because it needs a table
    with two foreign keys and a migration that drops one, and it self-clears on
    the next run rather than wedging forever -- but it is the same failure the
    rowid fix set out to remove, one field along.
    """
    engine = _engine_with_enforcement(tmp_path / "the_baseline_diff_survives_a.db")

    with engine.connect() as connection:
        connection.exec_driver_sql("CREATE TABLE pa (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE pb (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql(
            "CREATE TABLE child (id INTEGER PRIMARY KEY, "
            "a_id INTEGER REFERENCES pa(id), b_id INTEGER REFERENCES pb(id))"
        )
        connection.exec_driver_sql("INSERT INTO pa (id) VALUES (1)")
        connection.exec_driver_sql("INSERT INTO pb (id) VALUES (1)")
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql(
            "INSERT INTO child (id, a_id, b_id) VALUES (1, 999, 1)"
        )
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        before = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    assert before, "the fixture failed to create a pre-existing violation"

    with engine.connect() as connection:
        # `batch_op.drop_column('b_id')`: the `pa` foreign key shuffles down.
        with sqlite_foreign_keys_suspended(connection):
            connection.exec_driver_sql(
                "CREATE TABLE _tmp_child (id INTEGER PRIMARY KEY, "
                "a_id INTEGER REFERENCES pa(id))"
            )
            connection.exec_driver_sql(
                "INSERT INTO _tmp_child (id, a_id) SELECT id, a_id FROM child"
            )
            connection.exec_driver_sql("DROP TABLE child")
            connection.exec_driver_sql("ALTER TABLE _tmp_child RENAME TO child")
            connection.commit()

    with engine.connect() as connection:
        after = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    assert [row[3] for row in before] != [row[3] for row in after], (
        "this test proves nothing unless the rebuild actually renumbered the "
        f"constraint ids; before={before} after={after}"
    )


# ---------------------------------------------------------------------------
# The two proofs that CANNOT be schema-agnostic.
#
# Everything above hand-writes the move-and-copy, which is what lets it travel.
# That is also its one weakness: it no longer pins that Alembic's batch mode
# still rebuilds tables the way this guard assumes. These two do, and they need
# a real app, real migrations and a real runner, none of which exist here.
#
# They ship as SKIPS with instructions, deliberately, never as code that
# silently passes. A stub that passes is worse than an absent test: it reports
# the property as proven.
# ---------------------------------------------------------------------------

def test_a_guard_failure_stops_a_real_migration_and_says_why():
    pytest.skip(
        "INSTANTIATE THIS IN YOUR BUILD. Assert that a RuntimeError raised by "
        "the guard reaches the operator as a NON-ZERO EXIT through the migration "
        "runner, not merely that the guard raises. The chain from raise to exit "
        "code is the thing under test and a direct call cannot stand in for it: "
        "`flask bootstrap` calls upgrade(), so a guard that raises without "
        "failing the command leaves a release step that reports success over a "
        "half-applied migration. Needs: your app factory, your migrations/, and "
        "an invocation through flask_migrate.upgrade rather than the helper."
    )


def test_a_run_that_rebuilt_a_table_gives_the_connection_back_enforcing():
    pytest.skip(
        "INSTANTIATE THIS IN YOUR BUILD. Assert that after a real up/down/up "
        "across your own revisions -- at least one of which batch-rebuilds a "
        "foreign-key PARENT -- every connection in the pool comes back with "
        "PRAGMA foreign_keys = 1. The schema-agnostic tests above check one "
        "connection they created; this checks the pool the app will actually "
        "serve from, after the real Alembic batch path rather than a hand-written "
        "imitation of it. Needs: your app factory, your migrations/, and your "
        "revision identifiers."
    )


def test_the_two_build_specific_stubs_are_still_here():
    """The stubs above are the only pointer to what this file cannot prove.

    Deleting a skipped test is invisible in a green suite, and these two are
    exactly the ones a porter under time pressure deletes: they never pass, they
    never fail, and they look like unfinished work. They are not. They are the
    record of the two properties the schema-agnostic tests structurally cannot
    establish.
    """
    here = set(globals())
    for name in ("test_a_guard_failure_stops_a_real_migration_and_says_why",
                 "test_a_run_that_rebuilt_a_table_gives_the_connection_back_enforcing"):
        assert name in here, (
            f"{name} was removed. It is not unfinished work; it names a property "
            "this file cannot prove without a real app and real migrations. "
            "Restore it, or instantiate it in your build and say so here."
        )
