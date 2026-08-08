#!/usr/bin/env python3
"""Break the migration guard on purpose, and check its tests notice.

Run this after changing `app/sqlite_migration_guard.py`. A green suite says the
tests passed; this says they would have failed had the guard been wrong, which is
a different claim and the one that matters for a guard whose failure mode is
silent data loss.

    python tools/mutate_sqlite_migration_guard.py           # every mutation
    python tools/mutate_sqlite_migration_guard.py M1 M15    # named ones

Output is one line per mutation. `ZOMBIE` means the mutation was applied, the
suite still passed, and the test annotated as covering that behaviour did not
notice -- so either the test or the mutation is not doing what it claims.

WHY THIS EXISTS ALONGSIDE THE SUITE'S OWN PROBES
------------------------------------------------
`test_the_coverage_annotations_are_true` already mutation-checks the four steps
listed in `GUARD_CONNECTION_OPERATIONS`. That runs in CI and is deliberately
narrow: one probe per step, chosen to be cheap. This is the wide version -- the
restore, the read-backs, the baseline, the fail-open wording, the interrupt
precedence at each of its two sites -- and it is a development tool rather than a
test, because a full sweep spawns a pytest run per mutation.

WHY THE RESTORE IS BUILT THE WAY IT IS
--------------------------------------
Three separate agents on the originating build left a source file mutated when a
tool timeout SIGTERM'd them mid-batch. A `finally` does not run on SIGTERM. So:
a private pre-mutation copy outside the tree, a signal handler, and a digest
check on every restore. If the digest ever disagrees, the copy's path is printed
rather than swallowed.

TWO WAYS A MUTATION LIES, both of which have happened here
----------------------------------------------------------
* **It changes nothing.** An early version neutered the baseline with an `or`
  against a falsy value and fell straight through to the real call. It reported a
  surviving test; the test was fine.
* **It changes something equivalent.** Dropping the `name == "main"` filter in
  favour of the first row returns the identical answer on every database, because
  `main` is always seq 0.
Both look exactly like a thorough suite. When a ZOMBIE appears, suspect the
mutation first and the test second.
"""
import hashlib
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
GUARD = ROOT / "app" / "sqlite_migration_guard.py"
SUITE = "tests/test_sqlite_migration_guard.py"

