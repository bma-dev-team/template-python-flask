"""SQLite batch-migration foreign-key guard: the one shipped implementation.

**What it is for.** SQLite cannot drop or alter a column in place, so Alembic's
``batch_alter_table`` rebuilds the table by move-and-copy. With foreign-key
enforcement ON, the ``DROP TABLE`` in that sequence performs an implicit
``DELETE`` which *fires ON DELETE actions on children*, so rebuilding one table
can silently strip or null rows in another. This context manager holds
enforcement off across the migration run and then reports, loudly, any violation
the run actually introduced.

**The invariant, stated once:** no fallible statement may leave foreign-key
enforcement off without a path that either restores it or discards the
connection. Six rounds on the originating build each repaired one path and
opened another, which is why this ships as an implementation with tests rather
than as a snippet to copy.

**Zero dependencies, deliberately.** Nothing here imports SQLAlchemy. The
connection is duck-typed on ``exec_driver_sql``, ``dialect`` and ``invalidate``,
so this module costs a headless build nothing and the bare template still
installs no ORM. Only the tests need an engine.

**This is half of a policy; the other half is not in this file.** Enforcement
must also be switched ON for every connection, via an Engine ``connect``
listener. That half is three lines and the ``SQLITE-FK`` check in
``app/conventions_audit.py`` carries the canonical form.

**Wiring it into a build, and the step that is easy to lose.** The call belongs
in ``migrations/env.py``, wrapping ``context.run_migrations()``::

    from app.sqlite_migration_guard import sqlite_foreign_keys_suspended

    with context.begin_transaction():
        with sqlite_foreign_keys_suspended(connection):
            context.run_migrations()

``flask db init`` regenerates ``env.py`` from Flask-Migrate's own template,
which wraps ``run_migrations()`` with no guard at all. The template therefore
cannot ship that half: the call is added by hand *after* init, and re-running
init silently removes it again. ``SQLITE-BATCH-MIGRATION`` in the conventions
audit is the backstop that catches a regenerated ``env.py``.

**What the audit can and cannot tell you.** It asserts the guard is *called*.
It cannot tell a correct implementation from a broken one, and it
short-circuits on the substring ``foreign_keys_suspended`` appearing in
``env.py``. A build that copies this helper's name and none of its tests gets a
green audit and a green suite having proved nothing. Only the tests in
``tests/test_sqlite_migration_guard.py`` enforce that it *works*.

**Provenance.** Ported from the deposition build, where it took fourteen rounds
and six shipped defects to reach. The long-form notes inside
``sqlite_foreign_keys_suspended`` are that build's, kept deliberately: they name
the failure modes, the fitness cases and the porting traps, and every count in
them that is not asserted by a test has been wrong at least once.
"""
from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from contextlib import contextmanager

_log = logging.getLogger(__name__)


def _database_lives_in_the_connection(connection) -> bool:
    """True when dropping this connection would drop the database with it.

    Asked of the driver rather than inferred from the URL, because the URL lies.
    SQLAlchemy parses query parameters out of a SQLite URL, so
    ``sqlite:///file:x?mode=memory&cache=shared&uri=true`` has a ``url.database``
    of ``'file:x'`` -- indistinguishable from a filename, and that is the form
    most often paired with ``StaticPool`` in a pytest fixture. A URL check also
    cannot see through ``create_engine(..., creator=...)``, where the URL
    describes nothing the driver actually opened. A previous version of this
    checked ``url.database`` and got both wrong: it discarded -- and so deleted --
    the shared-cache in-memory form, and would have kept a genuinely disarmed
    connection to a real file opened via ``creator``.

    ``PRAGMA database_list`` reports the file backing each attached database and
    the empty string for anything with none: ``:memory:``, the ``mode=memory``
    URI forms, and the temporary on-disk database that ``sqlite:///`` opens,
    which SQLite also deletes when the last connection closes.
    """
    for _seq, name, file in connection.exec_driver_sql("PRAGMA database_list"):
        if name == "main":
            return not file
    # Unreachable in practice: SQLite always lists `main`. Kept deliberately
    # rather than left looking like an untested branch. (An earlier version of
    # this comment also claimed no mutation of the loop could reach it, which is
    # false -- matching `name == "temp"` reaches it and turns 12 tests red.)
    # If it ever runs, the safe answer is the one that protects the pool.
    return False


