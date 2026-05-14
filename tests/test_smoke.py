from app import create_app


def test_app_factory_creates_flask_app():
    app = create_app()
    assert app is not None


def test_health_endpoint_returns_ok():
    app = create_app()
    client = app.test_client()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
