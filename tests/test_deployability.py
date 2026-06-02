"""Deployability guard: this app must stay Railway-deployable.

Every per-build repo is provisioned to Railway as a hosted review/acceptance
instance (`flask bma review-instance up`). That only works if the app keeps the
production-serving pieces the template provides. These tests fail loudly in CI
if a build removes or breaks them, instead of the failure surfacing much later
at provision time as a silent HTTP 404.

See the "deployability contract" in BMA_DEVELOPER_CONVENTIONS.md (BMA repo) and
developer-guides/architecture/hosting-operations.md for the full why.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_procfile_runs_gunicorn_factory_bound_to_port():
    procfile = REPO_ROOT / "Procfile"
    assert procfile.exists(), "Procfile is required for Railway to start a web process"
    web = next((ln for ln in procfile.read_text().splitlines() if ln.strip().startswith("web:")), "")
    assert web, "Procfile must define a 'web:' process"
    assert "gunicorn" in web, "web process must run gunicorn (a production WSGI server)"
    assert "create_app" in web, "web process must serve the app:create_app() factory"
    assert "$PORT" in web or "${PORT" in web, "web process must bind Railway's $PORT"


def test_gunicorn_pinned_in_requirements():
    reqs = (REPO_ROOT / "requirements.txt").read_text().lower()
    assert "gunicorn" in reqs, "gunicorn must be in requirements.txt for the Procfile to run"


def test_health_check_path_returns_200():
    """Railway polls the bma.yaml health_check_path; it must return 200 or the
    provisioner's health check fails and the instance is marked failed."""
    import yaml

    manifest = yaml.safe_load((REPO_ROOT / "bma.yaml").read_text()) or {}
    health_path = manifest.get("health_check_path", "/health")
    from app import create_app

    client = create_app().test_client()
    resp = client.get(health_path)
    assert resp.status_code == 200, f"Railway polls {health_path} (from bma.yaml); it must return 200"
