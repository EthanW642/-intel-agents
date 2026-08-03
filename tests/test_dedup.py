import numpy as np

from agents.middle_east.sources import RawItem
from pipeline.dedup import dedup_items


def _item(title: str) -> RawItem:
    return RawItem(title=title, source="test", url=f"http://x/{title}", published="2026-01-01", text=title)


def test_dedup_collapses_near_duplicates():
    items = [_item("A"), _item("A-near-dup"), _item("B")]

    # Fake embedder: A and A-near-dup point the same direction, B is orthogonal.
    vectors = {
        "A. A": [1.0, 0.0],
        "A-near-dup. A-near-dup": [0.99, 0.01],
        "B. B": [0.0, 1.0],
    }

    def fake_embed(texts):
        arr = np.array([vectors[t] for t in texts])
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        return arr / norms

    kept = dedup_items(items, similarity_threshold=0.9, embed_fn=fake_embed)

    assert len(kept) == 2
    assert kept[0].title == "A"
    assert kept[1].title == "B"


def test_dedup_empty_input():
    assert dedup_items([], embed_fn=lambda texts: np.zeros((0, 2))) == []


def test_dedup_keeps_all_when_below_threshold():
    items = [_item("A"), _item("B")]

    def fake_embed(texts):
        return np.array([[1.0, 0.0], [0.0, 1.0]])

    kept = dedup_items(items, similarity_threshold=0.9, embed_fn=fake_embed)
    assert len(kept) == 2


# ---- exact URL dedup (free pre-pass before any model work) ----


def test_dedup_exact_collapses_same_url_keeping_longest_text():
    from pipeline.dedup import dedup_exact

    a_short = RawItem(title="A", source="GDELT", url="http://x/same", published="2026-01-01", text="short")
    a_long = RawItem(title="A2", source="RSS", url="http://x/same", published="2026-01-01", text="much longer text")
    b = RawItem(title="B", source="RSS", url="http://x/other", published="2026-01-01", text="b")

    kept = dedup_exact([a_short, a_long, b])

    assert len(kept) == 2
    kept_same = next(i for i in kept if i.url == "http://x/same")
    assert kept_same.text == "much longer text"


def test_dedup_exact_no_duplicates_is_identity():
    from pipeline.dedup import dedup_exact

    items = [_item("A"), _item("B")]
    assert dedup_exact(items) == items
