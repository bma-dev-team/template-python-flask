from flask import Flask


def create_app(config: dict | None = None) -> Flask:
    """Application factory.

    Per-build code registers blueprints, extensions, and configuration here.
    The /health endpoint is template scaffolding (Background IP); replace or
    remove it once buyer-specific routes are in place.
    """
    app = Flask(__name__)
    if config:
        app.config.update(config)

    @app.get("/health")
    def health():
        return {"status": "ok"}, 200

    return app
