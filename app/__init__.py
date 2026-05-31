import os

from flask import Flask


def _resolve_review_instance(app: Flask) -> bool:
    """REVIEW_INSTANCE precedence: explicit config wins, then the REVIEW_INSTANCE
    env var (1/true/yes/on), else default to debug/testing. Off on a real deploy."""
    if "REVIEW_INSTANCE" in app.config and app.config["REVIEW_INSTANCE"] is not None:
        return bool(app.config["REVIEW_INSTANCE"])
    env = os.environ.get("REVIEW_INSTANCE")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    return bool(app.debug or app.testing)


def create_app(config: dict | None = None) -> Flask:
    """Application factory.

    Per-build code registers blueprints, extensions, and configuration here.
    The /health endpoint is template scaffolding (Background IP); replace or
    remove it once buyer-specific routes are in place.
    """
    app = Flask(__name__)
    if config:
        app.config.update(config)

    app.config["REVIEW_INSTANCE"] = _resolve_review_instance(app)

    from app.demo import bp as demo_bp, review_login_disabled

    if app.config["REVIEW_INSTANCE"]:
        app.register_blueprint(demo_bp)

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
