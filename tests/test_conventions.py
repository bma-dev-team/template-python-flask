import os

from app.conventions_audit import audit


def test_no_convention_violations():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    violations = audit(repo_root)
    assert not violations, "Convention violations found:\n" + "\n".join(str(v) for v in violations)
