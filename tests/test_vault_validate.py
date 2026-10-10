from pathlib import Path

from postcard.validate import validate
from postcard.vault import VaultIndex, read_note, set_postcard_fields, slugify, split_frontmatter

from .conftest import FAKE_AWS_KEY, FAKE_PRIVATE_KEY_HEADER


def test_set_fields_preserves_everything_else(tmp_path: Path):
    p = tmp_path / "n.md"
    original_fm = "title: Hi\naliases: [a, b]\n# a yaml comment\n"
    p.write_text(f"---\n{original_fm}---\nBody stays.\n")
    set_postcard_fields(p, {"postcard_id": "abcDEF123456", "postcard_url": "https://x.dev/abcDEF123456/hi"})
    set_postcard_fields(p, {"postcard_id": "abcDEF123456", "postcard_status": "live"})
    text = p.read_text()
    assert text.startswith("---\n" + original_fm)
    assert text.endswith("---\nBody stays.\n")
    assert text.count("postcard_id") == 1
    assert "postcard_url" not in text  # replaced as a set
    fm, _ = split_frontmatter(text)
    assert fm["postcard_status"] == "live"


def test_set_fields_creates_frontmatter(tmp_path: Path):
    p = tmp_path / "n.md"
    p.write_text("# Just a body\n")
    set_postcard_fields(p, {"postcard_status": "live"})
    assert p.read_text() == "---\npostcard_status: live\n---\n# Just a body\n"


def test_slugify():
    assert slugify("Vietnam Micro Blog 🇻🇳") == "vietnam-micro-blog"
    assert slugify("Đà Nẵng: 3 days!") == "da-nang-3-days"
    assert slugify("🙂") == "note"


def test_valid_note_resolves_embeds(vault: Path):
    note = read_note(vault / "Travel" / "Vietnam Micro Blog.md", vault)
    r = validate(note)
    assert r.errors == [] and r.warnings == []
    assert r.embeds == [vault / "09 - Attachments" / "hoi-an.jpg"]


def _note(vault: Path, body: str, fm: str = ""):
    p = vault / "t.md"
    p.write_text((f"---\n{fm}---\n" if fm else "") + body)
    return read_note(p, vault)


def test_blocks_secrets_and_private(vault: Path):
    assert any("AWS" in e for e in validate(_note(vault, f"key {FAKE_AWS_KEY}")).errors)
    assert any("private key" in e for e in validate(_note(vault, FAKE_PRIVATE_KEY_HEADER)).errors)
    assert any("#private" in e for e in validate(_note(vault, "secret plans #private")).errors)
    assert any("private" in e for e in validate(_note(vault, "x", "tags: [private]\n")).errors)
    assert any("private" in e for e in validate(_note(vault, "x", "postcard: false\n")).errors)


def test_missing_embed_is_an_error_but_code_blocks_are_ignored(vault: Path):
    r = validate(_note(vault, "![[nope.png]]\n```\n![[also-nope.png]]\n```\n"))
    assert r.errors == ["Embedded file not found in the vault: nope.png"]


def test_pii_warnings_without_date_false_positives(vault: Path):
    r = validate(_note(vault, "Flight 2026-10-10 14:30. Call +91 98765 43210 or mail me@x.com"))
    joined = " ".join(r.warnings)
    assert "phone number" in joined and "+91 98765 43210" in joined
    assert "email" in joined
    assert "2026-10-10" not in joined


def test_index_resolves_like_obsidian(vault: Path):
    idx = VaultIndex(vault)
    src = vault / "Travel" / "Vietnam Micro Blog.md"
    assert idx.resolve("hoi-an.jpg", src) == vault / "09 - Attachments" / "hoi-an.jpg"
    assert idx.resolve("Vietnam Micro Blog", src) == src
    assert idx.resolve("Vietnam Micro Blog#Day 1|alias", src) == src
    assert idx.resolve("missing.png", src) is None


def test_icloud_placeholder_is_indexed(vault: Path):
    (vault / "09 - Attachments" / ".evicted.png.icloud").write_bytes(b"")
    assert VaultIndex(vault).resolve("evicted.png", vault / "x.md") == vault / "09 - Attachments" / "evicted.png"


def test_tags_and_embeds_in_code_are_ignored(vault: Path):
    body = "Use the `#private` tag or `![[x.png]]`.\n\n````markdown\n```dataview\nLIST\n```\n![[nope.png]] #private\n````\n"
    r = validate(_note(vault, body))
    assert r.errors == []
    assert not any("dataview" in i for i in r.info)  # only an example inside a code block


def test_real_plugin_block_is_noted(vault: Path):
    r = validate(_note(vault, "```dataview\nLIST FROM #travel\n```\n"))
    assert any("dataview" in i for i in r.info)
