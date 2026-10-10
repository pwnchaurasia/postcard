"""Reading notes from the vault: paths, iCloud downloads, frontmatter, titles, the file index."""

from __future__ import annotations

import hashlib
import re
import subprocess
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from .config import PostcardError

# Frontmatter keys Postcard owns. Flat keys (not a nested map) so Obsidian's Properties panel shows them nicely.
FM_KEYS = ("postcard_url", "postcard_id", "postcard_status", "postcard_published", "postcard_updated")
SKIP_DIRS = {".obsidian", ".trash", ".git", ".postcard", "node_modules"}
FM_RE = re.compile(r"\A---\r?\n(.*?\r?\n)?---[ \t]*(\r?\n|\Z)", re.S)


@dataclass
class Note:
    path: Path
    vault: Path
    text: str
    frontmatter: dict
    body: str

    @property
    def rel(self) -> str:
        return self.path.relative_to(self.vault).as_posix()

    @property
    def title(self) -> str:
        t = self.frontmatter.get("title")
        return str(t).strip() if t else self.path.stem

    @property
    def slug(self) -> str:
        return slugify(self.title)

    @property
    def page_id(self) -> str | None:
        v = self.frontmatter.get("postcard_id")
        return str(v) if v else None

    def content_hash(self, *extra: str) -> str:
        """Hash of what gets published: the note minus Postcard's own frontmatter lines, plus render options."""
        stripped = "\n".join(l for l in self.text.splitlines() if not l.startswith(FM_KEYS))
        h = hashlib.sha256(stripped.encode())
        for e in extra:
            h.update(b"\0" + e.encode())
        return h.hexdigest()[:16]


_TRANSLIT = str.maketrans({"đ": "d", "Đ": "D", "ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "ß": "ss", "æ": "ae", "Æ": "AE"})


def slugify(text: str) -> str:
    # NFKD splits most accents off (ẵ → a), but a few letters like Vietnamese đ need a manual mapping.
    text = unicodedata.normalize("NFKD", text.translate(_TRANSLIT)).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[\s_-]+", "-", text).strip("-") or "note"


def split_frontmatter(text: str) -> tuple[dict, str]:
    m = FM_RE.match(text)
    if not m:
        return {}, text
    try:
        data = yaml.safe_load(m.group(1) or "") or {}
    except yaml.YAMLError as e:
        raise PostcardError(f"Couldn't parse the note's frontmatter: {e}") from e
    return (data if isinstance(data, dict) else {}), text[m.end():]


def read_note(path: Path, vault: Path) -> Note:
    ensure_downloaded(path)
    text = path.read_text(encoding="utf-8")
    fm, body = split_frontmatter(text)
    return Note(path=path, vault=vault, text=text, frontmatter=fm, body=body)


def set_postcard_fields(path: Path, fields: dict[str, str]) -> None:
    """Rewrite only Postcard's frontmatter lines; everything else in the file stays byte-for-byte the same."""
    text = path.read_text(encoding="utf-8")
    new_lines = [f"{k}: {_yaml_scalar(v)}" for k, v in fields.items()]
    m = FM_RE.match(text)
    if m:
        inner = (m.group(1) or "").splitlines()
        kept = [l for l in inner if not l.startswith(tuple(f"{k}:" for k in FM_KEYS))]
        fm = "\n".join(kept + new_lines)
        text = f"---\n{fm}\n---\n" + text[m.end():]
    else:
        text = "---\n" + "\n".join(new_lines) + "\n---\n" + text
    path.write_text(text, encoding="utf-8")


def _yaml_scalar(v: str) -> str:
    s = str(v)
    # Plain scalars are fine for ids, URLs and ISO times; quote anything YAML might misread.
    if re.fullmatch(r"[A-Za-z0-9_./:+-]+", s) and not s.startswith(("-", ":")) and s.lower() not in {"yes", "no", "true", "false", "null", "on", "off"}:
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def now_iso() -> str:
    # No timezone suffix, so Obsidian shows it as a date-time property.
    return datetime.now().replace(microsecond=0).isoformat()


# --- iCloud ---------------------------------------------------------------

def icloud_placeholder(path: Path) -> Path:
    return path.with_name(f".{path.name}.icloud")


def ensure_downloaded(path: Path, timeout: float = 60) -> None:
    """iCloud may have evicted the file, leaving `.name.icloud`. Ask iCloud to bring it back and wait."""
    if path.exists():
        return
    if not icloud_placeholder(path).exists():
        raise PostcardError(f"File not found: {path}")
    subprocess.run(["brctl", "download", str(path)], capture_output=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.5)
    raise PostcardError(f"iCloud didn't download {path.name} within {timeout:.0f}s. Open it in Finder and retry.")


# --- finding files --------------------------------------------------------

class VaultIndex:
    """Every file in the vault, looked up the way Obsidian resolves [[links]]."""

    def __init__(self, vault: Path):
        self.vault = vault
        self.files: list[Path] = []
        for p in _walk(vault):
            name = p.name
            if name.startswith(".") and name.endswith(".icloud"):
                p = p.with_name(name[1:-len(".icloud")])  # evicted by iCloud; still a real file
            self.files.append(p)
        self.file_set = set(self.files)
        self.by_name: dict[str, list[Path]] = {}
        for p in self.files:
            self.by_name.setdefault(p.name.lower(), []).append(p)

    def resolve(self, link: str, source: Path) -> Path | None:
        link = link.strip().split("#")[0].split("|")[0].strip()
        if not link:
            return None
        candidates = [link] if Path(link).suffix else [link + ".md", link]
        for c in candidates:
            # 1. relative to the note, 2. vault-relative path, 3. unique-ish filename (shortest path wins)
            for p in (source.parent / c, self.vault / c):
                if p in self.file_set:
                    return p
            matches = self.by_name.get(Path(c).name.lower(), [])
            if "/" in c:
                matches = [m for m in matches if m.as_posix().lower().endswith(c.lower())]
            if matches:
                return min(matches, key=lambda m: len(m.parts))
        return None

    def notes(self) -> list[Path]:
        return [p for p in self.files if p.suffix == ".md"]


def _walk(root: Path):
    for p in root.iterdir():
        if p.is_dir():
            if p.name not in SKIP_DIRS:
                yield from _walk(p)
        elif p.is_file():
            yield p


def find_notes(query: str, vault: Path) -> list[Path]:
    """Fuzzy match a note by name: exact stem first, then substring."""
    q = query.lower().removesuffix(".md")
    notes = VaultIndex(vault).notes()
    exact = [p for p in notes if p.stem.lower() == q]
    if exact:
        return exact
    return sorted((p for p in notes if q in p.stem.lower()), key=lambda p: (len(p.stem), p.stem))
