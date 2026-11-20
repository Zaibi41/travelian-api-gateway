"""How a passenger title is written for people (issue #11): Mr, Mrs, Ms, Mstr, Miss. The stored and supplier codes are
unchanged (MR, MRS, MISS, MASTER); only printed text uses these words (itinerary, customer emails)."""

TITLE_TEXT = {"MR": "Mr", "MRS": "Mrs", "MS": "Ms", "MSTR": "Mstr", "MASTER": "Mstr", "MISS": "Miss"}


def title_text(code: str | None) -> str | None:
    if not code:
        return None
    return TITLE_TEXT.get(code.strip().upper(), code.strip().title())
