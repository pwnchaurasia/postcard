"""Publishing a whole folder (with subfolders) as one link: sidebar navigation, prev/next, working internal links.

    <site>/<id>/index.html            the folder's home page (index/readme/overview note, else the first note)
    <site>/<id>/<note-slug>/index.html
    <site>/<id>/a/<hash>.webp         images of all the folder's notes
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .build import BuildResult, NavContext, PageOptions, build_css, build_page
from .config import Config, PostcardError
from .core import RenderOptions, Renderer
from .render import RenderRequest, Rendered
from .validate import Report, _prose, validate
from .vault import Note, VaultIndex, now_iso, read_note, slugify

HOME_NAMES = re.compile(r"^(index|readme|home|start here)$|overview", re.I)
RESERVED_SLUGS = {"a"}  # <id>/a/ holds the images
CHEVRON = ('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" '
           'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
           'class="svg-icon right-triangle"><path d="M3 8L12 17L21 8"></path></svg>')


@dataclass
class FolderPage:
    note: Note
    rel: str  # path inside the published folder, e.g. "Example/AZURE data.md"
    slug: str

    @property
    def title(self) -> str:
        return self.note.title


@dataclass
class FolderPlan:
    folder: Path
    pages: list[FolderPage]
    home: FolderPage
    skipped: list[tuple[str, list[str]]] = field(default_factory=list)  # (rel, errors)
    warnings: list[tuple[str, str]] = field(default_factory=list)
    info: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.folder.name

    @property
    def slug(self) -> str:
        return slugify(self.folder.name)

    def link_map(self, page_id: str) -> dict[str, str]:
        """Vault path -> root-relative URL, so links work on the server and in `postcard preview` alike."""
        return {p.note.rel: f"/{page_id}/{p.slug}" for p in self.pages}

    def content_hash(self, ropts: RenderOptions) -> str:
        h = hashlib.sha256()
        for p in self.pages:
            h.update(f"{p.rel}\0{p.slug}\0{p.note.content_hash(*ropts.fingerprint())}\n".encode())
        return h.hexdigest()[:16]


# --- planning -----------------------------------------------------------------

def _natural_key(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def scan(folder: Path) -> list[Path]:
    """Notes in reading order: a folder's own notes first (natural sort: 2 before 10), then each subfolder."""
    files, dirs = [], []
    for p in folder.iterdir():
        name = p.name
        if name.startswith(".") and name.endswith(".md.icloud"):
            files.append(p.with_name(name[1:-len(".icloud")]))  # evicted by iCloud; downloaded when read
        elif name.startswith("."):
            continue
        elif p.is_dir():
            dirs.append(p)
        elif p.suffix.lower() == ".md":
            files.append(p)
    out = sorted(files, key=lambda p: _natural_key(p.name))
    for d in sorted(dirs, key=lambda p: _natural_key(p.name)):
        out += scan(d)
    return out


def plan(cfg: Config, folder: Path, index: VaultIndex | None = None) -> FolderPlan:
    index = index or VaultIndex(cfg.vault)
    pages: list[FolderPage] = []
    skipped, warnings, info = [], [], []
    used: set[str] = set(RESERVED_SLUGS)
    for path in scan(folder):
        rel = path.relative_to(folder).as_posix()
        note = read_note(path, cfg.vault)
        report: Report = validate(note, index)
        if report.errors:
            skipped.append((rel, report.errors))
            continue
        warnings += [(rel, w) for w in report.warnings]
        info += [f"{rel}: {i}" for i in report.info if "snapshot" not in i]
        slug = base = slugify(path.stem)
        n = 2
        while slug in used:
            slug, n = f"{base}-{n}", n + 1
        used.add(slug)
        pages.append(FolderPage(note=note, rel=rel, slug=slug))
    if not pages:
        detail = "".join(f"\n  {rel}: {errs[0]}" for rel, errs in skipped)
        raise PostcardError(f"Nothing to publish in {folder.name}.{detail}")
    top_level = [p for p in pages if "/" not in p.rel]
    home = next((p for p in top_level if HOME_NAMES.search(Path(p.rel).stem)), (top_level or pages)[0])
    return FolderPlan(folder=folder, pages=pages, home=home, skipped=skipped, warnings=warnings, info=info)


# --- rendering (with a cache, so republishing only re-renders notes that changed) -------------

# $inline$ or $$display$$ math. MathJax adds the CSS for each glyph only when it draws it.
MATH_RE = re.compile(r"\$\$|(?<![\\\w$])\$[^\s$][^$\n]*?(?<!\s)\$(?!\w)")


