"""The `postcard` command."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from . import __version__, core
from . import folders as fold
from .build import BuildResult
from .config import Config, PostcardError, find_vault, load_config, obsidian_appearance, write_config
from .core import (
    fresh_site_dir,
    is_id,
    make_site,
    mark_note_published,
    new_id,
    record_published,
    record_unpublished,
    render_options,
)
from .preview import serve
from .remote import Remote
from .render import (
    PLUGIN_ID,
    bundled_plugin_dir,
    cleanup_jobs,
    install_plugin,
    installed_plugin_version,
    obsidian_running,
    plugin_enabled,
)
from .themes import add_theme, installed_themes, resolve_theme, snippet_paths
from .validate import Report, validate
from .vault import Note, VaultIndex, find_notes, icloud_placeholder, read_note

app = typer.Typer(
    help="Share single Obsidian notes as unlisted pages on your own server.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    add_completion=False,
)
themes_app = typer.Typer(help="Obsidian themes to publish with.", no_args_is_help=True)
plugin_app = typer.Typer(help="The Postcard Bridge plugin inside Obsidian.", no_args_is_help=True)
server_app = typer.Typer(help="Server setup helpers.", no_args_is_help=True)
app.add_typer(themes_app, name="themes")
app.add_typer(plugin_app, name="plugin")
app.add_typer(server_app, name="server")

console = Console()
err = Console(stderr=True)

NoteArg = Annotated[str, typer.Argument(help="Path to a note or folder, or part of a note's name.")]
ThemeOpt = Annotated[Optional[str], typer.Option("--theme", "-t", help='Obsidian theme to publish with, e.g. "Dark Moss".')]
ModeOpt = Annotated[Optional[str], typer.Option("--mode", "-m", help="dark | light")]
YesOpt = Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")]

PREVIEW_ID = "previewNote1"


def main() -> None:
    try:
        app()
    except PostcardError as e:
        err.print(f"[red]✗[/red] {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


# --- helpers ----------------------------------------------------------------

def _resolve_note(query: str) -> tuple[Config, Path]:
    cfg, path = _resolve_target(query)
    if path.is_dir():
        raise PostcardError(f"{path.name} is a folder; this command works on single notes.")
    return cfg, path


def _resolve_target(query: str) -> tuple[Config, Path]:
    """A note or a folder: a path (absolute or relative), or part of a note's / exact folder's name."""
    p = Path(query.rstrip("/")).expanduser()
    if p.exists() or icloud_placeholder(p.absolute()).exists():
        path = p.absolute()
        vault = find_vault(path if path.is_dir() else path.parent)
        if vault not in path.parents:
            raise PostcardError(f"{path} isn't inside the vault {vault}." if path != vault else "Can't publish the whole vault.")
        return load_config(vault), path

    vault = find_vault(Path.cwd())
    rel = vault / query.rstrip("/")
    if rel.is_dir() and rel != vault:
        return load_config(vault), rel
    name = Path(query.rstrip("/")).name.lower()
    dirs = sorted(d for d in vault.rglob("*") if d.is_dir() and d.name.lower() == name and not any(
        part.startswith(".") for part in d.relative_to(vault).parts))
    matches = dirs + find_notes(query, vault)
    if not matches:
        raise PostcardError(f'No note or folder matching "{query}" in {vault.name}.')
    if len(matches) == 1:
        return load_config(vault), matches[0]
    shown = matches[:15]
    for i, m in enumerate(shown, 1):
        icon = "📁 " if m.is_dir() else ""
        console.print(f"  [cyan]{i:>2}[/cyan]  {icon}{m.relative_to(vault)}")
    pick = IntPrompt.ask("Which one?", choices=[str(i) for i in range(1, len(shown) + 1)], show_choices=False)
    return load_config(vault), shown[pick - 1]


def _show_report(report: Report) -> None:
    for m in report.info:
        console.print(f"[blue]ℹ[/blue] {m}")
    for m in report.warnings:
        console.print(f"[yellow]⚠[/yellow] {m}")
    for m in report.errors:
        console.print(f"[red]✗[/red] {m}")


def _show_build(result: BuildResult) -> None:
    for w in result.warnings:
        console.print(f"[yellow]⚠[/yellow] {w}")
    if result.gps_stripped:
        console.print(f"[blue]ℹ[/blue] Removed GPS location from {len(result.gps_stripped)} photo(s).")


def _ago(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        then = datetime.fromisoformat(str(iso))
    except ValueError:
        return str(iso)
    s = (datetime.now() - then).total_seconds()
    for unit, size in (("year", 31536000), ("month", 2592000), ("week", 604800), ("day", 86400), ("hour", 3600), ("minute", 60)):
        if s >= size:
            n = int(s // size)
            return f"{n} {unit}{'s' if n > 1 else ''} ago"
    return "just now"


def _copy(text: str) -> bool:
    try:
        subprocess.run(["pbcopy"], input=text, text=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def _registry(remote: Remote) -> dict:
    with console.status(f"Reading registry from {remote.host}…"):
        return remote.read_registry()


def _choose_id(note: Note, registry: dict, *, restore: bool, force: bool, yes: bool, content_hash: str) -> tuple[str, str]:
    """Decide which id this publish uses. Returns (id, verb for the summary)."""
    pages = registry["pages"]
    existing = note.page_id
    entry = pages.get(existing) if existing else None

    # A duplicated note carries its original's id; don't let the copy overwrite the original page.
    if entry and entry.get("source") != note.rel:
        original = note.vault / entry["source"]
        if original.exists() and read_note(original, note.vault).page_id == existing:
            console.print(f"[yellow]⚠[/yellow] This note has the same Postcard id as [bold]{entry['source']}[/bold] (a copy?). Giving it its own link.")
            existing, entry = None, None

    if entry and entry.get("status") == "live":
        if entry.get("content_hash") == content_hash and not force:
            console.print(f"[green]✓[/green] Nothing changed since the last publish ({_ago(entry.get('updated'))}). Skipping.  [dim](--force to push anyway)[/dim]")
            console.print(f"  {entry['url']}")
            raise typer.Exit(0)
        console.print(Panel.fit(
            f"[bold]{entry.get('title')}[/bold] is already published\n"
            f"URL:        {entry['url']}\n"
            f"Published:  {_ago(entry.get('published'))}\n"
            f"Updated:    {_ago(entry.get('updated'))}\n"
            f"Changes:    {'none (forced)' if entry.get('content_hash') == content_hash else 'note or theme changed'}",
            border_style="yellow",
        ))
        if not yes and not Confirm.ask("Update the live page? Same link, new content", default=False):
            raise typer.Exit(1)
        return existing, "Updated"

    if existing and (restore or (entry is None and note.frontmatter.get("postcard_status") == "live")):
        if entry is None and not restore:
            console.print("[blue]ℹ[/blue] The server doesn't know this page (new server or lost registry?). Re-publishing with the same link.")
        return existing, "Restored" if restore else "Republished"

    if existing and entry:
        console.print(f"[blue]ℹ[/blue] This note was {entry.get('status')} {_ago(entry.get('unpublished'))}. "
                      "It gets a [bold]new[/bold] link; the old one stays dead. (--restore to bring the old link back)")

    page_id = new_id()
    while page_id in pages:
        page_id = new_id()
    return page_id, "Published"


# --- commands ---------------------------------------------------------------

@app.command()
def publish(
    note: NoteArg,
    theme: ThemeOpt = None,
    mode: ModeOpt = None,
    yes: YesOpt = False,
    force: Annotated[bool, typer.Option("--force", help="Publish even if nothing changed.")] = False,
    restore: Annotated[bool, typer.Option("--restore", help="Re-use the old link of an unpublished note.")] = False,
    open_: Annotated[bool, typer.Option("--open", "-o", help="Open the page in the browser afterwards.")] = False,
):
    """Publish a note or a whole folder, or update it if it's already live."""
    cfg, path = _resolve_target(note)
    if path.is_dir():
        _publish_folder(cfg, path, theme=theme, mode=mode, yes=yes, force=force, restore=restore, open_=open_)
        return
    n = read_note(path, cfg.vault)
    index = VaultIndex(cfg.vault)
    console.print(f"[bold]{n.title}[/bold]  [dim]{n.rel}[/dim]")

    report = validate(n, index)
    _show_report(report)
    if report.errors:
        raise typer.Exit(1)
    if report.warnings and not yes and not Confirm.ask("Publish anyway?", default=False):
        raise typer.Exit(1)

    ropts = render_options(cfg, n, theme, mode)
    content_hash = n.content_hash(*ropts.fingerprint())
    remote = Remote.from_config(cfg)
    registry = _registry(remote)
    page_id, verb = _choose_id(n, registry, restore=restore, force=force, yes=yes, content_hash=content_hash)

    site = fresh_site_dir("site")
    with console.status(f"Rendering in Obsidian ({ropts.theme}, {ropts.mode})…"):
        result = make_site(cfg, n, page_id, ropts, site, index=index, link_map=fold.folder_link_map(registry))
    _show_build(result)
    with console.status(f"Uploading to {remote.host}…"):
        remote.publish_page(site, page_id)
    entry = record_published(cfg, registry, n, page_id, ropts, content_hash)
    remote.write_registry(registry)
    mark_note_published(n, page_id, entry)
    cleanup_jobs()

    copied = _copy(entry["url"])
    console.print(Panel.fit(
        f"[bold green]{verb}[/bold green]  {n.title}\n[link={entry['url']}]{entry['url']}[/link]"
        + ("\n[dim]Copied to clipboard.[/dim]" if copied else ""),
        border_style="green",
    ))
    if open_:
        webbrowser.open(entry["url"])


