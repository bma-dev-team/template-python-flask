# Review: a per-run Postgres test database in the template's conftest (learning L86)

ERW version: 2.6 (activation record 0d7d0a8cede6, https://github.com/Ox805/build_my_app/pull/15#issuecomment-5800547202)
Overrides in force: none
Consequence tier and why: M: the worst credible defect is the fixture dropping a database it did not create, or leaving two runs on one database (the defect it exists to remove). The independent control outside the change is the environment boundary: `TEST_DATABASE_URL` is set only to a disposable local or CI service server, whose named database every existing build fixture already empties with `drop_all()` on every test. (The fixture's own drop guard and Tim's merge are not counted as controls, per round 1.)
ERW Level: 2 (the change issues `CREATE DATABASE` and `DROP DATABASE`; destructive operations default to Level 2)
Characterization: not triggered, because the Postgres behaviour relied on (`CREATE DATABASE` / `DROP DATABASE` outside a transaction, the 63-byte identifier limit, `pg_terminate_backend`) is long-stable documented behaviour, and the implementation's Postgres leg exercises each of them against a real server.
Branch: erw/per-run-test-database
Design commit: 6de5212 (round 0); the round 1 revision is the commit adding "Round history"
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
3. A scheme that is not Postgres at all (`sqlite`, `mysql`, ...): the
   URL is yielded unchanged. SQLite files are out of scope (a build uses
   `tmp_path` for those).
4. **Accepted Postgres shapes, checked before any database operation
   (round 1 BLOCKER).** The scheme is `postgresql`, `postgres`,
   `postgresql+psycopg` or `postgresql+psycopg2`; the database is named in
   the URL path and is non-empty; and the query carries none of the keys
   that choose a destination by another route: `dbname`, `database`,
   `service`, `host`, `hostaddr`, `port`. Any other Postgres shape (another
   driver, an empty path, a listed key, even `?dbname=` equal to the path) is
   refused with the reason named, before `CREATE`, and nothing is yielded.
   SQLAlchemy's psycopg dialects build connection arguments from the path and
   then apply `opts.update(url.query)` (`create_connect_args` in
   `dialects/postgresql/_psycopg_common.py`), so a retained `dbname` would
   silently override the substituted path; `service` selects through
   `pg_service.conf`; `host` / `hostaddr` / `port` in the query are libpq
   multi-host selectors. Unrelated settings (`sslmode`, `connect_timeout`,
   `application_name`, `options`) are kept.
5. Derive `<base>_run_<pid>_<6 hex>`, with `<base>` the path database,
   truncated so the whole identifier is at most 63 bytes of UTF-8 and still
   ends in the suffix. Using SQLAlchemy (imported only on this path), connect
   to the base database with `AUTOCOMMIT` and run `CREATE DATABASE`, quoting
   the name with the dialect's identifier preparer. The ownership record is
   written only after `CREATE` succeeds.
6. **Destination identity, verified before yielding.** Build the run URL by
   replacing only the path database, open one connection with it, and require
   `SELECT current_database()` to equal the created name. On a mismatch the
   fixture drops the database it created and errors; it never yields a URL
   whose effective target it has not observed. This is the driver-independent
   backstop for any selector step 4 does not list.
7. Teardown, in `finally`, so a failed or interrupted-by-exception run still
   cleans up: terminate backends connected to `<name>` only
   (`WHERE datname = :name AND pid <> pg_backend_pid()`), then
   `DROP DATABASE IF EXISTS "<name>"`.
8. The drop goes through one helper that refuses any name other than the one
   this process created, recorded in module state at creation. It does not
   pattern-match: a name that merely looks like a run database is refused.
9. A drop that fails does not fail the run (the test results stand); it
   prints one line naming the database left behind.

Under pytest-xdist each worker is its own process and its own session, so
each gets its own database.

The README's "Local development" section gains two sentences: set
`TEST_DATABASE_URL` to a server and base database, and take the database
from `test_database_url`, never from the environment.

CI: `.github/workflows/test.yml` gains a `postgres` service, sets
`TEST_DATABASE_URL`, and installs `psycopg[binary]` test-only beside the
`sqlalchemy` it already installs, so the Postgres leg of the new tests runs
in CI on both matrix Python versions rather than skipping. With `CI` set and
the variable unset, the Postgres-leg tests fail instead of skipping.

## Important invariants

- The fixture never drops a database it did not create in this process.
- Two concurrent runs never receive the same database.
- The base database named in `TEST_DATABASE_URL` is only connected to, never
  written to or dropped.
- The yielded URL's effective connection target has been observed, by
  `current_database()`, to be the run database.
- A URL that could select its database by any route other than the path is
  refused before anything is created.
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
- SQLAlchemy's psycopg and psycopg2 dialects apply `url.query` over the
  path-derived `dbname` (`create_connect_args` in
  `sqlalchemy/dialects/postgresql/_psycopg_common.py`, rel_2_0; cited by the
  round 1 review). The refusal list in step 4 is built from that and from
  libpq's connection keywords; anything it misses is caught by the step 6
  identity check.
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
3. The derived URL keeps user, password, host, port and unrelated query
   settings; only the database changes.
4. A non-Postgres URL is returned unchanged.
4a. Each refused query key (`dbname`, `database`, `service`, `host`,
    `hostaddr`, `port`), including `?dbname=<the same base>`, is refused with
    the key named; `sslmode` and `application_name` are accepted and kept.
    For each accepted shape, `engine.dialect.create_connect_args(url)` on the
    run URL carries the run database as its only database argument (the
    effective DBAPI arguments, not the URL text).
4b. A URL with no database in its path, or a Postgres scheme with an
    unsupported driver, is refused.
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
9a. **The round 1 case against a real server:**
    `TEST_DATABASE_URL=<base URL>?dbname=<base>` is refused, no
    `<base>_run_*` database appears in `pg_database`, and the base sentinel
    table is unchanged. With `?sslmode=disable` instead, `SELECT
    current_database()` through the yielded URL equals the run name. The
    concurrent-run control of test 7 is repeated with `sslmode` in the URL.
9b. The identity backstop fires: with the run-URL builder patched to keep
    the base database, the fixture errors, the created database is dropped,
    and the base sentinel is unchanged.
10. With a connection deliberately left open by the inner test, teardown
    still drops the database.

Mutation anchors for the implementation: remove the `finally` drop (8 and 9
die); make the suffix constant (1 and 7 die); remove the drop guard (6 dies);
remove the backend termination (10 dies); drop `dbname` from the refusal list (4a and 9a die); remove the identity check (9b dies).

## Unresolved questions

- Whether existing builds (the deposition build is delivered and in
  acceptance) should be patched to use the fixture. Tim decided: template
  only (2026-09-30).

## Round history

- Round 1 (review 5367817681, head 5dbde9ff): DESIGN NOT APPROVED, one
  BLOCKER: preserving query parameters let `?dbname=` override the
  substituted database. Answered by architecture steps 4 and 6 (refuse
  destination-selecting keys before CREATE; verify `current_database()`
  before yielding), tests 4a, 4b, 9a, 9b, and a tier line that no longer
  counts the drop guard or the merge as controls.

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
