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
import logging
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

import app.sqlite_migration_guard as guard                          # noqa: E402
from app.sqlite_migration_guard import sqlite_foreign_keys_suspended  # noqa: E402


class _errors_from_the_guard(logging.Handler):
    """What the guard logged, collected off its own logger rather than `caplog`.

    Attached by name to the guard's module logger. In the originating build this
    was a workaround with teeth: Alembic's `env.py` calls `fileConfig()` on every
    migration invocation, which removes the root-logger handler `caplog`
    installs, so `caplog.records` came back empty while the message was plainly
    on stderr. Nothing here runs Alembic, so `caplog` would work -- but a build
    that instantiates the two stubs below *will* run it, and will reach for this
    file's idiom when it does. Kept for that reader.

    On the fail-open path the guard's log line is the entire safety mechanism:
    the run exits 0 having possibly committed a violation, and this is what
    reads the sentence that tells the operator so.
    """

    def __init__(self):
        super().__init__(logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())

    def __enter__(self):
        logging.getLogger(guard.__name__).addHandler(self)
        return self

    def __exit__(self, *_exc):
        logging.getLogger(guard.__name__).removeHandler(self)
        return False


def _a_check_that_cannot_run(connection):
    """Make `PRAGMA foreign_key_check` raise rather than return rows, for good.

    A foreign key whose parent column carries no unique index cannot be checked:
    SQLite raises `foreign key mismatch` instead of reporting. `parent.label` is
    an ordinary TEXT column, so pointing at it is enough.

    Any failure of the check would serve -- the one found in review was a lock
    timeout -- but this one is deterministic and needs no second writer. It fails
    the check on BOTH sides of the run, which is what reaches the guard's one
    documented fail-open.
    """
    connection.exec_driver_sql(
        "CREATE TABLE stray (id INTEGER PRIMARY KEY, who TEXT REFERENCES parent(label))"
    )
    connection.commit()


def _an_orphan_written_the_only_way_one_can_exist(engine, child_id=987654):
    """A child pointing at a parent that is not there, committed before the run.

    Enforcement is switched off to write it, because that is the only way such a
    row comes to exist at all: a legacy file, a partial restore, a hand edit made
    with the pragma at SQLite's shipped default of OFF.
    """
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql(
            "INSERT INTO child (id, label, parent_id) "
            f"VALUES ({child_id}, 'from an older file', 987654)"
        )
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def _a_batch_rebuild_of_the_parent(connection):
    """Alembic's move-and-copy for `batch_alter_table('parent'): drop_column('label')`.

    Hand-written, which is what lets these tests travel and is also their one
    weakness: it no longer pins that Alembic's batch mode still rebuilds tables
    this way. The two instantiate-me stubs at the bottom of this file are what
    pin that.
    """
    connection.exec_driver_sql("CREATE TABLE _tmp_parent (id INTEGER PRIMARY KEY)")
    connection.exec_driver_sql("INSERT INTO _tmp_parent (id) SELECT id FROM parent")
    connection.exec_driver_sql("DROP TABLE parent")
    connection.exec_driver_sql("ALTER TABLE _tmp_parent RENAME TO parent")
    connection.commit()


def _row_count(engine, table: str) -> int:
    with engine.connect() as connection:
        return connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar()


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


def test_the_guard_suspends_enforcement_and_puts_it_back(tmp_path):
    """Both halves of the guard, on one connection, directly.

    The end-to-end tests either side of this one prove the guard's *effect*
    through a rebuild. This proves the guard itself, which is what lets the
    failure be localised when one of them goes red.
    """
    engine = _a_schema_built_from_nothing(tmp_path / "suspends_and_restores.db")
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            with sqlite_foreign_keys_suspended(connection):
                assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 0
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    finally:
        engine.dispose()