def _plan_folder(cfg: Config, folder: Path, index: VaultIndex) -> fold.FolderPlan:
    console.print(f"[bold]📁 {folder.name}[/bold]  [dim]{folder.relative_to(cfg.vault)}[/dim]")
    with console.status("Checking notes…"):
        fplan = fold.plan(cfg, folder, index)
    console.print(f"{len(fplan.pages)} notes · home page: [bold]{fplan.home.rel}[/bold]")
    for rel, errs in fplan.skipped:
        console.print(f"[red]✗[/red] Skipping {rel}: {errs[0]}")
    for rel, w in fplan.warnings:
        console.print(f"[yellow]⚠[/yellow] {rel}: {w}")
    for i in fplan.info:
        console.print(f"[blue]ℹ[/blue] {i}")
    return fplan


def _render_folder(cfg: Config, fplan: fold.FolderPlan, page_id: str, ropts, site: Path, index: VaultIndex):
    with console.status("Rendering in Obsidian…") as st:
        def progress(done: int, total: int, rel: str) -> None:
            st.update(f"Rendering in Obsidian ({ropts.theme}, {ropts.mode}) {done}/{total}  [dim]{rel}[/dim]")
        rendered, css_render = fold.render_all(cfg, fplan, page_id, ropts, lambda req: core.render_with_obsidian(req), progress)
    results = fold.build_folder(cfg, fplan, page_id, rendered, css_render, site, ropts.mode, index)
    warnings = list(dict.fromkeys(w for r in results for w in r.warnings))
    for w in warnings:
        console.print(f"[yellow]⚠[/yellow] {w}")
    gps = sum(len(r.gps_stripped) for r in results)
    if gps:
        console.print(f"[blue]ℹ[/blue] Removed GPS location from {gps} photo(s).")


