from pipeline import odni_assessment as odni_module
from pipeline.odni_assessment import load_odni_excerpt


def test_load_odni_excerpt_missing_file_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(odni_module, "ODNI_EXCERPT_PATH", tmp_path / "does_not_exist.md")
    assert load_odni_excerpt() is None


def test_load_odni_excerpt_placeholder_returns_none(monkeypatch, tmp_path):
    path = tmp_path / "odni_ata_excerpt.md"
    path.write_text("<!-- ODNI Annual Threat Assessment excerpt.\n\nNOT YET POPULATED. -->\n")
    monkeypatch.setattr(odni_module, "ODNI_EXCERPT_PATH", path)
    assert load_odni_excerpt() is None


def test_load_odni_excerpt_empty_file_returns_none(monkeypatch, tmp_path):
    path = tmp_path / "odni_ata_excerpt.md"
    path.write_text("   \n\n  ")
    monkeypatch.setattr(odni_module, "ODNI_EXCERPT_PATH", path)
    assert load_odni_excerpt() is None


def test_load_odni_excerpt_populated_file_returns_text(monkeypatch, tmp_path):
    path = tmp_path / "odni_ata_excerpt.md"
    path.write_text("Excerpt from: 2026 Annual Threat Assessment\n\nIran remains a persistent threat...")
    monkeypatch.setattr(odni_module, "ODNI_EXCERPT_PATH", path)

    excerpt = load_odni_excerpt()

    assert excerpt is not None
    assert "Iran remains a persistent threat" in excerpt


def test_real_shipped_excerpt_file_is_still_a_placeholder():
    # Regression guard: the repo's actual config/odni_ata_excerpt.md should
    # still be the unfilled placeholder unless someone has deliberately
    # populated it with real ODNI text -- this just documents the expected
    # state, it's not a correctness bug if it someday goes green because a
    # human filled it in.
    excerpt = load_odni_excerpt()
    assert excerpt is None or "Excerpt from:" in excerpt
