import os

from flask import Flask


def _resolve_review_instance(app: Flask) -> bool:
    """REVIEW_INSTANCE precedence: explicit config wins, then the REVIEW_INSTANCE
    env var (1/true/yes/on), else TESTING only. Off on a real deploy.

    Never inferred from ``app.debug``: setting FLASK_DEBUG=1 on a real deploy must
    NOT flip this on, because a review instance disables login. The old
    ``app.debug or app.testing`` fallback meant a debug flag silently disabled
    auth and exposed every route (and the Werkzeug console) on a public URL,
    reproduced in the deposition build's security review, 2026-08-05."""
    if "REVIEW_INSTANCE" in app.config and app.config["REVIEW_INSTANCE"] is not None:
        return bool(app.config["REVIEW_INSTANCE"])
    env = os.environ.get("REVIEW_INSTANCE")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    return bool(app.testing)


def create_app(config: dict | None = None) -> Flask:
    """Application factory.

    Per-build code registers blueprints, extensions, and configuration here.
    The /health endpoint is template scaffolding (Background IP); replace or
    remove it once buyer-specific routes are in place.
    """
    app = Flask(__name__)
    if config:
        app.config.update(config)

    # CSRF protection (Flask-WTF). Registers the `csrf_token()` Jinja global the
    # shared form macros (_bma_ui.html: gated_form, remove_x, credentials_section)
    # emit, and validates the token on state-changing requests. Disabled under
    # TESTING so build test suites can POST without threading a token through
    # every request; production always runs with it on. A build exempts specific
    # non-form routes (webhooks, JSON APIs) with @csrf.exempt as needed.
    app.config.setdefault("WTF_CSRF_ENABLED", not app.testing)
    from flask_wtf import CSRFProtect

    CSRFProtect(app)

    app.config["REVIEW_INSTANCE"] = _resolve_review_instance(app)

    from app.demo import review_login_disabled

    if review_login_disabled(
        review_instance=app.config["REVIEW_INSTANCE"], testing=app.testing
    ):
        app.config["LOGIN_DISABLED"] = True

    @app.context_processor
    def _inject_review_instance():
        return {"review_instance": app.config["REVIEW_INSTANCE"]}

    @app.get("/health")
    def health():
        return {"status": "ok"}, 200

    return app
