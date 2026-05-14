# template-python-flask

A starter template for BMA Dev Team Python/Flask builds. This repository provides the scaffolding (Background IP — generic structure, CI, test harness, IP framework declaration) that every BMA Dev Team Flask engagement reuses. Per-build buyer-specific code (Foreground IP) is added on top after generating a new repo from this template.

## How to use this template

1. Generate a new repository from this template under the `bma-dev-team` GitHub organization.
2. Name the new repo `<buyer-slug>-<product-slug>` (e.g., `malindi-fpna-reporter`).
3. Set the new repo's visibility to **private**.
4. Populate `LICENSE` with the buyer's license terms (typically Exclusive per BMA's framework).
5. Update `NOTICES.md` with the per-build Foreground/Background IP boundaries.
6. Replace this README with one that describes the specific build.

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest
```

Then add your first feature under `app/` and corresponding tests under `tests/`.

## What's in the template

| Path | Purpose | IP category |
|---|---|---|
| `app/__init__.py` | Flask application factory | Background |
| `tests/test_smoke.py` | Smoke test asserting the app starts | Background |
| `.github/workflows/test.yml` | CI runs pytest on push and PR | Background |
| `requirements.txt` | Base Python dependencies | Background |
| `pyproject.toml` | Project metadata | Background |
| `.gitignore` | Python ignore rules | Background |
| `LICENSE` | Per-build license terms | replaced per build |
| `NOTICES.md` | IP framework declaration | populated per build |

## Where this template fits

`template-python-flask` is part of the BMA Dev Team's setup, described in `bma-dev-team/setup.md` in the BuildMyApp platform repo. The Background IP / Foreground IP / Residuals framework it references lives in BuildMyApp's `/terms` page and is operationalized in `developer-guides/architecture/BMA_DEVELOPER_CONVENTIONS.md`.
