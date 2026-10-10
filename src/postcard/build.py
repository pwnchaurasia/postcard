"""Turn the bridge's render output into publishable files:

    <site>/_s/<hash>.css, <hash>.woff2          shared by all pages, cached forever
    <site>/<id>/index.html
    <site>/<id>/a/<hash>.webp                   this page's images
"""

from __future__ import annotations

import hashlib
import html
import io
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

from .render import ASSET_TOKEN, Rendered
from .vault import Note, VaultIndex

STATIC = Path(__file__).parent / "static"
MAX_IMAGE_WIDTH = 2000
RASTER = {"png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"}
FONTS = {  # (file, weight, style) — registered under the names Obsidian users actually set
    "families": ["Hack Nerd Font", "Hack Nerd Font Mono", "Hack"],
    "files": [("hack-regular.woff2", 400, "normal"), ("hack-bold.woff2", 700, "normal"),
              ("hack-italic.woff2", 400, "italic"), ("hack-bolditalic.woff2", 700, "italic")],
}


@dataclass
class PageOptions:
    page_id: str
    title: str
    slug: str
    url: str
    mode: str
    updated: datetime


@dataclass
class BuildResult:
    site: Path
    page_dir: Path
    css_name: str
    warnings: list[str] = field(default_factory=list)
    gps_stripped: list[str] = field(default_factory=list)

    @property
    def page_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.page_dir.rglob("*") if p.is_file())


@dataclass
class NavContext:
    """Sidebar + previous/next links for pages published as part of a folder."""
    folder_title: str
    home_href: str
    sidebar_html: str
    pager_html: str


def build(rendered: Rendered, note: Note, opts: PageOptions, site: Path, index: VaultIndex | None = None) -> BuildResult:
    """A single published note: <site>/<id>/index.html."""
    root = site / opts.page_id
    if root.exists():
        shutil.rmtree(root)
    css_name = build_css(rendered, site)
    return build_page(rendered, note, opts, site, css_name, index)


def build_css(rendered: Rendered, site: Path) -> str:
    """Obsidian's CSS + theme + fonts + postcard.css as one content-hashed file in <site>/_s/. Returns its name."""
    shared = site / "_s"
    shared.mkdir(parents=True, exist_ok=True)
    css = rendered.css
    for asset in sorted(rendered.assets.iterdir()) if rendered.assets.is_dir() else []:
        token = ASSET_TOKEN + asset.name
        if token in css:
            data = asset.read_bytes()
            name = _hashed_name(data, asset.suffix.lstrip(".").lower())
            (shared / name).write_bytes(data)
            css = css.replace(token, name)  # the stylesheet lives in /_s/ too, so a bare name works
    css = css + "\n\n" + _font_css(shared) + "\n\n" + (STATIC / "postcard.css").read_text()
    css_name = _hashed_name(css.encode(), "css")
    (shared / css_name).write_text(css, encoding="utf-8")
    return css_name


def build_page(rendered: Rendered, note: Note, opts: PageOptions, site: Path, css_name: str,
               index: VaultIndex | None = None, *, page_path: str = "", nav: NavContext | None = None) -> BuildResult:
    """One page at <site>/<id>/<page_path>/index.html; its images go to <site>/<id>/a/ (shared within a folder)."""
    root = site / opts.page_id
    page_dir = root / page_path if page_path else root
    (root / "a").mkdir(parents=True, exist_ok=True)
    page_dir.mkdir(parents=True, exist_ok=True)

    meta = rendered.meta
    result = BuildResult(site=site, page_dir=root, css_name=css_name, warnings=list(meta.get("warnings", [])))
    content = rendered.content

    for asset in sorted(rendered.assets.iterdir()) if rendered.assets.is_dir() else []:
        token = ASSET_TOKEN + asset.name
        if token not in content:
            continue
        data, ext = asset.read_bytes(), asset.suffix.lstrip(".").lower()
        if ext in RASTER:
            data, ext, gps = process_image(data, ext)
            if gps:
                result.gps_stripped.append(asset.name)
        name = _hashed_name(data, ext)
        (root / "a" / name).write_bytes(data)
        content = content.replace(token, f"/{opts.page_id}/a/{name}")

    cover_src = None
    if cover := note.frontmatter.get("cover"):
        cover_src = _cover(str(cover), note, opts, root, index or VaultIndex(note.vault), result)

    # When a note opens with its own `# Heading`, that heading is the page title: Obsidian's inline title
    # on top of it reads as a duplicate. Keep the inline title only when it's an explicit frontmatter
    # `title:` that differs from the heading (e.g. title "Vietnam Micro Blog", first heading "# Day 1").
    first_h1 = re.match(r"\s*(?:<div>\s*)?<h1\b[^>]*>(.*?)</h1>", content, re.S)
    same_text = bool(first_h1) and _text(first_h1.group(1)).lower() == opts.title.strip().lower()
    title_in_content = bool(first_h1) and (same_text or not note.frontmatter.get("title"))
    body_text = content[first_h1.end():] if title_in_content else content

    page = render_page(
        content=content,
        title=opts.title,
        show_inline_title=not title_in_content,
        css_href=f"/_s/{css_name}",
        body_class=meta.get("body_class", f"theme-{opts.mode}"),
        body_style=_clean_body_style(meta.get("body_style", "")),
        icon=str(note.frontmatter.get("icon") or "") or None,
        cover_src=cover_src,
        description=str(note.frontmatter.get("description") or "") or _summary(body_text),
        url=opts.url,
        updated=opts.updated,
        nav=nav,
    )
    (page_dir / "index.html").write_text(page, encoding="utf-8")
    return result


