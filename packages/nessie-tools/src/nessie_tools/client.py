"""Thin synchronous HTTP client for Nessie.

Why a hand-written client instead of an SDK: Nessie's official SDKs are old and
untyped. We only need a handful of endpoints, and owning the client lets us add
the things that matter for this project: key redaction, timeouts, HTTPS enforcement.
The typed operations live in nessie_backend.py; this file only moves JSON.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from nessie_tools.errors import NessieDataError, NessieError

DEFAULT_BASE_URL = "https://api.nessieisreal.com"

_KEY_PARAM = re.compile(r"([?&]key=)[^&\s'\"]+")


def redact_key(text: str, api_key: str | None = None) -> str:
    """Remove the API key from a URL or message.

    Two passes: the literal key (wherever it appears) and any `?key=` / `&key=` param.
    """
    if api_key:
        text = text.replace(api_key, "***")
    return _KEY_PARAM.sub(r"\1***", text)


class NessieClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 10.0,
        http: httpx.Client | None = None,
    ) -> None:
        """Create a client.

        If `http` is given (tests inject one with a mock transport), that client is
        used as-is: its own base_url and timeout apply, and this class will NOT close
        it, because the caller owns it (F-23). Otherwise a client is created here and
        closed by close().
        """
        if not api_key:
            raise ValueError("Nessie API key is required")
        if not base_url.startswith("https://"):
            raise ValueError("Nessie base URL must use https:// (the API key is sent in the URL)")
        self._api_key = api_key
        self._owns_http = http is None
        self._http = http or httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, follow_redirects=False
        )

    # --- lifecycle -------------------------------------------------------------
    def close(self) -> None:
        if self._owns_http:
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
        """Send a request and return parsed JSON (or None for an empty body).

        Raises NessieError for transport errors and for any non-2xx status.
        """
        query = {"key": self._api_key, **(params or {})}
        try:
            resp = self._http.request(method, path, params=query, json=json)
        except httpx.HTTPError as exc:  # timeouts, DNS, connection refused, ...
            msg = redact_key(f"{type(exc).__name__} calling {method} {path}: {exc}", self._api_key)
            raise NessieError(msg) from None

        if not resp.is_success:
            url = redact_key(str(resp.request.url), self._api_key)
            body = redact_key(resp.text, self._api_key)[:500]
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
            snippet = redact_key(resp.text, self._api_key)[:200]
            raise NessieDataError(
                f"{method} {path} returned non-JSON body: {snippet!r}",
                status_code=resp.status_code,
            ) from None

    # --- used by the M0 smoke check --------------------------------------------
    def get_customers(self) -> list[dict[str, Any]]:
        data = self.request("GET", "/customers")
        if not isinstance(data, list):
            raise NessieDataError(f"expected a list of customers, got {type(data).__name__}")
        return data
