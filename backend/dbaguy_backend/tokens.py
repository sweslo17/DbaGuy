"""Bearer tokens: the API's (DBAGUY_TOKEN) and the MCP server's (spec ⑧ R-08-3), compared the same way."""

import secrets


def same_token(provided: str, expected: str) -> bool:
    # compare_digest raises TypeError on non-ASCII str operands; bytes never raise for any input.
    return secrets.compare_digest(
        provided.encode("utf-8", "surrogatepass"),
        expected.encode("utf-8", "surrogatepass"),
    )


def new_token() -> str:
    """43 URL-safe characters (32 random bytes)."""
    return secrets.token_urlsafe(32)
