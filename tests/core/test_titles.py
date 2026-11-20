"""Issue #11: titles print as Mr, Mrs, Ms, Mstr, Miss (codes unchanged)."""

import pytest

from app.core.titles import title_text


@pytest.mark.parametrize("code, text", [("MR", "Mr"), ("MRS", "Mrs"), ("MS", "Ms"), ("MASTER", "Mstr"), ("MSTR", "Mstr"), ("MISS", "Miss"),
                                        ("master", "Mstr"), (None, None), ("", None), ("DR", "Dr")])
def test_title_text(code, text):
    assert title_text(code) == text
