import io
import json
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

from postcard import cli
from postcard.build import process_image
from postcard.config import PostcardError
from postcard.core import fresh_site_dir, is_id, make_site, render_options
from postcard.preview import route
from postcard.remote import Remote
from postcard.vault import read_note, split_frontmatter

from .conftest import jpeg_with_gps

NOTE = "Travel/Vietnam Micro Blog.md"


def test_process_image_strips_gps_and_resizes():
    data, ext, had_gps = process_image(jpeg_with_gps(), "jpg")
    assert had_gps and ext == "webp"
    img = Image.open(io.BytesIO(data))
    assert img.width == 2000
    assert not img.getexif().get_ifd(0x8825)


def test_build_layout(cfg, fake_renderer):
    note = read_note(cfg.vault / NOTE, cfg.vault)
    site = fresh_site_dir("site")
    result = make_site(cfg, note, "AbCdEf123456", render_options(cfg, note), site)

    html = (site / "AbCdEf123456" / "index.html").read_text()
    assert '<div class="inline-title">Vietnam Micro Blog</div>' in html
    assert 'noindex' in html and "🇻🇳" in html
    assert "__POSTCARD_ASSET__" not in html
    imgs = list((site / "AbCdEf123456" / "a").iterdir())
    assert len(imgs) == 1 and imgs[0].suffix == ".webp" and f"/AbCdEf123456/a/{imgs[0].name}" in html
    css = (site / "_s" / result.css_name).read_text()
    assert "--text-title-h1:#289aff" in css  # Dark Moss made it in
    assert "Hack Nerd Font" in css
    assert result.gps_stripped


def test_theme_priority(cfg, monkeypatch):
    note = read_note(cfg.vault / NOTE, cfg.vault)
    assert render_options(cfg, note).theme == "Dark Moss"
    monkeypatch.setenv("POSTCARD_THEME", "default")
    assert render_options(cfg, note).theme == "default"
    assert render_options(cfg, note, theme="dark moss").theme == "Dark Moss"  # flag wins, case-insensitive
    with pytest.raises(PostcardError, match="postcard themes add"):
        render_options(cfg, note, theme="Nope")


def test_preview_routes_match_nginx():
    assert route("/AbCdEf123456") == "/AbCdEf123456/index.html"
    assert route("/AbCdEf123456/") == "/AbCdEf123456/index.html"
    assert route("/AbCdEf123456/any-slug") == "/AbCdEf123456/index.html"
    assert route("/AbCdEf123456/a/x.webp") == "/AbCdEf123456/a/x.webp"
    assert route("/_s/abc.css") == "/_s/abc.css"
    assert route("/") is None
    assert route("/.tmp-AbCdEf123456/index.html") is None
    assert route("/registry.json") is None


# --- full CLI flow against a local "server" folder --------------------------

@pytest.fixture
def run(cfg, fake_renderer, monkeypatch):
    monkeypatch.chdir(cfg.vault)
    monkeypatch.setattr(cli, "_copy", lambda text: True)
    runner = CliRunner()

    def invoke(*args, input=None):
        result = runner.invoke(cli.app, list(args), input=input, catch_exceptions=False)
        return result

    return invoke


def server(cfg) -> Path:
    return Path(cfg.remote_root)


def registry(cfg) -> dict:
    return json.loads((server(cfg) / "registry.json").read_text())


