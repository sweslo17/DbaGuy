from ..sql.offsets import Utf16Index


def offset_to_line_col(text: str, offset: int) -> tuple[int, int]:
    """1-based character offset → 1-based (line, column), clamped to the text.

    The column counts UTF-16 code units, the editor's unit (JavaScript strings), so characters outside the BMP
    earlier on the line do not shift it.
    """
    index = max(0, min(offset - 1, len(text)))
    line = text.count("\n", 0, index) + 1
    before = text[text.rfind("\n", 0, index) + 1:index]
    return line, Utf16Index(before).to_utf16(len(before)) + 1
