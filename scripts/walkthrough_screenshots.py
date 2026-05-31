"""Visual-walkthrough screenshot pass.

Reads bma.yaml's `screens:` list and captures one PNG per screen from a running
instance using headless Chromium. Produces the visual record + a surface Claude
can review multimodally. The UI-vs-mockup diff is a separate concern owned by
the mockup-fidelity track; this only produces the PNG set.

Usage:
    python scripts/walkthrough_screenshots.py --base-url http://localhost:5112 \
        --out _walkthrough --seed
Requires: pip install playwright && playwright install chromium
"""
from __future__ import annotations

import argparse
import os

import yaml


def load_screens(bma_yaml_path: str) -> list[dict]:
    """Return the [{name, path}, ...] screen list from a bma.yaml (empty if none)."""
    with open(bma_yaml_path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return list(data.get("screens") or [])


def capture(base_url: str, screens: list[dict], out_dir: str, seed: bool) -> list[str]:
    """Walk each screen and write one PNG per screen. Returns the file paths."""
    from playwright.sync_api import sync_playwright  # lazy import so tests need no browser

    os.makedirs(out_dir, exist_ok=True)
    written = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        if seed:
            page.goto(f"{base_url.rstrip('/')}/demo/seed")
        for i, screen in enumerate(screens, start=1):
            page.goto(f"{base_url.rstrip('/')}{screen['path']}")
            page.wait_for_load_state("networkidle")
            fname = os.path.join(out_dir, f"{i:02d}_{screen['name'].lower().replace(' ', '_')}.png")
            page.screenshot(path=fname, full_page=True)
            written.append(fname)
        browser.close()
    return written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--out", default="_walkthrough")
    ap.add_argument("--manifest", default="bma.yaml")
    ap.add_argument("--seed", action="store_true", help="hit /demo/seed first")
    args = ap.parse_args()
    screens = load_screens(args.manifest)
    paths = capture(args.base_url, screens, args.out, args.seed)
    for pth in paths:
        print(pth)


if __name__ == "__main__":
    main()
