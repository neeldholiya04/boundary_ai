"""Keep credentials in MCP server configs out of API responses.

Server configs can carry secrets: HTTP headers (Authorization, x-api-key), environment variables for
stdio servers, or a key in the URL query. `mask_config` replaces every secret-looking value with
MASK before a config leaves the API; `unmask_config` puts the stored value back when a client sends
MASK in an update, so editing a server in the dashboard never wipes its key.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MASK = "***"
_SECRET_NAME = re.compile(r"auth|key|token|secret|passw|cookie|session|credential|bearer", re.IGNORECASE)


def _is_secret(name: str) -> bool:
    return bool(_SECRET_NAME.search(name))


def _mask_url(url: str) -> str:
    parts = urlsplit(url)
    netloc = parts.netloc
    if parts.password:
        netloc = netloc.replace(f":{parts.password}@", f":{MASK}@")
    query = [(k, MASK if _is_secret(k) else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query, safe="*"), parts.fragment))


def mask_config(config: dict[str, Any] | None) -> dict[str, Any]:
    config = dict(config or {})
    for field in ("headers", "env"):
        if isinstance(config.get(field), dict):
            config[field] = {k: (MASK if _is_secret(k) else v) for k, v in config[field].items()}
    if isinstance(config.get("url"), str):
        config["url"] = _mask_url(config["url"])
    return config


def unmask_config(new: dict[str, Any], old: dict[str, Any] | None) -> dict[str, Any]:
    """`new` with every MASK value replaced by the stored one (MASK is never saved)."""
    old = old or {}
    merged = dict(new)
    for field in ("headers", "env"):
        if not isinstance(merged.get(field), dict):
            continue
        previous = old.get(field) or {}
        values: dict[str, Any] = {}
        for key, value in merged[field].items():
            if value != MASK:
                values[key] = value
            elif key in previous:
                values[key] = previous[key]
            # MASK with nothing stored behind it is dropped rather than saved as "***".
        merged[field] = values
    # An unchanged masked URL means "keep the stored one".
    url, old_url = merged.get("url"), old.get("url")
    if isinstance(url, str) and isinstance(old_url, str) and MASK in url and _mask_url(old_url) == url:
        merged["url"] = old_url
    return merged
