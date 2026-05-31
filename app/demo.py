"""Build-agnostic demo-seed mechanism (review-instance capability).

Registered by the app factory only when app.config['REVIEW_INSTANCE'] is truthy
(on for local dev and a hosted review instance, off for the customer-facing
deployment, so /demo/seed never exists there). The route delegates build-specific
staging to app.demo_seed.stage_demo() and redirects to whatever URL it returns.
This is a testing aid; it never touches real buyer data.
"""
from __future__ import annotations

from flask import Blueprint, current_app, redirect

bp = Blueprint("demo", __name__)


def review_login_disabled(*, review_instance, testing) -> bool:
    """Whether to auto-disable login for a frictionless walkthrough.

    True only when this is a review instance AND not under the test harness
    (local dev or the hosted review instance). Never under TESTING (the suite
    manages its own login + CSRF assertions) and never on the customer deploy
    (REVIEW_INSTANCE is off there), so it can never weaken the real deployment.
    """
    return bool(review_instance and not testing)


@bp.get("/demo/seed")
def seed():
    """Stage a full mock run via the build's stage_demo(), then redirect."""
    if not current_app.config.get("REVIEW_INSTANCE"):
        return ("Not found", 404)
    from app import demo_seed
    target = demo_seed.stage_demo()
    return redirect(target)
