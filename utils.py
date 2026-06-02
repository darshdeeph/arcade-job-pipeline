import re

IRRELEVANT_SENTINEL = "irrelevant"


def normalize_company(name: str) -> str:
    """Lowercase + strip legal suffixes so 'Acme Inc.' and 'Acme' match."""
    name = name.lower().strip()
    name = re.sub(r"\b(inc\.?|llc\.?|corp\.?|ltd\.?|co\.?|company)\b", "", name)
    return re.sub(r"\s+", " ", name).strip()
