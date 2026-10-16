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
