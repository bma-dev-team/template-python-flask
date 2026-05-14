# NOTICES — Foreground vs Background IP Declaration

This file declares which parts of this build are **Foreground IP** (buyer-owned under Exclusive; perpetual license under Non-Exclusive) versus **Background IP** (BMA Dev Team-retained, with the buyer holding a perpetual royalty-free license to use as embedded).

For the full framework, see BuildMyApp's `/terms` page (License Types and IP Rights section) and `developer-guides/architecture/BMA_DEVELOPER_CONVENTIONS.md` in the BuildMyApp platform repo.

## Background IP (BMA Dev Team retains)

The following paths and contents originate from the `template-python-flask` template and are Background IP. The buyer holds a perpetual royalty-free license to use them as embedded in this delivered build. BMA Dev Team may reuse them in other engagements.

- `app/__init__.py` — Flask application factory pattern (template smoke route may be removed once buyer routes are in place)
- `tests/test_smoke.py` — smoke test scaffolding
- `.github/workflows/test.yml` — CI pipeline scaffolding
- `requirements.txt` — base Python dependencies
- `pyproject.toml` — project metadata
- `.gitignore` — standard ignore rules

## Foreground IP (buyer-owned per license terms)

The following paths and contents are written specifically for this engagement and constitute Foreground IP under the buyer's chosen license:

- (Per build: list buyer-specific business logic, data models, integrations, UI templates, and branding paths here. Examples for a typical build: `app/quickbooks/`, `app/shopify/`, `app/templates/`, `app/models/`, `app/cli/`, `tests/test_<feature>.py` for non-smoke tests.)

## Residuals (BMA Dev Team retains)

Methods, techniques, design patterns, and know-how applied during this engagement remain with BMA Dev Team per the Residuals carve-out in the IP framework. This carve-out does not authorize disclosure of Confidential Information the buyer has identified as confidential.

## Build identification

| Field | Value |
|---|---|
| Build slug | (filled in per build, e.g., `malindi-fpna-reporter`) |
| Buyer | (filled in per build) |
| Build start date | (filled in per build) |
| License type | (filled in per build: Exclusive / Non-Exclusive) |
| BMA proposal ID | (filled in per build) |
| Delivery date | (filled in at delivery) |