def process_image(data: bytes, ext: str) -> tuple[bytes, str, bool]:
    """Strip all metadata (incl. GPS), fix rotation, cap the width, convert to WebP. Returns (bytes, ext, had_gps)."""
    try:
        img = Image.open(io.BytesIO(data))
        had_gps = bool(img.getexif().get_ifd(0x8825))
        if getattr(img, "n_frames", 1) > 1:
            return data, ext, had_gps  # animated: keep as-is
        img = ImageOps.exif_transpose(img)
        if img.width > MAX_IMAGE_WIDTH:
            img = img.resize((MAX_IMAGE_WIDTH, round(img.height * MAX_IMAGE_WIDTH / img.width)), Image.LANCZOS)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if "A" in img.getbands() or img.mode == "P" else "RGB")
        buf = io.BytesIO()
        if ext == "png":  # screenshots/diagrams: keep text crisp
            img.save(buf, "WEBP", lossless=True, method=4)
        else:
            img.save(buf, "WEBP", quality=82, method=4)
        return buf.getvalue(), "webp", had_gps
    except Exception:
        return data, ext, False  # unknown format (e.g. HEIC): ship the original bytes


def _cover(cover: str, note: Note, opts: PageOptions, page_dir: Path, index: VaultIndex, result: BuildResult) -> str | None:
    if re.match(r"https?://", cover):
        return cover
    link = cover.strip()
    if link.startswith("[[") and link.endswith("]]"):
        link = link[2:-2]
    path = index.resolve(link.split("|")[0], note.path)
    if path is None or not path.exists():
        result.warnings.append(f"Cover image not found: {cover}")
        return None
    data, ext, gps = process_image(path.read_bytes(), path.suffix.lstrip(".").lower())
    if gps:
        result.gps_stripped.append(path.name)
    name = _hashed_name(data, ext)
    (page_dir / "a" / name).write_bytes(data)
    return f"/{opts.page_id}/a/{name}"


def _font_css(shared: Path) -> str:
    faces = []
    for file, weight, style in FONTS["files"]:
        data = (STATIC / "fonts" / file).read_bytes()
        name = _hashed_name(data, "woff2")
        if not (shared / name).exists():
            (shared / name).write_bytes(data)
        for family in FONTS["families"]:
            faces.append(
                f'@font-face{{font-family:"{family}";src:url("{name}") format("woff2");'
                f"font-weight:{weight};font-style:{style};font-display:swap}}"
            )
    return "\n".join(faces)


def _hashed_name(data: bytes, ext: str) -> str:
    return f"{hashlib.sha256(data).hexdigest()[:16]}.{ext}"


def _text(fragment: str) -> str:
    text = re.sub(r"<(script|style|svg)\b.*?</\1>", " ", fragment, flags=re.S)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text).strip()


def _summary(content: str, limit: int = 160) -> str:
    text = _text(content)
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def _clean_body_style(style: str) -> str:
    # Obsidian's window zoom is an app setting, not part of the note's look.
    return ";".join(d for d in style.split(";") if d.strip() and not d.strip().startswith("--zoom-factor"))


