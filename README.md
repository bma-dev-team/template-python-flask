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

### A bare `pytest` may be running half your suite

**Whatever a build needs in order to run its WHOLE test suite belongs in this
README**, next to the command, and not only in a checklist somewhere else.

A build that keeps a database-backed leg behind an environment variable will
SKIP that leg silently when the variable is unset -- and a skip is not a
failure, so the run is green. One build ran with 739 of its tests skipped on
every fresh clone for three days. The invocation that turns them on had been
written down since the first day, in a checklist the developer had no reason
to open, and never in the repo. Half the suite was not being run and nothing
said so.

So: if this project grows a leg that needs a service, a variable or a browser,
put the command HERE, and make the suite say what it skipped:

```bash
pytest -rA          # -rA prints every skip WITH ITS REASON
```

A skip count is not information. A skip reason is. Read the reasons before
believing a green run covered what you think it covered.

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