def _publish_folder(cfg: Config, folder: Path, *, theme, mode, yes: bool, force: bool, restore: bool, open_: bool,
                    rotate_from: str | None = None) -> None:
    index = VaultIndex(cfg.vault)
    folder_rel = folder.relative_to(cfg.vault).as_posix()
    fplan = _plan_folder(cfg, folder, index)
    if (fplan.warnings or fplan.skipped) and not yes and not Confirm.ask("Publish anyway?", default=False):
        raise typer.Exit(1)

    ropts = render_options(cfg, fplan.home.note, theme, mode)
    content_hash = fplan.content_hash(ropts)
    remote = Remote.from_config(cfg)
    registry = _registry(remote)
    if rotate_from:
        page_id, verb = new_id(), "New link for"
    else:
        page_id, verb = _choose_folder_id(cfg, folder_rel, registry, restore=restore, force=force, yes=yes,
                                          content_hash=content_hash)

    site = fresh_site_dir("site")
    _render_folder(cfg, fplan, page_id, ropts, site, index)
    with console.status(f"Uploading {len(fplan.pages)} pages to {remote.host}…"):
        remote.publish_page(site, page_id)
        if rotate_from:
            remote.unpublish_page(rotate_from, cfg.trash_days)
    if rotate_from:
        record_unpublished(registry, rotate_from, None, status="rotated")
    entry = fold.record(cfg, registry, fplan, page_id, ropts, content_hash)
    remote.write_registry(registry)
    fold.write_local_state(cfg, folder_rel, page_id, entry["url"], "live")
    cleanup_jobs()

    copied = _copy(entry["url"])
    console.print(Panel.fit(
        f"[bold green]{verb}[/bold green]  📁 {fplan.title}  ({len(fplan.pages)} notes)\n[link={entry['url']}]{entry['url']}[/link]"
        + ("\n[dim]Copied to clipboard.[/dim]" if copied else "")
        + ("\n[dim]Old link is dead.[/dim]" if rotate_from else ""),
        border_style="green",
    ))
    if open_:
        webbrowser.open(entry["url"])