def test_publish_update_unpublish_lifecycle(cfg, run, fake_renderer):
    note_path = cfg.vault / NOTE

    # 1. first publish
    r = run("publish", NOTE, "--yes")
    assert r.exit_code == 0, r.output
    fm, _ = split_frontmatter(note_path.read_text())
    page_id = fm["postcard_id"]
    assert is_id(page_id) and fm["postcard_status"] == "live"
    assert fm["postcard_url"] == f"https://publish.example.com/{page_id}/vietnam-micro-blog"
    assert (server(cfg) / "site" / page_id / "index.html").exists()
    assert registry(cfg)["pages"][page_id]["source"] == NOTE

    # 2. republish without changes: skipped, nothing rendered
    r = run("publish", NOTE, "--yes")
    assert "Nothing changed" in r.output and len(fake_renderer) == 1

    # 3. change the note: asks, and declining leaves it alone
    note_path.write_text(note_path.read_text() + "\nDay 2: Ha Long Bay.\n")
    r = run("publish", NOTE, input="n\n")
    assert "already published" in r.output and r.exit_code == 1 and len(fake_renderer) == 1

    # 4. accept: same id, updated
    r = run("publish", NOTE, input="y\n")
    assert r.exit_code == 0 and "Updated" in r.output
    assert split_frontmatter(note_path.read_text())[0]["postcard_id"] == page_id
    assert not list((server(cfg) / "site").glob(".tmp-*")) and not list((server(cfg) / "site").glob(".old-*"))

    # 5. list
    r = run("list")
    assert "1 live" in r.output and page_id in r.output

    # 6. unpublish: page goes to trash, frontmatter + registry updated
    r = run("unpublish", NOTE, "--yes")
    assert r.exit_code == 0, r.output
    assert not (server(cfg) / "site" / page_id).exists()
    assert len(list((server(cfg) / "trash").iterdir())) == 1
    assert registry(cfg)["pages"][page_id]["status"] == "unpublished"
    assert split_frontmatter(note_path.read_text())[0]["postcard_status"] == "unpublished"

    # 7. publish again: NEW id, old link stays dead
    r = run("publish", NOTE, "--yes")
    new_id = split_frontmatter(note_path.read_text())[0]["postcard_id"]
    assert new_id != page_id and "new" in r.output
    assert not (server(cfg) / "site" / page_id).exists()


def test_restore_and_rotate(cfg, run):
    note_path = cfg.vault / NOTE
    run("publish", NOTE, "--yes")
    first = split_frontmatter(note_path.read_text())[0]["postcard_id"]
    run("unpublish", NOTE, "--yes")
    r = run("publish", NOTE, "--yes", "--restore")
    assert "Restored" in r.output
    assert split_frontmatter(note_path.read_text())[0]["postcard_id"] == first

    r = run("rotate", NOTE, "--yes")
    assert r.exit_code == 0, r.output
    rotated = split_frontmatter(note_path.read_text())[0]["postcard_id"]
    assert rotated != first
    assert not (server(cfg) / "site" / first).exists() and (server(cfg) / "site" / rotated).exists()
    assert registry(cfg)["pages"][first]["status"] == "rotated"


def test_lost_server_republishes_with_same_link(cfg, run):
    note_path = cfg.vault / NOTE
    run("publish", NOTE, "--yes")
    page_id = split_frontmatter(note_path.read_text())[0]["postcard_id"]
    import shutil
    shutil.rmtree(server(cfg))  # droplet died
    r = run("publish", NOTE, "--yes")
    assert "same link" in r.output
    assert (server(cfg) / "site" / page_id / "index.html").exists()


def test_copied_note_gets_its_own_link(cfg, run):
    note_path = cfg.vault / NOTE
    run("publish", NOTE, "--yes")
    original = split_frontmatter(note_path.read_text())[0]["postcard_id"]
    copy = cfg.vault / "Travel" / "Vietnam copy.md"
    copy.write_text(note_path.read_text())
    r = run("publish", "Travel/Vietnam copy.md", "--yes")
    assert "a copy?" in r.output
    assert split_frontmatter(copy.read_text())[0]["postcard_id"] != original
    assert (server(cfg) / "site" / original).exists()


def test_errors_block_publish(cfg, run):
    (cfg.vault / "Secret.md").write_text("aws AKIAIOSFODNN7EXAMPLE")
    r = run("publish", "Secret.md", "--yes")
    assert r.exit_code == 1 and "AWS access key" in r.output
    assert not server(cfg).exists()


def test_fuzzy_note_lookup(cfg, run):
    r = run("publish", "vietnam", "--yes")
    assert r.exit_code == 0 and "Published" in r.output


def test_remote_registry_roundtrip(tmp_path):
    remote = Remote("local", str(tmp_path / "srv"))
    remote.ensure_layout()
    assert remote.read_registry() == {"version": 1, "pages": {}}
    remote.write_registry({"version": 1, "pages": {"x": {"title": "it's \"quoted\""}}})
    assert remote.read_registry()["pages"]["x"]["title"] == "it's \"quoted\""


def test_server_home_upload(cfg, run):
    r = run("server", "home")
    assert r.exit_code == 0, r.output
    home = server(cfg) / "site" / "_home.html"
    assert home.exists() and "Return to sender" in home.read_text()
