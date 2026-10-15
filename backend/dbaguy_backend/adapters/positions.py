def offset_to_line_col(text: str, offset: int) -> tuple[int, int]:
    """Convert a 1-based character offset into a 1-based (line, column), clamped to the text."""
    index = max(0, min(offset - 1, len(text)))
    line = text.count("\n", 0, index) + 1
    column = index - text.rfind("\n", 0, index)
    return line, column