def test_a_suspension_that_did_not_take_is_refused_rather_than_trusted(tmp_path):
    """ac-01. The OFF is read back too, not just the ON.

    SQLite discards `PRAGMA foreign_keys` inside an open transaction in *both*
    directions -- which is the whole reason the restore is read back. Issued on a
    connection that already has one open, the suspension is accepted and dropped,
    and then every downstream signal says the run was fine: the rebuild ran under
    enforcement, so `foreign_key_check` is clean, and the restore reads `1`
    because nothing ever turned it off. The child rows come back stripped and the
    guard reports success.

    `foreign_key_check` cannot stand in for this. The reference is
    `ON DELETE SET NULL` over a nullable column, so nulling every child is not a
    violation -- zero rows, no report. The only way to know the suspension took is
    to read it back.
    """
    engine = _a_schema_built_from_nothing(tmp_path / "suspension_did_not_take.db")
    try:
        with engine.connect() as connection:
            # A real SQLite transaction, open before the guard is entered.
            connection.exec_driver_sql(
                "INSERT INTO parent (id, label) VALUES (2, 'already_writing')"
            )

            # Not `pytest.raises`: the body has to run so the pragma it sees can
            # be reported, and "the guard yielded with enforcement at 1" is the
            # finding.
            observed = []
            raised = None
            try:
                with sqlite_foreign_keys_suspended(connection):
                    observed.append(
                        connection.exec_driver_sql("PRAGMA foreign_keys").scalar()
                    )
            except RuntimeError as exc:
                raised = exc

            assert observed == [], (
                f"the guard yielded with enforcement still reading {observed}, so a "
                f"batch rebuild would have run under it and stripped the children"
            )
            assert raised is not None and "did not take" in str(raised)
            # Refused, not half-applied: nothing was suspended, so there is
            # nothing to restore and the connection is still fit to hand back.
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    finally:
        engine.dispose()


def test_a_clean_run_reports_nothing_and_restores_enforcement(tmp_path):
    """The other half of the check below: it has to be quiet when nothing is
    wrong, or it is noise that gets suppressed and then ignored."""
    engine = _a_schema_built_from_nothing(tmp_path / "clean_run.db")
    try:
        with engine.connect() as connection:
            with sqlite_foreign_keys_suspended(connection):
                pass
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    finally:
        engine.dispose()


def test_rows_orphaned_while_enforcement_was_off_are_reported(tmp_path):
    """ac-01. Suspension makes violations possible for the length of the run.
    This is what stops them also being silent.

    `PRAGMA foreign_key_check` is SQLite's own documented companion to switching
    enforcement off for a table rebuild, and it is the difference between "a
    future batch rebuild orphans rows" being a caveat in a docstring and being a
    message naming the table.

    It is not, however, a backstop for the failure family that produced every
    round of this defect. `ON DELETE SET NULL` over a nullable column leaves a
    database `foreign_key_check` calls clean. What this covers is the
    complementary case: a row left pointing at a parent that is not there, which
    enforcement would have refused and the suspension admits.
    """
    engine = _a_schema_built_from_nothing(tmp_path / "orphans_reported.db")
    try:
        with engine.connect() as connection:
            with pytest.raises(RuntimeError) as raised:
                with sqlite_foreign_keys_suspended(connection):
                    # Accepted only because enforcement is suspended -- which is
                    # the point: this is what a batch rebuild gone wrong looks
                    # like.
                    connection.exec_driver_sql(
                        "INSERT INTO child (id, label, parent_id) "
                        "VALUES (2, 'nobody', 987654)"
                    )
                    connection.commit()

            assert "child" in str(raised.value), "the report does not name the table"
            # ...and the pragma is still put back, because the report is about
            # what happened during the run, not a reason to leave the connection
            # disarmed.
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    finally:
        engine.dispose()


