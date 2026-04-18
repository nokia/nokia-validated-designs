"""SR Linux JSON-RPC helper for system tests.

Minimal client that wraps the ``set`` and ``get`` methods exposed on
``https://<host>:443/jsonrpc``. Intended for containerlab targets where
the device presents a self-signed certificate (TLS verification is off).
"""

from __future__ import annotations

import time
from typing import Any

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class SrlCommitError(RuntimeError):
    """Raised when a JSON-RPC set/get returns an error."""


def _post(
    host: str,
    port: int,
    user: str,
    pw: str,
    payload: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    url = f"https://{host}:{port}/jsonrpc"
    resp = requests.post(
        url,
        json=payload,
        auth=(user, pw),
        verify=False,
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise SrlCommitError(
            f"JSON-RPC HTTP {resp.status_code}: {resp.text[:500]}"
        )
    body = resp.json()
    if body.get("error"):
        raise SrlCommitError(f"JSON-RPC error: {body['error']}")
    return body


def commit_set(
    host: str,
    port: int,
    user: str,
    pw: str,
    updates: list[dict],
    replaces: list[dict],
    deletes: list[dict],
    timeout: float = 60.0,
) -> dict[str, Any]:
    """POST a JSON-RPC ``set`` with delete/replace/update op classes.

    Wire order is delete -> replace -> update, matching the
    ``nokia.srlinux.config`` module behaviour.
    """
    commands: list[dict[str, Any]] = []
    for entry in deletes:
        commands.append({"action": "delete", "path": entry["path"]})
    for entry in replaces:
        cmd = {"action": "replace", "path": entry["path"]}
        if "value" in entry:
            cmd["value"] = entry["value"]
        commands.append(cmd)
    for entry in updates:
        cmd = {"action": "update", "path": entry["path"]}
        if "value" in entry:
            cmd["value"] = entry["value"]
        commands.append(cmd)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "set",
        "params": {"commands": commands},
    }
    return _post(host, port, user, pw, payload, timeout)


def get_path(
    host: str,
    port: int,
    user: str,
    pw: str,
    path: str,
    timeout: float = 15.0,
) -> dict[str, Any]:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "get",
        "params": {"commands": [{"path": path}]},
    }
    return _post(host, port, user, pw, payload, timeout)


def wait_json_rpc_ready(
    host: str,
    port: int,
    user: str,
    pw: str,
    timeout_s: float = 180.0,
) -> None:
    """Poll ``/system/name`` until the endpoint returns success."""
    deadline = time.time() + timeout_s
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            get_path(host, port, user, pw, "/system/name", timeout=5.0)
            return
        except (requests.RequestException, SrlCommitError) as exc:
            last_err = exc
            time.sleep(3)
    raise SrlCommitError(
        f"JSON-RPC on {host}:{port} not ready within {timeout_s}s: {last_err}"
    )
