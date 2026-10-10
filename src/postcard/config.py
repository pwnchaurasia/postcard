"""Finding the vault and loading `.postcard/config.toml` (which lives inside the vault, so iCloud syncs it)."""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CACHE_DIR = Path.home() / ".cache" / "postcard"  # the bridge plugin only accepts jobs from here
ICLOUD_OBSIDIAN = Path.home() / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "Documents"
CONFIG_REL = Path(".postcard") / "config.toml"


class PostcardError(Exception):
    """An error with a message meant for the user (printed without a traceback)."""


@dataclass
class Config:
    vault: Path
    base_url: str
    ssh_host: str  # an alias from ~/.ssh/config, or "local" to publish into a local folder (testing)
    remote_root: str
    theme: str | None = None  # None = Obsidian's default theme
    mode: str = "dark"
    snippets: list[str] | None = None  # None = whatever is enabled in the vault
    trash_days: int = 30
    extra: dict = field(default_factory=dict)

    @property
    def config_path(self) -> Path:
        return self.vault / CONFIG_REL

    def page_url(self, page_id: str, slug: str) -> str:
        return f"{self.base_url.rstrip('/')}/{page_id}/{slug}"


def is_vault(path: Path) -> bool:
    return (path / ".obsidian").is_dir()


def find_vault(near: Path | None = None) -> Path:
    """The vault containing `near`; else $POSTCARD_VAULT; else the only iCloud Obsidian vault."""
    if near is not None:
        for p in [near, *near.parents]:
            if is_vault(p):
                return p
    env = os.environ.get("POSTCARD_VAULT")
    if env:
        p = Path(env).expanduser()
        if not is_vault(p):
            raise PostcardError(f"POSTCARD_VAULT={env} is not an Obsidian vault (no .obsidian folder).")
        return p
    if ICLOUD_OBSIDIAN.is_dir():
        vaults = [p for p in ICLOUD_OBSIDIAN.iterdir() if is_vault(p)]
        if len(vaults) == 1:
            return vaults[0]
        if len(vaults) > 1:
            names = ", ".join(v.name for v in vaults)
            raise PostcardError(f"Several vaults found ({names}). Set POSTCARD_VAULT to pick one.")
    raise PostcardError("Couldn't find your Obsidian vault. Set POSTCARD_VAULT=/path/to/vault.")


def load_config(vault: Path) -> Config:
    path = vault / CONFIG_REL
    if not path.exists():
        raise PostcardError(f"No config at {path}. Run `postcard init` first.")
    data = tomllib.loads(path.read_text())
    server = data.get("server", {})
    render = data.get("render", {})
    missing = [k for k, v in {"base_url": data.get("base_url"), "server.ssh_host": server.get("ssh_host")}.items() if not v]
    if missing:
        raise PostcardError(f"{path} is missing: {', '.join(missing)}")
    theme = render.get("theme", "")
    return Config(
        vault=vault,
        base_url=data["base_url"],
        ssh_host=server["ssh_host"],
        remote_root=server.get("root", "/srv/postcard"),
        theme=theme or None,
        mode=render.get("mode", "dark"),
        snippets=render.get("snippets"),
        trash_days=int(server.get("trash_days", 30)),
        extra=data,
    )


def write_config(vault: Path, base_url: str, ssh_host: str, root: str, theme: str | None, mode: str) -> Path:
    path = vault / CONFIG_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""# Postcard config. Lives in the vault so a new laptop gets it through iCloud.
base_url = {json.dumps(base_url)}

[server]
ssh_host = {json.dumps(ssh_host)}   # alias from ~/.ssh/config ("local" = publish to a local folder, for testing)
root = {json.dumps(root)}
trash_days = 30            # unpublished pages are kept this long for `postcard publish --restore`

[render]
theme = {json.dumps(theme or "")}   # any Obsidian theme name; "" = Obsidian's default theme
mode = {json.dumps(mode)}            # dark | light
# snippets = ["Colored Sidebar Items"]   # default: the snippets enabled in Obsidian
"""
    )
    return path


def obsidian_appearance(vault: Path) -> dict:
    try:
        return json.loads((vault / ".obsidian" / "appearance.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}
