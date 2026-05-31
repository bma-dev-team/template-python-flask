import textwrap
from scripts.walkthrough_screenshots import load_screens


def test_load_screens_reads_name_path_pairs(tmp_path):
    p = tmp_path / "bma.yaml"
    p.write_text(textwrap.dedent("""
        port: 8080
        health_check_path: /health
        screens:
          - name: Upload
            path: /upload
          - name: Review
            path: /review
    """))
    screens = load_screens(str(p))
    assert screens == [
        {"name": "Upload", "path": "/upload"},
        {"name": "Review", "path": "/review"},
    ]


def test_load_screens_empty_when_no_screens_key(tmp_path):
    p = tmp_path / "bma.yaml"
    p.write_text("port: 8080\n")
    assert load_screens(str(p)) == []