def _choose_folder_id(cfg: Config, folder_rel: str, registry: dict, *, restore: bool, force: bool, yes: bool,
                      content_hash: str) -> tuple[str, str]:
    if live := fold.find_live(registry, folder_rel):
        page_id, entry = live
        if entry.get("content_hash") == content_hash and not force:
            console.print(f"[green]✓[/green] Nothing changed since the last publish ({_ago(entry.get('updated'))}). Skipping.  [dim](--force to push anyway)[/dim]")
            console.print(f"  {entry['url']}")
            raise typer.Exit(0)
        console.print(Panel.fit(
            f"📁 [bold]{entry.get('title')}[/bold] is already published ({len(entry.get('pages', {}))} notes)\n"
            f"URL:        {entry['url']}\n"
            f"Published:  {_ago(entry.get('published'))}\n"
            f"Updated:    {_ago(entry.get('updated'))}",
            border_style="yellow",
        ))
        if not yes and not Confirm.ask("Update it? Same link, new content", default=False):
            raise typer.Exit(1)
        return page_id, "Updated"

    latest = fold.find_latest(registry, folder_rel)
    local = fold.read_local_state(cfg).get(folder_rel)
    if restore and (latest or local):
        return (latest[0] if latest else local["id"]), "Restored"
    if local and local.get("status") == "live" and not latest:
        console.print("[blue]ℹ[/blue] The server doesn't know this folder (new server or lost registry?). Re-publishing with the same link.")
        return local["id"], "Republished"
    if latest:
        console.print(f"[blue]ℹ[/blue] This folder was {latest[1].get('status')} {_ago(latest[1].get('unpublished'))}. "
                      "It gets a [bold]new[/bold] link; the old one stays dead. (--restore to bring the old link back)")
    page_id = new_id()
    while page_id in registry["pages"]:
        page_id = new_id()
    return page_id, "Published"


@app.command()
def preview(
    note: NoteArg,
    theme: ThemeOpt = None,
    mode: ModeOpt = None,
    port: Annotated[int, typer.Option(help="Local port (0 = any free port).")] = 0,
    no_open: Annotated[bool, typer.Option("--no-open", help="Don't open the browser.")] = False,
):
    """Render a note or folder exactly as it would be published, and open it locally. Nothing is uploaded."""
    cfg, path = _resolve_target(note)
    if path.is_dir():
        index = VaultIndex(cfg.vault)
        fplan = _plan_folder(cfg, path, index)
        ropts = render_options(cfg, fplan.home.note, theme, mode)
        site = fresh_site_dir("preview")
        _render_folder(cfg, fplan, PREVIEW_ID, ropts, site, index)
        cleanup_jobs()
        serve(site, f"/{PREVIEW_ID}/{fplan.slug}", port=port, open_browser=not no_open)
        return
    n = read_note(path, cfg.vault)
    index = VaultIndex(cfg.vault)
    report = validate(n, index)
    _show_report(report)
    if report.errors:
        console.print("[yellow]These errors would block `postcard publish`.[/yellow]")
    ropts = render_options(cfg, n, theme, mode)
    site = fresh_site_dir("preview")
    with console.status(f"Rendering in Obsidian ({ropts.theme}, {ropts.mode})…"):
        result = make_site(cfg, n, PREVIEW_ID, ropts, site, index=index)
    _show_build(result)
    console.print(f"[dim]Page size: {result.page_bytes / 1024:.0f} KB + shared CSS/fonts[/dim]")
    cleanup_jobs()
    serve(site, f"/{PREVIEW_ID}/{n.slug}", port=port, open_browser=not no_open)


