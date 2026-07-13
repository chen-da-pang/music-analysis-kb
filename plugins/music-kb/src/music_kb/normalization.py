from __future__ import annotations

import re
import unicodedata


_SPACE = re.compile(r"\s+")
_FTS_TOKEN = re.compile(r"[\w\u3400-\u9fff]+", flags=re.UNICODE)


def normalized(value: str) -> str:
    """Normalize a human search key without discarding meaningful characters."""

    return _SPACE.sub(" ", unicodedata.normalize("NFKC", value).casefold()).strip()


def require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        from .errors import ValidationError

        raise ValidationError(f"{field} must be a non-empty string")
    return value.strip()


def fts_query(value: str) -> str | None:
    """Build a conservative FTS5 query rather than passing user syntax through."""

    tokens = _FTS_TOKEN.findall(normalized(value))
    if not tokens:
        return None
    # A quoted token prevents FTS operators in user input from changing query
    # semantics. AND gives predictable narrowing for multi-token input.
    return " AND ".join(f'"{token.replace(chr(34), "")}"' for token in tokens)
