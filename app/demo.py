"""Review-instance login resolution (build-agnostic).

A preview deployment runs the real app from a clean state: no preloaded data, no fabricated
run. The only review-mode convenience left here is the login-bypass default; it is consulted
only when app.config['REVIEW_INSTANCE'] is truthy and never weakens the customer deployment.
"""
from __future__ import annotations


def review_login_disabled(*, review_instance, testing) -> bool:
    """Whether to auto-disable login for a frictionless walkthrough.

    True only when this is a review instance AND not under the test harness (local dev or a
    hosted review instance). Never under TESTING and never on the customer deploy
    (REVIEW_INSTANCE is off there), so it can never weaken the real deployment.
    """
    return bool(review_instance and not testing)