@app.command()
def unpublish(
    target: Annotated[str, typer.Argument(help="Note path/name, or a page id from `postcard list`.")],
    yes: YesOpt = False,
):
    """Take a page down. Its link stops working for everyone."""
    folder_rel = None
    if is_id(target) and not Path(target).exists():
        cfg = load_config(find_vault(Path.cwd()))
        page_id, note_path = target, None
        remote = Remote.from_config(cfg)
        registry = _registry(remote)
    else:
        cfg, path = _resolve_target(target)
        remote = Remote.from_config(cfg)
        registry = _registry(remote)
        if path.is_dir():
            folder_rel, note_path = path.relative_to(cfg.vault).as_posix(), None
            live = fold.find_live(registry, folder_rel)
            if not live:
                raise PostcardError(f"📁 {path.name} isn't published.")
            page_id = live[0]
        else:
            note_path = path
            page_id = read_note(note_path, cfg.vault).page_id
            if not page_id:
                raise PostcardError("This note has never been published.")

    entry = registry["pages"].get(page_id)
    if not entry or entry.get("status") != "live":
        raise PostcardError(f"Page {page_id} isn't live.")
    if entry.get("kind") == "folder":
        folder_rel, note_path = entry["source"], None
    elif note_path is None and (cfg.vault / entry["source"]).exists():
        note_path = cfg.vault / entry["source"]

    console.print(f"[bold]{entry['title']}[/bold]\n{entry['url']}")
    if not yes and not Confirm.ask("Unpublish? The link stops working for everyone", default=False):
        raise typer.Exit(1)
    with console.status("Unpublishing…"):
        remote.unpublish_page(page_id, cfg.trash_days)
        record_unpublished(registry, page_id, note_path)
        remote.write_registry(registry)
        if folder_rel:
            fold.write_local_state(cfg, folder_rel, page_id, entry["url"], "unpublished")
    console.print(f"[green]✓[/green] Unpublished. Publishing again gives a new link (or use --restore within {cfg.trash_days} days).")


@app.command()
def rotate(note: NoteArg, yes: YesOpt = False):
    """Give a live page or folder a new link. The old link dies immediately."""
    cfg, path = _resolve_target(note)
    if path.is_dir():
        folder_rel = path.relative_to(cfg.vault).as_posix()
        live = fold.find_live(_registry(Remote.from_config(cfg)), folder_rel)
        if not live:
            raise PostcardError(f"📁 {path.name} isn't live, so there's no link to rotate.")
        if not yes and not Confirm.ask(f"Kill {live[1]['url']} and make a new link?", default=False):
            raise typer.Exit(1)
        _publish_folder(cfg, path, theme=None, mode=None, yes=True, force=True, restore=False, open_=False, rotate_from=live[0])
        return
    n = read_note(path, cfg.vault)
    remote = Remote.from_config(cfg)
    registry = _registry(remote)
    old = n.page_id
    if not old or registry["pages"].get(old, {}).get("status") != "live":
        raise PostcardError("This note isn't live, so there's no link to rotate.")
    if not yes and not Confirm.ask(f"Kill {registry['pages'][old]['url']} and make a new link?", default=False):
        raise typer.Exit(1)

    ropts = render_options(cfg, n)
    page_id = new_id()
    site = fresh_site_dir("site")
    with console.status("Rendering in Obsidian…"):
        result = make_site(cfg, n, page_id, ropts, site)
    _show_build(result)
    with console.status("Uploading…"):
        remote.publish_page(site, page_id)
        remote.unpublish_page(old, cfg.trash_days)
    record_unpublished(registry, old, None, status="rotated")
    entry = record_published(cfg, registry, n, page_id, ropts, n.content_hash(*ropts.fingerprint()))
    remote.write_registry(registry)
    mark_note_published(n, page_id, entry)
    _copy(entry["url"])
    console.print(f"[green]✓[/green] New link (copied): {entry['url']}\n[dim]Old link is dead.[/dim]")


