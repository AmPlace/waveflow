from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_SENSITIVE_KEYS = {
    "access_token",
    "admin_token",
    "authorization",
    "cookie",
    "desktop_session",
    "password",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "secret",
    "signature",
    "sig",
    "auth",
    "credential",
}

_SENSITIVE_TEXT = re.compile(
    r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie)\s*:\s*[^\r\n]+"
    r"|\b(token|access_token|refresh_token|api[-_]?key|secret|password|signature|sig|auth|credential)"
    r"\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s&,;]+)"
)
_URL = re.compile(r"https?://[^\s\"'<>]+", re.I)
_OPAQUE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{32,}$")


def redact_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return "[invalid-url]"
    path_parts = parts.path.split("/")
    for index, segment in enumerate(path_parts):
        marker = segment.lower().replace("-", "_")
        previous = path_parts[index - 1].lower().replace("-", "_") if index else ""
        if (
            segment
            and (marker in _SENSITIVE_KEYS or previous in _SENSITIVE_KEYS
                 or _OPAQUE_PATH_SEGMENT.fullmatch(segment))
        ):
            path_parts[index] = "[redacted]"
    redacted_path = "/".join(path_parts)
    if not parts.query:
        return urlunsplit((parts.scheme, parts.netloc, redacted_path, parts.query, parts.fragment))
    query = []
    for key, item in parse_qsl(parts.query, keep_blank_values=True):
        query.append((key, "[redacted]" if key.lower() in _SENSITIVE_KEYS else item))
    return urlunsplit((parts.scheme, parts.netloc, redacted_path, urlencode(query), parts.fragment))


def redact_text(value: str, *, limit: int | None = None) -> str:
    """Redact credentials from bounded diagnostic text.

    This is intentionally presentation-only. It does not attempt to parse or
    validate arbitrary log formats; it removes the common URL/header/query
    forms that can cross a developer diagnostic boundary.
    """
    text = str(value or "")
    text = _URL.sub(lambda match: redact_url(match.group(0)), text)
    text = _SENSITIVE_TEXT.sub(
        lambda match: (
            f"{match.group(1)}: [redacted]" if match.group(1)
            else f"{match.group(2)}=[redacted]"
        ),
        text,
    )
    return text[:limit] if limit is not None else text
