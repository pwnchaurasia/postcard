"""Obsidian themes and CSS snippets: installed in the vault, or downloaded into `.postcard/themes/`."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from .config import PostcardError, obsidian_appearance

COMMUNITY_THEMES_URL = "https://raw.githubusercontent.com/obsidianmd/obsidian-releases/master/community-css-themes.json"
DEFAULT_NAMES = {"", "default", "obsidian", "none"}


def theme_dirs(vault: Path) -> list[Path]:
    return [vault / ".obsidian" / "themes", vault / ".postcard" / "themes"]


def installed_themes(vault: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for d in theme_dirs(vault):
        if d.is_dir():
            for t in sorted(d.iterdir()):
                if (t / "theme.css").is_file():
                    found.setdefault(t.name, t / "theme.css")
    return found


def resolve_theme(vault: Path, name: str | None) -> Path | None:
    """theme.css for a theme name (case-insensitive). None means Obsidian's default theme."""
    if name is None or name.strip().lower() in DEFAULT_NAMES:
        return None
    themes = installed_themes(vault)
    for n, p in themes.items():
        if n.lower() == name.strip().lower():
            return p
    have = ", ".join(themes) or "none"
    raise PostcardError(f'Theme "{name}" isn\'t available (have: {have}). Get it with: postcard themes add "{name}"')


def canonical_theme_name(vault: Path, name: str | None) -> str:
    path = resolve_theme(vault, name)
    return path.parent.name if path else "default"


def snippet_paths(vault: Path, names: list[str] | None) -> list[Path]:
    """CSS snippets to include. `None` = the ones currently enabled in Obsidian."""
    if names is None:
        names = obsidian_appearance(vault).get("enabledCssSnippets", [])
    out = []
    for n in names:
        p = vault / ".obsidian" / "snippets" / f"{n}.css"
        if not p.is_file():
            raise PostcardError(f"CSS snippet not found: {p}")
        out.append(p)
    return out


def add_theme(vault: Path, name: str) -> Path:
    """Download a community theme's theme.css into `.postcard/themes/<Name>/` (Obsidian itself is untouched)."""
    try:
        catalog = json.loads(_get(COMMUNITY_THEMES_URL))
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        raise PostcardError(f"Couldn't fetch the community theme list: {e}") from e
    entry = next((t for t in catalog if t.get("name", "").lower() == name.strip().lower()), None)
    if entry is None:
        close = [t["name"] for t in catalog if name.lower() in t.get("name", "").lower()][:8]
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        raise PostcardError(f'No community theme named "{name}".{hint}')

    repo = entry["repo"]
    css = None
    for url in (
        f"https://github.com/{repo}/releases/latest/download/theme.css",
        f"https://raw.githubusercontent.com/{repo}/HEAD/theme.css",
    ):
        try:
            css = _get(url)
            break
        except urllib.error.URLError:
            continue
    if css is None:
        raise PostcardError(f"Couldn't download theme.css from github.com/{repo}")

    dest = vault / ".postcard" / "themes" / entry["name"] / "theme.css"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(css, encoding="utf-8")
    return dest


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "postcard"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")
