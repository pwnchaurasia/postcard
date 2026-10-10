"""Asking Obsidian (via the postcard-bridge plugin) to render a note. See plugin/src/main.ts for the other side."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlencode

from .config import CACHE_DIR, PostcardError

PLUGIN_ID = "postcard-bridge"
ASSET_TOKEN = "__POSTCARD_ASSET__/"


@dataclass
class RenderRequest:
    vault: Path
    note_rel: str
    theme_css: Path | None
    snippet_css: list[Path]
    mode: str


@dataclass
class Rendered:
    """What the bridge produced: content.html, styles.css, assets/, meta.json."""
    dir: Path

    @property
    def content(self) -> str:
        return (self.dir / "content.html").read_text(encoding="utf-8")

    @property
    def css(self) -> str:
        return (self.dir / "styles.css").read_text(encoding="utf-8")

    @property
    def meta(self) -> dict:
        return json.loads((self.dir / "meta.json").read_text())

    @property
    def assets(self) -> Path:
        return self.dir / "assets"


def render_with_obsidian(req: RenderRequest, timeout: float = 90) -> Rendered:
    job = CACHE_DIR / "jobs" / uuid.uuid4().hex[:12]
    out = job / "out"
    out.mkdir(parents=True)
    (job / "request.json").write_text(
        json.dumps(
            {
                "version": 1,
                "file": req.note_rel,
                "out": str(out),
                "theme_css": str(req.theme_css) if req.theme_css else None,
                "snippet_css": [str(p) for p in req.snippet_css],
                "mode": req.mode,
                "settle_ms": 800,
                "timeout_ms": 20000,
            }
        )
    )
    url = "obsidian://postcard?" + urlencode({"vault": req.vault.name, "req": str(job / "request.json")}, quote_via=quote)
    obsidian_was_running = obsidian_running()
    subprocess.run(["open", "-g", url], check=True)

    ack_wait = 20 if obsidian_was_running else 45  # cold start takes a while
    _wait_for(out / "ack.json", ack_wait, lambda: PostcardError(_no_ack_hint(req.vault, obsidian_was_running)))
    _wait_for(out / "done.json", timeout, lambda: PostcardError(f"Obsidian took longer than {timeout:.0f}s to render the note."))
    done = json.loads((out / "done.json").read_text())
    if not done.get("ok"):
        raise PostcardError(f"Obsidian failed to render the note:\n{done.get('error')}")
    return Rendered(out)


def _no_ack_hint(vault: Path, was_running: bool) -> str:
    msg = "Obsidian didn't pick up the render request."
    if installed_plugin_version(vault) is None:
        return msg + "\n  → The Postcard Bridge plugin isn't installed: run `postcard plugin install`."
    if not plugin_enabled(vault):
        return msg + "\n  → Enable Postcard Bridge in Obsidian → Settings → Community plugins."
    if was_running:
        return msg + "\n  → The plugin is installed but not loaded yet. In Obsidian: Cmd+P → “Reload app without saving”."
    return msg + "\n  → Obsidian may still be starting up. Try again in a few seconds."


def _wait_for(path: Path, timeout: float, error) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise error()
        time.sleep(0.2)


def obsidian_running() -> bool:
    return subprocess.run(["pgrep", "-xq", "Obsidian"]).returncode == 0


def cleanup_jobs(keep: int = 5) -> None:
    jobs = CACHE_DIR / "jobs"
    if jobs.is_dir():
        for old in sorted(jobs.iterdir(), key=lambda p: p.stat().st_mtime)[:-keep]:
            shutil.rmtree(old, ignore_errors=True)


# --- installing the plugin into the vault ---------------------------------

def bundled_plugin_dir() -> Path:
    return Path(__file__).parent / "bridge"


def plugin_install_dir(vault: Path) -> Path:
    return vault / ".obsidian" / "plugins" / PLUGIN_ID


def installed_plugin_version(vault: Path) -> str | None:
    try:
        return json.loads((plugin_install_dir(vault) / "manifest.json").read_text())["version"]
    except (OSError, KeyError, json.JSONDecodeError):
        return None


def plugin_enabled(vault: Path) -> bool:
    try:
        return PLUGIN_ID in json.loads((vault / ".obsidian" / "community-plugins.json").read_text())
    except (OSError, json.JSONDecodeError):
        return False


def install_plugin(vault: Path) -> bool:
    """Copy the bundled plugin into the vault and mark it enabled. Returns True if it was newly enabled."""
    dest = plugin_install_dir(vault)
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("main.js", "manifest.json"):
        shutil.copy2(bundled_plugin_dir() / name, dest / name)

    cp = vault / ".obsidian" / "community-plugins.json"
    enabled = json.loads(cp.read_text()) if cp.exists() else []
    if PLUGIN_ID in enabled:
        return False
    enabled.append(PLUGIN_ID)
    cp.write_text(json.dumps(enabled, indent=2))
    return True
