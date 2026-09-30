# Review: a per-run Postgres test database in the template's conftest (learning L86)

ERW version: 2.6 (activation record 0d7d0a8cede6, https://github.com/Ox805/build_my_app/pull/15#issuecomment-5800547202)
Overrides in force: none
Consequence tier and why: M: the worst credible defect is the fixture dropping a database it did not create, or leaving two runs on one database (the defect it exists to remove). Independent controls outside the change: `TEST_DATABASE_URL` already names a database every existing build fixture empties with `drop_all()` on every test, so it holds nothing a test run may not destroy; the variable is only ever set to a local or CI service server; nothing reaches the template's main, or any build generated from it, without Tim's merge.
ERW Level: 2 (the change issues `CREATE DATABASE` and `DROP DATABASE`; destructive operations default to Level 2)
Characterization: not triggered, because the Postgres behaviour relied on (`CREATE DATABASE` / `DROP DATABASE` outside a transaction, the 63-byte identifier limit, `pg_terminate_backend`) is long-stable documented behaviour, and the implementation's Postgres leg exercises each of them against a real server.
Branch: erw/per-run-test-database
Design commit: 6de5212 (the design as first pushed; this line added after)
Implementation: filled at step 4
PR: #1 (https://github.com/bma-dev-team/template-python-flask/pull/1)
CI targets: tests/test_per_run_database.py tests/test_absent_leg_reporting.py (the template's workflow has no targeted mode and skips Draft PRs; each Draft head is run in full by `workflow_dispatch` on this branch, and that run is the disposition)

## Objective

Make test-database isolation structural in every build generated from this
template: a suite that uses Postgres gets a database of its own, created for
the run and dropped after it, instead of reading one shared
`TEST_DATABASE_URL` that every concurrent run and every other process on the
machine also uses.

## Problem being solved

Learning L86 (deposition build, 2026-09-02). Two Claude Code sessions on one
machine used the same `TEST_DATABASE_URL`. One ran the full suite, whose
fixtures `drop_all()` / `create_all()` every few seconds; the other ran
`flask bootstrap` against the same database. For 45 minutes each session's
failures (missing relations, a `drop_all` deadlock, a vanished `session`
table) looked like defects in its own code. The conventions doc
(`BMA_DEVELOPER_CONVENTIONS.md` Section III, main 06ad89fa) already states the
rule: "every concurrently running suite gets its own database name, derived
per run rather than read from a shared `TEST_DATABASE_URL`". A rule that
depends on each build remembering it is the prose-only fix L86 says is not
enough; the template is where every build's conftest starts.

## Runtime characterization (when triggered)
Not triggered (see the header).

## Proposed architecture

One session-scoped fixture, `test_database_url`, in `tests/conftest.py`, with
two small helpers beside it. Build fixtures take their database from it rather
than from the environment.

1. Load `.env` (`find_dotenv(usecwd=True)`), then read `TEST_DATABASE_URL`.
   Loading first is deliberate: the deposition build twice read `os.environ`
   before anything had loaded `.env`, and silently skipped its Postgres leg.
2. Unset: the fixture yields `None` and imports nothing, so a build without a
   database is untouched and needs no ORM or driver.
3. A scheme other than `postgresql` / `postgresql+<driver>` / `postgres`: the
   URL is yielded unchanged. SQLite files are out of scope (a build uses
   `tmp_path` for those).
4. Postgres: derive `<base>_run_<pid>_<6 hex>`, with `<base>` the database
   the URL names, truncated so the whole identifier is at most 63 bytes. Using
   SQLAlchemy (imported only on this path), connect to the base database with
   `AUTOCOMMIT` and run `CREATE DATABASE "<name>"`. Yield the URL with only the
   database component replaced (user, password, host, port and query kept).
5. Teardown, in `finally`, so a failed or interrupted-by-exception run still
   cleans up: terminate backends connected to `<name>` only
   (`WHERE datname = :name AND pid <> pg_backend_pid()`), then
   `DROP DATABASE IF EXISTS "<name>"`.
6. The drop goes through one helper that refuses any name other than the one
   this process created, recorded in module state at creation. It does not
   pattern-match: a name that merely looks like a run database is refused.
7. A drop that fails does not fail the run (the test results stand); it
   prints one line naming the database left behind.

Under pytest-xdist each worker is its own process and its own session, so
each gets its own database.

The README's "Local development" section gains two sentences: set
`TEST_DATABASE_URL` to a server and base database, and take the database
from `test_database_url`, never from the environment.

CI: `.github/workflows/test.yml` gains a `postgres` service, sets
`TEST_DATABASE_URL`, and installs `psycopg[binary]` test-only beside the
`sqlalchemy` it already installs, so the Postgres leg of the new tests runs
in CI rather than skipping.

## Important invariants

- The fixture never drops a database it did not create in this process.
- Two concurrent runs never receive the same database.
- The base database named in `TEST_DATABASE_URL` is only connected to, never
  written to or dropped.
- With the variable unset, the fixture has no side effect and no import.

## Assumptions about platform / API behavior (each points into the matrix)

- `CREATE DATABASE` and `DROP DATABASE` cannot run inside a transaction
  block; SQLAlchemy's `AUTOCOMMIT` isolation level issues them outside one.
- Postgres truncates identifiers over 63 bytes silently (NAMEDATALEN - 1), so
  the name is truncated by the fixture, where the truncation is visible and
  the unique suffix is kept.
- `DROP DATABASE` fails while other sessions are connected; the fixture's own
  engines are disposed and remaining backends on that one database are
  terminated first.
- The role in `TEST_DATABASE_URL` has `CREATEDB` (true of a local superuser
  and of the CI service container's default user). Without it, creation fails
  loudly at session start, naming the privilege.

## Failure modes

- Creation fails (no privilege, server down): the fixture errors at session
  start with the reason; every test that uses it errors rather than skipping.
- The process is killed (SIGKILL, runner timeout): `finally` never runs and a
  `<base>_run_*` database is left. It is never reused, because the next run
  derives a new name; it only occupies space.
- The drop fails: one line names the leftover; results stand.
- A test holds a connection open past teardown: the backend is terminated
  before the drop.

## Failure-direction analysis
- If the mechanism fails: creation failure errors the run (loud); drop
  failure leaves an orphan and says so (loud, harmless).
- If state is stale, missing, corrupt or partially written: an orphan from a
  killed run is never selected again, so no run inherits another's schema.
- If related hooks or services disagree: a build fixture that still reads
  `TEST_DATABASE_URL` directly keeps today's shared behaviour; the README line
  is what points it at the fixture.
- Direction the system should fail: toward an error or a leftover database,
  never toward sharing one or dropping one it did not make.

## Alternatives considered and rejected

- **A schema per run inside the shared database.** Build fixtures call
  `db.drop_all()` / `create_all()` and Alembic, which default to the
  `public` schema; isolating by schema needs `search_path` plumbing in every
  build, and a stray `drop_all` on the default schema still reaches the
  other run.
- **Name from the branch or session id.** Two runs on one branch (a suite
  and a reviewer's mutation run) would still share it; the pid plus random
  suffix does not depend on anything a person sets.
- **A sweep of old `_run_` databases at session start.** Dropping by pattern
  is exactly the operation the invariant forbids, and it would drop a
  concurrent run's live database. Left out; the known limitation below.
- **Postgres template databases (`CREATE DATABASE ... TEMPLATE`).** Faster
  for a migrated schema, but it requires no connections to the template and
  couples the fixture to the build's migration path. A build can add it.

## Files expected to change

- `tests/conftest.py`: the fixture and its two helpers.
- `tests/test_per_run_database.py`: new.
- `.github/workflows/test.yml`: the `postgres` service, `TEST_DATABASE_URL`,
  `psycopg[binary]` test-only.
- `README.md`: two sentences under "Local development".

## Proposed tests

Pure (always run):
1. Two derivations from one URL give two different names, both starting
   `<base>_run_`.
2. A base name of 80 characters gives an identifier of at most 63 bytes that
   still ends in the unique suffix.
3. The derived URL keeps user, password, host, port and query; only the
   database changes.
4. A non-Postgres URL is returned unchanged.
5. Unset variable: an inner pytester session using the fixture receives
   `None`, and `sqlalchemy` is not in `sys.modules` afterwards.
6. The drop helper refuses a name it did not create, including one shaped
   exactly like a run database (`<base>_run_1_abcdef`): the false-positive
   half of the guard.

Postgres leg (runs when `TEST_DATABASE_URL`, `sqlalchemy` and a driver are
present, which CI provides; skips with that reason otherwise):
7. **The L86 incident, reproduced:** two inner pytest sessions started
   concurrently as subprocesses, each creating a table, then `drop_all` in a
   loop while the other checks its own table still exists. Both pass; and a
   control run of the same pair pointed at the shared URL directly fails,
   so the test can tell the difference.
8. After an inner session ends, its database no longer exists
   (`pg_database`), and a sentinel table placed in the base database before
   it ran is still there.
9. After an inner session whose test FAILS, its database no longer exists.
10. With a connection deliberately left open by the inner test, teardown
    still drops the database.

Mutation anchors for the implementation: remove the `finally` drop (8 and 9
die); make the suffix constant (1 and 7 die); remove the drop guard (6 dies);
remove the backend termination (10 dies).

## Unresolved questions

- Whether existing builds (the deposition build is delivered and in
  acceptance) should be patched to use the fixture. Tim decided: template
  only (2026-09-30).

## Self-review (the pre-handoff checks, as they apply to the design)

- Failure direction: loud on creation, a named leftover on drop; never
  a shared or wrongly dropped database.
- Concurrency and cross-session: the whole point; test 7 measures it against
  a control.
- Destructive operations: one `DROP DATABASE`, gated on an exact name this
  process created; test 6 covers the lookalike.
- Residue: nothing superseded; the template has no database fixture today.
- Boundaries: template repository files only; no build repo, nothing
  installed or deployed.
