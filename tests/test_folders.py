import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from postcard import cli
from postcard.folders import plan, scan
from postcard.preview import route

from .conftest import FAKE_AWS_KEY

FOLDER = "02 - Study/Deployment"


@pytest.fixture
def deployment(cfg) -> Path:
    d = cfg.vault / FOLDER
    (d / "Example").mkdir(parents=True)
    (d / "000-Overview-and-Architecture.md").write_text("# Overview\nStart with [[002-Network]].\n")
    (d / "001-Account.md").write_text("# Account\n")
    (d / "002-Network.md").write_text("# Network\nBack to [[000-Overview-and-Architecture]].\n")
    (d / "010-Later.md").write_text("# Later\n")
    (d / "Example" / "AZURE data.md").write_text(f"key {FAKE_AWS_KEY}\n")
    (d / "Example" / "Sub note.md").write_text("# Sub\n")
    (d / "a.md").write_text("# Named like the assets folder\n")
    return d


@pytest.fixture
def run(cfg, fake_renderer, monkeypatch):
    monkeypatch.chdir(cfg.vault)
    monkeypatch.setattr(cli, "_copy", lambda text: True)
    runner = CliRunner()
    return lambda *args, input=None: runner.invoke(cli.app, list(args), input=input, catch_exceptions=False)


def registry(cfg) -> dict:
    return json.loads((Path(cfg.remote_root) / "registry.json").read_text())


def site(cfg) -> Path:
    return Path(cfg.remote_root) / "site"


def test_scan_order_is_natural_files_then_subfolders(deployment):
    names = [p.relative_to(deployment).as_posix() for p in scan(deployment)]
    assert names == ["000-Overview-and-Architecture.md", "001-Account.md", "002-Network.md", "010-Later.md", "a.md",
                     "Example/AZURE data.md", "Example/Sub note.md"]


def test_plan_skips_failing_notes_and_avoids_reserved_slug(cfg, deployment):
    fplan = plan(cfg, deployment)
    assert [rel for rel, _ in fplan.skipped] == ["Example/AZURE data.md"]
    assert fplan.home.rel == "000-Overview-and-Architecture.md"
    slugs = {p.rel: p.slug for p in fplan.pages}
    assert slugs["a.md"] == "a-2"  # /<id>/a/ is the images folder


def test_publish_folder_lifecycle(cfg, deployment, run, fake_renderer):
    r = run("publish", FOLDER, "--yes")
    assert r.exit_code == 0, r.output
    assert "Skipping Example/AZURE data.md" in r.output
    (pid, entry), = registry(cfg)["pages"].items()
    assert entry["kind"] == "folder" and entry["status"] == "live" and len(entry["pages"]) == 6
    assert entry["url"] == f"https://publish.example.com/{pid}/deployment"

    root = site(cfg) / pid
    assert (root / "index.html").exists()  # the folder's home page
    for slug in ["000-overview-and-architecture", "001-account", "002-network", "010-later", "sub-note", "a-2"]:
        assert (root / slug / "index.html").exists(), slug
    assert not (root / "azure-data").exists()

    # links between the folder's notes are handed to Obsidian as folder URLs
    link_map = fake_renderer[0].link_map
    assert link_map[f"{FOLDER}/002-Network.md"] == f"/{pid}/002-network"
    assert f"{FOLDER}/Example/AZURE data.md" not in link_map
    # the CSS is collected last, after every note has been rendered
    assert not fake_renderer[-1].skip_css and all(req.skip_css for req in fake_renderer[:-1])

    # sidebar + pager
    page = (root / "001-account" / "index.html").read_text()
    assert 'class="tree-item-self nav-file-title is-clickable is-active" data-path="02 - Study/Deployment/001-Account.md"' in page
    assert f'data-path="{FOLDER}"' in page  # root folder carries the vault path, so prefix-colour snippets apply
    assert f'href="/{pid}/000-overview-and-architecture"><small>← Previous' in page
    assert f'href="/{pid}/002-network"><small>Next →' in page
    assert 'nav-folder is-collapsed' in page  # Example/ stays closed here…
    sub = (root / "sub-note" / "index.html").read_text()
    assert 'nav-folder is-collapsed' not in sub  # …and opens on its own pages
    assert (Path(cfg.vault) / ".postcard" / "folders.json").exists()

    # republish without changes: skipped, nothing re-rendered
    renders = len(fake_renderer)
    r = run("publish", FOLDER, "--yes")
    assert "Nothing changed" in r.output and len(fake_renderer) == renders

    # change one note: only the changed note and the CSS pass are rendered again
    (deployment / "010-Later.md").write_text("# Later\nNew stuff.\n")
    r = run("publish", FOLDER, "--yes")
    assert "Updated" in r.output, r.output
    assert len(fake_renderer) == renders + 2
    assert next(iter(registry(cfg)["pages"])) == pid

    # list shows it as a folder
    assert "📁 Deployment (6)" in run("list").output

    # unpublish → dead link; publish again → new link; --restore → old link
    r = run("unpublish", FOLDER, "--yes")
    assert r.exit_code == 0, r.output
    assert not root.exists() and registry(cfg)["pages"][pid]["status"] == "unpublished"
    r = run("publish", FOLDER, "--yes")
    new = [k for k, v in registry(cfg)["pages"].items() if v["status"] == "live"]
    assert new and new[0] != pid
    run("unpublish", FOLDER, "--yes")
    r = run("publish", FOLDER, "--yes", "--restore")  # brings back the most recent link
    assert "Restored" in r.output and registry(cfg)["pages"][new[0]]["status"] == "live"


