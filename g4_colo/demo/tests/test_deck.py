"""Tests for the walkthrough deck served at /deck/ (app.deck): index, assets, exclusions, no-cache, missing dir."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ["DEMO_MOCK"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

import app as demo_app  # noqa: E402


@pytest.fixture
def deck(tmp_path, monkeypatch):
    d = tmp_path / "deck"
    (d / "css").mkdir(parents=True)
    (d / "fonts").mkdir()
    (d / "__pycache__").mkdir()
    (d / "index.html").write_text('<!doctype html><link rel="stylesheet" href="css/deck.css"><h1>deck</h1>')
    (d / "css" / "deck.css").write_text('@font-face { src: url("../fonts/f.woff2"); }')
    (d / "fonts" / "f.woff2").write_bytes(b"wOF2")
    (d / "deck.pptx").write_bytes(b"PK")
    (d / "build_pptx.py").write_text("print('secret build script')")
    (d / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"\0")
    (d / ".hidden").write_text("x")
    (tmp_path / "outside.txt").write_text("outside")
    monkeypatch.setattr(demo_app, "DECK_DIR", d)
    return d


@pytest.fixture
def client():
    return TestClient(demo_app.app)


def test_deck_index_and_assets(deck, client):
    r = client.get("/deck/")
    assert r.status_code == 200 and "<h1>deck</h1>" in r.text and r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-cache"
    r = client.get("/deck/css/deck.css")
    assert r.status_code == 200 and "font-face" in r.text and r.headers["cache-control"] == "no-cache"
    assert client.get("/deck/fonts/f.woff2").content == b"wOF2"  # ../fonts from css/ resolves under /deck/
    assert client.get("/deck/index.html").status_code == 200
    r = client.get("/deck", follow_redirects=False)  # trailing slash so relative asset paths work
    assert r.status_code in (307, 308) and r.headers["location"] == "/deck/"
    assert r.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("path", ["deck.pptx", "build_pptx.py", "__pycache__/x.cpython-312.pyc", ".hidden",
                                  "missing.css", "../outside.txt", "%2e%2e/outside.txt", "css/../../outside.txt"])
def test_deck_never_serves_build_files_or_escapes(deck, client, path):
    assert client.get(f"/deck/{path}").status_code == 404


def test_deck_missing_dir_is_a_clear_404(tmp_path, monkeypatch, client):
    monkeypatch.setattr(demo_app, "DECK_DIR", tmp_path / "nope")
    r = client.get("/deck/")
    assert r.status_code == 404 and "deck not available" in r.json()["detail"] and "sync_deck.sh" in r.json()["detail"]
    assert client.get("/").status_code == 200  # the demo itself is unaffected


def test_deck_dir_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_DECK_DIR", str(tmp_path / "x"))
    assert demo_app._deck_dir() == tmp_path / "x"
    monkeypatch.delenv("DEMO_DECK_DIR")
    d = demo_app._deck_dir()
    here = Path(demo_app.__file__).resolve().parent
    assert d == (here / "deck" if (here / "deck").is_dir() else here.parent / "deck")


def test_header_has_deck_link(client):
    html = client.get("/").text
    assert 'href="/deck/"' in html and 'target="_blank"' in html


def test_sync_deck_script_copies_with_exclusions(deck, tmp_path):
    fake_demo = tmp_path / "demo"
    (fake_demo / "tools").mkdir(parents=True)
    script = fake_demo / "tools" / "sync_deck.sh"
    script.write_text((HERE / "tools" / "sync_deck.sh").read_text())
    script.chmod(0o755)
    subprocess.run(["bash", str(script), str(deck)], check=True, capture_output=True)
    out = fake_demo / "deck"
    files = sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file())
    assert files == ["css/deck.css", "fonts/f.woff2", "index.html"]