def render_all(cfg: Config, fplan: FolderPlan, page_id: str, ropts: RenderOptions, renderer: Renderer,
               progress=lambda done, total, rel: None) -> tuple[dict[str, Rendered], Rendered]:
    """Returns (rendered by page rel, a render that carries the CSS)."""
    from .core import CACHE_DIR  # read at call time so tests can point it elsewhere

    cache = CACHE_DIR / "render-cache"
    cache.mkdir(parents=True, exist_ok=True)
    link_map = fplan.link_map(page_id)
    links_key = json.dumps(link_map, sort_keys=True)
    out: dict[str, Rendered] = {}
    total = len(fplan.pages) + 1

    def request(page: FolderPage, skip_css: bool) -> RenderRequest:
        return RenderRequest(cfg.vault, page.note.rel, ropts.theme_css, ropts.snippets, ropts.mode,
                             link_map=link_map, skip_css=skip_css)

    for i, page in enumerate(fplan.pages):
        progress(i, total, page.rel)
        key = hashlib.sha256(
            "\0".join([page.note.text, links_key, *ropts.fingerprint(), page.note.rel]).encode()
        ).hexdigest()[:24]
        cached = cache / key
        # Notes with math are always re-rendered, so MathJax's glyph styles exist when the CSS is collected below.
        if cached.is_dir() and not MATH_RE.search(_prose(page.note.body)):
            os.utime(cached)  # keep recently used entries out of _prune
            out[page.rel] = Rendered(cached)
            continue
        rendered = renderer(request(page, skip_css=True))
        shutil.rmtree(cached, ignore_errors=True)
        shutil.copytree(rendered.dir, cached)
        out[page.rel] = Rendered(cached)

    # Collect the CSS last: by now it includes every style Obsidian added while rendering the notes above.
    progress(len(fplan.pages), total, "styles")
    css_render = renderer(request(fplan.home, skip_css=False))
    progress(total, total, "")
    _prune(cache)
    return out, css_render


def _prune(cache: Path, days: int = 30) -> None:
    cutoff = time.time() - days * 86400
    for d in cache.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)


# --- building -----------------------------------------------------------------

def build_folder(cfg: Config, fplan: FolderPlan, page_id: str, rendered: dict[str, Rendered], css_render: Rendered,
                 site: Path, mode: str, index: VaultIndex | None = None) -> list[BuildResult]:
    root = site / page_id
    if root.exists():
        shutil.rmtree(root)
    css_name = build_css(css_render, site)
    index = index or VaultIndex(cfg.vault)
    now = datetime.now()
    results = []
    pages = fplan.pages
    for i, page in enumerate(pages):
        prev_p = pages[i - 1] if i > 0 else None
        next_p = pages[i + 1] if i + 1 < len(pages) else None
        nav = NavContext(
            folder_title=fplan.title,
            home_href=f"/{page_id}/{fplan.slug}",
            sidebar_html=sidebar(fplan, page_id, page),
            pager_html=pager(page_id, prev_p, next_p),
        )
        opts = PageOptions(page_id=page_id, title=page.title, slug=page.slug,
                           url=cfg.page_url(page_id, page.slug), mode=mode, updated=now)
        # The home note is also the folder's own page (/<id>/ and /<id>/<folder-slug>).
        for path in [page.slug, ""] if page is fplan.home else [page.slug]:
            results.append(build_page(rendered[page.rel], page.note, opts, site, css_name, index, page_path=path, nav=nav))
    return results