def test_rotate_folder(cfg, deployment, run):
    run("publish", FOLDER, "--yes")
    (old,) = registry(cfg)["pages"]
    r = run("rotate", FOLDER, "--yes")
    assert r.exit_code == 0, r.output
    pages = registry(cfg)["pages"]
    assert pages[old]["status"] == "rotated"
    (new,) = [k for k, v in pages.items() if v["status"] == "live"]
    assert not (site(cfg) / old).exists() and (site(cfg) / new / "index.html").exists()


def test_single_note_links_into_published_folder(cfg, deployment, run, fake_renderer):
    run("publish", FOLDER, "--yes")
    (pid,) = registry(cfg)["pages"]
    run("publish", "Travel/Vietnam Micro Blog.md", "--yes")
    assert fake_renderer[-1].link_map[f"{FOLDER}/002-Network.md"] == f"/{pid}/002-network"


def test_preview_routes_folder_subpages(tmp_path):
    (tmp_path / "AbCdEf123456" / "002-network").mkdir(parents=True)
    (tmp_path / "AbCdEf123456" / "002-network" / "index.html").write_text("x")
    assert route("/AbCdEf123456/002-network", tmp_path) == "/AbCdEf123456/002-network/index.html"
    assert route("/AbCdEf123456/deployment", tmp_path) == "/AbCdEf123456/index.html"  # folder home
    assert route("/AbCdEf123456", tmp_path) == "/AbCdEf123456/index.html"


def test_folder_name_lookup(cfg, deployment, run):
    r = run("publish", "Deployment", "--yes")
    assert r.exit_code == 0 and "📁 Deployment" in r.output


def test_notes_with_math_skip_the_render_cache(cfg, deployment, run, fake_renderer):
    (deployment / "001-Account.md").write_text("# Account\nCost is $E = mc^2$.\n")
    run("publish", FOLDER, "--yes")
    before = len(fake_renderer)
    run("publish", FOLDER, "--yes", "--force")
    rendered_again = [r.note_rel for r in fake_renderer[before:]]
    assert f"{FOLDER}/001-Account.md" in rendered_again          # math: always fresh
    assert f"{FOLDER}/002-Network.md" not in rendered_again[:-1]  # no math: from cache (last one is the CSS pass)


def test_find_latest_breaks_timestamp_ties_by_registry_order():
    from postcard.folders import find_latest
    same = "2026-10-10T12:00:00"
    reg = {"pages": {
        "OldOldOld123": {"kind": "folder", "source": "F", "updated": same},
        "NewNewNew123": {"kind": "folder", "source": "F", "updated": same},
    }}
    assert find_latest(reg, "F")[0] == "NewNewNew123"