def test_a_connection_whose_restore_failed_never_goes_back_to_the_pool(tmp_path):
    """The last path by which enforcement can leak into the pool.

    Reproduces the original defect's mechanism directly rather than by mutation:
    leaving a transaction open across the exit means SQLite discards the
    restoring pragma exactly as it did when the guard lived inside the migration.
    The guard has to notice, say so, and -- because a connection that is not
    enforcing must never be handed to whoever checks out next -- drop the
    connection rather than return it.
    """
    engine = _a_schema_built_from_nothing(tmp_path / "restore_failed.db")
    try:
        with engine.connect() as connection:
            with pytest.raises(RuntimeError) as raised:
                with sqlite_foreign_keys_suspended(connection):
                    # Opens a real SQLite transaction and leaves it open, which
                    # is what makes `PRAGMA foreign_keys=ON` a no-op on the way
                    # out.
                    connection.exec_driver_sql(
                        "INSERT INTO parent (id, label) VALUES (2, 'leaks')"
                    )

            assert "did not take" in str(raised.value)
            assert connection.invalidated, (
                "the disarmed connection was handed back to the pool"
            )
            # The mechanism is pinned by the line above; this pins what the
            # operator is actually told. Dropping the sentence left the suite
            # green, and on this guard a message is a safety mechanism rather
            # than a nicety.
            assert "has been discarded" in str(raised.value), (
                f"the report does not say the connection was discarded: {raised.value}"
            )
    finally:
        engine.dispose()


def test_a_violation_the_run_committed_is_reported_even_if_the_restore_fails(tmp_path):
    """ac-01. The damage that outlives the connection is the damage that matters.

    A batch rebuild commits. So a violation it leaves is on disk, and dropping
    the connection does not take it back -- unlike the rows still sitting in an
    open transaction, which `invalidate()` does roll back.

    Those two were once conflated: the check was skipped whenever the restore
    failed, on the reasoning that the still-open transaction was about to be
    discarded anyway. That reasoning was measured on an *uncommitted* orphan,
    where it holds, and shipped as though it held generally. It does not, and the
    case where it fails is the only one that leaves committed data damaged.

    The check is runnable on this path -- `invalidate()` has not happened yet --
    so reporting it costs nothing.
    """
    engine = _a_schema_built_from_nothing(tmp_path / "violation_and_failed_restore.db")
    try:
        with engine.connect() as connection:
            with pytest.raises(RuntimeError) as raised:
                with sqlite_foreign_keys_suspended(connection):
                    # What a batch rebuild does: orphan a row, and commit it.
                    connection.exec_driver_sql(
                        "INSERT INTO child (id, label, parent_id) "
                        "VALUES (2, 'nobody', 987654)"
                    )
                    connection.commit()
                    # ...and then leave a transaction open, so the restore fails.
                    connection.exec_driver_sql(
                        "INSERT INTO parent (id, label) VALUES (2, 'leaks')"
                    )

        assert "did not take" in str(raised.value), "the failed restore went unreported"
        assert "child" in str(raised.value), (
            "the committed violation was dropped: the run reported the failed "
            "restore and said nothing about the orphan it left on disk"
        )

        # ...and it really is on disk, so the report was about something real.
        # The uncommitted `parent` row is not: `invalidate()` rolled it back.
        assert _row_count(engine, "child") == 2
        assert _row_count(engine, "parent") == 1
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    finally:
        engine.dispose()


def test_the_guard_never_replaces_the_migration_failure_it_is_unwinding(tmp_path):
    """ac-01. The error the operator reads is the one that broke the migration.

    The guard already declines to *raise* over a propagating failure. But its own
    exit statements were once unguarded, so a run that died with the connection
    -- which is a normal way for a migration to die -- surfaced
    `ProgrammingError: Cannot operate on a closed database. [SQL: PRAGMA ...]`
    and buried the real cause. The intent was stated in the code and not enforced
    by it; this is the enforcement.
    """
    engine = _a_schema_built_from_nothing(tmp_path / "never_replaces.db")
    try:
        with engine.connect() as connection:
            with pytest.raises(RuntimeError, match="THE REAL MIGRATION FAILURE"):
                with sqlite_foreign_keys_suspended(connection):
                    # The connection dies, then the migration does -- the order a
                    # dropped connection or a killed backend produces.
                    connection.connection.dbapi_connection.close()
                    raise RuntimeError("THE REAL MIGRATION FAILURE")

            # ...and the connection still does not go back to the pool:
            # enforcement could not be confirmed, so it is discarded rather than
            # trusted.
            assert connection.invalidated
    finally:
        engine.dispose()