def sidebar(fplan: FolderPlan, page_id: str, active: FolderPage) -> str:
    """Obsidian's file-explorer markup, so the theme and sidebar snippets (e.g. colored folders) apply."""
    vault_prefix = fplan.folder.relative_to(fplan.pages[0].note.vault).as_posix()
    tree: dict = {}
    for p in fplan.pages:
        node = tree
        parts = p.rel.split("/")
        for d in parts[:-1]:
            node = node.setdefault(("dir", d), {})
        node[("file", parts[-1])] = p

    e = html.escape

    def render(node: dict, rel_dir: str) -> str:
        out = []
        files = [(k, v) for k, v in node.items() if k[0] == "file"]
        dirs = [(k, v) for k, v in node.items() if k[0] == "dir"]
        for (_, name), page in files:
            cls = "tree-item-self nav-file-title is-clickable" + (" is-active" if page is active else "")
            out.append(
                f'<div class="tree-item nav-file"><a class="{cls}" data-path="{e(page.note.rel)}" '
                f'href="/{page_id}/{page.slug}"><div class="tree-item-inner nav-file-title-content">'
                f"{e(Path(name).stem)}</div></a></div>"
            )
        for (_, name), child in dirs:
            child_rel = f"{rel_dir}/{name}" if rel_dir else name
            open_ = active.rel.startswith(child_rel + "/")
            out.append(
                f'<div class="tree-item nav-folder{"" if open_ else " is-collapsed"}">'
                f'<div class="tree-item-self nav-folder-title is-clickable mod-collapsible" data-path="{e(vault_prefix + "/" + child_rel)}">'
                f'<div class="tree-item-icon collapse-icon">{CHEVRON}</div>'
                f'<div class="tree-item-inner nav-folder-title-content">{e(name)}</div></div>'
                f'<div class="tree-item-children nav-folder-children">{render(child, child_rel)}</div></div>'
            )
        return "".join(out)

    # The published folder itself is the root folder, so prefix-based colors (02 - Study/…) match the vault.
    return (
        f'<div class="tree-item nav-folder mod-root"><div class="tree-item-self nav-folder-title" data-path="{e(vault_prefix)}">'
        f'<div class="tree-item-inner nav-folder-title-content">{e(fplan.title)}</div></div>'
        f'<div class="tree-item-children nav-folder-children">{render(tree, "")}</div></div>'
    )


def pager(page_id: str, prev_p: FolderPage | None, next_p: FolderPage | None) -> str:
    e = html.escape
    parts = []
    if prev_p:
        parts.append(f'<a class="postcard-prev" href="/{page_id}/{prev_p.slug}"><small>← Previous</small><span>{e(prev_p.title)}</span></a>')
    if next_p:
        parts.append(f'<a class="postcard-next" href="/{page_id}/{next_p.slug}"><small>Next →</small><span>{e(next_p.title)}</span></a>')
    return f'<nav class="postcard-pager">{"".join(parts)}</nav>' if parts else ""


# --- state ----------------------------------------------------------------------

def find_live(registry: dict, folder_rel: str) -> tuple[str, dict] | None:
    for pid, entry in registry["pages"].items():
        if entry.get("kind") == "folder" and entry.get("source") == folder_rel and entry.get("status") == "live":
            return pid, entry
    return None


def find_latest(registry: dict, folder_rel: str) -> tuple[str, dict] | None:
    matches = [(pid, e) for pid, e in registry["pages"].items() if e.get("kind") == "folder" and e.get("source") == folder_rel]
    if not matches:
        return None
    # Timestamps have 1-second resolution; on a tie the entry added to the registry later is the newer one.
    _, latest = max(enumerate(matches), key=lambda im: (im[1][1].get("updated", ""), im[0]))
    return latest


def record(cfg: Config, registry: dict, fplan: FolderPlan, page_id: str, ropts: RenderOptions, content_hash: str) -> dict:
    now = now_iso()
    prev = registry["pages"].get(page_id, {})
    folder_rel = fplan.folder.relative_to(cfg.vault).as_posix()
    entry = {
        "kind": "folder",
        "title": fplan.title,
        "slug": fplan.slug,
        "url": cfg.page_url(page_id, fplan.slug),
        "source": folder_rel,
        "status": "live",
        "published": prev.get("published") if prev.get("status") == "live" else now,
        "updated": now,
        "content_hash": content_hash,
        "theme": ropts.theme,
        "mode": ropts.mode,
        "pages": {p.note.rel: p.slug for p in fplan.pages},
    }
    registry["pages"][page_id] = entry
    return entry


def local_state_path(cfg: Config) -> Path:
    return cfg.vault / ".postcard" / "folders.json"


def read_local_state(cfg: Config) -> dict:
    try:
        return json.loads(local_state_path(cfg).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def write_local_state(cfg: Config, folder_rel: str, page_id: str, url: str, status: str) -> None:
    """A copy in the vault (iCloud) so a lost server can be rebuilt with the same links."""
    state = read_local_state(cfg)
    state[folder_rel] = {"id": page_id, "url": url, "status": status, "updated": now_iso()}
    path = local_state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True))


def folder_link_map(registry: dict) -> dict[str, str]:
    """Links from single notes into live folders: vault path -> root-relative URL."""
    out = {}
    for pid, entry in registry["pages"].items():
        if entry.get("kind") == "folder" and entry.get("status") == "live":
            for rel, slug in entry.get("pages", {}).items():
                out[rel] = f"/{pid}/{slug}"
    return out

