from __future__ import annotations

import base64
import logging
import os
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import requests

logger = logging.getLogger(__name__)


class WordPressRadarError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status_code, self.retryable = status_code, retryable


@dataclass
class WordPressRadarClient:
    base_url: str
    username: str
    app_password: str
    session: object = requests
    timeout: float = 15.0

    @classmethod
    def from_env(cls, **kwargs) -> "WordPressRadarClient":
        return cls(os.getenv("WORDPRESS_BASE_URL", ""), os.getenv("WORDPRESS_USERNAME", ""), os.getenv("WORDPRESS_APP_PASSWORD", ""), **kwargs)

    def validate(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise WordPressRadarError("WordPress URL must use HTTPS")
        if not self.username or not self.app_password:
            raise WordPressRadarError("WordPress radar credentials are not configured")

    @property
    def endpoint(self) -> str:
        if os.getenv("WORDPRESS_REST_API_MODE", "path").strip().lower() == "query":
            return self.base_url.rstrip("/") + "/?rest_route=/finance-radar/v1/snapshots"
        return urljoin(self.base_url.rstrip("/") + "/", "wp-json/finance-radar/v1/snapshots")

    def _headers(self) -> dict:
        token = base64.b64encode(f"{self.username}:{self.app_password}".encode()).decode()
        return {"Authorization": f"Basic {token}", "Content-Type": "application/json", "User-Agent": "Finance-X-Radar/1.0"}

    def publish(self, payload: dict, *, attempts: int = 3) -> dict:
        self.validate()
        last_error = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                response = self.session.post(self.endpoint, json=payload, headers=self._headers(), timeout=self.timeout)
                if response.status_code in (200, 201):
                    data = response.json()
                    return data if isinstance(data, dict) else {"status": "published"}
                retryable = response.status_code in (429,) or response.status_code >= 500
                error = WordPressRadarError(f"WordPress returned HTTP {response.status_code}", status_code=response.status_code, retryable=retryable)
            except (requests.Timeout, requests.ConnectionError) as exc:
                error = WordPressRadarError(type(exc).__name__, retryable=True)
            except (ValueError, requests.RequestException) as exc:
                error = WordPressRadarError(type(exc).__name__, retryable=False)
            last_error = error
            if not error.retryable or attempt >= attempts:
                raise error
            time.sleep(min(2 ** (attempt - 1), 4))
        raise last_error or WordPressRadarError("WordPress publish failed")

    def status(self) -> dict:
        configured = bool(self.base_url and self.username and self.app_password)
        result = {"enabled": os.getenv("WORDPRESS_RADAR_ENABLED", "false").lower() in {"1", "true", "yes"},
                  "public_enabled": os.getenv("WORDPRESS_RADAR_PUBLIC_ENABLED", "false").lower() in {"1", "true", "yes"},
                  "configured": configured, "https": urlparse(self.base_url).scheme == "https" if self.base_url else False}
        if not configured:
            result["status"] = "not_configured"
        else:
            result["status"] = "ready" if result["https"] else "invalid_url"
        return result