def test_a_discard_that_cannot_happen_is_reported_not_raised(tmp_path):
    """ac-01. `connection.invalidate()` is fallible too, and it is not SQL.

    Every other statement in the exit path is a pragma, and the walk in
    `test_the_invariant_holds_on_a_schema_the_guard_has_never_seen` fails each of
    them by shadowing `exec_driver_sql`. The discard is neither: it was the last
    statement in the block sitting outside a `try`, and it is exactly the shape of
    thing that gets overlooked because it does not look like the others.

    A `Connection` closed by the run makes it raise `ResourceClosedError`, which
    unguarded replaces the guard's whole report -- and in a real migration would
    replace the migration's own error, the property the test above is explicit
    about preserving. It has to be reported like anything else, including the part
    that matters most: that the connection may have gone back to the pool
    unprotected.
    """
    engine = _a_schema_built_from_nothing(tmp_path / "discard_cannot_happen.db")
    try:
        # Deliberately not a `with`: the run closes this connection itself.
        connection = engine.connect()
        with pytest.raises(RuntimeError) as raised:
            with sqlite_foreign_keys_suspended(connection):
                # A migration that closes its own connection: everything in the
                # exit path fails after this, the discard included.
                connection.close()

        assert "could not be discarded" in str(raised.value), (
            f"the failed discard was not reported; got: {raised.value}"
        )
        assert "may have returned to the pool unprotected" in str(raised.value), (
            "the report does not say the connection may be unprotected, which is "
            "the one thing an operator would act on"
        )
    finally:
        engine.dispose()


def test_a_failed_in_memory_check_is_reported_and_not_swallowed(tmp_path, monkeypatch):
    """ac-01. The step that decides whether to destroy the database.

    `_database_lives_in_the_connection` was once annotated as covered by the two
    in-memory tests. Neither exercises its *failure*, and it had both of the
    faults its neighbours were fixed for: an interrupt caught there was swallowed,
    and its failure was the only one in the exit path that was never reported.
    Silent, in the branch that decides whether to delete a schema.
    """
    def failing_check(_connection):
        raise KeyboardInterrupt("operator pressed Ctrl-C during the check")

    monkeypatch.setattr(guard, "_database_lives_in_the_connection", failing_check)

    engine = _a_schema_built_from_nothing(tmp_path / "in_memory_check_failed.db")
    reported = _errors_from_the_guard()
    try:
        with engine.connect() as connection:
            with pytest.raises(KeyboardInterrupt):
                with reported:
                    with sqlite_foreign_keys_suspended(connection):
                        # Open transaction, so the restore fails and the branch
                        # that calls the check is reached.
                        connection.exec_driver_sql(
                            "INSERT INTO parent (id, label) VALUES (2, 'ctrl_c')"
                        )

        message = " ".join(reported.messages)
        assert "Could not determine whether this database lives in the connection" in message, (
            f"the failed check was never reported; logged: {message}"
        )
        assert "deletes an in-memory database" in message, (
            "the report does not say what the blind decision costs"
        )
    finally:
        engine.dispose()


def test_an_interrupt_on_the_exit_path_is_not_swallowed(tmp_path):
    """ac-01. One of the three non-SQL steps the walk cannot reach.

    `raise interrupt` issues no SQL, so the invariant walk is blind to it:
    deleting both its lines left the originating suite green. The consequence is
    not cosmetic. An interrupt caught while running the exit `foreign_key_check`,
    on a database whose baseline also could not run, goes into `notes` rather than
    `problems` -- so `problems` is empty, the guard returns normally, and the
    process exits 0. Ctrl-C during a migration becomes a successful migration.

    The sibling test covers an interrupt arriving from the migration *body*,
    which is a different path: that one sets `failed` and propagates on its own.
    This is the exit path, where the guard has caught the interrupt itself and has
    to give it back.
    """
    engine = _a_schema_built_from_nothing(tmp_path / "interrupt_on_exit.db")
    try:
        with engine.connect() as connection:
            real = connection.exec_driver_sql
            checks = []

            def failing(sql, *args, **kwargs):
                if sql == "PRAGMA foreign_key_check":
                    checks.append(sql)
                    if len(checks) == 1:
                        # No baseline, so the exit check's failure lands in
                        # `notes` and leaves `problems` empty -- the case where
                        # nothing else would carry the interrupt out.
                        raise RuntimeError("injected: baseline unavailable")
                    raise KeyboardInterrupt("operator pressed Ctrl-C")
                return real(sql, *args, **kwargs)

            connection.exec_driver_sql = failing
            try:
                with pytest.raises(KeyboardInterrupt):
                    with sqlite_foreign_keys_suspended(connection):
                        pass
            finally:
                connection.exec_driver_sql = real

        # An interrupt landing between a pragma completing and its result being
        # assigned leaves a SQLite handle reachable only from the traceback,
        # holding the write lock until it is collected. Collected here so the
        # enforcement read below is not racing a lock this test made.
        gc.collect()
        assert _foreign_key_enforcement(engine) == {1}, "and the pool is still armed"
    finally:
        engine.dispose()


