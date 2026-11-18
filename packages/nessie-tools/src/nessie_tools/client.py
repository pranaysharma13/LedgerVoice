"""Thin synchronous HTTP client for Nessie.

Why a hand-written client instead of an SDK: Nessie's official SDKs are old and
untyped. We only need a handful of endpoints, and owning the client lets us add
the things that matter for this project: key redaction, timeouts, and (in M1) a
fake backend with the same interface.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://api.nessieisreal.com"

_KEY_PARAM = re.compile(r"(key=)[^&\s'\"]+")


def redact_key(text: str, api_key: str | None = None) -> str:
    """Remove the API key from a URL or message.

    Two passes: the literal key (wherever it appears) and any `key=...` query param.
    """
    if api_key:
        text = text.replace(api_key, "***")
    return _KEY_PARAM.sub(r"\1***", text)


class NessieError(RuntimeError):
    """Raised for HTTP or transport errors. The message never contains the API key."""

    def __init__(self, message: str, status_code: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class NessieClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 10.0,
        http: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Nessie API key is required")
        self._api_key = api_key
        self._http = http or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    # --- lifecycle -------------------------------------------------------------
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> NessieClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- core ------------------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Send a request and return parsed JSON (or None for an empty body)."""
        query = {"key": self._api_key, **(params or {})}
        try:
            resp = self._http.request(method, path, params=query, json=json)
        except httpx.HTTPError as exc:  # timeouts, DNS, connection refused, ...
            msg = redact_key(f"{type(exc).__name__} calling {method} {path}: {exc}", self._api_key)
            raise NessieError(msg) from None

        if resp.is_error:
            url = redact_key(str(resp.request.url), self._api_key)
            body = redact_key(resp.text[:500], self._api_key)
            raise NessieError(
                f"{method} {url} -> HTTP {resp.status_code}: {body}",
                status_code=resp.status_code,
                body=body,
            )
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            raise NessieError(
                f"{method} {path} returned non-JSON body: {resp.text[:200]!r}",
                status_code=resp.status_code,
            ) from None

    # --- endpoints used in M0 --------------------------------------------------
    def get_customers(self) -> list[dict[str, Any]]:
        data = self.request("GET", "/customers")
        return data if isinstance(data, list) else []