def _violations_by_constraint(rows):
    """`PRAGMA foreign_key_check` rows as a multiset of (child table, parent table).

    Both of the pragma's other fields are dropped, and for the same reason: a
    move-and-copy renumbers them. The **rowid** is stable only where a table's
    primary key is an `INTEGER PRIMARY KEY` alias. The **constraint** field is
    SQLite's `fkid`, an index into `foreign_key_list` assigned in reverse
    declaration order, so dropping any FK-bearing column shuffles the survivors
    down. Keying on either makes the diff correct for one schema and wrong for
    others; both were measured doing exactly that.

    What is left is two table names, which a rebuild does preserve.
    """
    return Counter((row[0], row[2]) for row in rows)



@contextmanager
def sqlite_foreign_keys_suspended(connection):
    """Hold SQLite's foreign-key enforcement off for the duration of a migration run.

    **Why it is needed.** SQLite cannot drop or alter a column in place, so
    Alembic's ``batch_alter_table`` does it by move-and-copy: build a temporary
    table, copy the rows, ``DROP TABLE`` the original, rename. Under the pragma
    :func:`_enforce_sqlite_foreign_keys` sets above, SQLite's ``DROP TABLE``
    performs an implicit ``DELETE FROM`` which **fires foreign key actions**, so
    rebuilding a table that other rows point at runs their ``ON DELETE`` rules. In
    this schema a batch rebuild of ``participant`` runs
    ``utterance.participant_id``'s ``ON DELETE SET NULL`` over every stored line
    and hands back a deposition with the speakers stripped out of it, silently.
    Five revisions batch-drop a column from a table that is a foreign key parent
    -- ``f8cf64c6119a`` and ``77b111965bee`` and ``244e04cf11b4`` (``operator``,
    parent of ``session.operator_id``), ``fdacc86a35ae`` (``session``),
    ``a32ef7817b9d`` (``participant``) -- and none of them wants those actions to
    fire. The criterion is a batch block containing a ``drop_column``, in either
    direction: ``582ec9a2361c`` and ``0dd59a77d98a`` also use
    ``batch_alter_table`` but only for indexes, which Alembic does without a
    table rebuild.

    Postgres needs none of this: there the same operation is a native
    ``ALTER TABLE ... DROP COLUMN`` and touches no other table. A no-op on every
    other dialect.

    **Why the caller has to be ``migrations/env.py`` and not a migration.** SQLite
    ignores ``PRAGMA foreign_keys`` while a transaction is open -- in *both*
    directions. A migration function can switch it off, because it runs before any
    DML, but it structurally cannot switch it back on: by the time its ``finally``
    runs, the batch copy's ``INSERT ... SELECT`` has opened a transaction and the
    restore is accepted and discarded. It was written that way first and measured:
    enforcement read ``0`` after the rollback and stayed ``0``, leaving every later
    step of the run unprotected and returning the connection to the pool disarmed,
    since the listener above fires on *connect* and not on checkout.

    **The property that matters is wrapping ``run_migrations()``**, which is where
    Alembic opens the transactions that actually exist here -- one per migration.
    It is *not* the nesting against ``context.begin_transaction()``, which on
    SQLite is a ``nullcontext``: ``SQLiteImpl.transactional_ddl`` is ``False``
    (hence the run log's "Will assume non-transactional DDL"). Measured both ways;
    moving this inside that ``with`` changes nothing. An earlier version of this
    docstring claimed that nesting was load-bearing, which would have invited
    someone to preserve the wrong invariant while breaking the real one.

    **Both ends of the suspension are read back, not assumed.** "SQLite ignores
    the pragma inside a transaction" is a symmetric argument, so checking only the
    restore leaves the other half on trust. Issued on a connection that already
    has a transaction open, the ``OFF`` is accepted and discarded exactly as the
    ``ON`` was above -- and then every downstream signal reports success, because
    the rebuild ran *under* enforcement: ``foreign_key_check`` is clean and the
    restore reads ``1`` because nothing ever turned it off. That is the original
    data-loss path with the alarm wired to agree with it, so the ``OFF`` is read
    back and refused before the yield rather than after.

    **What is checked is the two ends, not the middle.** Both read-backs together
    establish that the suspension took and that enforcement is back -- not that
    enforcement stayed off throughout. If this connection were replaced mid-run,
    :func:`_enforce_sqlite_foreign_keys` would arm the replacement and both
    read-backs would still agree: measured, enforcement reads ``0`` inside the
    guard, ``1`` after a forced reconnect, and the guard reports nothing. No
    trigger for that exists today -- Alembic holds one connection for the whole
    run, and a reconnect mid-run would fail the migration long before it reached
    here -- so it is a stated limit rather than a defect, and the thing to
    remember if this helper is ever reused somewhere a connection can be
    recycled underneath it.

    **What ``foreign_key_check`` covers, and what it does not.** It is SQLite's
    own documented companion to switching enforcement off for a table rebuild,
    and it reports rows the suspension let through -- an orphan created while
    enforcement was off, which nothing else would notice. It is compared against
    a baseline taken before the run, because it scans the whole database rather
    than the run's work.

    It is **no backstop for the defect this guard exists for.**
    ``utterance.participant_id`` is nullable, so ``ON DELETE SET NULL`` stripping
    the speaker off every line of a deposition is not a violation: measured on
    fifty unnamed lines, ``foreign_key_check`` returns no rows and
    ``integrity_check`` returns ``ok``. Nothing this check reports will tell you
    the suspension failed. Reading both ends of the pragma back is what does
    that, and it is not optional because the check is quiet.

    **What fails the run.** Never damage known to predate it; where it cannot
    tell, the tie-break is whether failing would ever clear. Four branches, and
    all four are reachable:

    - **Known inherited** -- baseline taken, the violation was in it: subtracted,
      never fatal. Blaming a migration for a legacy orphan bricked ``upgrade()``
      permanently, which is why the baseline exists at all.
    - **Known caused** -- baseline taken, the violation is new: fatal.
    - **Undetermined** -- no baseline, violations present now: **fatal**, and
      reported as undetermined rather than as caused. This one *can* fail a run
      over damage it merely inherited, and that is accepted rather than
      overlooked: it is self-clearing. The next run whose baseline succeeds
      subtracts those same rows and passes. Measured both halves.
    - **Undetectable** -- ``foreign_key_check`` unrunnable on both sides: logged,
      **not** fatal, and the only fail-open here. Unlike the branch above it
      would never clear, and ``app/cli.py``'s ``flask bootstrap`` calls
      ``upgrade()``, so failing forever on a pre-existing condition is not a
      strict alarm, it is an undeployable release command. The cost is real and
      is stated where the message is built: a run on such a database can commit
      violations nobody will be told about, so that ERROR line is the only
      signal and has to be alerted on rather than left to an exit code.

    An earlier version of this compressed all four into "failed for what the run
    did, and never for what it inherited", which reads well and is false of the
    third branch.

    **"Fatal" means exit 1 after the migration has already committed**, not
    instead of it. The check runs on the way out, and on SQLite each migration
    commits as it goes: measured, ``alembic_version`` had advanced and the column
    was gone before the exit-1. So this reports damage, it does not prevent it --
    which is the honest reading of what a post-hoc check can do, and worth
    knowing before anyone builds a rollback expectation on top of it.

    **Notes for reuse elsewhere.** This is written for one build's schema and is
    heading for wider use, so what it assumes:

    - **Not re-entrant.** Nesting two of these on one connection re-arms
      enforcement when the inner one exits, while the outer still needs it off.
      There is one caller, in ``migrations/env.py``, and it should stay that way.
    - **Two full-database scans per migration run**, one baseline and one check.
      Linear in row count -- 11.4 ms per scan over 120,000 utterances here -- and
      paid on every ``flask db upgrade``, including deploys that migrate nothing.
      Immaterial at this size; a build with a very large table should measure
      rather than assume.
    - **This guard has never been executed on the interpreter its CI pins.**
      CI runs Python 3.11 (``.github/workflows/test.yml``, and the same in
      ``template-python-flask``); the only interpreter on the machine every green
      number in its development came from is 3.12. That is not a hypothetical
      gap: a multi-line f-string replacement field -- PEP 701, 3.12-only -- was
      committed here and stood for a commit, parsing locally and red on push.
      ``test_no_source_file_here_needs_a_newer_python_than_ci_runs`` is a
      tripwire for that one construct, not evidence of 3.11 compatibility.
      **Run the suite on 3.11 before trusting it.**
    - **Porting the tests is where this goes wrong, and the destination has no
      schema.** ``template-python-flask`` has no ``app/models``, no
      ``migrations/`` and no app factory with tables -- verified, not assumed. So
      "copy and re-fixture" is not available: there is nothing to re-fixture
      against. Of 38 test functions here, 14 take no `migrated_app` and 24 depend on
      ``migrated_app``, and **every proof of the invariant is among them**.
      Either they are rewritten to build their own schema or the proof does not
      travel. Classification, so nobody has to re-derive it from the test bodies:

      **(a) Can be schema-agnostic -- rewrite against two throwaway tables.**
      Twenty-one of the 24 (21 + 2 + 1 = 24). They need a parent, a child with a foreign key, and a
      row in each; nothing about depositions. Full names, because a reader
      working this list is deciding what to drop:
      ``test_the_guard_suspends_enforcement_and_puts_it_back``,
      ``test_a_suspension_that_did_not_take_is_refused_rather_than_trusted``,
      ``test_rows_orphaned_while_enforcement_was_off_are_reported``,
      ``test_an_orphan_that_predates_the_run_is_not_blamed_on_it``,
      ``test_the_baseline_diff_survives_a_rebuild_that_renumbers_rowids``,
      ``test_the_baseline_diff_survives_a_rebuild_that_renumbers_constraints``,
      ``test_a_table_that_is_its_own_parent_survives_its_own_rebuild``,
      ``test_a_clean_run_reports_nothing_and_restores_enforcement``,
      ``test_a_connection_whose_restore_failed_never_goes_back_to_the_pool``,
      ``test_a_violation_the_run_committed_is_reported_even_if_the_restore_fails``,
      ``test_no_statement_in_the_guard_can_leave_the_pool_disarmed``,
      ``test_the_one_fail_open_warns_about_the_run_rather_than_excusing_it``,
      ``test_the_guard_never_replaces_the_migration_failure_it_is_unwinding``,
      ``test_a_discard_that_cannot_happen_is_reported_not_raised``,
      ``test_a_failed_in_memory_check_is_reported_and_not_swallowed``,
      ``test_an_interrupt_on_the_exit_path_is_not_swallowed``,
      ``test_an_interrupted_migration_surfaces_the_interrupt``,
      ``test_the_operators_interrupt_wins_not_the_one_the_guard_provoked``,
      ``test_rolling_back_past_the_session_rebuild_keeps_the_participants``
      (as: parent with an ``ON DELETE CASCADE`` child, rebuild the parent,
      assert the children survive),
      ``test_rolling_the_title_column_back_does_not_unname_the_testimony``
      (the headline data-loss path -- already ported as
      ``test_a_rebuild_does_not_strip_the_child_rows_on_a_schema_built_here``),
      and ``test_a_failure_inside_the_guards_own_exit_still_restores_enforcement``
      -- **do not drop this one**: it is the only evidence for fitness case 5b,
      the documented fail-open, and an earlier version of this list omitted it.

      ``test_the_invariant_holds_on_a_schema_the_guard_has_never_seen`` and
      ``test_a_rebuild_does_not_strip_the_child_rows_on_a_schema_built_here``
      are the worked examples of the rewrite, already done here and already
      copyable. Three others -- the two renumbering tests and the
      self-referential one -- already build their own tables and use the fixture
      only for an engine, so they are nearly copies today.

      **(b) Needs a real app + migrations stack; ship as a test a build must
      instantiate.** Two.
      ``test_a_guard_failure_stops_a_real_migration_and_says_why`` must assert
      that a guard ``RuntimeError`` reaches the operator as a non-zero exit
      through the migration runner -- that chain is the thing under test and a
      direct call cannot stand in for it.
      ``test_a_run_that_rebuilt_a_table_gives_the_connection_back_enforcing``
      must assert enforcement across the pool after a real up/down/up over the
      build's own revisions. Both should ship as documented stubs stating what to
      assert, not as code that silently passes.

      **(c) Specific to this build; should not travel.** One.
      ``test_running_the_migrations_leaves_nothing_for_autogenerate_to_add``
      compares this build's models to its migrations and says nothing about the
      guard.

      **Also needs re-pointing, though it copies as code:**
      ``test_the_coverage_annotations_are_true`` is name-coupled to three of the
      24 through its probe table. Copied unchanged into a build that renamed
      them, its probes name tests that no longer exist -- which is caught loudly
      only because that check now requires pytest to exit 1 rather than nonzero.
      Re-point the probe table when you re-fixture.

      This classification is asserted complete by
      ``test_the_port_classification_names_every_fixture_bound_test``; an earlier
      version enumerated 21 of them and the two it dropped were load-bearing.

      **Every count in these notes that is not asserted has been wrong at
      least once.** Five were corrected in one round: 31/8, 63, 16, and two
      stale 23s that sat outside the count regex. The three that are
      asserted (`test_the_port_classification_names_every_fixture_bound_test`)
      have not drifted since. Treat any bare number here as indicative.

      **A mutation harness needs a restore path that runs on signals.**
      Three separate agents on this build left a source file mutated when a
      tool timeout SIGTERM'd them mid-batch; a `finally` does not run on
      SIGTERM. Restore from a private pre-mutation copy, verify by digest,
      and install a signal handler -- not just a happy-path cleanup.

      **The suite's own guard-rails can be deleted, and that is documented
      rather than defended.** `test_the_suite_still_has_its_keystone` asserts
      that every parametrized table is non-empty and every meta test still
      exists, and names itself so its own removal is not silent; the
      classification test's count assertion catches the keystone going missing,
      so the two cover each other. Deleting **both together** is green -- measured, 66 passed.
      No arrangement of tests inside one file prevents that -- which is why the
      port checklist, read by a human, is the backstop rather than another test.

      **And the audit will not catch you.** ``template-python-flask``'s
      ``SQLITE-BATCH-MIGRATION`` check short-circuits the moment the substring
      ``foreign_keys_suspended`` appears in ``env.py`` -- "when env.py uses it,
      trust it". So a build that copies this helper's *name* and none of its
      tests gets a green audit and a green suite, having proved nothing. The
      check enforces that the guard is *called*; only the tests above enforce
      that it *works*. That gap cannot be closed from the audit side, which is
      why it is stated here.

      One further note for whoever ports: a test that borrows the standard
      fixture can only confirm the standard case. ``WITHOUT ROWID``, the
      self-referential foreign key and both renumbering cases were all found by
      building the shape rather than borrowing one, so the schema-agnostic form
      is the better test, not a weakened substitute.
    - **``foreign_key_check`` covers the ``main`` database only.** A build that
      ``ATTACH``es another database gets a clean report while the attached one
      holds violations. Not handled: nothing here attaches, and doing it properly
      means enumerating ``database_list`` and checking each, which is cost paid
      by every build for a case almost none have.
    - **Offline mode is a no-op.** ``alembic upgrade --sql`` has no connection to
      suspend anything on; the guard returns immediately rather than rendering
      pragmas into a script someone may apply elsewhere.
    - **A second writer's orphan is attributed to the migration.**
      ``foreign_key_check`` scans the whole database, not this run's work, so a
      violation another connection commits mid-run is indistinguishable from one
      the rebuild caused. The baseline removes what predates the run, not what
      happens beside it.
    - **On a Postgres-only build most of this is inert.** Everything is a no-op
      off SQLite and the SQLite-specific tests skip, so the behaviour that
      matters is not exercised. 14 test functions take no `migrated_app`, so
      they run once rather than per backend, and that number is asserted.
      Three fixture-bound tests also execute against Postgres (the
      autogenerate check and the two seeded rollbacks), so 17 run there in
      total -- that figure is measured, not asserted, and will drift. The 14
      are
      the offline no-op, the in-memory detection, the `creator=` case, the
      attached-database filter, the schema-agnostic invariant walk and rebuild,
      and the source-level checks -- so the suite is not entirely vacuous there,
      but a green tick on Postgres alone is not evidence the suspension works.
      (Both counts in this note are asserted by
      ``test_the_port_classification_names_every_fixture_bound_test``; every
      earlier hand-written count in these notes went stale, twice.)
    """
    if connection is None or not hasattr(connection, "exec_driver_sql"):
        # Alembic's offline mode (`alembic upgrade --sql`) renders SQL to stdout
        # instead of executing it. `context.get_bind()` there returns a
        # `MockConnection`, NOT None -- measured on alembic 1.19.0 -- and its
        # `.dialect.name` is `sqlite`, so a dialect check alone sails straight
        # into the online path. What it lacks is `exec_driver_sql`, so that is
        # what this asks about: the capability the guard needs, rather than a
        # sentinel value that is never produced.
        #
        # Measured before this was a duck-type check: the guard raised
        # `AttributeError` and, on the way out, logged three ERROR lines
        # including the whole "this run may have left foreign key violations"
        # warning -- on a run that never touched a database.
        #
        # There is nothing to suspend and nothing to restore, and emitting a
        # pragma into the generated script would be worse than useless: a DBA may
        # apply that script to a different engine than the one it was rendered
        # against. (It emits none today only because `MockConnection` cannot
        # execute -- by accident, not by design. This makes it deliberate.)
        #
        # `migrations/env.py` here calls the guard only from
        # `run_migrations_online()`, so this build cannot reach it; a build that
        # passes `context.get_bind()` reaches it on its first `--sql` run.
        yield
        return

    if connection.dialect.name != "sqlite":
        yield
        return

    # What was already broken before the run started. `foreign_key_check` scans
    # the whole database rather than the run's work, so without this the report
    # blames the migration for an orphan that predates it -- and, since such a
    # row is never cleaned up, fails every migration from then on. `app/cli.py`'s
    # `flask bootstrap` calls `upgrade()`, so that is a release command that can
    # never be run again on that database.
    #
    # Taken before the suspension, so a failure here leaves enforcement untouched
    # and there is nothing to restore. Best-effort, though: a baseline that
    # cannot be taken is a reason to stop *attributing*, not a reason to refuse
    # the migration -- refusing would be a new way to wedge the same deploy this
    # baseline exists to unwedge.
    try:
        pre_existing = _violations_by_constraint(
            connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        )
    except Exception:
        pre_existing = None

    # ---- from here to the `finally`, enforcement may be off ----
    #
    # THE INVARIANT: no fallible statement may leave enforcement off without a
    # path that restores or discards. Everything that can disarm this connection
    # -- the suspension itself, its read-back, and the run -- is inside this
    # `try`, so the `finally` runs on every way out, including a raise from the
    # very first statement.
    #
    # Nothing may move above this line on the argument that it "cannot fail" or
    # "has not disarmed anything yet". Two separate rounds of this guard shipped
    # a defect that was exactly one statement sitting on the wrong side of it,
    # once at each end, and neither was a case anyone had thought of. The test
    # that holds this is not a list of situations: it fails each of these
    # statements in turn and asks the pool the same question every time.
    #
    # THE WALK COVERS SQL STATEMENTS ONLY. It injects failures by shadowing
    # `exec_driver_sql` and cross-checks against SQLite's trace callback, so a
    # step that issues no SQL is invisible to both and the walk stays green
    # without it. Every non-SQL step here therefore owes a test of its own.
    #
    # That list is NOT maintained here. It used to be, and it went stale in a
    # single commit: a fourth non-SQL step was added directly beneath a comment
    # reading "There are three, and each has one". Six rounds have now added a
    # step to this function outside the protection the previous round
    # established, so the list is derived from this source instead, by
    # `test_every_step_the_guard_takes_is_accounted_for`. Add an operation on
    # `connection`, or a `raise`, and that test fails and names it.
    #
    # The sharpest reason it matters: `sqlite3.Connection.setconfig(
    # SQLITE_DBCONFIG_ENABLE_FKEY, False)` disarms enforcement while emitting no
    # SQL whatsoever, so a guard keeping all six statements below and adding that
    # one line would pass the entire walk.
    failed = False
    try:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        if connection.exec_driver_sql("PRAGMA foreign_keys").scalar():
            raise RuntimeError(
                "PRAGMA foreign_keys=OFF did not take before the migration run (a "
                "transaction was already open on this connection); a batch rebuild "
                "would have run under enforcement and stripped rows from other tables."
            )
        yield
    except BaseException:
        failed = True
        raise
    finally:
        # The restore goes first, and nothing that can fail is allowed ahead of
        # it. Reporting first looks harmless and is not: a report that raises
        # would skip the restore, the read-back and the `invalidate()` below, and
        # the connection would then close normally back into the pool with
        # enforcement off -- trading a loud problem for a silent one, which is
        # the wrong direction for every defect this guard has had.
        #
        # And every statement here is caught, because a migration that failed by
        # losing its connection makes all of them fail too -- an unguarded
        # `PRAGMA` in this block surfaces "Cannot operate on a closed database"
        # in place of the error that actually broke the run.
        #
        # `BaseException`, not `Exception`: a `KeyboardInterrupt` arriving during
        # the restore is not a reason to skip the discard below and hand the pool
        # a connection that is not enforcing. It is re-raised after the discard,
        # so it still reaches the operator -- caught, not swallowed.
        problems = []   # logged, and raised when the run itself succeeded
        notes = []      # logged only: already true before this run started
        restored = False
        interrupt = None
        try:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            restored = bool(connection.exec_driver_sql("PRAGMA foreign_keys").scalar())
            if not restored:
                problems.append(
                    "PRAGMA foreign_keys=ON did not take after the migration run "
                    "(a transaction was still open)."
                )
        except BaseException as exc:
            # No `interrupt is None` here: this is the first of the three
            # assignment sites, so it is None by construction. The guard the
            # other two carry would be dead code, and its comment -- about a
            # later handler overwriting an earlier one -- cannot apply at the
            # first one.
            if not isinstance(exc, Exception):
                interrupt = exc
            problems.append(
                f"PRAGMA foreign_keys=ON could not be run after the migration run "
                f"({type(exc).__name__}: {exc})."
            )

        # Run whether or not the restore took. `invalidate()` has not happened
        # yet, so the check is still runnable on either path -- and a rebuild
        # COMMITS, so a violation it left is on disk and outlives the connection
        # being dropped. An earlier version skipped this when the restore failed,
        # reasoning that the still-open transaction was about to be rolled back.
        # That is true of the uncommitted rows and false of the committed ones,
        # and it silently lost the row naming damage that survives. Measured both
        # ways.
        #
        # It can also report a violation sitting in the doomed transaction, which
        # will not survive. That is acceptable in a way the reverse is not: the
        # restore having failed means `problems` is already non-empty and the run
        # is already failing, so an extra line can only add detail. It can never
        # turn a clean run red.
        try:
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        except BaseException as exc:
            if not isinstance(exc, Exception) and interrupt is None:
                # The FIRST interrupt wins. A later handler overwriting it would
                # hand back the one this guard provoked while unwinding, not the
                # one the operator sent.
                interrupt = exc
            unverified = (
                f"PRAGMA foreign_key_check could not be run after the migration run "
                f"({type(exc).__name__}: {exc}), so what the suspension let through "
                f"is unknown."
            )
            if pre_existing is None:
                # The one fail-open in this guard, taken deliberately: the check
                # was already broken before this run, so failing would block every
                # future migration on a condition none of them caused -- the same
                # trap the baseline above exists to avoid.
                #
                # But it is a fail-open on a guard that exists because data loss
                # here is silent, so the message leads with the risk and not the
                # excuse. The run exits 0; this line is the only signal anyone
                # gets, which is why it says what to do about it.
                notes.append(
                    unverified + " It could not be run before the migration either, "
                    "so the migration has NOT been failed over it -- which means "
                    "this run may have left foreign key violations that nothing "
                    "detected, including rows silently stripped from another table. "
                    "Treat this database's contents as unverified and repair its "
                    "foreign key definitions. This is logged at ERROR and the "
                    "process still exits 0, so alert on this line: the exit status "
                    "will not carry it."
                )
            else:
                problems.append(unverified)
        else:
            # Only what this run added, counted per (child, parent) table pair
            # and not per row.
            #
            # `foreign_key_check` reports four fields and two of them renumber
            # when a table is rebuilt, so neither can be part of a key that has
            # to survive one. The **rowid** is stable only where the primary key
            # is an `INTEGER PRIMARY KEY` alias -- measured, 3,4,5 became 1,2,3
            # and every pre-existing orphan was reported as new, forever, on a
            # release command. The **constraint** field is `fkid`, an index into
            # `foreign_key_list` in reverse declaration order, so a batch
            # `drop_column` on any FK-bearing column shuffles the rest down --
            # measured, ('child','pa',1) became ('child','pa',0) and a deploy
            # exited 1 after the migration had committed.
            #
            # What survives a rebuild is the two table names, so that is the key.
            #
            # Three costs, and they run in both directions.
            #
            # FALSE NEGATIVES, from coarsening: changes leaving a pair's count
            # unchanged are invisible. A run that repairs one violation between a
            # child and parent and creates another nets to zero; so does a
            # violation that merely moves between rows of the same pair.
            #
            # A FALSE POSITIVE, from the key itself: `op.rename_table()` on a
            # parent changes the key without changing the violation, so every
            # pre-existing orphan pointing at it is reported as new. Measured --
            # `('ch','pa')` became `('ch','parent_v2')` and the run exited 1 after
            # committing. Self-clearing on the next run, like the undetermined
            # branch, but a real failing deploy on an ordinary Alembic operation.
            # Not fixable from this pragma: it reports table names, and a rename
            # is indistinguishable from a different table.
            #
            # Row identity that survives a rebuild is not on offer here, so the
            # alternative to this key is not a finer one -- it is a key that is
            # wrong on other people's schemas, which is what the last two rounds
            # removed.
            found = _violations_by_constraint(violations)
            if pre_existing is None:
                new = found
                blame = (
                    f"The database has {sum(new.values())} foreign key violation(s) "
                    f"after the migration run, and no baseline could be taken "
                    f"before it, so whether the run caused them is unknown"
                )
            else:
                new = found - pre_existing
                blame = (
                    f"The migration run left {sum(new.values())} new foreign key "
                    f"violation(s)"
                )
            if new:
                # Counts by constraint decide; the rows are given as diagnostics
                # only, and may include violations that predate the run.
                problems.append(
                    f"{blame}, as (table, parent): "
                    f"{sorted(new.elements())}. Rows currently violating, which "
                    f"may include some the run did not cause, as "
                    f"(table, rowid, parent, constraint): "
                    f"{[tuple(row) for row in violations]}."
                )

        if not restored:
            # The read-back above has to come BEFORE this -- but not, as an
            # earlier version of this comment had it, because a later read would
            # silently open a fresh connection and answer about that one instead.
            # Measured: it cannot answer at all. The guard's own pragmas have
            # already autobegun a transaction, so after `invalidate()` the next
            # execute raises `PendingRollbackError: Can't reconnect until invalid
            # transaction is rolled back`. SQLAlchemy's silent-reconnect path is
            # unreachable from here. The ordering is required either way; this is
            # the reason it is required.
            #
            # Dropped rather than returned, because a connection that is not
            # enforcing must never reach whoever checks out next; the pool opens
            # a replacement that the listener arms.
            #
            # Caught like everything else here: this is the only statement in the
            # block that is not SQL, and it was the only one outside a `try`. On a
            # `Connection` already closed by the run it raises `ResourceClosedError`,
            # which would replace the migration's own error and skip the reporting
            # below -- the same defect as the pragmas, in the one place that did
            # not look like a pragma.
            try:
                keeps_the_database = _database_lives_in_the_connection(connection)
            except BaseException as exc:
                # Cannot tell, so discard: a connection that will not answer a
                # pragma is broken, a broken connection has already lost an
                # in-memory database, and discarding still protects the pool
                # everywhere else.
                keeps_the_database = False
                # Reported like every other step here. This one decides whether
                # to destroy the database, and its failure means that decision
                # was made blind -- on an in-memory database the operator gets a
                # deleted schema and, without this line, is told only that a
                # connection was discarded.
                problems.append(
                    f"Could not determine whether this database lives in the "
                    f"connection ({type(exc).__name__}: {exc}); it has been "
                    f"treated as file-backed and discarded, which deletes an "
                    f"in-memory database."
                )
                # ...and an interrupt is still an interrupt, exactly as in the
                # two handlers above. Without this it is swallowed and a
                # RuntimeError reaches the caller instead.
                if not isinstance(exc, Exception) and interrupt is None:
                    interrupt = exc

            if keeps_the_database:
                # On an in-memory database the discard IS the data loss: the
                # schema this migration just built lives in the connection, so
                # dropping it deletes everything. Measured -- the table is gone,
                # under the default pool and under StaticPool alike.
                #
                # So it is not discarded here, and the tradeoff is real rather
                # than avoided. Keeping it means a connection that may not be
                # enforcing stays reachable, and under `StaticPool` it is *the*
                # connection every later checkout gets. Against that: an
                # in-memory database is a test or dev process that can be
                # restarted, whereas destroying the schema is irreversible and
                # turns every later test into a "no such table" that someone will
                # catch and mistake for a clean run.
                #
                # The report is always LOGGED at ERROR. It is raised only when
                # the run itself succeeded -- if the migration was already
                # failing, `problems` is suppressed so the operator reads the
                # real error first, and then this warning exists only in the log.
                # An earlier version of this comment claimed the path "always
                # raises, so the failure is announced rather than silent", and
                # used that as one of three reasons for keeping the connection.
                # It is not true on that branch, so it is not a reason.
                #
                # (Considered and not taken: rolling back to make the restore
                # succeed. It would resolve the tension, but it discards the
                # run's uncommitted work on a path that is not always fatal, and
                # this is not the round to add a new recovery path.)
                problems.append(
                    "The connection has NOT been discarded, because this is an "
                    "in-memory database and discarding it would delete the schema "
                    "the migration just built; foreign key enforcement may be off "
                    "on it for the rest of this process, so restart rather than "
                    "carrying on."
                )
            else:
                try:
                    connection.invalidate()
                except BaseException as exc:
                    problems.append(
                        f"Enforcement was not confirmed back on and the connection "
                        f"could not be discarded either ({type(exc).__name__}: {exc}); "
                        f"it may have returned to the pool unprotected."
                    )
                else:
                    problems.append(
                        "The connection has been discarded rather than returned to "
                        "the pool unprotected."
                    )

        for message in notes + problems:
            _log.error("%s", message)
        # An interrupt caught above is re-raised now that the connection is safe
        # -- caught in order to finish the discard, not in order to suppress it.
        #
        # Not "never swallowed", which would be false: when the run itself
        # already failed, the interrupt is dropped and the original failure wins.
        # That is the same rule the RuntimeError below follows -- this guard does
        # not replace the error that broke the migration -- and the case is an
        # interrupt arriving during the exit path of a run that was failing
        # anyway. Deliberate, and narrow, and stated rather than implied.
        if interrupt is not None and not failed:
            raise interrupt
        # Not raised over a migration failure that is already propagating: that
        # one is what the operator needs to read first, and it is likely why
        # these symptoms exist at all.
        if problems and not failed:
            raise RuntimeError(" ".join(problems))
