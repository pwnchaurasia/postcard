import io
import json
from pathlib import Path

import pytest
from PIL import Image

from postcard import core
from postcard.config import Config, write_config
from postcard.render import ASSET_TOKEN, Rendered, RenderRequest

# Fake secrets for testing the secret checks, assembled at runtime so secret scanners don't flag the repo:
# AWS's documented example access key, and a bare private-key header with no key under it.
FAKE_AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
FAKE_PRIVATE_KEY_HEADER = "-----BEGIN OPENSSH " + "PRIVATE KEY-----"


def jpeg_with_gps() -> bytes:
    img = Image.new("RGB", (2400, 1200), (40, 120, 200))
    exif = Image.Exif()
    exif[0x0112] = 1  # orientation
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = "N", (12.0, 34.0, 56.0)
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "Vault"
    (v / ".obsidian" / "themes" / "Dark Moss").mkdir(parents=True)
    (v / ".obsidian" / "themes" / "Dark Moss" / "theme.css").write_text(".theme-dark{--text-title-h1:#289aff}")
    (v / ".obsidian" / "snippets").mkdir()
    (v / ".obsidian" / "appearance.json").write_text(json.dumps({"cssTheme": "Dark Moss", "enabledCssSnippets": []}))
    (v / "09 - Attachments").mkdir()
    (v / "09 - Attachments" / "hoi-an.jpg").write_bytes(jpeg_with_gps())
    (v / "Travel").mkdir()
    (v / "Travel" / "Vietnam Micro Blog.md").write_text(
        "---\ntitle: Vietnam Micro Blog\ntags: [travel]\nicon: 🇻🇳\n---\n# Day 1\nLanded in Hanoi.\n\n![[hoi-an.jpg]]\n"
    )
    return v


@pytest.fixture
def cfg(vault: Path, tmp_path: Path) -> Config:
    write_config(vault, "https://publish.example.com", "local", str(tmp_path / "server"), "Dark Moss", "dark")
    from postcard.config import load_config
    return load_config(vault)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setattr(core, "CACHE_DIR", cache)
    return cache


@pytest.fixture
def fake_renderer(tmp_path: Path, monkeypatch):
    """Stands in for Obsidian: writes what the bridge plugin would."""
    calls: list[RenderRequest] = []

    def render(req: RenderRequest) -> Rendered:
        calls.append(req)
        out = tmp_path / f"render-{len(calls)}"
        (out / "assets").mkdir(parents=True)
        (out / "assets" / "1.jpg").write_bytes((req.vault / "09 - Attachments" / "hoi-an.jpg").read_bytes())
        (out / "assets" / "2.woff2").write_bytes(b"fontdata")
        (out / "content.html").write_text(
            f'<div><h1 id="day-1">Day 1</h1></div><div><p>Landed in Hanoi.</p></div>'
            f'<div><img src="{ASSET_TOKEN}1.jpg"></div>'
        )
        theme = req.theme_css.read_text() if req.theme_css else ""
        (out / "styles.css").write_text(f'@font-face{{src:url("{ASSET_TOKEN}2.woff2")}}\n{theme}')
        (out / "meta.json").write_text(json.dumps({"body_class": f"theme-{req.mode}", "body_style": "", "warnings": []}))
        return Rendered(out)

    monkeypatch.setattr(core, "render_with_obsidian", render)
    return calls
