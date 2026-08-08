import ast
import io
import os
import pathlib
import tokenize

from app.conventions_audit import (
    audit,
    check_sqlite_foreign_keys,
    check_sqlite_batch_migration_guard,
)

# The oldest interpreter CI runs. Keep this equal to the lowest entry in the
# matrix in .github/workflows/test.yml; the check below is only as true as this
# number.
OLDEST_PYTHON_CI_RUNS = (3, 11)

# Directories that are not this repo's source, so a dependency written for a
# newer Python is not this repo's problem.
NOT_OUR_SOURCE = {
    "venv", ".venv", "env", ".tox", "node_modules", "__pycache__", ".git",
    "build", "dist", ".pytest_cache", ".mypy_cache",
}


def test_no_convention_violations():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    violations = audit(repo_root)
    assert not violations, "Convention violations found:\n" + "\n".join(str(v) for v in violations)


def test_no_source_file_here_needs_a_newer_python_than_ci_runs():
    """Local green must not mean less than CI green.

    A multi-line f-string replacement field is PEP 701 -- valid on 3.12, a syntax
    error on 3.11 -- so it parses on a 3.12 workstation, passes every local run,
    and turns CI red on push. That happened on the build this guard came from and
    stood for a commit, because the only interpreter on that machine was 3.12.

    Two passes, because neither is sufficient. `ast.parse(feature_version=...)`
    catches the PEP 695 family and anything else the grammar gained after the
    floor, but it explicitly ignores `feature_version` for f-strings, so it
    cannot see the case that actually happened. The tokenize pass catches that
    one.

    Every tracked `.py` file, not a list. The originating version named three
    files, which is a check that goes quietly out of date the moment someone adds
    a fourth -- and the whole failure mode here is a green run that is not
    checking what its reader thinks.

    This is a tripwire for one construct family, not evidence of compatibility
    with the floor. The only thing that establishes that is running the suite on
    that interpreter, which is what the CI matrix is for.
    """
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    sources = [
        path for path in repo_root.rglob("*.py")
        if not NOT_OUR_SOURCE & set(path.relative_to(repo_root).parts)
    ]
    assert sources, (
        "no Python sources were discovered, so this check would pass vacuously "
        f"over {repo_root}"
    )

    unparseable = []
    offenders = []
    for path in sources:
        name = path.relative_to(repo_root)
        text = path.read_text(encoding="utf-8")
        try:
            ast.parse(text, feature_version=OLDEST_PYTHON_CI_RUNS)
        except SyntaxError as exc:
            unparseable.append(f"{name}:{exc.lineno}: {exc.msg}")
            continue  # tokenizing a file that will not parse adds noise, not news
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
        for index, token in enumerate(tokens):
            if token.type != getattr(tokenize, "FSTRING_START", None):
                continue
            if token.string.endswith(('"""', "'''")):
                continue  # a triple-quoted f-string may legally span lines
            for later in tokens[index:]:
                if later.type == tokenize.FSTRING_END:
                    if later.start[0] != token.start[0]:
                        offenders.append(f"{name}:{token.start[0]}-{later.start[0]}")
                    break

    floor = ".".join(str(part) for part in OLDEST_PYTHON_CI_RUNS)
    assert not unparseable, (
        f"these do not parse under Python {floor}, which CI pins: {unparseable}"
    )
    assert not offenders, (
        f"single-quoted f-string spanning multiple lines (PEP 701, Python 3.12+), "
        f"but CI pins {floor} -- these will not compile there: {offenders}"
    )


def _write(app_root, rel, text):
    path = os.path.join(app_root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def test_sqlite_fk_check_flags_orm_without_pragma(tmp_path):
    root = str(tmp_path)
    _write(root, "app/extensions.py",
           "from flask_sqlalchemy import SQLAlchemy\ndb = SQLAlchemy()\n")
    violations = check_sqlite_foreign_keys(root)
    assert len(violations) == 1
    assert violations[0].rule == "SQLITE-FK"
    assert violations[0].file == os.path.join("app", "extensions.py")


def test_sqlite_fk_check_passes_with_pragma(tmp_path):
    root = str(tmp_path)
    _write(root, "app/extensions.py",
           "from flask_sqlalchemy import SQLAlchemy\n"
           "from sqlalchemy import event\n"
           "from sqlalchemy.engine import Engine\n"
           "db = SQLAlchemy()\n\n\n"
           "@event.listens_for(Engine, 'connect')\n"
           "def _fk(conn, _r):\n"
           "    conn.cursor().execute('PRAGMA foreign_keys=ON')\n")
    assert check_sqlite_foreign_keys(root) == []


def test_sqlite_fk_check_noop_without_orm(tmp_path):
    root = str(tmp_path)
    _write(root, "app/__init__.py", "def create_app():\n    return None\n")
    assert check_sqlite_foreign_keys(root) == []


def test_batch_migration_check_flags_missing_guard(tmp_path):
    root = str(tmp_path)
    _write(root, "migrations/env.py", "def run_migrations_online():\n    pass\n")
    _write(root, "migrations/versions/0001_x.py",
           "def upgrade():\n    with op.batch_alter_table('participant') as b:\n        b.drop_column('x')\n")
    violations = check_sqlite_batch_migration_guard(root)
    assert len(violations) == 1
    assert violations[0].rule == "SQLITE-BATCH-MIGRATION"


def test_batch_migration_check_passes_with_guard(tmp_path):
    root = str(tmp_path)
    _write(root, "migrations/env.py",
           "def run_migrations_online():\n"
           "    with sqlite_foreign_keys_suspended(connection):\n"
           "        conn.exec_driver_sql('PRAGMA foreign_keys=OFF')\n"
           "        conn.exec_driver_sql('PRAGMA foreign_key_check')\n"
           "        run_migrations()\n")
    _write(root, "migrations/versions/0001_x.py",
           "def upgrade():\n    with op.batch_alter_table('participant') as b:\n        b.drop_column('x')\n")
    assert check_sqlite_batch_migration_guard(root) == []


def test_batch_migration_check_noop_without_batch(tmp_path):
    root = str(tmp_path)
    _write(root, "migrations/env.py", "def run_migrations_online():\n    pass\n")
    _write(root, "migrations/versions/0001_x.py",
           "def upgrade():\n    op.add_column('participant', 'x')\n")
    assert check_sqlite_batch_migration_guard(root) == []


def test_batch_migration_check_noop_without_migrations(tmp_path):
    root = str(tmp_path)
    _write(root, "app/__init__.py", "x = 1\n")
    assert check_sqlite_batch_migration_guard(root) == []


def test_batch_migration_check_flags_inline_suspend_without_check(tmp_path):
    root = str(tmp_path)
    _write(root, "migrations/env.py",
           "def run_migrations_online():\n"
           "    conn.exec_driver_sql('PRAGMA foreign_keys=OFF')\n"
           "    run_migrations()\n")
    _write(root, "migrations/versions/0001_x.py",
           "def upgrade():\n    with op.batch_alter_table('participant') as b:\n        b.drop_column('x')\n")
    v = check_sqlite_batch_migration_guard(root)
    assert len(v) == 1
    assert v[0].rule == "SQLITE-BATCH-MIGRATION"
    assert "foreign_key_check" in v[0].message
