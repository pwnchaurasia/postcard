"""Checks that run on the raw note before anything leaves the Mac."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .vault import Note, VaultIndex, ensure_downloaded, icloud_placeholder

# Known secret formats: these block publishing.
SECRET_PATTERNS = {
    "private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "AWS access key": r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    "GitHub token": r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{50,}\b",
    "Anthropic/OpenAI key": r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b",
    "Slack token": r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b",
    "Google API key": r"\bAIza[0-9A-Za-z_-]{35}\b",
    "Stripe key": r"\b[rs]k_live_[0-9A-Za-z]{20,}\b",
}
# Looser patterns: these only warn.
SECRET_HINTS = {
    "credential-looking line": r"(?im)^\s*[-*]?\s*(?:password|passwd|pwd|secret|api[_ -]?key|token)\s*[:=]\s*\S{4,}",
}
PII_PATTERNS = {
    "email address": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
    "phone number": r"(?<![\w/.-])\+?\d[\d \-]{8,}\d(?![\w/.-])",
    "passport-like number": r"\b[A-PR-WY][1-9]\d\s?\d{4}[1-9]\b",
}

# A fenced block closes with the same fence (so ```` can contain ``` examples).
FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*([\w-]*)[^\n]*\n.*?^[ \t]*\1[ \t]*$", re.S | re.M)
INLINE_CODE_RE = re.compile(r"(`+)(?!`).+?(?<!`)\1(?!`)")
WIKI_EMBED_RE = re.compile(r"!\[\[([^\]]+)\]\]")
MD_EMBED_RE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)>]+?)>?(?:\s+\"[^\"]*\")?\s*\)")
PRIVATE_TAG_RE = re.compile(r"(?<![\w&/])#private\b", re.I)
PLUGIN_LANGS = {"dataview", "dataviewjs", "tasks", "chart"}


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    embeds: list[Path] = field(default_factory=list)


def validate(note: Note, index: VaultIndex | None = None) -> Report:
    r = Report()
    fm = note.frontmatter

    if note.path.suffix.lower() != ".md":
        r.errors.append("Only .md notes can be published.")
    if fm.get("postcard") is False or fm.get("private") is True:
        r.errors.append("Note is marked private (`postcard: false` or `private: true` in frontmatter).")
    tags = fm.get("tags") or []
    tags = [tags] if isinstance(tags, str) else tags
    if any(str(t).lstrip("#").lower() == "private" for t in tags) or PRIVATE_TAG_RE.search(_prose(note.body)):
        r.errors.append("Note has the #private tag.")

    for name, pat in SECRET_PATTERNS.items():
        if m := re.search(pat, note.body):
            r.errors.append(f"Looks like a {name}: {_redact(m.group(0))}. Remove it before publishing.")
    for name, pat in SECRET_HINTS.items():
        for m in re.finditer(pat, note.body):
            r.warnings.append(f"{name.capitalize()}: {_redact(m.group(0).strip())}")
    for name, pat in PII_PATTERNS.items():
        found = sorted({m.group(0).strip() for m in re.finditer(pat, FENCE_RE.sub("", note.body))})
        if name == "phone number":
            found = [f for f in found if _looks_like_phone(f)]
        if found:
            shown = ", ".join(found[:3]) + (f" (+{len(found) - 3} more)" if len(found) > 3 else "")
            r.warnings.append(f"Contains {name}{'s' if len(found) > 1 else ''}: {shown}")

    index = index or VaultIndex(note.vault)
    for target in _embed_targets(note.body):
        found = index.resolve(target, note.path)
        if found is None:
            r.errors.append(f"Embedded file not found in the vault: {target}")
            continue
        if not found.exists() and icloud_placeholder(found).exists():
            r.info.append(f"Downloading from iCloud: {found.name}")
            ensure_downloaded(found)
        r.embeds.append(found)

    cover = fm.get("cover")
    if cover and not re.match(r"https?://", str(cover)):
        if (p := index.resolve(_strip_link(str(cover)), note.path)) is None:
            r.errors.append(f"Cover image not found: {cover}")
        else:
            ensure_downloaded(p)

    blocks = sorted({m.group(2) for m in FENCE_RE.finditer(note.body)} & PLUGIN_LANGS)
    if blocks:
        r.info.append(f"Plugin blocks ({', '.join(blocks)}) are published as a snapshot of what they show right now.")
    return r


def _prose(body: str) -> str:
    """The note without code (fenced blocks and inline code): what Obsidian treats as tags/embeds."""
    return INLINE_CODE_RE.sub("", FENCE_RE.sub("", body))


def _embed_targets(body: str) -> list[str]:
    text = _prose(body)
    targets = [m.group(1).split("|")[0].split("#")[0].strip() for m in WIKI_EMBED_RE.finditer(text)]
    for m in MD_EMBED_RE.finditer(text):
        t = m.group(1).strip()
        if not re.match(r"[a-z][a-z0-9+.-]*:", t, re.I):  # skip http:, data:, app: …
            targets.append(re.sub(r"%20", " ", t))
    return [t for t in dict.fromkeys(targets) if t]


def _looks_like_phone(s: str) -> bool:
    digits = re.sub(r"\D", "", s)
    is_date = re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", s) is not None
    return 10 <= len(digits) <= 15 and not is_date


def _strip_link(s: str) -> str:
    s = s.strip()
    if s.startswith("[[") and s.endswith("]]"):
        s = s[2:-2]
    return s.split("|")[0]


def _redact(s: str) -> str:
    s = s.replace("\n", " ")
    return s[:6] + "…" if len(s) > 10 else s
