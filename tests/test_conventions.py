import os

from app.conventions_audit import audit, check_sqlite_foreign_keys


def test_no_convention_violations():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    violations = audit(repo_root)
    assert not violations, "Convention violations found:\n" + "\n".join(str(v) for v in violations)


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