@app.command("list")
def list_(all_: Annotated[bool, typer.Option("--all", "-a", help="Include unpublished pages.")] = False):
    """Everything you've published (read from the server, so it works from any machine)."""
    cfg = load_config(find_vault(Path.cwd()))
    pages = _registry(Remote.from_config(cfg))["pages"]
    live = {k: v for k, v in pages.items() if v.get("status") == "live"}
    shown = pages if all_ else live
    other = len(pages) - len(live)
    console.print(f"[bold]{len(live)} live[/bold]" + (f" · {other} unpublished" if other else ""))
    if not shown:
        return
    t = Table(box=None, pad_edge=False)
    for col in ("ID", "TITLE", "UPDATED", "THEME", "URL"):
        t.add_column(col, style="dim" if col in ("ID", "THEME") else None, overflow="fold")
    if all_:
        t.add_column("STATUS")
    for pid, e in sorted(shown.items(), key=lambda kv: kv[1].get("updated", ""), reverse=True):
        title = f"📁 {e.get('title', '')} ({len(e.get('pages', {}))})" if e.get("kind") == "folder" else e.get("title", "")
        row = [pid, title, _ago(e.get("updated")), e.get("theme", ""), e.get("url", "")]
        if all_:
            row.append(e.get("status", ""))
        t.add_row(*row)
    console.print(t)


@app.command()
def status(note: NoteArg):
    """Is this note or folder live, and has it changed since it was published?"""
    cfg, path = _resolve_target(note)
    if path.is_dir():
        registry = _registry(Remote.from_config(cfg))
        live = fold.find_live(registry, path.relative_to(cfg.vault).as_posix())
        if not live:
            console.print("Not published.")
            return
        _, entry = live
        fplan = fold.plan(cfg, path)
        changed = entry.get("content_hash") != fplan.content_hash(render_options(cfg, fplan.home.note))
        console.print(f"📁 [bold]{entry['title']}[/bold] — live, {len(entry.get('pages', {}))} notes")
        console.print(f"URL:       {entry['url']}")
        console.print(f"Published: {_ago(entry.get('published'))}   Updated: {_ago(entry.get('updated'))}   Theme: {entry.get('theme')}")
        console.print("[yellow]Changed since last publish.[/yellow]" if changed else "[green]Up to date.[/green]")
        return
    n = read_note(path, cfg.vault)
    if not n.page_id:
        console.print("Not published.")
        return
    entry = _registry(Remote.from_config(cfg))["pages"].get(n.page_id)
    if not entry:
        console.print(f"[yellow]Frontmatter says {n.frontmatter.get('postcard_status')}, but the server doesn't know id {n.page_id}.[/yellow]")
        return
    changed = entry.get("content_hash") != n.content_hash(*render_options(cfg, n).fingerprint())
    console.print(f"[bold]{entry['title']}[/bold] — {entry['status']}")
    console.print(f"URL:       {entry['url']}")
    console.print(f"Published: {_ago(entry.get('published'))}   Updated: {_ago(entry.get('updated'))}   Theme: {entry.get('theme')}")
    if entry["status"] == "live":
        console.print("[yellow]Changed since last publish.[/yellow]" if changed else "[green]Up to date.[/green]")


@app.command("open")
def open_cmd(note: NoteArg):
    """Open a note's or folder's live page in the browser."""
    cfg, path = _resolve_target(note)
    if path.is_dir():
        live = fold.find_live(_registry(Remote.from_config(cfg)), path.relative_to(cfg.vault).as_posix())
        if not live:
            raise PostcardError(f"📁 {path.name} isn't published.")
        webbrowser.open(live[1]["url"])
        return
    n = read_note(path, cfg.vault)
    url = n.frontmatter.get("postcard_url")
    if not url or n.frontmatter.get("postcard_status") != "live":
        raise PostcardError("This note isn't published.")
    webbrowser.open(str(url))


