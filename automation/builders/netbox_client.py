"""
Thin wrapper around ``pynetbox`` for NVD tooling.

pynetbox >= 7.6 natively supports NetBox 4.5 v2 API tokens (``nbt_...``
prefix) — it switches between the ``Token`` and ``Bearer`` schemes based
on the token's format. We do not need to patch the auth header.

The wrapper centralises two things:

1. Reading credentials from ``NETBOX_URL`` / ``NETBOX_TOKEN`` env vars
   (or explicit arguments).
2. Exposing the underlying ``requests.Session`` as ``nb.http_session``
   so callers that need to hit endpoints not wrapped by pynetbox
   (journal entries, /api/extras/scripts, etc.) can share the same
   authenticated session and TLS settings.
"""

from __future__ import annotations

import logging
import os
import urllib3

import pynetbox

logger = logging.getLogger(__name__)


def make_client(
    url: str | None = None,
    token: str | None = None,
    *,
    verify_tls: bool = False,
    timeout: int = 60,
) -> "pynetbox.api":
    """Build an authenticated ``pynetbox.api`` client and sanity-probe it."""
    url = (url or os.environ.get("NETBOX_URL", "")).rstrip("/")
    token = token or os.environ.get("NETBOX_TOKEN", "")
    if not url:
        raise ValueError("NetBox URL must be provided via --netbox-url or NETBOX_URL env var")
    if not token:
        raise ValueError(
            "NetBox token must be provided via --netbox-token or NETBOX_TOKEN env var"
        )

    nb = pynetbox.api(url, token=token)
    nb.http_session.verify = verify_tls
    # pynetbox sets the auth header per-request internally, not on the
    # session. Mirror the header onto the session so direct calls via
    # nb.http_session.{get,post,...} (journal entries, scripts API, etc.)
    # are authenticated too.
    scheme = "Bearer" if token.startswith("nbt_") and "." in token[4:] else "Token"
    nb.http_session.headers.update({
        "Accept": "application/json",
        "Authorization": f"{scheme} {token}",
    })

    if not verify_tls:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    nb._nvd_base_url = url
    nb._nvd_token = token
    nb._nvd_verify_tls = verify_tls
    nb._nvd_timeout = timeout

    status = nb.http_session.get(f"{url}/api/status/", timeout=timeout)
    if status.status_code != 200:
        raise RuntimeError(
            f"NetBox status probe failed: {status.status_code} {status.text[:200]}"
        )
    info = status.json()
    logger.debug(
        "Connected to NetBox %s (django %s, python %s)",
        info.get("netbox-version"),
        info.get("django-version"),
        info.get("python-version"),
    )
    return nb


def request(
    nb: "pynetbox.api",
    method: str,
    path: str,
    **kwargs: object,
) -> requests.Response:
    """Raw authenticated request against the NetBox API.

    Used for endpoints not wrapped by pynetbox (journal entries, custom
    script trigger, etc.). ``path`` is appended to the NetBox base URL.
    """
    url = f"{nb._nvd_base_url}{path}"
    kwargs.setdefault("timeout", nb._nvd_timeout)
    return nb.http_session.request(method, url, **kwargs)
