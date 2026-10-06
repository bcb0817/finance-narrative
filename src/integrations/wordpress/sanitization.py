from __future__ import annotations

import html
import re
from urllib.parse import urlparse

TAG_RE = re.compile(r"<[^>]*>")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
SECRET_RE = re.compile(
    r"(?i)(authorization|application[_ -]?password|api[_ -]?key|webhook|bearer|"
    r"xai_api_key|openai_api_key|wordpress_app_password|discord_webhook)"
)


def clean_text(value: object, limit: int) -> str:
    text = html.unescape(str(value or ""))
    text = TAG_RE.sub("", text)
    text = CONTROL_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def safe_x_url(value: object) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        return ""
    if not re.fullmatch(r"/[A-Za-z0-9_]{1,15}/status/[0-9]+/?", parsed.path):
        return ""
    return parsed._replace(query="", fragment="").geturl()


def contains_secret_shape(value: object) -> bool:
    return bool(SECRET_RE.search(str(value)))
