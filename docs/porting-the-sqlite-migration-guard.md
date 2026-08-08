# Porting the SQLite migration guard into a build

The guard ships at `app/sqlite_migration_guard.py` with tests at
`tests/test_sqlite_migration_guard.py`. This checklist is the backstop that no
test can be, and it is read by a human on purpose: the originating build proved
that a suite can lose its own guard-rails and stay green.

## Why a checklist and not another test

`SQLITE-BATCH-MIGRATION` in `app/conventions_audit.py` asserts the guard is
**called**. It cannot tell a correct implementation from a broken one, and it
short-circuits the moment the substring `foreign_keys_suspended` appears in
`migrations/env.py`. So a build that copies the helper's *name* and none of its
tests gets a green audit and a green suite, having proved nothing.

That gap cannot be closed from the audit side. This file is what closes it.

## Steps

- [ ] **Wire the call into `migrations/env.py`**, wrapping `run_migrations()`:

      from app.sqlite_migration_guard import sqlite_foreign_keys_suspended

      with context.begin_transaction():
          with sqlite_foreign_keys_suspended(connection):
              context.run_migrations()

- [ ] **Re-add it after any `flask db init`.** Init regenerates `env.py` from
      Flask-Migrate's own template, which wraps `run_migrations()` with no guard.
      The template cannot ship that half. `SQLITE-BATCH-MIGRATION` is what
      catches a regenerated file.

- [ ] **Wire the other half.** The guard *restores* enforcement; it never
      establishes it. SQLite's default is OFF. Register an Engine `connect`
      listener setting `PRAGMA foreign_keys=ON`; `SQLITE-FK` carries the
      canonical form. Without it the guard has nothing to put back, and the
      shipped tests demonstrate exactly that failure.

- [ ] **Instantiate the two skipped tests** at the bottom of the test file.
      They are skips with instructions, not unfinished work, and they name the
      two properties the schema-agnostic tests structurally cannot establish:
      that a guard failure reaches the operator as a non-zero exit through your
      migration runner, and that the pool comes back enforcing after a real
      up/down/up over your own revisions.

- [ ] **Do not delete a skipped test to make the suite tidy.** Deleting one is
      invisible in a green run. `test_the_two_build_specific_stubs_are_still_here`
      exists because that is the likeliest thing to happen to them.

- [ ] **Check the guard's tests actually ran.** They `importorskip`
      SQLAlchemy, so a missing dependency skips them silently and ships the
      guard unproven while the audit reports it present.
      `test_the_guard_tests_actually_ran` fails in CI if that happens. Keep it.

- [ ] **Run the suite on the interpreter your CI pins**, not only the one on
      your machine. The originating build shipped a guard that had never
      executed on its CI interpreter, and separately shipped two seam guards
      that *errored* rather than failed there, so a green suite was reporting
      checks that never ran.

## What is knowingly not proven here

The schema-agnostic tests hand-write Alembic's move-and-copy. That is what lets
them travel, and it means they no longer pin that Alembic's batch mode still
rebuilds tables the way this guard assumes. The two instantiate-me tests are the
only things that pin it. If you skip them, say so in your acceptance audit
rather than leaving the gap silent.

## Counts

Do not trust a count in prose, here or anywhere. The originating build corrected
five wrong counts in its own reuse notes in a single round, and the only ones
that never drifted were the ones asserted by a test. Read the file.
