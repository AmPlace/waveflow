from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_SENSITIVE_KEYS = {
    "access_token",
    "admin_token",
    "authorization",
    "cookie",
    "desktop_session",
    "password",
    "token",
}


def redact_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return "[invalid-url]"
    if not parts.query:
        return value
    query = []
    for key, item in parse_qsl(parts.query, keep_blank_values=True):
        query.append((key, "[redacted]" if key.lower() in _SENSITIVE_KEYS else item))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
