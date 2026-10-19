from bisect import bisect_left


class Utf16Index:
    """Maps code-point offsets of a string to UTF-16 code-unit offsets (JavaScript) and back."""

    def __init__(self, text: str) -> None:
        self._length = len(text)
        self._astral = [i for i, ch in enumerate(text) if ord(ch) > 0xFFFF]

    def to_utf16(self, cp: int) -> int:
        cp = max(0, min(cp, self._length))
        return cp + bisect_left(self._astral, cp)

    def from_utf16(self, unit: int) -> int:
        """Largest code-point offset whose UTF-16 offset is <= unit (clamps inside surrogate pairs)."""
        lo, hi = 0, self._length
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.to_utf16(mid) <= unit:
                lo = mid
            else:
                hi = mid - 1
        return lo


def utf16_column(text: str, line: int, column: int) -> int:
    """A 1-based column counted in code points on 1-based `line` of `text` (Couchbase's error positions, survey §3.3),
    counted in UTF-16 units as the editor does. Lines end at "\\n" only (the server counts a lone "\\r", U+2028 and
    form feed as characters of the line). Columns past the line's end, and lines past the text, count as given."""
    lines = text.split("\n")
    if not 1 <= line <= len(lines):
        return column
    content = lines[line - 1]
    within = min(column - 1, len(content))
    return Utf16Index(content).to_utf16(within) + (column - 1 - within) + 1


def utf16_offset(text: str, cp: int) -> int:
    """The UTF-16 offset of code-point offset `cp` of `text` (clamped to the text), counted at C speed: no Python loop
    over the text, as `Utf16Index` would make over a 20 MiB document."""
    cp = max(0, min(cp, len(text)))
    return len(text[:cp].encode("utf-16-le", "surrogatepass")) // 2