@app.command()
def init(
    base_url: Annotated[Optional[str], typer.Option(help="e.g. https://publish.example.com")] = None,
    ssh_host: Annotated[Optional[str], typer.Option(help="Host alias from ~/.ssh/config")] = None,
    root: Annotated[str, typer.Option(help="Folder on the server")] = "/srv/postcard",
    theme: Annotated[Optional[str], typer.Option(help="Default theme (default: your current Obsidian theme)")] = None,
    mode: Annotated[str, typer.Option(help="dark | light")] = "dark",
):
    """Create .postcard/config.toml in your vault and install the Obsidian plugin."""
    vault = find_vault(Path.cwd())
    console.print(f"Vault: [bold]{vault}[/bold]")
    base_url = base_url or Prompt.ask("Base URL for published pages", default="https://publish.example.com")
    ssh_host = ssh_host or Prompt.ask("SSH host alias (from ~/.ssh/config)")
    theme = theme if theme is not None else obsidian_appearance(vault).get("cssTheme", "")
    if theme:
        resolve_theme(vault, theme)
    path = write_config(vault, base_url.rstrip("/"), ssh_host, root, theme, mode)
    console.print(f"[green]✓[/green] Wrote {path}")
    _plugin_install(vault)
    console.print("\nNext: [bold]postcard server nginx[/bold] (server config), then [bold]postcard doctor[/bold].")


@app.command()
def doctor():
    """Check vault, plugin, SSH, server, DNS and HTTPS."""
    ok = True

    def check(label: str, passed: bool, detail: str = "") -> bool:
        nonlocal ok
        ok &= passed
        console.print(f"{'[green]✓' if passed else '[red]✗'}[/] {label}" + (f"  [dim]{detail}[/dim]" if detail else ""))
        return passed

    try:
        vault = find_vault(Path.cwd())
        check("Vault", True, str(vault))
        cfg = load_config(vault)
        check("Config", True, str(cfg.config_path))
    except PostcardError as e:
        check("Vault/config", False, str(e))
        raise typer.Exit(1)

    bundled = json.loads((bundled_plugin_dir() / "manifest.json").read_text())["version"]
    inst = installed_plugin_version(vault)
    check("Plugin installed", inst == bundled, f"installed {inst or 'no'}, bundled {bundled}" + ("" if inst == bundled else " → postcard plugin install"))
    check("Plugin enabled", plugin_enabled(vault), "" if plugin_enabled(vault) else "Obsidian → Settings → Community plugins")
    check("Obsidian running", obsidian_running(), "" if obsidian_running() else "it will be launched when you publish")
    try:
        css = resolve_theme(vault, cfg.theme)
        snippets = snippet_paths(vault, cfg.snippets)
        check("Theme", True, f"{css.parent.name if css else 'default'} ({cfg.mode}), snippets: {', '.join(p.stem for p in snippets) or 'none'}")
    except PostcardError as e:
        check("Theme", False, str(e))

    remote = Remote.from_config(cfg)
    try:
        user = remote.ping().splitlines()[-1]
        check("SSH", True, f"{cfg.ssh_host} as {user}")
        remote.ensure_layout()
        remote.run(f"touch {remote.path('site', '.write-test')} && rm {remote.path('site', '.write-test')}")
        check("Server folder writable", True, cfg.remote_root)
        pages = remote.read_registry()["pages"]
        check("Registry", True, f"{sum(1 for p in pages.values() if p.get('status') == 'live')} live pages")
    except PostcardError as e:
        check("Server", False, str(e).splitlines()[-1])

    host = cfg.base_url.split("://", 1)[-1].split("/")[0]
    if remote.is_local:
        console.print("[dim]  (ssh_host = local: skipping DNS/HTTPS checks)[/dim]")
    else:
        try:
            check("DNS", True, f"{host} → {socket.gethostbyname(host)}")
            with urllib.request.urlopen(f"{cfg.base_url.rstrip('/')}/robots.txt", timeout=10) as r:
                body = r.read().decode()
                check("HTTPS + nginx", "Disallow: /" in body and r.headers.get("X-Robots-Tag", "").startswith("noindex"),
                      "robots.txt and noindex header present" if "Disallow" in body else "robots.txt doesn't block crawlers")
        except OSError as e:
            check("DNS/HTTPS", False, f"{host}: {e}")
    raise typer.Exit(0 if ok else 1)


