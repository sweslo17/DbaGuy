"""Text that can be written to a file. A `str` may hold a lone surrogate (the JSON string `"\\ud800"` decodes to one,
while a pair `"\\ud83d\\ude00"` decodes to the one character it spells), and pydantic takes it in a plain field, but
no UTF-8 encoder does: the write of `workspace.json` fails, and with it every save. So every surrogate left in a Python
`str` is lone. The frontend's rule is the same (`src/lib/unicodeText.ts`): it never sends one where text is kept.
`LONE_SURROGATE` is the backend's one definition of it (`sql/` and the adapters import it from here)."""

import re

# A surrogate code point in a str: a decoded `\ud800`, never half of a pair (a str holds pairs as one character).
LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def has_lone_surrogate(text: str) -> bool:
    return LONE_SURROGATE.search(text) is not None


def well_formed(text: str) -> str:
    """`text` with each lone surrogate replaced by U+FFFD (what the frontend writes in its place)."""
    return LONE_SURROGATE.sub("\ufffd", text)
