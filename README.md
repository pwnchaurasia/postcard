# Postcard

Share a single Obsidian note, or a whole folder, as an **unlisted web page on your own server**. Obsidian renders it
itself, so it looks exactly like your vault: your theme, CSS snippets, callouts, Dataview tables, Excalidraw, charts.

```bash
postcard publish "Travel/Vietnam Micro Blog.md"
# ✓ Published  Vietnam Micro Blog
#   https://notes.example.com/k3X9qM2pLw7a/vietnam-micro-blog   (copied to clipboard)

postcard publish "Study/Kubernetes"
# ✓ Published  📁 Kubernetes  (22 notes)
#   https://notes.example.com/Xy7Qm2LpR4tB/kubernetes
```

Think of it as a digital postcard: you send someone the link and they can read it. Nobody else can find it. No
index, no sitemap, `noindex` everywhere, unguessable ids. Unlisted is not private, though: anyone who has the link
can read the page and pass it on.

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
* **A note keeps track of its own state** in frontmatter (`postcard_url`, `postcard_id`, `postcard_status`, …).
  That state syncs with the vault and survives renames and moves. Published folders are tracked in
  `.postcard/folders.json` inside the vault, so your notes don't get extra lines.
* **The server keeps a registry** (`registry.json`) of everything published, so `postcard list` works from any machine.

## Requirements