def test_an_interrupted_migration_surfaces_the_interrupt(tmp_path):
    """ac-01. `except BaseException` on the run, and why it is not `Exception`.

    The guard suppresses its own complaints when the run itself failed, so the
    operator reads the real cause. That decision keys off `failed`, which is set
    by the handler around the `yield` -- and if that handler only caught
    `Exception`, a `KeyboardInterrupt` would leave `failed` False, so the guard
    would raise its own `RuntimeError` about the restore *over* the interrupt.
    Ctrl-C during a migration would report a foreign-key problem.

    Narrowing that handler to `except Exception` left the whole originating suite
    green before this test existed, which is the only reason it does.
    """
    engine = _a_schema_built_from_nothing(tmp_path / "interrupted_migration.db")
    try:
        with engine.connect() as connection:
            with pytest.raises(KeyboardInterrupt):
                with sqlite_foreign_keys_suspended(connection):
                    # An open transaction, so the restore fails and the guard has
                    # something of its own it would otherwise raise about.
                    connection.exec_driver_sql(
                        "INSERT INTO parent (id, label) VALUES (2, 'interrupted')"
                    )
                    raise KeyboardInterrupt

            # The complaint is still logged and the connection still discarded --
            # suppressed as an exception, not as a fact.
            assert connection.invalidated
    finally:
        engine.dispose()


@pytest.mark.parametrize("later_site", ["the exit check", "the in-memory check"])
def test_the_operators_interrupt_wins_not_the_one_the_guard_provoked(
    tmp_path, monkeypatch, later_site
):
    """ac-01. `interrupt is None` on both sites that can overwrite, not one.

    Three handlers assign `interrupt`. The first cannot overwrite anything -- it
    is None there by construction -- so it carries no guard. The other two run
    after it, in order: the exit `foreign_key_check`, then the in-memory check.
    Without their guards the interrupt handed back is whichever fired *last*,
    which is one the guard provoked while unwinding rather than the one the
    operator sent.

    Parametrized over both, because an earlier version exercised only the
    in-memory site: removing the guard from the exit-check handler was a real
    semantic change that passed.
    """
    first = KeyboardInterrupt("the operator pressed Ctrl-C")
    later = KeyboardInterrupt("provoked later, while unwinding")

    def failing_check(_connection):
        if later_site == "the in-memory check":
            raise later
        return False

    monkeypatch.setattr(guard, "_database_lives_in_the_connection", failing_check)

    engine = _a_schema_built_from_nothing(tmp_path / f"interrupt_{later_site[4:]}.db")
    try:
        with engine.connect() as connection:
            real = connection.exec_driver_sql
            checks = []

            def failing(sql, *args, **kwargs):
                if sql == "PRAGMA foreign_keys=ON":
                    raise first                     # first assignment site
                if sql == "PRAGMA foreign_key_check":
                    checks.append(sql)
                    # The SECOND one is the exit check; the first is the
                    # baseline, which runs before anything is suspended and whose
                    # handler deliberately lets a BaseException through.
                    if len(checks) == 2 and later_site == "the exit check":
                        raise later                 # second assignment site
                return real(sql, *args, **kwargs)

            connection.exec_driver_sql = failing
            try:
                with pytest.raises(KeyboardInterrupt) as raised:
                    with sqlite_foreign_keys_suspended(connection):
                        pass
            finally:
                connection.exec_driver_sql = real

        assert raised.value is first, (
            f"with the interrupt arriving at {later_site}, the guard handed back "
            f"{str(raised.value)!r} -- which it provoked itself -- instead of the "
            f"operator's interrupt"
        )
    finally:
        gc.collect()
        engine.dispose()


