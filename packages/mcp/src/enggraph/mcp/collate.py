"""Order strings the way String.prototype.localeCompare does for ASCII.

The previous server sorted some lists with the ICU root collation: punctuation
before digits before letters, and case only breaking a tie. Anything outside
ASCII sorts after the letters, by code point.
"""

from __future__ import annotations

PUNCTUATION = "\t\n\r _-,;:!?.'\"()[]{}@*/\\&#%`^+<=>|~$"
ORDER = {char: index for index, char in enumerate(PUNCTUATION)}
ORDER.update({char: 100 + index for index, char in enumerate("0123456789")})
ORDER.update(
    {char: 200 + index for index, char in enumerate("abcdefghijklmnopqrstuvwxyz")}
)
BEYOND = 1000


def key(text: str) -> tuple[list[int], list[bool]]:
    """Return a sort key: the letters first, then which of them are capitals."""
    primary = [ORDER.get(char.lower(), BEYOND + ord(char)) for char in text]
    return primary, [char.isupper() for char in text]
