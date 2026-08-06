"""Static audit of BMA UX/dev conventions over a build's templates.

Each check maps to a rule id in developer-guides/architecture/BMA_UX_CONVENTIONS.md (BMA repo).
The build's CI runs tests/test_conventions.py, which fails on any violation -- so the conventions
are enforced per build without manual testing. Suppress a specific violation with an inline
marker on the violating line or the line directly above it:

    {# conventions: allow UX-I.B -- AC #7 GET-only filter form #}
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Violation:
    rule: str
    file: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line} [{self.rule}] {self.message}"


_ALLOW_RE = re.compile(r"conventions:\s*allow\s+([A-Za-z0-9.\-]+)")
_FORM_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.DOTALL | re.IGNORECASE)
_BUTTON_RE = re.compile(r"<button\b([^>]*)>", re.IGNORECASE)
_TYPE_RE = re.compile(r'type\s*=\s*"([^"]+)"', re.IGNORECASE)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _allowed(rule: str, lines: list[str], line_no: int) -> bool:
    """True if an `allow <rule>` marker is on the violating line or the line directly above it."""
    for j in (line_no - 1, line_no - 2):  # 0-based: the line itself, then the one above
        if 0 <= j < len(lines):
            m = _ALLOW_RE.search(lines[j])
            if m and m.group(1) == rule:
                return True
    return False


def _is_submit_button(button_attrs: str) -> bool:
    m = _TYPE_RE.search(button_attrs)
    return m is None or m.group(1).lower() == "submit"


def _form_has_submit(body: str) -> bool:
    for bm in _BUTTON_RE.finditer(body):
        if _is_submit_button(bm.group(1)):
            return True
    return bool(re.search(r'<input\b[^>]*type\s*=\s*"submit"', body, re.IGNORECASE))


def check_gated_submit(rel: str, text: str, lines: list[str]) -> list[Violation]:
    """UX-I.B: a POST form with a submit button must carry data-gated-submit (confirm the click)."""
    out = []
    for m in _FORM_RE.finditer(text):
        attrs, body = m.group(1), m.group(2)
        if "post" not in attrs.lower():
            continue
        if not _form_has_submit(body):
            continue
        if "data-gated-submit" in attrs:
            continue
        line = _line_of(text, m.start())
        if _allowed("UX-I.B", lines, line):
            continue
        out.append(Violation(
            "UX-I.B", rel, line,
            "POST form with a submit button lacks data-gated-submit (action buttons must confirm the click)",
        ))
    return out


_ENABLE_BTN_RE = re.compile(r"<button\b[^>]*\bdata-enable-when-filled\b[^>]*>", re.IGNORECASE)
_TS_EXPR_RE = re.compile(r"\{\{[^}]*\b\w*_at\b[^}]*\}\}")


def check_enable_when_filled_disabled(rel: str, text: str, lines: list[str]) -> list[Violation]:
    """UX-I.A: a data-enable-when-filled button must also render `disabled` (no-JS fallback)."""
    out = []
    for m in _ENABLE_BTN_RE.finditer(text):
        if re.search(r"\bdisabled\b", m.group(0)):
            continue
        line = _line_of(text, m.start())
        if _allowed("UX-I.A", lines, line):
            continue
        out.append(Violation(
            "UX-I.A", rel, line,
            "button has data-enable-when-filled but not disabled (must be inactive without JS)",
        ))
    return out


def check_removable_uploads(rel: str, text: str, lines: list[str]) -> list[Violation]:
    """UX-II: a file rendered in an uploaded dropzone must have a remove control in the same file."""
    out = []
    if "dropzone-uploaded" in text and "dropzone-remove" not in text:
        line = _line_of(text, text.index("dropzone-uploaded"))
        if not _allowed("UX-II", lines, line):
            out.append(Violation(
                "UX-II", rel, line,
                "dropzone-uploaded present but no dropzone-remove (uploaded files must be removable in place)",
            ))
    return out


def check_timestamps_wrapped(rel: str, text: str, lines: list[str]) -> list[Violation]:
    """UX-IV: a Jinja timestamp expression (`{{ ..._at }}`) must be inside a <time datetime=...>
    or delegated to the localized_time() macro from _bma_ui.html."""
    out = []
    for m in _TS_EXPR_RE.finditer(text):
        line = _line_of(text, m.start())
        line_text = lines[line - 1] if 0 <= line - 1 < len(lines) else ""
        if "<time" in line_text or 'datetime="' in line_text:
            continue
        if "localized_time(" in line_text:
            continue
        if _allowed("UX-IV", lines, line):
            continue
        out.append(Violation(
            "UX-IV", rel, line,
            "timestamp expression not wrapped in <time datetime=...> (must localize to viewer-local time)",
        ))
    return out


_PER_TEMPLATE_CHECKS = (
    check_gated_submit,
    check_enable_when_filled_disabled,
    check_removable_uploads,
    check_timestamps_wrapped,
)


def check_foundation_scripts(app_root: str) -> list[Violation]:
    """FOUNDATION: base.html must load form_gating.js and localize_timestamps.js."""
    base = os.path.join(app_root, "app", "templates", "base.html")
    if not os.path.isfile(base):
        return []
    with open(base, encoding="utf-8") as fh:
        text = fh.read()
    rel = os.path.relpath(base, app_root)
    out = []
    for fname in ("form_gating.js", "localize_timestamps.js"):
        if fname not in text:
            out.append(Violation("FOUNDATION", rel, 1, f"base.html does not load {fname}"))
    return out


_ORM_USAGE_RE = re.compile(
    r"(?:from\s+flask_sqlalchemy\s+import|import\s+flask_sqlalchemy"
    r"|from\s+sqlalchemy(?:\.\w+)*\s+import|import\s+sqlalchemy"
    r"|SQLAlchemy\s*\()"
)
_FK_PRAGMA_RE = re.compile(r"foreign_keys\s*=\s*on\b", re.IGNORECASE)


def _app_python_files(app_root: str):
    """Yield (relpath, text) for each .py under app/, skipping this auditor
    itself (its own docstring/message name the strings it scans for)."""
    adir = os.path.join(app_root, "app")
    for root, _dirs, files in os.walk(adir):
        for name in sorted(files):
            if not name.endswith(".py") or name == "conventions_audit.py":
                continue
            path = os.path.join(root, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    yield os.path.relpath(path, app_root), fh.read()
            except (OSError, UnicodeDecodeError):
                continue


def check_sqlite_foreign_keys(app_root: str) -> list[Violation]:
    """SQLITE-FK: an app that uses SQLAlchemy must enable SQLite foreign-key
    enforcement on Engine connect, or ON DELETE / passive_deletes silently
    no-op on the SQLite backend (local dev and the SQLite half of the test
    matrix) while Postgres enforces them. That two-backend divergence has
    drawn blood on this platform more than once (an ac-01 truncation 500, an
    ac-03 orphaned-rows-on-delete). No-op for apps without SQLAlchemy, so the
    bare template and any headless build pass untouched.

    Canonical fix (register on the Engine class so it covers app, migrations
    and the test harness alike; it is a no-op on Postgres):

        @event.listens_for(Engine, "connect")
        def _enforce_sqlite_foreign_keys(dbapi_connection, _record):
            if isinstance(dbapi_connection, sqlite3.Connection):
                cur = dbapi_connection.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
    """
    orm_file = None
    has_pragma = False
    for rel, text in _app_python_files(app_root):
        if orm_file is None and _ORM_USAGE_RE.search(text):
            orm_file = rel
        if _FK_PRAGMA_RE.search(text):
            has_pragma = True
    if orm_file is not None and not has_pragma:
        return [Violation(
            "SQLITE-FK", orm_file, 1,
            "app uses SQLAlchemy but no SQLite foreign-key pragma (PRAGMA "
            "foreign_keys ON via an Engine 'connect' listener) was found; "
            "SQLite silently skips ON DELETE / passive_deletes",
        )]
    return []


def check_sqlite_batch_migration_guard(app_root: str) -> list[Violation]:
    """SQLITE-BATCH-MIGRATION: a build whose Alembic migrations use
    ``batch_alter_table`` must, in ``migrations/env.py``, suspend SQLite
    foreign-key enforcement around the migration run AND run
    ``PRAGMA foreign_key_check``.

    Why env.py and not the migration: SQLite ignores ``PRAGMA foreign_keys``
    while a transaction is open, in both directions. A migration can turn it OFF
    (it runs before any DML) but cannot turn it back ON, because by its ``finally``
    the batch copy's INSERT has opened a transaction and the restore is silently
    discarded. So the suspension must wrap ``run_migrations()`` in env.py, which is
    where the per-migration transactions are opened. Why it matters: on SQLite a
    ``batch_alter_table`` rebuild of a foreign-key *parent* does an implicit
    ``DELETE`` that fires ``ON DELETE`` actions on children, so rebuilding one table
    can silently strip rows (e.g. null every line's speaker) from another. The
    ``foreign_key_check`` makes any violation loud instead of silent.

    No-op for builds with no ``migrations/env.py`` or that never batch-migrate."""
    env_py = os.path.join(app_root, "migrations", "env.py")
    if not os.path.isfile(env_py):
        return []
    versions = os.path.join(app_root, "migrations", "versions")
    uses_batch = False
    if os.path.isdir(versions):
        for root, _dirs, files in os.walk(versions):
            for name in files:
                if not name.endswith(".py"):
                    continue
                try:
                    with open(os.path.join(root, name), encoding="utf-8") as fh:
                        if "batch_alter_table" in fh.read():
                            uses_batch = True
                            break
                except (OSError, UnicodeDecodeError):
                    continue
            if uses_batch:
                break
    if not uses_batch:
        return []
    try:
        with open(env_py, encoding="utf-8") as fh:
            env_lc = fh.read().lower()
    except (OSError, UnicodeDecodeError):
        return []
    # The canonical guard is the ``sqlite_foreign_keys_suspended()`` context
    # manager (it does suspend + foreign_key_check + restore in one place); when
    # env.py uses it, trust it and don't demand the pragmas be inlined here too.
    if "foreign_keys_suspended" in env_lc:
        return []
    problems = []
    if "foreign_keys=off" not in env_lc.replace(" ", ""):
        problems.append("no SQLite foreign-key suspension around the migration run")
    elif "foreign_key_check" not in env_lc:
        problems.append(
            "suspends foreign keys inline but has no PRAGMA foreign_key_check to "
            "surface violations"
        )
    if not problems:
        return []
    rel = os.path.relpath(env_py, app_root)
    return [Violation(
        "SQLITE-BATCH-MIGRATION", rel, 1,
        "migrations use batch_alter_table but env.py has " + "; ".join(problems)
        + " -- a SQLite batch rebuild of a foreign-key parent fires ON DELETE and "
        "can silently strip child rows",
    )]


def _templates(app_root: str):
    tdir = os.path.join(app_root, "app", "templates")
    for root, _dirs, files in os.walk(tdir):
        for name in sorted(files):
            if name.endswith(".html"):
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as fh:
                    yield os.path.relpath(path, app_root), fh.read()


def audit(app_root: str) -> list[Violation]:
    """All convention violations under app_root, sorted by (file, line, rule)."""
    out = []
    for rel, text in _templates(app_root):
        lines = text.splitlines()
        for check in _PER_TEMPLATE_CHECKS:
            out.extend(check(rel, text, lines))
    out.extend(check_foundation_scripts(app_root))
    out.extend(check_sqlite_foreign_keys(app_root))
    out.extend(check_sqlite_batch_migration_guard(app_root))
    return sorted(out, key=lambda v: (v.file, v.line, v.rule))
