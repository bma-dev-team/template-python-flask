from app import create_app
from app.demo import review_login_disabled


def test_review_login_disabled_truth_table():
    assert review_login_disabled(review_instance=True, testing=False) is True
    assert review_login_disabled(review_instance=True, testing=True) is False
    assert review_login_disabled(review_instance=False, testing=False) is False
    assert review_login_disabled(review_instance=False, testing=True) is False


def test_seed_route_registered_and_redirects_when_review_on():
    app = create_app({"REVIEW_INSTANCE": True, "TESTING": True})
    client = app.test_client()
    resp = client.get("/demo/seed")
    assert resp.status_code == 302


def test_seed_route_absent_when_review_off():
    app = create_app({"REVIEW_INSTANCE": False, "TESTING": True})
    client = app.test_client()
    resp = client.get("/demo/seed")
    assert resp.status_code == 404