def test_an_orphan_that_predates_the_run_is_not_blamed_on_it(tmp_path):
    """ac-01. The report is about the run, and `foreign_key_check` is not.

    The check scans the whole database, so on its own it cannot tell a row the run
    orphaned from one that was already there -- a legacy file, a partial restore,
    a hand edit made with enforcement off, which is how SQLite ships. Reported
    without a baseline, such a row is attributed to a migration that did not cause
    it, and because nothing ever clears it, it fails *every* subsequent run. A
    bootstrap command that calls `upgrade()` is then a release step that can never
    be run again on that database.

    Two runs, not one. A baseline makes the first run clean; only the second shows
    that the condition does not accumulate.

    This covers the plain case only: one pre-existing orphan and the baseline
    subtracting it. It says nothing about *how* the baseline is keyed, and it
    cannot -- both tables here have an `INTEGER PRIMARY KEY`, so the rowid happens
    to survive a rebuild and a row-keyed diff would pass this too. The two
    renumbering tests above are what hold the key.
    """
    engine = _a_schema_built_from_nothing(tmp_path / "predates_the_run.db")
    try:
        _an_orphan_written_the_only_way_one_can_exist(engine)

        for run in (1, 2):
            with engine.connect() as connection:
                # No `pytest.raises`: not failing is the behaviour under test.
                with sqlite_foreign_keys_suspended(connection):
                    _a_batch_rebuild_of_the_parent(connection)
            assert _foreign_key_enforcement(engine) == {1}, (
                f"run {run} left the pool disarmed"
            )

        # The orphan is still there -- the runs did not quietly repair it, which
        # would make the two clean runs above prove nothing.
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall(), (
                "the pre-existing orphan disappeared, so nothing was subtracted "
                "and this test would pass with no baseline at all"
            )
    finally:
        engine.dispose()


def test_a_failure_inside_the_guards_own_exit_still_restores_enforcement(tmp_path):
    """ac-01. Nothing in the exit path gets to run ahead of the restore.

    The exit path does two things -- put the pragma back, and report what the
    suspension let through -- and only one of them is what protects the pool. If
    the report runs first and raises, the restore, the read-back and the
    `invalidate()` are all skipped, the connection closes normally, and whoever
    checks it out next runs unenforced. That is worse than the violation the
    report was trying to surface, because it is silent.

    **Do not drop this one when trimming.** It is the only evidence for the
    documented fail-open below it: that a run whose check was already broken is
    not failed, but is said out loud.
    """
    engine = _a_schema_built_from_nothing(tmp_path / "failure_inside_the_exit.db")
    reported = _errors_from_the_guard()
    try:
        with engine.connect() as connection:
            _a_check_that_cannot_run(connection)

        with reported:
            with engine.connect() as connection:
                with sqlite_foreign_keys_suspended(connection):
                    pass

        assert _foreign_key_enforcement(engine) == {1}, (
            "the exit path failed before the restore and handed the pool a "
            "connection with enforcement still switched off"
        )
        # The check was broken before this run started, so the run is not failed
        # over it -- failing would wedge every future migration on a condition
        # that predates them all, which is the same trap the baseline exists to
        # avoid. It is still said out loud, because the run went unverified.
        assert any(
            "has NOT been failed over it" in message for message in reported.messages
        ), f"the unverified run passed without saying so; logged: {reported.messages}"
    finally:
        engine.dispose()


