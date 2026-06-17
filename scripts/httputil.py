"""Shared HTTP helpers for sync_*.py ingesters.

Consolidates the `_get()` retry/backoff helper that was duplicated across
sync_raisin.py, sync_rawwine.py, sync_splendido.py, and the `fetch_page()`
helpers in gaultmillau_at/ch + wirtshauskultur.
"""

from __future__ import annotations

import http.client
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

DEFAULT_USER_AGENT = "restaurant-finder-skill/0.1 (+contact in skill repo)"

# Transient network errors that warrant a retry.
_RETRY_EXCEPTIONS = (
    TimeoutError,
    urllib.error.URLError,
    http.client.RemoteDisconnected,
    http.client.IncompleteRead,
    http.client.BadStatusLine,
    ConnectionResetError,
    ConnectionError,
)


def get(
    url: str,
    *,
    accept: str = "text/html",
    retries: int = 3,
    timeout: int = 60,
    user_agent: str = DEFAULT_USER_AGENT,
    extra_headers: Optional[dict] = None,
) -> bytes:
    """GET `url` with retries on transient network errors.

    Returns the response body as bytes. Non-2xx responses raise
    `urllib.error.HTTPError` immediately (no retry) — callers should
    decide whether to skip the URL or fail.
    """
    safe_url = urllib.parse.quote(url, safe=":/?&=#%")
    headers = {"User-Agent": user_agent, "Accept": accept}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(safe_url, headers=headers)
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except _RETRY_EXCEPTIONS as e:
            last_err = e
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"unreachable: last_err={last_err}")


def fetch_page(
    url: str,
    *,
    timeout: int = 20,
    user_agent: str = DEFAULT_USER_AGENT,
    extra_headers: Optional[dict] = None,
) -> str:
    """Fetch a page and decode as UTF-8 (replacing bad bytes).

    Thin wrapper around `get()` for the common ingester case.
    """
    body = get(
        url,
        accept="text/html",
        timeout=timeout,
        user_agent=user_agent,
        extra_headers=extra_headers,
    )
    return body.decode("utf-8", errors="replace")