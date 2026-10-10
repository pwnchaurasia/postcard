"""Publishing operations, independent of the CLI's prompts and printing."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import string
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .build import BuildResult, PageOptions, build
from .config import CACHE_DIR, Config, PostcardError
from .render import Rendered, RenderRequest, render_with_obsidian
from .themes import resolve_theme, snippet_paths
from .vault import Note, VaultIndex, now_iso, read_note, set_postcard_fields

ID_ALPHABET = string.ascii_letters + string.digits
ID_LEN = 12  # ~71 bits: not guessable, not enumerable
ID_RE = re.compile(rf"^[A-Za-z0-9]{{{ID_LEN}}}$")

Renderer = Callable[[RenderRequest], Rendered]


def new_id() -> str:
    return "".join(secrets.choice(ID_ALPHABET) for _ in range(ID_LEN))


def is_id(s: str) -> bool:
    return bool(ID_RE.match(s))


@dataclass
class RenderOptions:
    theme: str  # display name, "default" for Obsidian's own
    theme_css: Path | None
    snippets: list[Path]
    mode: str

    def fingerprint(self) -> list[str]:
        return [self.theme, self.mode, *sorted(p.name for p in self.snippets)]


def render_options(cfg: Config, note: Note, theme: str | None = None, mode: str | None = None) -> RenderOptions:
    """Theme priority: --theme flag > note's `postcard_theme` > $POSTCARD_THEME > config. Same for mode."""
    fm = note.frontmatter
    chosen = next((t for t in (theme, fm.get("postcard_theme"), os.environ.get("POSTCARD_THEME"), cfg.theme) if t), None)
    chosen_mode = next((m for m in (mode, fm.get("postcard_mode"), os.environ.get("POSTCARD_MODE"), cfg.mode) if m), "dark")
    if chosen_mode not in ("dark", "light"):
        raise PostcardError(f'Mode must be "dark" or "light", got "{chosen_mode}".')
    css = resolve_theme(cfg.vault, str(chosen) if chosen else None)
    return RenderOptions(
        theme=css.parent.name if css else "default",
        theme_css=css,
        snippets=snippet_paths(cfg.vault, cfg.snippets),
        mode=str(chosen_mode),
    )


def make_site(cfg: Config, note: Note, page_id: str, ropts: RenderOptions, site: Path,
              renderer: Renderer | None = None, index: VaultIndex | None = None,
              link_map: dict[str, str] | None = None) -> BuildResult:
    rendered = (renderer or render_with_obsidian)(
        RenderRequest(cfg.vault, note.rel, ropts.theme_css, ropts.snippets, ropts.mode, link_map=link_map or None)
    )
    opts = PageOptions(
        page_id=page_id,
        title=note.title,
        slug=note.slug,
        url=cfg.page_url(page_id, note.slug),
        mode=ropts.mode,
        updated=datetime.now(),
    )
    return build(rendered, note, opts, site, index)


def fresh_site_dir(name: str) -> Path:
    site = CACHE_DIR / name
    shutil.rmtree(site, ignore_errors=True)
    site.mkdir(parents=True)
    return site


def record_published(cfg: Config, registry: dict, note: Note, page_id: str, ropts: RenderOptions, content_hash: str) -> dict:
    """Record a successful upload in the registry (in memory). Call `mark_note_published` after the
    registry is safely written, so the note never claims to be live when the server doesn't know it."""
    now = now_iso()
    url = cfg.page_url(page_id, note.slug)
    prev = registry["pages"].get(page_id, {})
    published = prev.get("published") if prev.get("status") == "live" else None
    entry = {
        "title": note.title,
        "slug": note.slug,
        "url": url,
        "source": note.rel,
        "status": "live",
        "published": published or now,
        "updated": now,
        "content_hash": content_hash,
        "theme": ropts.theme,
        "mode": ropts.mode,
    }
    registry["pages"][page_id] = entry
    return entry


def mark_note_published(note: Note, page_id: str, entry: dict) -> None:
    set_postcard_fields(note.path, {
        "postcard_url": entry["url"],
        "postcard_id": page_id,
        "postcard_status": "live",
        "postcard_published": entry["published"],
        "postcard_updated": entry["updated"],
    })


def record_unpublished(registry: dict, page_id: str, note_path: Path | None, status: str = "unpublished") -> None:
    entry = registry["pages"].get(page_id)
    if entry is not None:
        entry["status"] = status
        entry["unpublished"] = now_iso()
    if note_path is not None and note_path.exists():
        note = read_note(note_path, note_path.parent)
        if note.frontmatter.get("postcard_id") == page_id:
            fields = {k: _fm_str(note.frontmatter[k]) for k in ("postcard_url", "postcard_id", "postcard_published") if note.frontmatter.get(k)}
            set_postcard_fields(note_path, {**fields, "postcard_status": status, "postcard_updated": now_iso()})



def _fm_str(v: object) -> str:
    # YAML turns `2026-10-10T09:40:00` into a datetime; keep the ISO "T" form when writing it back.
    return v.isoformat() if isinstance(v, datetime) else str(v)
