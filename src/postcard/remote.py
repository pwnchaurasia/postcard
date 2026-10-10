"""Everything that touches the server: rsync uploads, atomic swaps, the registry.

Server layout (under `root`):
    registry.json          id -> page record; `postcard list` reads this
    trash/<id>-<ts>/       unpublished pages, kept `trash_days` for --restore
    site/                  nginx web root
        _s/                shared CSS + fonts (content-hashed)
        <id>/index.html, a/
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config, PostcardError

SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    # One TCP connection for the whole publish (registry, rsyncs, swap…). Without this a publish opens ~6
    # connections in a few seconds, which trips firewall rate limits like `ufw limit ssh`.
    "-o", "ControlMaster=auto",
    "-o", "ControlPath=~/.ssh/postcard-%C",
    "-o", "ControlPersist=60",
]


@dataclass
class Remote:
    host: str  # ssh alias, or "local" to treat `root` as a local folder (tests, dry runs)
    root: str

    @classmethod
    def from_config(cls, cfg: Config) -> "Remote":
        return cls(cfg.ssh_host, cfg.remote_root)

    @property
    def is_local(self) -> bool:
        return self.host == "local"

    def path(self, *parts: str) -> str:
        return "/".join([self.root.rstrip("/"), *parts])

    def run(self, script: str, stdin: str | None = None) -> str:
        cmd = ["sh", "-c", script] if self.is_local else ["ssh", *SSH_OPTS, self.host, "sh -c " + shlex.quote(script)]
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
        if p.returncode != 0:
            where = "locally" if self.is_local else f"on {self.host}"
            raise PostcardError(f"Command failed {where} (exit {p.returncode}):\n{p.stderr.strip() or p.stdout.strip()}")
        return p.stdout

    def push(self, local_dir: Path, remote_dir: str, *, delete: bool = False, ignore_existing: bool = False) -> None:
        dest = remote_dir.rstrip("/") + "/"
        if not self.is_local:
            dest = f"{self.host}:{dest}"
        cmd = ["rsync", "-rlt"]
        if delete:
            cmd.append("--delete")
        if ignore_existing:
            cmd.append("--ignore-existing")
        if not self.is_local:
            cmd += ["-e", "ssh " + " ".join(SSH_OPTS)]
        cmd += [str(local_dir).rstrip("/") + "/", dest]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            raise PostcardError(f"Upload failed:\n{p.stderr.strip()}")

    # --- setup / health -----------------------------------------------------

    def ensure_layout(self) -> None:
        q = shlex.quote
        self.run(f"mkdir -p {q(self.path('site', '_s'))} {q(self.path('trash'))}")

    def ping(self) -> str:
        return self.run("echo ok && id -un").strip()

    # --- registry -------------------------------------------------------------

    def read_registry(self) -> dict:
        raw = self.run(f"cat {shlex.quote(self.path('registry.json'))} 2>/dev/null || true")
        if not raw.strip():
            return {"version": 1, "pages": {}}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise PostcardError(f"registry.json on the server is corrupt: {e}") from e
        data.setdefault("pages", {})
        return data

    def write_registry(self, registry: dict) -> None:
        reg = shlex.quote(self.path("registry.json"))
        tmp = shlex.quote(self.path("registry.json.tmp"))
        self.run(f"cat > {tmp} && mv {tmp} {reg}", stdin=json.dumps(registry, indent=2, sort_keys=True))

    # --- pages ----------------------------------------------------------------

    def publish_page(self, site: Path, page_id: str) -> None:
        """Upload shared assets, then the page into a temp dir, then swap it in so readers never see a half page."""
        q = shlex.quote
        self.ensure_layout()
        self.push(site / "_s", self.path("site", "_s"), ignore_existing=True)
        tmp = self.path("site", f".tmp-{page_id}")
        self.push(site / page_id, tmp, delete=True)
        live, old = self.path("site", page_id), self.path("site", f".old-{page_id}")
        self.run(
            "set -e; "
            f"chmod -R u=rwX,go=rX {q(tmp)} {q(self.path('site', '_s'))}; "
            f"rm -rf {q(old)}; "
            f"if [ -d {q(live)} ]; then mv {q(live)} {q(old)}; fi; "
            f"mv {q(tmp)} {q(live)}; "
            f"rm -rf {q(old)}"
        )

    def upload_home(self, html: Path) -> None:
        """The mailbox page at "/" (also nginx's 404 page). Lives at site/_home.html."""
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy(html, Path(tmp) / "_home.html")
            self.ensure_layout()
            self.push(Path(tmp), self.path("site"))
        self.run(f"chmod 644 {shlex.quote(self.path('site', '_home.html'))}")

    def unpublish_page(self, page_id: str, trash_days: int) -> None:
        q = shlex.quote
        live = self.path("site", page_id)
        trashed = self.path("trash", f"{page_id}-{int(time.time())}")
        self.run(
            f"if [ -d {q(live)} ]; then mv {q(live)} {q(trashed)}; fi; "
            f"find {q(self.path('trash'))} -mindepth 1 -maxdepth 1 -type d -mtime +{int(trash_days)} -exec rm -rf {{}} +"
        )