PAGE_JS = """
(function () {
  var sb = document.querySelector(".postcard-sidebar"), active = sb && sb.querySelector(".is-active");
  if (active) sb.scrollTop = active.offsetTop - sb.clientHeight / 2;  // only the sidebar scrolls, not the page
})();
document.addEventListener("click", function (e) {
  var title = e.target.closest(".callout.is-collapsible > .callout-title");
  if (title) {
    var c = title.parentElement, body = c.querySelector(":scope > .callout-content");
    c.classList.toggle("is-collapsed");
    if (body) body.style.display = c.classList.contains("is-collapsed") ? "none" : "";
  }
  var folder = e.target.closest(".postcard-sidebar .nav-folder-title.mod-collapsible");
  if (folder) folder.parentElement.classList.toggle("is-collapsed");
  var menu = e.target.closest(".postcard-menu");
  if (menu || e.target.closest(".postcard-scrim")) {
    var open = document.body.classList.toggle("postcard-nav-open");
    var m = document.querySelector(".postcard-menu");
    if (m) m.setAttribute("aria-expanded", String(open));
  }
  var btn = e.target.closest(".copy-code-button");
  if (btn) {
    var code = btn.parentElement.querySelector("code");
    navigator.clipboard.writeText(code ? code.innerText : "");
    var old = btn.textContent; btn.textContent = "Copied!";
    setTimeout(function () { btn.textContent = old; }, 1200);
  }
});
"""


def render_page(*, content: str, title: str, show_inline_title: bool, css_href: str, body_class: str, body_style: str, icon: str | None,
                cover_src: str | None, description: str, url: str, updated: datetime, nav: NavContext | None = None) -> str:
    e = html.escape
    base = url.split("/", 3)
    origin = "/".join(base[:3]) if len(base) >= 3 else ""
    og_image = (origin + cover_src) if cover_src and cover_src.startswith("/") else cover_src
    favicon = (
        "data:image/svg+xml," + e(f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>{icon}</text></svg>")
        if icon else None
    )
    head = [
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="robots" content="noindex, nofollow, noarchive">',
        '<meta name="referrer" content="no-referrer">',
        f"<title>{e(title + (' · ' + nav.folder_title if nav else ''))}</title>",
        f'<meta name="description" content="{e(description)}">',
        f'<meta property="og:title" content="{e(title)}">',
        f'<meta property="og:description" content="{e(description)}">',
        f'<meta property="og:url" content="{e(url)}">',
        '<meta property="og:type" content="article">',
        f'<link rel="stylesheet" href="{e(css_href)}">',
    ]
    if og_image:
        head += [f'<meta property="og:image" content="{e(og_image)}">', '<meta name="twitter:card" content="summary_large_image">']
    if favicon:
        head.append(f'<link rel="icon" href="{favicon}">')

    top = []
    if cover_src:
        top.append(f'<img class="postcard-cover" src="{e(cover_src)}" alt="">')
    if icon:
        top.append(f'<div class="postcard-icon">{e(icon)}</div>')
    updated_html = f'<div class="postcard-updated">Updated {updated.strftime("%-d %b %Y")}</div>'
    if show_inline_title:
        top += [f'<div class="inline-title">{e(title)}</div>', updated_html]
    else:
        content = content.replace("</h1>", "</h1>" + updated_html, 1)  # right under the note's own title

    menu = sidebar = pager = ""
    if nav:
        body_class += " postcard-collection"
        menu = (f'<button class="postcard-menu" type="button" aria-controls="postcard-sidebar" aria-expanded="false">'
                f'<span aria-hidden="true">☰</span> {e(nav.folder_title)}</button>')
        sidebar = (f'<div class="workspace-split mod-left-split postcard-sidebar" id="postcard-sidebar">'
                   f'<a class="postcard-sidebar-title" href="{e(nav.home_href)}">{e(nav.folder_title)}</a>'
                   f'<div class="nav-files-container">{nav.sidebar_html}</div></div>'
                   f'<div class="postcard-scrim" aria-hidden="true"></div>')
        pager = nav.pager_html

    # Same nesting as Obsidian's reading view (and file explorer), so theme and snippet selectors like
    # `.workspace-leaf-content[data-type="markdown"] .markdown-preview-view h1` still match.
    return f"""<!doctype html>
<html lang="en">
<head>
{chr(10).join(head)}
</head>
<body class="{e(body_class)} postcard" style="{e(body_style)}">
{menu}
<div class="app-container"><div class="workspace">{sidebar}<div class="workspace-split mod-root"><div class="workspace-leaf mod-active">
<div class="workspace-leaf-content" data-type="markdown" data-mode="preview"><div class="view-content"><div class="markdown-reading-view">
<div class="markdown-preview-view markdown-rendered node-insert-event is-readable-line-width allow-fold-headings show-indentation-guide">
<div class="markdown-preview-sizer markdown-preview-section">
{chr(10).join(top)}
{content}
{pager}
</div>
</div>
</div></div></div>
</div></div></div></div>
<script>{PAGE_JS}</script>
</body>
</html>
"""