# name -> (what it breaks, find, replace, tests that MUST go red)
MUTATIONS = {
    "M1": (
        "the restore never runs",
        '            connection.exec_driver_sql("PRAGMA foreign_keys=ON")\n',
        "            pass  # MUTANT\n",
        ["test_the_guard_suspends_enforcement_and_puts_it_back",
         "test_a_clean_run_reports_nothing_and_restores_enforcement",
         "test_rows_orphaned_while_enforcement_was_off_are_reported"],
    ),
    "M2": (
        "the OFF is trusted instead of read back",
        '        if connection.exec_driver_sql("PRAGMA foreign_keys").scalar():\n',
        "        if False:  # MUTANT\n",
        ["test_a_suspension_that_did_not_take_is_refused_rather_than_trusted"],
    ),
    "M3": (
        "the disarmed connection goes back to the pool",
        "                    connection.invalidate()\n",
        "                    pass  # MUTANT\n",
        ["test_a_connection_whose_restore_failed_never_goes_back_to_the_pool"],
    ),
    "M4": (
        "the exit check is skipped when the restore failed",
        '            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()\n',
        '            violations = [] if not restored else connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()  # MUTANT\n',
        ["test_a_violation_the_run_committed_is_reported_even_if_the_restore_fails"],
    ),
    "M5": (
        "the exit check never reports anything",
        '            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()\n',
        "            violations = []  # MUTANT\n",
        ["test_rows_orphaned_while_enforcement_was_off_are_reported",
         "test_a_violation_the_run_committed_is_reported_even_if_the_restore_fails"],
    ),
    "M6": (
        "the guard raises its own complaint over the run's failure",
        "        if problems and not failed:\n",
        "        if problems:  # MUTANT\n",
        ["test_an_interrupted_migration_surfaces_the_interrupt",
         "test_the_guard_never_replaces_the_migration_failure_it_is_unwinding"],
    ),
    # The overwrite guard sits at BOTH later assignment sites, so it is mutated
    # one site at a time. Mutating them together lets either half of the
    # parametrization cover for the other -- which is the exact gap the
    # originating build found.
    "M7a": (
        "the exit-check handler overwrites the operator's interrupt",
        "        except BaseException as exc:\n"
        "            if not isinstance(exc, Exception) and interrupt is None:\n",
        "        except BaseException as exc:\n"
        "            if not isinstance(exc, Exception):  # MUTANT\n",
        ["test_the_operators_interrupt_wins_not_the_one_the_guard_provoked"],
    ),
    "M7b": (
        "the in-memory-check handler overwrites the operator's interrupt",
        "                if not isinstance(exc, Exception) and interrupt is None:\n",
        "                if not isinstance(exc, Exception):  # MUTANT\n",
        ["test_the_operators_interrupt_wins_not_the_one_the_guard_provoked"],
    ),
    # Neutered to an EMPTY Counter, not to None. None takes the documented
    # fail-open, which does not raise, so the mutation would be invisible for a
    # reason that has nothing to do with the baseline.
    "M8": (
        "no baseline is taken, so pre-existing orphans are blamed on the run",
        "        pre_existing = _violations_by_constraint(\n"
        '            connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()\n'
        "        )\n",
        "        pre_existing = Counter()  # MUTANT\n",
        ["test_an_orphan_that_predates_the_run_is_not_blamed_on_it"],
    ),
    "M9": (
        "the interrupt caught on the exit path is never given back",
        "        if interrupt is not None and not failed:\n            raise interrupt\n",
        "        if False:  # MUTANT\n            raise interrupt\n",
        ["test_an_interrupt_on_the_exit_path_is_not_swallowed",
         "test_the_operators_interrupt_wins_not_the_one_the_guard_provoked"],
    ),
    "M10": (
        "a discard that could not happen is raised instead of reported",
        "                try:\n                    connection.invalidate()\n"
        "                except BaseException as exc:\n",
        "                try:\n                    connection.invalidate()\n"
        "                except SystemExit as exc:  # MUTANT\n",
        ["test_a_discard_that_cannot_happen_is_reported_not_raised"],
    ),
    "M11": (
        "the blind in-memory decision is made silently",
        '                problems.append(\n                    f"Could not determine whether this database lives in the "\n',
        '                problems.append(  # MUTANT\n                    f"" or f"redacted "\n',
        ["test_a_failed_in_memory_check_is_reported_and_not_swallowed"],
    ),
    "M12": (
        "the documented fail-open is removed and an unverifiable run is failed",
        '                notes.append(\n                    unverified + " It could not be run before the migration either, "\n',
        '                problems.append(  # MUTANT\n                    unverified + " It could not be run before the migration either, "\n',
        ["test_a_failure_inside_the_guards_own_exit_still_restores_enforcement"],
    ),
    "M13": (
        "the fail-open reassures about the database instead of warning about the run",
        '                    "this run may have left foreign key violations that nothing "\n',
        '                    "the database was already unverifiable and nothing "  # MUTANT\n',
        ["test_the_one_fail_open_warns_about_the_run_rather_than_excusing_it"],
    ),
    "M14": (
        "the fail-open does not say the exit status will not carry it",
        '                    "process still exits 0, so alert on this line: the exit status "\n',
        '                    "process continues, so alert on this line: the exit status "  # MUTANT\n',
        ["test_the_one_fail_open_warns_about_the_run_rather_than_excusing_it"],
    ),
    "M15": (
        "the suspension silently never takes -- the original data-loss path",
        '        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")\n'
        '        if connection.exec_driver_sql("PRAGMA foreign_keys").scalar():\n',
        '        connection.exec_driver_sql("PRAGMA foreign_keys")  # MUTANT\n'
        "        if False:\n",
        ["test_a_rebuild_of_a_cascade_parent_keeps_the_children",
         "test_a_rebuild_does_not_strip_the_child_rows_on_a_schema_built_here"],
    ),
    "M16": (
        "offline mode is detected by a None sentinel instead of a capability",
        '    if connection is None or not hasattr(connection, "exec_driver_sql"):\n',
        "    if connection is None:  # MUTANT\n",
        ["test_offline_mode_is_a_no_op"],
    ),
    "M17": (
        "an in-memory database is discarded, which deletes it",
        "            if keeps_the_database:\n",
        "            if False:  # MUTANT\n",
        ["test_an_in_memory_database_survives_a_failed_restore"],
    ),
    # NOT "reads the first row": `main` is always seq 0, so dropping the filter
    # in favour of the first row returns the identical answer and the mutation is
    # a no-op. Only a LAST-row reading distinguishes the two.
    "M18": (
        "the detector reads the last attached database instead of main",
        '    for _seq, name, file in connection.exec_driver_sql("PRAGMA database_list"):\n'
        '        if name == "main":\n'
        "            return not file\n",
        "    last = False  # MUTANT\n"
        '    for _seq, name, file in connection.exec_driver_sql("PRAGMA database_list"):\n'
        "        last = not file\n"
        "    return last\n",
        ["test_the_detector_reads_main_and_not_an_attached_database"],
    ),
    "M19": (
        "the detector asks the URL instead of the driver",
        '    for _seq, name, file in connection.exec_driver_sql("PRAGMA database_list"):\n',
        '    return connection.engine.url.database in (None, "", ":memory:")  # MUTANT\n'
        '    for _seq, name, file in connection.exec_driver_sql("PRAGMA database_list"):\n',
        ["test_a_file_reached_through_creator_is_still_discarded"],
    ),
}


