import re

PART_RE = re.compile(
    r'^(?P<base>.+?)[\s.:,\-–]*\bpart\s+(?P<num>[0-9]+|[IVXLCDM]+)\b',
    re.IGNORECASE,
)

# Strict Roman numeral grammar (not just "made of I/V/X/L/C/D/M letters"),
# so ordinary words like "Mix" or "Civil" aren't misparsed as numerals.
_ROMAN_GRAMMAR_RE = re.compile(
    r'^M{0,4}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$'
)
_ROMAN_VALUES = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}

# A multi-part blog series realistically never runs past a couple dozen
# installments; this rejects the rare Roman-numeral word (e.g. "Mix" = 1009)
# that happens to also be grammatically valid.
_MAX_PLAUSIBLE_PART = 50


def _roman_to_int(numeral: str) -> int | None:
    numeral = numeral.upper()
    if not _ROMAN_GRAMMAR_RE.match(numeral):
        return None
    total = 0
    max_val = 0
    for ch in reversed(numeral):
        val = _ROMAN_VALUES[ch]
        total += -val if val < max_val else val
        max_val = max(max_val, val)
    return total


def parse_part(title: str) -> tuple[str, int] | None:
    """Return (normalized_base_title, part_number) if title contains 'Part N', else None.

    Matches 'Part N' anywhere in the title, not just at the end, since some
    series append a per-part subtitle after the number (e.g. "... Part 2:
    Cracking the Sandbox"). N may be Arabic ("Part 2") or Roman ("Part II").
    """
    match = PART_RE.match(title)
    if not match:
        return None
    base = " ".join(match.group("base").lower().split())
    if not base:
        return None
    num_str = match.group("num")
    part_number = int(num_str) if num_str.isdigit() else _roman_to_int(num_str)
    if part_number is None or part_number > _MAX_PLAUSIBLE_PART:
        return None
    return base, part_number