@app.command()
def version():
    """Show the version."""
    console.print(f"postcard {__version__}")


# --- themes -----------------------------------------------------------------

@themes_app.command("list")
def themes_list():
    """Themes available for publishing."""
    vault = find_vault(Path.cwd())
    try:
        default = load_config(vault).theme
    except PostcardError:
        default = None
    for name, path in installed_themes(vault).items():
        where = "obsidian" if ".obsidian" in path.parts else "postcard"
        star = " [green]← default[/green]" if default and name.lower() == default.lower() else ""
        console.print(f"  {name}  [dim]({where})[/dim]{star}")
    console.print('  default  [dim](Obsidian\'s built-in theme)[/dim]')


@themes_app.command("add")
def themes_add(name: Annotated[str, typer.Argument(help='Community theme name, e.g. "Minimal"')]):
    """Download a community theme for publishing (Obsidian itself is untouched)."""
    vault = find_vault(Path.cwd())
    with console.status(f'Downloading "{name}"…'):
        dest = add_theme(vault, name)
    console.print(f"[green]✓[/green] {dest.parent.name} → {dest}\n  Try it: postcard preview <note> --theme \"{dest.parent.name}\"")


# --- plugin -----------------------------------------------------------------

def _plugin_install(vault: Path) -> None:
    newly_enabled = install_plugin(vault)
    console.print(f"[green]✓[/green] Installed {PLUGIN_ID} into {vault.name}/.obsidian/plugins/")
    if newly_enabled and obsidian_running():
        console.print("  [yellow]Reload Obsidian once[/yellow] so it loads the plugin: Cmd+P → “Reload app without saving”.")
    elif not newly_enabled:
        console.print("  Updated. Reload Obsidian (Cmd+P → “Reload app without saving”) to use the new version.")


@plugin_app.command("install")
def plugin_install():
    """Install/update the bridge plugin in your vault."""
    _plugin_install(find_vault(Path.cwd()))


# --- server -----------------------------------------------------------------

@server_app.command("nginx")
def server_nginx():
    """Print the nginx config for your domain."""
    cfg = load_config(find_vault(Path.cwd()))
    domain = cfg.base_url.split("://", 1)[-1].split("/")[0]
    tpl = Path(__file__).parent / "server_templates"
    console.print(f"[bold]1.[/bold] On the server: [cyan]/etc/nginx/snippets/postcard-headers.conf[/cyan]\n")
    print((tpl / "postcard-headers.conf").read_text())
    console.print(f"[bold]2.[/bold] On the server: [cyan]/etc/nginx/sites-available/{domain}[/cyan]\n")
    print((tpl / "nginx.conf").read_text().format(domain=domain, root=cfg.remote_root.rstrip("/")))
    console.print(f"[bold]3.[/bold] DNS: A record [cyan]{domain}[/cyan] → your droplet's IP.  Then: [cyan]postcard server setup[/cyan]")


@server_app.command("setup")
def server_setup():
    """Create the Postcard folders on the server."""
    cfg = load_config(find_vault(Path.cwd()))
    remote = Remote.from_config(cfg)
    remote.ensure_layout()
    console.print(f"[green]✓[/green] {cfg.ssh_host}:{cfg.remote_root}/site and /trash exist.")


@server_app.command("home")
def server_home():
    """Upload the mailbox page shown at "/" and for dead links."""
    cfg = load_config(find_vault(Path.cwd()))
    remote = Remote.from_config(cfg)
    with console.status(f"Uploading to {cfg.ssh_host}…"):
        remote.upload_home(Path(__file__).parent / "server_templates" / "home.html")
    console.print(f"[green]✓[/green] {cfg.base_url}/ now shows the mailbox page (needs the nginx lines from `postcard server nginx`).")