def main(argv):
    original = GUARD.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    backup = tempfile.NamedTemporaryFile(delete=False, suffix=".guard.bak").name
    shutil.copyfile(GUARD, backup)

    def restore(*_signal_args):
        shutil.copyfile(backup, GUARD)
        if hashlib.sha256(GUARD.read_bytes()).hexdigest() != digest:
            sys.exit(f"RESTORE FAILED. The pre-mutation copy is at {backup}")

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_a: (restore(), sys.exit(1)))

    wanted = argv or list(MUTATIONS)
    unknown = [name for name in wanted if name not in MUTATIONS]
    if unknown:
        sys.exit(f"unknown mutation(s): {unknown}. Known: {sorted(MUTATIONS)}")

    failures = []
    try:
        for name in wanted:
            what, find, replace, must_die = MUTATIONS[name]
            source = original.decode()
            if source.count(find) != 1:
                print(f"{name:5s} ANCHOR MISS ({source.count(find)}x)  {what}")
                failures.append(
                    f"{name}: its anchor matched {source.count(find)} times, not 1. "
                    f"The guard moved under it; re-point the mutation."
                )
                continue
            GUARD.write_text(source.replace(find, replace))
            run = subprocess.run(
                [sys.executable, "-m", "pytest", SUITE, "-q", "-p", "no:cacheprovider",
                 "--no-header", "--tb=no"],
                cwd=ROOT, capture_output=True, text=True,
            )
            restore()
            died = {
                line.split("::")[1].split()[0].split("[")[0]
                for line in run.stdout.splitlines() if line.startswith("FAILED")
            }
            survived = [t for t in must_die if t not in died]
            print(f"{name:5s} {'OK    ' if not survived else 'ZOMBIE'}  {what}")
            for test in survived:
                print(f"        SURVIVED: {test}")
                failures.append(f"{name}: {test} passed under a mutation it must catch")
    finally:
        restore()

    print()
    print("clean" if not failures else "PROBLEMS:")
    for failure in failures:
        print(" ", failure)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
