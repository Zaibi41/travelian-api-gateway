"""IATA codes of airports in Pakistan, used to tell domestic from international routes.

Only used for the passport-validity warning (under 6 months on an international route). A code
missing from this list counts as international, so a gap can only add a warning, never block.
"""

PAKISTAN_AIRPORTS = frozenset({
    "BHV", "BNP", "CJL", "DBA", "DEA", "DSK", "GIL", "GWD", "HDD", "ISB", "JIW", "KDU", "KHI", "LHE",
    "LYP", "MFG", "MJD", "MUX", "ORW", "PEW", "PJG", "PSI", "PZH", "RAZ", "RYK", "SBQ", "SKT", "SKZ",
    "TUK", "UET", "WNS",
})


def is_domestic(*codes: str) -> bool:
    return all(code.upper() in PAKISTAN_AIRPORTS for code in codes)