* **macOS.** Postcard uses `open` to talk to Obsidian, `pbcopy` for the clipboard and `brctl` for iCloud files.
* **Obsidian desktop.** It does the rendering, so it must be installed and able to open your vault.
* **Python ≥ 3.11** and [uv](https://docs.astral.sh/uv/).
* **A Linux server you can SSH into with a key**, with **nginx** and **certbot**. A small droplet is plenty.
* **A domain or subdomain** you control, with an A record pointing at that server.

No server? See [Don't have a server?](#dont-have-a-server) below.

## Setup

**1. Install the CLI** on your Mac:

```bash
uv tool install git+https://github.com/pwnchaurasia/postcard      # or: uv tool install .  (from a checkout)
```

**2. Point it at your vault and server.** Run this from inside your vault:

```bash
cd "<your vault>"
postcard init --base-url https://notes.example.com --ssh-host my-droplet    # an alias from ~/.ssh/config
```

This writes `.postcard/config.toml` into the vault and installs the Postcard Bridge plugin. **Reload Obsidian once**
(Cmd+P → "Reload app without saving") so the plugin loads.

**3. Set up the server.** Point DNS for your subdomain at the server, then print the nginx config:

```bash
postcard server nginx     # prints the headers snippet + the site config, filled in for your domain
```

On the server, save the two files it prints:

* `/etc/nginx/snippets/postcard-headers.conf`
* `/etc/nginx/sites-available/<your domain>`

Then enable the site and get a certificate:

```bash
ln -s /etc/nginx/sites-available/<your domain> /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
certbot --nginx -d <your domain>
```

**4. Finish from your Mac:**

```bash
postcard server setup     # creates /srv/postcard/{site,trash} on the server
postcard server home      # uploads the landing page (see "The landing page" below)
postcard doctor           # checks vault, plugin, SSH, server, DNS and HTTPS end to end
```

### Upgrading an existing server for folder publishing

Folder pages live at `/<id>/<page>`. If you set up nginx before folders existed, change the page `location` in your
site config from

```nginx
try_files /$1/index.html =404;
```

to

```nginx
try_files /$1$2/index.html /$1/index.html =404;
```

and run `nginx -t && systemctl reload nginx`. Single-note links keep working exactly as before. The landing page
also needs the `error_page 404 /_home.html;` and `location = /` / `location = /_home.html` blocks: compare with
the output of `postcard server nginx`.

## Commands

| Command | What it does |
|---|---|
| `postcard publish <note or folder>` | Publish, or update if already live (asks first; skips if nothing changed). `--theme`, `--mode`, `--open`, `--force`, `--restore`, `-y` |
| `postcard preview <note or folder>` | Same render, served on `localhost`. Nothing is uploaded. |
| `postcard unpublish <note, folder or id>` | Take it down. The link stops working (kept in server trash for 30 days). |
| `postcard rotate <note or folder>` | Give it a new link and kill the old one ("sent it to the wrong person"). |
| `postcard list [--all]` | Everything that's live, from the server registry. Folders show as `📁 Name (n)`. |
| `postcard status <note or folder>` | Live? Changed since publishing? |
| `postcard open <note or folder>` | Open the live page. |
| `postcard themes list` / `add "<name>"` | Themes you can publish with; download any community theme. |
| `postcard server nginx` / `setup` / `home` | Print the nginx config / create the server folders / upload the landing page. |
| `postcard plugin install` | Install or update the Obsidian plugin in your vault (then reload Obsidian). |
| `postcard doctor` | Vault, plugin, SSH, server, DNS, HTTPS checks. |

`<note or folder>` can be a path, part of a note's name (`postcard publish vietnam`), or a folder's exact name
(`postcard publish Deployment`).

## Publishing a folder

Send one link instead of twenty. `postcard publish "Study/Deployment"` publishes every note in the folder,
**including subfolders**, as one site:

* **A sidebar** with the folder tree, built from Obsidian's own file-explorer markup, so your theme and sidebar
  snippets (e.g. coloured folders) apply. The current page is highlighted. On phones it becomes a ☰ menu.
* **A home page:** a note named `index`, `readme`, `home` or containing `overview`, else the first note.
* **Previous / Next** buttons in reading order. Files are sorted naturally (`2` before `10`), each folder's own notes
  come before its subfolders.
* **Working internal links:** `[[Other note]]` goes to that note's page in the folder. Links to notes outside the
  folder become plain text, unless that note is published on its own.
* **Notes that fail the safety checks are skipped** with a warning; the rest still publish.
* **Fast republishing:** only notes that changed are re-rendered.

The links for a folder follow the same rules as single notes: one link for the whole folder, plus a stable sub-link
per page (`/<id>/<page>`) if you want to point someone at a specific note.

## Links: when does the id change?

| Action | Link |
|---|---|
| Republish / edit / rename / move the note | **same** |
| Unpublish | dies (shows "Return to sender") |
| Publish again after unpublishing | **new** link (or `--restore` the old one) |
| `rotate` | **new** link, old one dies immediately |

## The landing page

`postcard server home` uploads a small page that's shown at the root of your domain and for dead links:

* **At `/`:** an airmail postcard: *"This is a personal postcard box. Nothing here is public. If I want to show you
  something, I'll send you the link."* Tap it to flip it over; tap anywhere else to send postcards flying.
* **On any dead link** (unpublished, rotated, mistyped): the same card with a **RETURN TO SENDER** stamp. It still
  returns HTTP 404.

It's one self-contained HTML file (no external fonts, scripts or tracking) at
`src/postcard/server_templates/home.html`. Edit the wording, then run `postcard server home` again.

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

## Good to know

Postcard started as a personal tool, so a few defaults reflect that:

* **The landing page signs off as "— the sender".** It's generic on purpose; change it in `home.html` if you like.
* **Hack is the only bundled web font.** Postcard ships the Hack font and registers it as `Hack`, `Hack Nerd Font`
  and `Hack Nerd Font Mono`. If your Obsidian uses another font that visitors don't have installed, their browser
  falls back to its default for that font stack.
* **The theme falls back to whatever your Obsidian uses.** `postcard init` picks your current Obsidian theme as the
  default. If you leave `theme = ""` in the config, pages use Obsidian's built-in theme.
* **Firewalls that rate-limit SSH** (e.g. `ufw limit ssh`) are fine. Postcard reuses a single SSH connection per
  publish.

## New laptop

The vault (notes, frontmatter, plugin, `.postcard/config.toml`, `.postcard/folders.json`) arrives via iCloud. The
server holds the pages and the registry. You only need to reinstall the CLI and add an SSH key:

```bash
uv tool install git+https://github.com/pwnchaurasia/postcard
ssh-copy-id my-droplet          # or use the 1Password SSH agent
postcard doctor
```

If the server is lost, set up nginx again and republish your notes and folders. Each note keeps its id in its
frontmatter, and folders keep theirs in `.postcard/folders.json`, so the same links work again.

## Don't have a server?

Postcard assumes your own server. If you don't have one, existing tools are a better fit:

* **[Share Note](https://github.com/alangrainger/share-note)** (Obsidian plugin): the closest to Postcard. Same
  exact-theme rendering and encrypted links, hosted for free, and it can be self-hosted with Docker later.
* **[Obsidian Publish](https://obsidian.md/publish):** the official paid option, but it publishes a whole public site
  rather than unlisted single notes.
* **[Just Share Please](https://github.com/Ellpeck/ObsidianJustSharePlease):** simple unlisted links, hosted or
  self-hosted on any cheap PHP host.

## Development

```bash
uv sync && uv run pytest                       # Python tests (fake renderer + local "server" folder)
cd plugin && npm install && npm run build      # rebuilds src/postcard/bridge/main.js (committed)
uv run postcard plugin install                 # copy the new build into your vault, then reload Obsidian
```

## License

[MIT](LICENSE) © 2026 Pawan Chaurasia
