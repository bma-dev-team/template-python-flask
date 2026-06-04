from app import create_app
from app.demo import review_login_disabled


def test_review_login_disabled_truth_table():
    assert review_login_disabled(review_instance=True, testing=False) is True
    assert review_login_disabled(review_instance=True, testing=True) is False
    assert review_login_disabled(review_instance=False, testing=False) is False
    assert review_login_disabled(review_instance=False, testing=True) is False


def test_no_demo_seed_route():
    app = create_app({"TESTING": True, "REVIEW_INSTANCE": True})
    assert app.test_client().get("/demo/seed").status_code == 404