def test_the_one_fail_open_warns_about_the_run_rather_than_excusing_it(tmp_path):
    """ac-01. The deliberate fail-open, pinned as deliberate.

    When `foreign_key_check` cannot run on either side of the migration, the run
    is not failed -- because that condition never clears, and a permanently
    failing bootstrap is worse than a logged warning. That decision is kept. What
    it costs is real and is asserted here: a run can commit a genuine violation,
    exit 0, and be reported only in a log line.

    Which is why the wording is a test and not a preference. The first version
    said the database "was already unverifiable and the run has not been failed
    over it" -- true, reassuring, and silent about the run possibly having
    destroyed data. On the one path where this guard does not fail closed, the
    message is the entire safety mechanism.
    """
    engine = _a_schema_built_from_nothing(tmp_path / "the_one_fail_open.db")
    reported = _errors_from_the_guard()
    try:
        with engine.connect() as connection:
            _a_check_that_cannot_run(connection)

        with engine.connect() as connection:
            with reported:
                # No `pytest.raises`: not failing is the behaviour under test.
                with sqlite_foreign_keys_suspended(connection):
                    connection.exec_driver_sql(
                        "INSERT INTO child (id, label, parent_id) "
                        "VALUES (2, 'nobody', 987654)"
                    )
                    connection.commit()

        # Specifically an orphan, not merely a row: a fixture that later adds a
        # legitimate child would satisfy a bare count vacuously, and the point of
        # this test is that the fail-open let real damage through.
        with engine.connect() as connection:
            orphans = connection.exec_driver_sql(
                "SELECT count(*) FROM child WHERE parent_id NOT IN "
                "(SELECT id FROM parent)"
            ).scalar()
        assert orphans == 1, "the run did not actually commit a foreign key violation"

        message = " ".join(reported.messages)
        assert "may have left foreign key violations" in message, (
            f"the fail-open reported the database's state and not the run's risk; "
            f"logged: {message}"
        )
        assert "exits 0" in message, (
            "the message does not tell the operator that the exit status will not "
            "carry this, which is the only thing that makes it actionable"
        )
    finally:
        engine.dispose()


def test_a_rebuild_of_a_cascade_parent_keeps_the_children(tmp_path):
    """ac-01. The other data-loss path, and the bigger one.

    Ported from `test_rolling_back_past_the_session_rebuild_keeps_the_participants`.
    In the originating build a downgrade batch-dropped two columns from a table
    that was the parent of an `ON DELETE CASCADE` child, so an unguarded rollback
    past that point did not merely null a reference: it deleted every child row.
    Measured there with the guard removed, the child table went to 0.

    `ON DELETE SET NULL` -- what the shared fixture uses, and what the headline
    rebuild test exercises -- loses the *reference*. `ON DELETE CASCADE` loses the
    *row*. They are different severities of the same mechanism and the guard has
    to hold both, so this builds the cascade shape rather than borrowing the
    fixture's.
    """
    engine = _engine_with_enforcement(tmp_path / "cascade_parent_rebuild.db")
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("CREATE TABLE room (id INTEGER PRIMARY KEY, label TEXT)")
            connection.exec_driver_sql(
                "CREATE TABLE occupant (id INTEGER PRIMARY KEY, name TEXT, "
                "room_id INTEGER REFERENCES room(id) ON DELETE CASCADE)"
            )
            connection.exec_driver_sql("INSERT INTO room (id, label) VALUES (1, 'room 3')")
            for i, name in enumerate(("first", "second", "third"), start=1):
                connection.exec_driver_sql(
                    f"INSERT INTO occupant (id, name, room_id) VALUES ({i}, '{name}', 1)"
                )
            connection.commit()

        with engine.connect() as connection:
            with sqlite_foreign_keys_suspended(connection):
                # `batch_alter_table('room') as batch_op: drop_column('label')`
                connection.exec_driver_sql("CREATE TABLE _tmp_room (id INTEGER PRIMARY KEY)")
                connection.exec_driver_sql("INSERT INTO _tmp_room (id) SELECT id FROM room")
                connection.exec_driver_sql("DROP TABLE room")
                connection.exec_driver_sql("ALTER TABLE _tmp_room RENAME TO room")
                connection.commit()

        with engine.connect() as connection:
            surviving = connection.exec_driver_sql(
                "SELECT id, name, room_id FROM occupant ORDER BY id"
            ).fetchall()
        assert [tuple(row) for row in surviving] == [
            (1, "first", 1), (2, "second", 1), (3, "third", 1)
        ], "the rebuild cascaded and emptied the room"
        assert _foreign_key_enforcement(engine) == {1}
    finally:
        engine.dispose()


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
