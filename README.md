# Postcard

Share a single Obsidian note as an **unlisted web page on your own server**, rendered by Obsidian itself, so it
looks exactly like your vault: your theme, CSS snippets, callouts, Dataview tables, Excalidraw, charts.

```bash
postcard publish "Travel/Vietnam Micro Blog.md"
# ✓ Published  Vietnam Micro Blog
#   https://publish.example.com/k3X9qM2pLw7a/vietnam-micro-blog   (copied to clipboard)
```

Only people you send the link to can find the page. No index, no sitemap, `noindex` everywhere, unguessable ids.
Unlisted is not private, though: anyone who has the link can read the page and pass it on.

## How it works

```
note.md ──► postcard (Python CLI) ──obsidian://──► Postcard Bridge (Obsidian plugin)
                                                      renders with Obsidian's engine,
                                                      collects CSS + images
           ◄──────────── ~/.cache/postcard/… ◄────────┘
   strip EXIF/GPS, WebP, hash assets, build page
           ──rsync/ssh──► server: nginx serves /srv/postcard/site
```

* **No app runs on the server.** It only needs nginx and a folder. SSH is the only way in.
* **The note keeps track of its own state** in frontmatter (`postcard_url`, `postcard_id`, `postcard_status`, …).
  That state syncs with the vault and survives renames and moves.
* **The server keeps a registry** (`registry.json`) of everything published, so `postcard list` works from any machine.

## Install

```bash
uv tool install git+https://github.com/<you>/postcard      # or: uv tool install .  (from a checkout)
cd "<your vault>"
postcard init --base-url https://publish.example.com --ssh-host my-droplet
# Reload Obsidian once (Cmd+P → "Reload app without saving") so the plugin loads.
postcard server nginx      # prints the nginx config for your domain
postcard server setup      # creates /srv/postcard on the server
postcard doctor            # checks everything end to end
```

Requires macOS (iCloud and `open`), Obsidian desktop, Python ≥ 3.11, and `rsync` + `ssh` with a key for the server.

## Commands

| Command | What it does |
|---|---|
| `postcard publish <note>` | Publish, or update if already live (asks first; skips if nothing changed). `--theme`, `--mode`, `--open`, `--force`, `--restore`, `-y` |
| `postcard preview <note>` | Same render, served on `localhost`. Nothing is uploaded. |
| `postcard unpublish <note or id>` | Take it down. The link stops working (page kept in server trash for 30 days). |
| `postcard rotate <note>` | Give the page a new link and kill the old one ("sent it to the wrong person"). |
| `postcard list [--all]` | Everything that's live, from the server registry. |
| `postcard status <note>` | Live? Changed since publishing? |
| `postcard open <note>` | Open the live page. |
| `postcard themes list` / `add "<name>"` | Themes you can publish with; download any community theme. |
| `postcard doctor` | Vault, plugin, SSH, server, DNS, HTTPS checks. |

`<note>` can be a path or part of a note's name (`postcard publish vietnam`).

## Links: when does the id change?

| Action | Link |
|---|---|
| Republish / edit / rename / move the note | **same** |
| Unpublish | dies (404) |
| Publish again after unpublishing | **new** link (or `--restore` the old one) |
| `rotate` | **new** link, old one dies immediately |

## Themes

The page uses your vault's theme by default. Override it, highest priority first:

1. `postcard publish note.md --theme "Minimal" --mode light`
2. In the note's frontmatter: `postcard_theme: Things` / `postcard_mode: light`
3. Environment: `POSTCARD_THEME="AnuPpuccin"` / `POSTCARD_MODE`
4. `.postcard/config.toml` → `[render] theme = "Dark Moss"`

Themes come from `.obsidian/themes/` or `postcard themes add "<name>"`. The second downloads the theme into
`.postcard/themes/` without touching Obsidian.

## Page extras (optional frontmatter)

```yaml
cover: "[[hoi-an.jpg]]"            # banner image
icon: 🇻🇳                           # page icon + favicon
description: 14 days, north to south   # link previews (WhatsApp, Telegram…)
```

## Safety checks before upload

* **Blocks:** private keys, API tokens (AWS, GitHub, OpenAI/Anthropic, Slack, Google, Stripe), `#private` tag,
  `private: true` / `postcard: false`, missing embedded files.
* **Warns:** emails, phone numbers, passport-like numbers, `password:`-style lines, links to unpublished notes
  (shown as plain text so private note names don't leak).
* **Always:** strips EXIF/GPS from photos, renames files to content hashes, removes `%% comments %%` and frontmatter.

## New laptop

The vault (notes, frontmatter, plugin, `.postcard/config.toml`) arrives via iCloud. The server holds the pages and
the registry. You only need to reinstall the CLI and add an SSH key:

```bash
uv tool install git+https://github.com/<you>/postcard
ssh-copy-id my-droplet          # or use the 1Password SSH agent
postcard doctor
```

If the server is lost, set up nginx again and republish your notes. Each note keeps its id in its frontmatter,
so the same links work again.

## Development

```bash
uv sync && uv run pytest                       # Python tests (fake renderer + local "server" folder)
cd plugin && npm install && npm run build      # rebuilds src/postcard/bridge/main.js (committed)
uv run postcard plugin install                 # copy the new build into your vault, then reload Obsidian
```
