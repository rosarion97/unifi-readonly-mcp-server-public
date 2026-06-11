"""
Ubiquiti UniFi Network Read-Only MCP Server
===========================================
A production-grade Model Context Protocol (MCP) server that exposes
READ-ONLY access to a single UniFi OS console over stdio transport.

It wraps two API surfaces on the SAME console:

  * Network Integration API (official) — `X-API-KEY` auth, modern endpoints,
    UUID identifiers. Base path: `/proxy/network/integration/v1/...`
  * Classic / Internal Controller API — cookie-session auth (username +
    password), broad legacy coverage. Base path: `/proxy/network/api/...`

The Site Manager (cloud) API is intentionally NOT wrapped.

HARD CONSTRAINTS
----------------
* Every tool maps to an HTTP GET. The single shared request helper refuses
  any other verb. The only POST in the codebase is the Classic-API login,
  which is performed internally during session bootstrap and is not
  reachable from any tool.
* Credentials (API key, username, password) are read from environment
  variables at startup. They are never logged and never returned in any
  tool response or error message.
* Transport is stdio. The container is invoked directly by an MCP client.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from typing import Any

import requests
import urllib3
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT = 30  # seconds


def _env(name: str, default: str = "") -> str:
    """Read an env var, treating empty values and the Docker MCP gateway's
    sentinel ``<UNKNOWN>`` as unset.

    The gateway injects every secret declared in the catalog. If the user
    has not stored a value for an optional secret, the gateway still sets
    the env var — to the literal string ``<UNKNOWN>``. A naive
    ``os.environ.get(name, default)`` would never see that as missing, so
    code like ``int(os.environ.get("UNIFI_MAX_RESPONSE_BYTES", "120000"))``
    would crash with ``ValueError: invalid literal for int() with base 10:
    '<UNKNOWN>'`` at module load. This helper normalizes both cases so the
    rest of the file can treat the gateway and direct env-file paths
    identically.
    """
    value = os.environ.get(name, "").strip()
    if not value or value == "<UNKNOWN>":
        return default
    return value


def _require_env(name: str, hint: str) -> str:
    value = _env(name)
    if not value:
        print(f"ERROR: {name} is not set. {hint}", file=sys.stderr)
        sys.exit(1)
    return value


# Cap the JSON size of any single tool response. UniFi endpoints like
# /stat/event, /stat/sta, /stat/dpi, and /clients can otherwise return
# megabytes of JSON and blow past the model's context window. Override with
# UNIFI_MAX_RESPONSE_BYTES (e.g. 250000 for big-window models).
MAX_RESPONSE_BYTES = max(
    int(_env("UNIFI_MAX_RESPONSE_BYTES", "120000")),
    10_000,
)

# Hard ceiling for any tool's per_page / limit parameter. The model can ask
# for less; it cannot ask for more.
PER_PAGE_CAP = 200
EVENT_PER_PAGE_DEFAULT = 25
LIST_PER_PAGE_DEFAULT = 50


UNIFI_HOST = _require_env(
    "UNIFI_HOST",
    "Provide the UniFi OS console host or IP (e.g. 192.168.1.1) via "
    "--env-file .env or a podman/docker secret injected with type=env.",
)
UNIFI_PORT = _env("UNIFI_PORT", "443")

UNIFI_API_KEY = _require_env(
    "UNIFI_API_KEY",
    "Generate it in the UniFi Network UI: Settings -> Control Plane -> "
    "Integrations -> Create API Key. Inject via --env-file or a secret with "
    "type=env. The key inherits the parent admin's permissions, so use a "
    "Limited Admin (Read-Only) admin.",
)

# Integration API uses UUIDs; Classic API uses a short site name (typically
# "default"). They are NOT interchangeable.
UNIFI_SITE_ID = _require_env(
    "UNIFI_SITE_ID",
    "The Integration-API site UUID this instance is pinned to. Discover IDs "
    "by calling unifi_int_list_sites once with a temporary configuration, "
    "then pin the chosen UUID.",
)
UNIFI_SITE_NAME = _env("UNIFI_SITE_NAME", "default")

# Classic API auth is optional — if absent, only Integration tools work and
# Classic tools raise a clear ValueError instead of failing at startup.
UNIFI_CLASSIC_USERNAME = _env("UNIFI_CLASSIC_USERNAME")
UNIFI_CLASSIC_PASSWORD = _env("UNIFI_CLASSIC_PASSWORD")

# TLS verification. UniFi OS ships with a self-signed cert by default; most
# homelab and on-LAN deployments leave verify off. Set true once a real cert
# is installed on the console.
_verify_env = _env("UNIFI_VERIFY_TLS", "false").lower()
UNIFI_VERIFY_TLS = _verify_env in {"1", "true", "yes", "on"}

if not UNIFI_VERIFY_TLS:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Base URLs. Both API surfaces sit behind the UniFi OS reverse proxy on the
# same host:port. /api/auth/login is the only path NOT under /proxy/network.
_BASE = f"https://{UNIFI_HOST}:{UNIFI_PORT}"
INTEGRATION_BASE = f"{_BASE}/proxy/network/integration/v1"
CLASSIC_BASE = f"{_BASE}/proxy/network/api"
CLASSIC_LOGIN_URL = f"{_BASE}/api/auth/login"

# ---------------------------------------------------------------------------
# HTTP sessions
# ---------------------------------------------------------------------------

# Integration API: stateless, X-API-KEY on every request.
_int_session = requests.Session()
_int_session.headers.update(
    {
        "X-API-KEY": UNIFI_API_KEY,
        "Accept": "application/json",
        "User-Agent": "unifi-readonly-mcp/1.0 (stdio)",
    }
)
_int_session.verify = UNIFI_VERIFY_TLS

# Classic API: cookie-session, lazily logged in on first use.
_classic_session = requests.Session()
_classic_session.headers.update(
    {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "unifi-readonly-mcp/1.0 (stdio)",
    }
)
_classic_session.verify = UNIFI_VERIFY_TLS
_classic_logged_in = False


# ---------------------------------------------------------------------------
# READ-ONLY guard — every tool routes through here
# ---------------------------------------------------------------------------

READ_ONLY_REFUSAL = "This server is read-only. Operation refused."
ALLOWED_METHODS = {"GET"}


def _request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
) -> requests.Response:
    """Single HTTP entry point for every tool. Refuses any non-GET verb.

    The Classic-API login flow does NOT go through this helper; it calls
    requests directly so the guard here can stay strictly GET-only.
    """
    if method.upper() not in ALLOWED_METHODS:
        raise PermissionError(
            f"{READ_ONLY_REFUSAL} (attempted {method.upper()} {url})"
        )
    try:
        return session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as exc:
        raise ValueError(f"Network error contacting UniFi console: {exc}") from None


# ---------------------------------------------------------------------------
# Integration API helper
# ---------------------------------------------------------------------------


def _int_get(path: str, params: dict[str, Any] | None = None) -> Any:
    """Issue a GET to the Network Integration API and return parsed JSON.

    Raises ValueError with a descriptive (but credential-free) message on
    any non-200 response.
    """
    url = f"{INTEGRATION_BASE}{path}"
    response = _request(_int_session, "GET", url, params=params)
    status = response.status_code
    if status == 200:
        try:
            return response.json()
        except ValueError:
            raise ValueError(
                "UniFi Integration API returned a non-JSON 200 response."
            ) from None
    if status == 401:
        raise ValueError(
            "401 Unauthorized: the configured UNIFI_API_KEY was rejected. "
            "Confirm the key is valid and the parent admin still exists."
        )
    if status == 403:
        raise ValueError(
            "403 Forbidden: the API key's parent admin lacks permission for "
            "this resource (Limited Admin scope too narrow?)."
        )
    if status == 404:
        raise ValueError(
            f"Not found: {path}. The endpoint may require a newer Network "
            "Application version (≥ 9.0; richer coverage in 9.3+). Call "
            "unifi_int_get_info to confirm the running version."
        )
    if status == 429:
        raise ValueError("UniFi Integration API rate-limited. Retry after a moment.")
    body_preview = response.text[:200] if response.text else ""
    raise ValueError(f"UniFi Integration API error {status}: {body_preview}")


# ---------------------------------------------------------------------------
# Classic API helper (cookie-session, with lazy login + one re-login retry)
# ---------------------------------------------------------------------------


def _classic_login() -> None:
    """Establish a cookie session against the UniFi OS auth layer.

    The login itself is performed via a one-off ``requests.request("POST",
    ...)`` call — never on a long-lived session — and the resulting cookies
    are copied onto ``_classic_session``. This keeps the codebase invariant
    that neither shared session ever issues anything other than GET, which
    is what the read-only grep lint verifies.
    """
    global _classic_logged_in
    if not UNIFI_CLASSIC_USERNAME or not UNIFI_CLASSIC_PASSWORD:
        raise ValueError(
            "Classic-API tools require UNIFI_CLASSIC_USERNAME and "
            "UNIFI_CLASSIC_PASSWORD. Create a dedicated local Limited Admin "
            "(Read-Only) — never a UI.com cloud account, which enforces MFA — "
            "and set both variables. The Integration-API tools "
            "(unifi_int_*) work without these and may be sufficient on their "
            "own."
        )
    payload = {
        "username": UNIFI_CLASSIC_USERNAME,
        "password": UNIFI_CLASSIC_PASSWORD,
        "remember": True,
    }
    try:
        response = requests.request(
            "POST",
            CLASSIC_LOGIN_URL,
            json=payload,
            timeout=DEFAULT_TIMEOUT,
            verify=UNIFI_VERIFY_TLS,
        )
    except requests.RequestException as exc:
        raise ValueError(
            f"Network error logging into UniFi console: {exc}"
        ) from None
    if response.status_code == 499:
        raise ValueError(
            "UniFi login returned 499: the configured admin has 2FA enabled. "
            "Create a dedicated local admin without MFA for this MCP server."
        )
    if response.status_code == 401 or response.status_code == 403:
        raise ValueError(
            "Classic-API login rejected (401/403). Verify "
            "UNIFI_CLASSIC_USERNAME / UNIFI_CLASSIC_PASSWORD and that the "
            "admin is a *local* account (not UI.com cloud)."
        )
    if response.status_code >= 400:
        body_preview = response.text[:200] if response.text else ""
        raise ValueError(
            f"Classic-API login failed ({response.status_code}): {body_preview}"
        )
    # Copy the auth cookies onto the long-lived session so subsequent GETs
    # carry the session.
    _classic_session.cookies.update(response.cookies)
    # Capture the CSRF token if the console sent one; harmless for GETs but
    # tracked for completeness.
    csrf = response.headers.get("X-CSRF-Token") or response.headers.get(
        "x-csrf-token"
    )
    if csrf:
        _classic_session.headers["X-CSRF-Token"] = csrf
    _classic_logged_in = True


def _classic_get(path: str, params: dict[str, Any] | None = None) -> Any:
    """Issue a GET to the Classic Controller API and return parsed JSON.

    Branches on ``meta.rc`` before HTTP status, per the UniFi convention.
    Performs one re-login on session expiry, then surfaces a ValueError.
    """
    global _classic_logged_in
    if not _classic_logged_in:
        _classic_login()
    url = f"{CLASSIC_BASE}{path}"

    def _do() -> requests.Response:
        return _request(_classic_session, "GET", url, params=params)

    response = _do()
    if response.status_code in (401, 403):
        # Stale or rejected session — try one re-login.
        _classic_logged_in = False
        _classic_login()
        response = _do()

    status = response.status_code
    if status == 200:
        try:
            body = response.json()
        except ValueError:
            raise ValueError(
                "UniFi Classic API returned a non-JSON 200 response."
            ) from None
        # The Classic API can return HTTP 200 with meta.rc=error — branch
        # on meta.rc first, per the developer guide.
        if isinstance(body, dict):
            meta = body.get("meta", {})
            if isinstance(meta, dict) and meta.get("rc") == "error":
                msg = meta.get("msg", "")
                if msg == "api.err.LoginRequired":
                    _classic_logged_in = False
                    _classic_login()
                    response = _do()
                    if response.status_code == 200:
                        return response.json()
                raise ValueError(
                    f"UniFi Classic API returned error: {msg or 'unspecified'}"
                )
        return body
    if status == 404:
        raise ValueError(
            f"Not found: {path}. The endpoint may not exist on this "
            "controller version, or the site name may be wrong (current: "
            f"'{UNIFI_SITE_NAME}')."
        )
    if status == 429:
        raise ValueError("UniFi Classic API rate-limited. Retry after a moment.")
    body_preview = response.text[:200] if response.text else ""
    raise ValueError(f"UniFi Classic API error {status}: {body_preview}")


# ---------------------------------------------------------------------------
# Response-size guardrails
# ---------------------------------------------------------------------------


def _clamp_per_page(per_page: int, cap: int = PER_PAGE_CAP) -> int:
    """Clamp the caller's per_page / limit request to a safe maximum."""
    try:
        n = int(per_page)
    except (TypeError, ValueError):
        return cap
    if n < 1:
        return 1
    return min(n, cap)


def _bounded(
    payload: Any,
    hint: str = (
        "Narrow the query: shorter window, lower limit, or filter "
        "by id."
    ),
) -> Any:
    """Cap the JSON-serialised size of a tool response.

    If the payload fits ``MAX_RESPONSE_BYTES``, return it unchanged.
    Otherwise return a truncation envelope describing what was kept and how
    to re-query.
    """
    try:
        raw = json.dumps(payload, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return payload

    if len(raw) <= MAX_RESPONSE_BYTES:
        return payload

    if isinstance(payload, list):
        kept: list[Any] = []
        running = 0
        for item in payload:
            chunk = len(json.dumps(item, separators=(",", ":"), default=str))
            if running + chunk > MAX_RESPONSE_BYTES:
                break
            kept.append(item)
            running += chunk
        return {
            "_truncated": True,
            "_returned": len(kept),
            "_total": len(payload),
            "_bytes_cap": MAX_RESPONSE_BYTES,
            "_hint": hint,
            "data": kept,
        }

    # Dict-shaped payloads (envelopes from both APIs): if the embedded
    # `data` list is the culprit, truncate that list and keep the envelope.
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        original = payload["data"]
        kept = []
        running = 0
        for item in original:
            chunk = len(json.dumps(item, separators=(",", ":"), default=str))
            if running + chunk > MAX_RESPONSE_BYTES:
                break
            kept.append(item)
            running += chunk
        envelope = {k: v for k, v in payload.items() if k != "data"}
        envelope.update(
            {
                "_truncated": True,
                "_returned": len(kept),
                "_total": len(original),
                "_bytes_cap": MAX_RESPONSE_BYTES,
                "_hint": hint,
                "data": kept,
            }
        )
        return envelope

    return {
        "_truncated": True,
        "_original_bytes": len(raw),
        "_bytes_cap": MAX_RESPONSE_BYTES,
        "_hint": hint,
        "preview": raw[:MAX_RESPONSE_BYTES],
    }


def _project(items: Any, keep: set[str], verbose: bool) -> Any:
    """Narrow a list of dicts to the ``keep`` fields unless verbose=True."""
    if verbose or not isinstance(items, list):
        return items
    return [
        {k: v for k, v in item.items() if k in keep}
        for item in items
        if isinstance(item, dict)
    ]


def _classic_project_envelope(
    envelope: Any, keep: set[str], verbose: bool
) -> Any:
    """Apply field projection to the `data` array of a Classic envelope."""
    if isinstance(envelope, dict) and isinstance(envelope.get("data"), list):
        envelope = dict(envelope)
        envelope["data"] = _project(envelope["data"], keep, verbose)
    return envelope


def _int_project_envelope(envelope: Any, keep: set[str], verbose: bool) -> Any:
    """Apply field projection to the `data` array of an Integration envelope."""
    if isinstance(envelope, dict) and isinstance(envelope.get("data"), list):
        envelope = dict(envelope)
        envelope["data"] = _project(envelope["data"], keep, verbose)
    return envelope


# Field projections for the highest-cardinality endpoints. Conservative —
# common troubleshooting fields. Set ``verbose=True`` for full records.

_INT_DEVICE_KEEP = {
    "id", "name", "model", "macAddress", "ipAddress", "state",
    "firmwareVersion", "adoptedAt", "productLine", "shortname",
    "uplink",
}
_INT_CLIENT_KEEP = {
    "id", "name", "macAddress", "ipAddress", "type", "connectedAt",
    "lastSeen", "network", "site", "uplinkDeviceId", "access",
    "isWired",
}
_INT_NETWORK_KEEP = {
    "id", "name", "vlanId", "enabled", "management", "dhcpGuarding",
    "ipSubnet", "purpose",
}
_INT_BROADCAST_KEEP = {
    "id", "name", "type", "enabled", "hideName", "network",
    "mloEnabled", "bandSteeringEnabled", "clientIsolationEnabled",
    "broadcastingFrequenciesGHz",
}
_INT_VOUCHER_KEEP = {
    "id", "name", "code", "createdAt", "expiresAt", "authorizedGuestLimit",
    "timeLimitMinutes", "dataUsageLimitMBytes", "rxRateLimitKbps",
    "txRateLimitKbps", "remainingAuthorizations",
}

_CLASSIC_DEVICE_KEEP = {
    "_id", "mac", "name", "model", "type", "state", "ip", "version",
    "site_id", "adopted", "uptime", "last_seen", "serial", "device_id",
    "num_sta", "user-num_sta", "guest-num_sta",
}
_CLASSIC_CLIENT_KEEP = {
    "_id", "mac", "hostname", "name", "ip", "is_wired", "network",
    "essid", "ap_mac", "sw_mac", "sw_port", "first_seen", "last_seen",
    "uptime", "user_id", "oui", "vlan", "satisfaction", "signal", "noise",
}
_CLASSIC_EVENT_KEEP = {
    "_id", "key", "msg", "time", "datetime", "subsystem", "is_admin",
    "ap", "sw", "gw", "user", "ssid", "network",
}
_CLASSIC_ALARM_KEEP = {
    "_id", "key", "msg", "time", "datetime", "subsystem", "archived",
    "handled_admin_id", "handled_time", "ap", "sw", "gw",
}


# ---------------------------------------------------------------------------
# FastMCP instance
# ---------------------------------------------------------------------------

mcp = FastMCP("unifi-readonly")


# ===========================================================================
# NETWORK INTEGRATION API TOOLS (X-API-KEY)
# ===========================================================================


# ---------------------------------------------------------------------------
# Application & Sites
# ---------------------------------------------------------------------------


@mcp.tool()
def unifi_int_get_info() -> Any:
    """Return Network application version and capabilities.

    Endpoint: GET /info. Always call this first when troubleshooting — the
    Integration-API surface varies between Network 9.0, 9.3, and 10.x.
    """
    return _int_get("/info")


@mcp.tool()
def unifi_int_list_sites() -> Any:
    """List sites local to the configured UniFi OS console.

    Endpoint: GET /sites. Diagnostic only — this server is pinned to
    UNIFI_SITE_ID and every other Integration tool ignores the rest of this
    list. Use this once to discover the UUID, then pin it.
    """
    return _int_get("/sites")


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


@mcp.tool()
def unifi_int_list_devices(
    offset: int = 0, limit: int = LIST_PER_PAGE_DEFAULT, verbose: bool = False
) -> Any:
    """List adopted UniFi devices in the pinned site.

    Endpoint: GET /sites/{siteId}/devices. ``limit`` is clamped server-side
    to PER_PAGE_CAP (200). Narrowed by default; pass ``verbose=True`` for
    full device records.
    """
    limit = _clamp_per_page(limit)
    data = _int_get(
        f"/sites/{UNIFI_SITE_ID}/devices",
        params={"offset": int(offset), "limit": limit},
    )
    data = _int_project_envelope(data, _INT_DEVICE_KEEP, verbose)
    return _bounded(
        data,
        hint="Lower limit, page via offset, or call unifi_int_get_device for one record.",
    )


@mcp.tool()
def unifi_int_get_device(device_id: str) -> Any:
    """Return details of one adopted device (UUID).

    Endpoint: GET /sites/{siteId}/devices/{deviceId}.
    """
    return _int_get(f"/sites/{UNIFI_SITE_ID}/devices/{device_id}")


@mcp.tool()
def unifi_int_get_device_statistics_latest(device_id: str) -> Any:
    """Return the latest statistics snapshot for one adopted device.

    Endpoint: GET /sites/{siteId}/devices/{deviceId}/statistics/latest.
    """
    return _int_get(
        f"/sites/{UNIFI_SITE_ID}/devices/{device_id}/statistics/latest"
    )


@mcp.tool()
def unifi_int_list_pending_devices(
    offset: int = 0, limit: int = LIST_PER_PAGE_DEFAULT
) -> Any:
    """List devices pending adoption across the console.

    Endpoint: GET /pending-devices. Not site-scoped.
    """
    limit = _clamp_per_page(limit)
    data = _int_get(
        "/pending-devices",
        params={"offset": int(offset), "limit": limit},
    )
    return _bounded(data, hint="Lower limit or page via offset.")


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


@mcp.tool()
def unifi_int_list_clients(
    offset: int = 0,
    limit: int = LIST_PER_PAGE_DEFAULT,
    verbose: bool = False,
    filter: str | None = None,
) -> Any:
    """List currently connected clients in the pinned site.

    Endpoint: GET /sites/{siteId}/clients. Optional ``filter`` uses the
    Integration-API filter syntax (e.g. ``name.like('guest*')``,
    ``and(isWired.eq(true), lastSeen.gt(2025-01-01))``). Narrowed by
    default; pass ``verbose=True`` for full client records.
    """
    limit = _clamp_per_page(limit)
    params: dict[str, Any] = {"offset": int(offset), "limit": limit}
    if filter:
        params["filter"] = filter
    data = _int_get(f"/sites/{UNIFI_SITE_ID}/clients", params=params)
    data = _int_project_envelope(data, _INT_CLIENT_KEEP, verbose)
    return _bounded(
        data,
        hint="Lower limit, page via offset, or apply a filter expression (e.g. type.eq('WIRELESS')).",
    )


@mcp.tool()
def unifi_int_get_client(client_id: str) -> Any:
    """Return details of one connected client.

    Endpoint: GET /sites/{siteId}/clients/{clientId}.
    """
    return _int_get(f"/sites/{UNIFI_SITE_ID}/clients/{client_id}")


# ---------------------------------------------------------------------------
# Networks
# ---------------------------------------------------------------------------


@mcp.tool()
def unifi_int_list_networks(
    offset: int = 0, limit: int = LIST_PER_PAGE_DEFAULT, verbose: bool = False
) -> Any:
    """List configured networks (VLANs) in the pinned site.

    Endpoint: GET /sites/{siteId}/networks.
    """
    limit = _clamp_per_page(limit)
    data = _int_get(
        f"/sites/{UNIFI_SITE_ID}/networks",
        params={"offset": int(offset), "limit": limit},
    )
    data = _int_project_envelope(data, _INT_NETWORK_KEEP, verbose)
    return _bounded(data, hint="Set verbose=True for full network records.")


@mcp.tool()
def unifi_int_get_network(network_id: str) -> Any:
    """Return details of one network (VLAN).

    Endpoint: GET /sites/{siteId}/networks/{networkId}.
    """
    return _int_get(f"/sites/{UNIFI_SITE_ID}/networks/{network_id}")


@mcp.tool()
def unifi_int_get_network_references(network_id: str) -> Any:
    """Return objects (WLANs, firewall rules, etc.) referencing a network.

    Endpoint: GET /sites/{siteId}/networks/{networkId}/references.
    """
    return _int_get(
        f"/sites/{UNIFI_SITE_ID}/networks/{network_id}/references"
    )


# ---------------------------------------------------------------------------
# WiFi broadcasts (SSIDs)
# ---------------------------------------------------------------------------


@mcp.tool()
def unifi_int_list_wifi_broadcasts(
    offset: int = 0, limit: int = LIST_PER_PAGE_DEFAULT, verbose: bool = False
) -> Any:
    """List WiFi broadcasts (SSIDs) in the pinned site.

    Endpoint: GET /sites/{siteId}/wifi/broadcasts.
    """
    limit = _clamp_per_page(limit)
    data = _int_get(
        f"/sites/{UNIFI_SITE_ID}/wifi/broadcasts",
        params={"offset": int(offset), "limit": limit},
    )
    data = _int_project_envelope(data, _INT_BROADCAST_KEEP, verbose)
    return _bounded(data, hint="Set verbose=True for full SSID configuration.")


@mcp.tool()
def unifi_int_get_wifi_broadcast(wifi_broadcast_id: str) -> Any:
    """Return details of one WiFi broadcast (SSID).

    Endpoint: GET /sites/{siteId}/wifi/broadcasts/{wifiBroadcastId}.
    """
    return _int_get(
        f"/sites/{UNIFI_SITE_ID}/wifi/broadcasts/{wifi_broadcast_id}"
    )


# ---------------------------------------------------------------------------
# Firewall (zones, policies, ordering)
# ---------------------------------------------------------------------------


@mcp.tool()
def unifi_int_list_firewall_zones(
    offset: int = 0, limit: int = LIST_PER_PAGE_DEFAULT
) -> Any:
    """List firewall zones in the pinned site.

    Endpoint: GET /sites/{siteId}/firewall/zones.
    """
    limit = _clamp_per_page(limit)
    data = _int_get(
        f"/sites/{UNIFI_SITE_ID}/firewall/zones",
        params={"offset": int(offset), "limit": limit},
    )
    return _bounded(data)


@mcp.tool()
def unifi_int_get_firewall_zone(firewall_zone_id: str) -> Any:
    """Return details of one firewall zone.

    Endpoint: GET /sites/{siteId}/firewall/zones/{firewallZoneId}.
    """
    return _int_get(
        f"/sites/{UNIFI_SITE_ID}/firewall/zones/{firewall_zone_id}"
    )


@mcp.tool()
def unifi_int_list_firewall_policies(
    offset: int = 0, limit: int = LIST_PER_PAGE_DEFAULT
) -> Any:
    """List firewall policies in the pinned site.

    Endpoint: GET /sites/{siteId}/firewall/policies.
    """
    limit = _clamp_per_page(limit)
    data = _int_get(
        f"/sites/{UNIFI_SITE_ID}/firewall/policies",
        params={"offset": int(offset), "limit": limit},
    )
    return _bounded(data, hint="Lower limit or page via offset on busy zones.")


@mcp.tool()
def unifi_int_get_firewall_policy(firewall_policy_id: str) -> Any:
    """Return details of one firewall policy.

    Endpoint: GET /sites/{siteId}/firewall/policies/{firewallPolicyId}.
    """
    return _int_get(
        f"/sites/{UNIFI_SITE_ID}/firewall/policies/{firewall_policy_id}"
    )


@mcp.tool()
def unifi_int_get_firewall_policy_ordering(
    source_firewall_zone_id: str, destination_firewall_zone_id: str
) -> Any:
    """Return user-defined firewall policy ordering between two zones.

    Endpoint: GET /sites/{siteId}/firewall/policies/ordering.
    """
    return _int_get(
        f"/sites/{UNIFI_SITE_ID}/firewall/policies/ordering",
        params={
            "sourceFirewallZoneId": source_firewall_zone_id,
            "destinationFirewallZoneId": destination_firewall_zone_id,
        },
    )


# ---------------------------------------------------------------------------
# Access Control (ACL Rules)
# ---------------------------------------------------------------------------


@mcp.tool()
def unifi_int_list_acl_rules(
    offset: int = 0, limit: int = LIST_PER_PAGE_DEFAULT
) -> Any:
    """List ACL rules (IPV4 / MAC) in the pinned site.

    Endpoint: GET /sites/{siteId}/acl-rules.
    """
    limit = _clamp_per_page(limit)
    data = _int_get(
        f"/sites/{UNIFI_SITE_ID}/acl-rules",
        params={"offset": int(offset), "limit": limit},
    )
    return _bounded(data)


@mcp.tool()
def unifi_int_get_acl_rule(acl_rule_id: str) -> Any:
    """Return details of one ACL rule.

    Endpoint: GET /sites/{siteId}/acl-rules/{aclRuleId}.
    """
    return _int_get(f"/sites/{UNIFI_SITE_ID}/acl-rules/{acl_rule_id}")


@mcp.tool()
def unifi_int_get_acl_rule_ordering() -> Any:
    """Return user-defined ACL rule ordering.

    Endpoint: GET /sites/{siteId}/acl-rules/ordering.
    """
    return _int_get(f"/sites/{UNIFI_SITE_ID}/acl-rules/ordering")


# ---------------------------------------------------------------------------
# Switching (stacks, MC-LAG, LAGs)
# ---------------------------------------------------------------------------


@mcp.tool()
def unifi_int_list_switch_stacks(
    offset: int = 0, limit: int = LIST_PER_PAGE_DEFAULT
) -> Any:
    """List switch stacks in the pinned site.

    Endpoint: GET /sites/{siteId}/switching/switch-stacks.
    """
    limit = _clamp_per_page(limit)
    data = _int_get(
        f"/sites/{UNIFI_SITE_ID}/switching/switch-stacks",
        params={"offset": int(offset), "limit": limit},
    )
    return _bounded(data)


@mcp.tool()
def unifi_int_get_switch_stack(switch_stack_id: str) -> Any:
    """Return details of one switch stack.

    Endpoint: GET /sites/{siteId}/switching/switch-stacks/{switchStackId}.
    """
    return _int_get(
        f"/sites/{UNIFI_SITE_ID}/switching/switch-stacks/{switch_stack_id}"
    )


@mcp.tool()
def unifi_int_list_mc_lag_domains(
    offset: int = 0, limit: int = LIST_PER_PAGE_DEFAULT
) -> Any:
    """List MC-LAG domains in the pinned site.

    Endpoint: GET /sites/{siteId}/switching/mc-lag-domains.
    """
    limit = _clamp_per_page(limit)
    data = _int_get(
        f"/sites/{UNIFI_SITE_ID}/switching/mc-lag-domains",
        params={"offset": int(offset), "limit": limit},
    )
    return _bounded(data)


@mcp.tool()
def unifi_int_get_mc_lag_domain(mc_lag_domain_id: str) -> Any:
    """Return details of one MC-LAG domain.

    Endpoint: GET /sites/{siteId}/switching/mc-lag-domains/{mcLagDomainId}.
    """
    return _int_get(
        f"/sites/{UNIFI_SITE_ID}/switching/mc-lag-domains/{mc_lag_domain_id}"
    )


@mcp.tool()
def unifi_int_list_lags(
    offset: int = 0, limit: int = LIST_PER_PAGE_DEFAULT
) -> Any:
    """List LAGs (LOCAL / SWITCH_STACK / MULTI_CHASSIS) in the pinned site.

    Endpoint: GET /sites/{siteId}/switching/lags.
    """
    limit = _clamp_per_page(limit)
    data = _int_get(
        f"/sites/{UNIFI_SITE_ID}/switching/lags",
        params={"offset": int(offset), "limit": limit},
    )
    return _bounded(data)


@mcp.tool()
def unifi_int_get_lag(lag_id: str) -> Any:
    """Return details of one LAG.

    Endpoint: GET /sites/{siteId}/switching/lags/{lagId}.
    """
    return _int_get(f"/sites/{UNIFI_SITE_ID}/switching/lags/{lag_id}")


# ---------------------------------------------------------------------------
# DNS Policies
# ---------------------------------------------------------------------------


@mcp.tool()
def unifi_int_list_dns_policies(
    offset: int = 0, limit: int = LIST_PER_PAGE_DEFAULT
) -> Any:
    """List DNS policies (A/AAAA/CNAME/MX/TXT/SRV/FORWARD_DOMAIN) in the pinned site.

    Endpoint: GET /sites/{siteId}/dns/policies.
    """
    limit = _clamp_per_page(limit)
    data = _int_get(
        f"/sites/{UNIFI_SITE_ID}/dns/policies",
        params={"offset": int(offset), "limit": limit},
    )
    return _bounded(data)


@mcp.tool()
def unifi_int_get_dns_policy(dns_policy_id: str) -> Any:
    """Return details of one DNS policy.

    Endpoint: GET /sites/{siteId}/dns/policies/{dnsPolicyId}.
    """
    return _int_get(
        f"/sites/{UNIFI_SITE_ID}/dns/policies/{dns_policy_id}"
    )


# ---------------------------------------------------------------------------
# Traffic Matching Lists
# ---------------------------------------------------------------------------


@mcp.tool()
def unifi_int_list_traffic_matching_lists(
    offset: int = 0, limit: int = LIST_PER_PAGE_DEFAULT
) -> Any:
    """List traffic matching lists (PORTS / IPV4_ADDRESSES / IPV6_ADDRESSES).

    Endpoint: GET /sites/{siteId}/traffic-matching-lists.
    """
    limit = _clamp_per_page(limit)
    data = _int_get(
        f"/sites/{UNIFI_SITE_ID}/traffic-matching-lists",
        params={"offset": int(offset), "limit": limit},
    )
    return _bounded(data)


@mcp.tool()
def unifi_int_get_traffic_matching_list(traffic_matching_list_id: str) -> Any:
    """Return details of one traffic matching list.

    Endpoint: GET /sites/{siteId}/traffic-matching-lists/{trafficMatchingListId}.
    """
    return _int_get(
        f"/sites/{UNIFI_SITE_ID}/traffic-matching-lists/{traffic_matching_list_id}"
    )


# ---------------------------------------------------------------------------
# Hotspot (vouchers — read only)
# ---------------------------------------------------------------------------


@mcp.tool()
def unifi_int_list_hotspot_vouchers(
    offset: int = 0, limit: int = 100, verbose: bool = False
) -> Any:
    """List hotspot vouchers in the pinned site.

    Endpoint: GET /sites/{siteId}/hotspot/vouchers. ``limit`` is clamped
    server-side to PER_PAGE_CAP (200) — the API itself accepts up to 1000.
    """
    limit = _clamp_per_page(limit)
    data = _int_get(
        f"/sites/{UNIFI_SITE_ID}/hotspot/vouchers",
        params={"offset": int(offset), "limit": limit},
    )
    data = _int_project_envelope(data, _INT_VOUCHER_KEEP, verbose)
    return _bounded(data, hint="Page via offset; voucher tables can be long.")


@mcp.tool()
def unifi_int_get_hotspot_voucher(voucher_id: str) -> Any:
    """Return details of one hotspot voucher.

    Endpoint: GET /sites/{siteId}/hotspot/vouchers/{voucherId}.
    """
    return _int_get(
        f"/sites/{UNIFI_SITE_ID}/hotspot/vouchers/{voucher_id}"
    )


# ---------------------------------------------------------------------------
# Supporting Resources (WAN, VPN, RADIUS, tags, DPI catalog, countries)
# ---------------------------------------------------------------------------


@mcp.tool()
def unifi_int_list_wans(
    offset: int = 0, limit: int = LIST_PER_PAGE_DEFAULT
) -> Any:
    """List WAN interfaces in the pinned site.

    Endpoint: GET /sites/{siteId}/wans.
    """
    limit = _clamp_per_page(limit)
    data = _int_get(
        f"/sites/{UNIFI_SITE_ID}/wans",
        params={"offset": int(offset), "limit": limit},
    )
    return _bounded(data)


@mcp.tool()
def unifi_int_list_vpn_site_to_site_tunnels(
    offset: int = 0, limit: int = LIST_PER_PAGE_DEFAULT
) -> Any:
    """List site-to-site VPN tunnels in the pinned site.

    Endpoint: GET /sites/{siteId}/vpn/site-to-site-tunnels.
    """
    limit = _clamp_per_page(limit)
    data = _int_get(
        f"/sites/{UNIFI_SITE_ID}/vpn/site-to-site-tunnels",
        params={"offset": int(offset), "limit": limit},
    )
    return _bounded(data)


@mcp.tool()
def unifi_int_list_vpn_servers(
    offset: int = 0, limit: int = LIST_PER_PAGE_DEFAULT
) -> Any:
    """List VPN servers in the pinned site.

    Endpoint: GET /sites/{siteId}/vpn/servers.
    """
    limit = _clamp_per_page(limit)
    data = _int_get(
        f"/sites/{UNIFI_SITE_ID}/vpn/servers",
        params={"offset": int(offset), "limit": limit},
    )
    return _bounded(data)


@mcp.tool()
def unifi_int_list_radius_profiles(
    offset: int = 0, limit: int = LIST_PER_PAGE_DEFAULT
) -> Any:
    """List RADIUS profiles in the pinned site.

    Endpoint: GET /sites/{siteId}/radius/profiles.
    """
    limit = _clamp_per_page(limit)
    data = _int_get(
        f"/sites/{UNIFI_SITE_ID}/radius/profiles",
        params={"offset": int(offset), "limit": limit},
    )
    return _bounded(data)


@mcp.tool()
def unifi_int_list_device_tags(
    offset: int = 0, limit: int = LIST_PER_PAGE_DEFAULT
) -> Any:
    """List device tags in the pinned site.

    Endpoint: GET /sites/{siteId}/device-tags.
    """
    limit = _clamp_per_page(limit)
    data = _int_get(
        f"/sites/{UNIFI_SITE_ID}/device-tags",
        params={"offset": int(offset), "limit": limit},
    )
    return _bounded(data)


@mcp.tool()
def unifi_int_list_dpi_categories(
    offset: int = 0, limit: int = LIST_PER_PAGE_DEFAULT
) -> Any:
    """List DPI application categories (global catalog).

    Endpoint: GET /dpi/categories. Not site-scoped.
    """
    limit = _clamp_per_page(limit)
    data = _int_get(
        "/dpi/categories",
        params={"offset": int(offset), "limit": limit},
    )
    return _bounded(data, hint="Page via offset; this is a large catalog.")


@mcp.tool()
def unifi_int_list_dpi_applications(
    offset: int = 0, limit: int = LIST_PER_PAGE_DEFAULT
) -> Any:
    """List DPI applications (global catalog).

    Endpoint: GET /dpi/applications. Not site-scoped.
    """
    limit = _clamp_per_page(limit)
    data = _int_get(
        "/dpi/applications",
        params={"offset": int(offset), "limit": limit},
    )
    return _bounded(data, hint="Page via offset; this catalog has thousands of rows.")


@mcp.tool()
def unifi_int_list_countries(
    offset: int = 0, limit: int = LIST_PER_PAGE_DEFAULT
) -> Any:
    """List ISO countries (reference data).

    Endpoint: GET /countries. Not site-scoped.
    """
    limit = _clamp_per_page(limit)
    data = _int_get(
        "/countries", params={"offset": int(offset), "limit": limit}
    )
    return _bounded(data)


# ===========================================================================
# CLASSIC CONTROLLER API TOOLS (cookie session)
# ===========================================================================


def _site_path(suffix: str) -> str:
    """Build a Classic-API path under the pinned site."""
    return f"/s/{UNIFI_SITE_NAME}{suffix}"


# ---------------------------------------------------------------------------
# Site / system
# ---------------------------------------------------------------------------


@mcp.tool()
def unifi_classic_list_sites() -> Any:
    """List sites visible to the configured Classic-API admin.

    Endpoint: GET /self/sites. Diagnostic only — this server is pinned to
    UNIFI_SITE_NAME for every other Classic tool.
    """
    return _classic_get("/self/sites")


@mcp.tool()
def unifi_classic_health() -> Any:
    """Return per-subsystem health for the pinned site.

    Endpoint: GET /s/{site}/stat/health.
    """
    return _classic_get(_site_path("/stat/health"))


@mcp.tool()
def unifi_classic_sysinfo() -> Any:
    """Return controller system info for the pinned site.

    Endpoint: GET /s/{site}/stat/sysinfo.
    """
    return _classic_get(_site_path("/stat/sysinfo"))


@mcp.tool()
def unifi_classic_list_admins() -> Any:
    """List all admin accounts on this controller.

    Endpoint: GET /stat/admin (not site-scoped).
    """
    return _classic_get("/stat/admin")


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


@mcp.tool()
def unifi_classic_list_devices_basic() -> Any:
    """List devices with the lightweight basic payload.

    Endpoint: GET /s/{site}/stat/device-basic. Prefer this for inventory
    listings — the full /stat/device payload is up to 100x larger.
    """
    data = _classic_get(_site_path("/stat/device-basic"))
    return _bounded(
        data,
        hint="Large sites: filter client-side by type/state or call unifi_classic_get_device(mac).",
    )


@mcp.tool()
def unifi_classic_list_devices(verbose: bool = False) -> Any:
    """List devices with the full controller payload.

    Endpoint: GET /s/{site}/stat/device. Narrowed by default; pass
    ``verbose=True`` for full device records. Payloads are large — use
    unifi_classic_list_devices_basic when possible.
    """
    data = _classic_get(_site_path("/stat/device"))
    data = _classic_project_envelope(data, _CLASSIC_DEVICE_KEEP, verbose)
    return _bounded(
        data,
        hint="Use unifi_classic_list_devices_basic for inventory, or set verbose=False (default).",
    )


@mcp.tool()
def unifi_classic_get_device(mac: str) -> Any:
    """Return details of one device by MAC address.

    Endpoint: GET /s/{site}/stat/device/{mac}. MAC should be lowercase
    without separators (e.g. ``aabbccddeeff``) — this tool normalizes.
    """
    normalized = mac.lower().replace(":", "").replace("-", "")
    return _classic_get(_site_path(f"/stat/device/{normalized}"))


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


@mcp.tool()
def unifi_classic_list_active_clients(verbose: bool = False) -> Any:
    """List currently connected clients.

    Endpoint: GET /s/{site}/stat/sta. Narrowed by default; pass
    ``verbose=True`` for full client records.
    """
    data = _classic_get(_site_path("/stat/sta"))
    data = _classic_project_envelope(data, _CLASSIC_CLIENT_KEEP, verbose)
    return _bounded(
        data, hint="Busy sites: set verbose=False (default) or filter client-side."
    )


@mcp.tool()
def unifi_classic_list_all_clients(
    within: int = 24, verbose: bool = False
) -> Any:
    """List clients seen in the past ``within`` hours (including offline).

    Endpoint: GET /s/{site}/stat/alluser. Default 24 hours.
    """
    data = _classic_get(
        _site_path("/stat/alluser"), params={"within": int(within)}
    )
    data = _classic_project_envelope(data, _CLASSIC_CLIENT_KEEP, verbose)
    return _bounded(
        data,
        hint="Shorten `within`; this endpoint returns one row per historical client.",
    )


@mcp.tool()
def unifi_classic_get_client(mac: str) -> Any:
    """Return details of one client by MAC address.

    Endpoint: GET /s/{site}/stat/user/{mac}.
    """
    normalized = mac.lower().replace(":", "").replace("-", "")
    return _classic_get(_site_path(f"/stat/user/{normalized}"))


@mcp.tool()
def unifi_classic_list_known_clients() -> Any:
    """List known / configured clients (named entries).

    Endpoint: GET /s/{site}/rest/user.
    """
    data = _classic_get(_site_path("/rest/user"))
    return _bounded(data)


# ---------------------------------------------------------------------------
# Networks, WLANs, VLAN config
# ---------------------------------------------------------------------------


@mcp.tool()
def unifi_classic_list_networks() -> Any:
    """List network / VLAN configurations.

    Endpoint: GET /s/{site}/rest/networkconf.
    """
    return _classic_get(_site_path("/rest/networkconf"))


@mcp.tool()
def unifi_classic_list_wlans() -> Any:
    """List WLAN (SSID) configurations.

    Endpoint: GET /s/{site}/rest/wlanconf.
    """
    return _classic_get(_site_path("/rest/wlanconf"))


@mcp.tool()
def unifi_classic_list_wlan_groups() -> Any:
    """List WLAN groups.

    Endpoint: GET /s/{site}/rest/wlangroup.
    """
    return _classic_get(_site_path("/rest/wlangroup"))


# ---------------------------------------------------------------------------
# Firewall, port forwards, routing
# ---------------------------------------------------------------------------


@mcp.tool()
def unifi_classic_list_firewall_rules() -> Any:
    """List firewall rules (legacy ruleset).

    Endpoint: GET /s/{site}/rest/firewallrule.
    """
    return _classic_get(_site_path("/rest/firewallrule"))


@mcp.tool()
def unifi_classic_list_firewall_groups() -> Any:
    """List firewall groups.

    Endpoint: GET /s/{site}/rest/firewallgroup.
    """
    return _classic_get(_site_path("/rest/firewallgroup"))


@mcp.tool()
def unifi_classic_get_ips_settings() -> Any:
    """Return IDS / IPS settings.

    Endpoint: GET /s/{site}/rest/setting/ips.
    """
    return _classic_get(_site_path("/rest/setting/ips"))


@mcp.tool()
def unifi_classic_list_port_forwards() -> Any:
    """List port-forwarding rules on the gateway.

    Endpoint: GET /s/{site}/rest/portforward.
    """
    return _classic_get(_site_path("/rest/portforward"))


@mcp.tool()
def unifi_classic_list_port_profiles() -> Any:
    """List switch port profiles.

    Endpoint: GET /s/{site}/rest/portconf.
    """
    return _classic_get(_site_path("/rest/portconf"))


@mcp.tool()
def unifi_classic_list_traffic_rules() -> Any:
    """List traffic rules (L7 / bandwidth controls).

    Endpoint: GET /s/{site}/rest/trafficrule.
    """
    return _classic_get(_site_path("/rest/trafficrule"))


@mcp.tool()
def unifi_classic_list_traffic_routes() -> Any:
    """List traffic routes (policy routing).

    Endpoint: GET /s/{site}/rest/trafficroute.
    """
    return _classic_get(_site_path("/rest/trafficroute"))


@mcp.tool()
def unifi_classic_list_static_routes() -> Any:
    """List static routes configured on the gateway.

    Endpoint: GET /s/{site}/rest/routing.
    """
    return _classic_get(_site_path("/rest/routing"))


@mcp.tool()
def unifi_classic_list_dynamic_dns() -> Any:
    """List Dynamic DNS configurations.

    Endpoint: GET /s/{site}/rest/dynamicdns.
    """
    return _classic_get(_site_path("/rest/dynamicdns"))


# ---------------------------------------------------------------------------
# Events & alarms
# ---------------------------------------------------------------------------


@mcp.tool()
def unifi_classic_list_events(
    within: int = 1, per_page: int = EVENT_PER_PAGE_DEFAULT
) -> Any:
    """List recent events in the past ``within`` hours.

    Endpoint: GET /s/{site}/stat/event. Defaults: last 1 hour, 25 rows.
    The controller caps events at 3000; page with ``_start`` if needed by
    calling this tool with a narrower window. ``per_page`` is clamped to
    PER_PAGE_CAP.
    """
    per_page = _clamp_per_page(per_page)
    data = _classic_get(
        _site_path("/stat/event"),
        params={"within": int(within), "_limit": per_page},
    )
    data = _classic_project_envelope(data, _CLASSIC_EVENT_KEEP, verbose=False)
    return _bounded(
        data, hint="Shorten `within` or lower per_page; events accumulate fast."
    )


@mcp.tool()
def unifi_classic_list_alarms(
    archived: bool = False, per_page: int = EVENT_PER_PAGE_DEFAULT
) -> Any:
    """List alarms (active or archived).

    Endpoint: GET /s/{site}/stat/alarm. ``per_page`` is clamped server-side.
    """
    per_page = _clamp_per_page(per_page)
    params: dict[str, Any] = {
        "archived": "true" if archived else "false",
        "_limit": per_page,
    }
    data = _classic_get(_site_path("/stat/alarm"), params=params)
    data = _classic_project_envelope(data, _CLASSIC_ALARM_KEEP, verbose=False)
    return _bounded(data, hint="Lower per_page or set archived=False.")


@mcp.tool()
def unifi_classic_list_rogue_aps(within: int = 24) -> Any:
    """List detected rogue / neighbouring APs.

    Endpoint: GET /s/{site}/stat/rogueap. Default window 24 hours.
    """
    data = _classic_get(
        _site_path("/stat/rogueap"), params={"within": int(within)}
    )
    return _bounded(data, hint="Shorten `within`; dense RF areas return many rows.")


@mcp.tool()
def unifi_classic_list_known_rogues() -> Any:
    """List known / acknowledged rogue APs.

    Endpoint: GET /s/{site}/rest/rogueknown.
    """
    return _classic_get(_site_path("/rest/rogueknown"))


# ---------------------------------------------------------------------------
# Statistics & monitoring
# ---------------------------------------------------------------------------


@mcp.tool()
def unifi_classic_dpi_aggregate() -> Any:
    """Return aggregate DPI (application-usage) statistics for the site.

    Endpoint: GET /s/{site}/stat/dpi.
    """
    data = _classic_get(_site_path("/stat/dpi"))
    return _bounded(data, hint="Filter client-side by category or app.")


@mcp.tool()
def unifi_classic_dpi_per_client() -> Any:
    """Return per-client DPI breakdown.

    Endpoint: GET /s/{site}/stat/stadpi. Large on busy sites.
    """
    data = _classic_get(_site_path("/stat/stadpi"))
    return _bounded(data, hint="Large payload — consider filtering by client MAC client-side.")


@mcp.tool()
def unifi_classic_gateway_stats() -> Any:
    """Return live gateway statistics.

    Endpoint: GET /s/{site}/stat/gateway.
    """
    return _classic_get(_site_path("/stat/gateway"))


@mcp.tool()
def unifi_classic_list_sessions(
    start: int, end: int, type: str = "all"
) -> Any:
    """Return client sessions between two epoch timestamps (seconds).

    Endpoint: GET /s/{site}/stat/session. ``type`` is one of all / guest /
    user. Sessions are dense — query a narrow window.
    """
    data = _classic_get(
        _site_path("/stat/session"),
        params={"start": int(start), "end": int(end), "type": type},
    )
    return _bounded(data, hint="Narrow the start/end window.")


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@mcp.tool()
def unifi_classic_report_daily_site(
    start: int | None = None, end: int | None = None
) -> Any:
    """Return the daily site report.

    Endpoint: GET /s/{site}/stat/report/daily.site. Optional ``start`` /
    ``end`` are epoch milliseconds.
    """
    params: dict[str, Any] = {}
    if start is not None:
        params["start"] = int(start)
    if end is not None:
        params["end"] = int(end)
    data = _classic_get(
        _site_path("/stat/report/daily.site"), params=params or None
    )
    return _bounded(data)


@mcp.tool()
def unifi_classic_report_hourly_site(
    start: int | None = None, end: int | None = None
) -> Any:
    """Return the hourly site report.

    Endpoint: GET /s/{site}/stat/report/hourly.site.
    """
    params: dict[str, Any] = {}
    if start is not None:
        params["start"] = int(start)
    if end is not None:
        params["end"] = int(end)
    data = _classic_get(
        _site_path("/stat/report/hourly.site"), params=params or None
    )
    return _bounded(data, hint="Narrow the start/end window.")


@mcp.tool()
def unifi_classic_report_5min_site(
    start: int | None = None, end: int | None = None
) -> Any:
    """Return the 5-minute site report.

    Endpoint: GET /s/{site}/stat/report/5minutes.site. Returns one sample
    per 5 minutes — keep the start/end window short.
    """
    params: dict[str, Any] = {}
    if start is not None:
        params["start"] = int(start)
    if end is not None:
        params["end"] = int(end)
    data = _classic_get(
        _site_path("/stat/report/5minutes.site"), params=params or None
    )
    return _bounded(
        data, hint="Narrow the start/end window; samples are per 5 minutes."
    )


# ---------------------------------------------------------------------------
# Hotspot
# ---------------------------------------------------------------------------


@mcp.tool()
def unifi_classic_list_vouchers() -> Any:
    """List hotspot vouchers (Classic surface).

    Endpoint: GET /s/{site}/stat/voucher.
    """
    data = _classic_get(_site_path("/stat/voucher"))
    return _bounded(data)


@mcp.tool()
def unifi_classic_list_payments() -> Any:
    """List hotspot payment records.

    Endpoint: GET /s/{site}/stat/payment.
    """
    data = _classic_get(_site_path("/stat/payment"))
    return _bounded(data)


@mcp.tool()
def unifi_classic_list_hotspot_operators() -> Any:
    """List configured hotspot operators.

    Endpoint: GET /s/{site}/rest/hotspotop.
    """
    return _classic_get(_site_path("/rest/hotspotop"))


# ===========================================================================
# EXPLICIT WRITE REFUSAL TOOL
# ===========================================================================


@mcp.tool()
def attempt_write_operation(
    method: str = "POST", path: str = "/example"
) -> str:
    """Refuses any write attempt. This server is READ-ONLY by design.

    Use this tool to verify the server's read-only stance; it always
    returns the standard refusal string and performs no network I/O.
    """
    return f"{READ_ONLY_REFUSAL} (attempted {method.upper()} {path})"


# ===========================================================================
# INFO RESOURCE (server self-description)
# ===========================================================================
# The single MCP resource this server exposes. MCP clients (Claude Desktop)
# surface resources as attachable info cards, giving an at-a-glance view of
# the instance configuration. Built only from env-derived values captured at
# startup — performs NO network I/O and NEVER echoes a credential. Presence
# of optional credentials is reported as yes/no only.


@mcp.resource("unifi://info")
def unifi_info() -> str:
    """Configuration summary of this server instance — no secrets, no I/O."""
    classic_configured = (
        "yes" if (UNIFI_CLASSIC_USERNAME and UNIFI_CLASSIC_PASSWORD) else "no"
    )
    return (
        f"Host: {UNIFI_HOST}\n"
        f"Port: {UNIFI_PORT}\n"
        f"Integration API key configured: yes\n"
        f"Integration site ID: {UNIFI_SITE_ID}\n"
        f"Classic site name: {UNIFI_SITE_NAME}\n"
        f"Classic API credentials configured: {classic_configured}\n"
        f"TLS verification: {'on' if UNIFI_VERIFY_TLS else 'off'}\n"
        f"Response byte cap: {MAX_RESPONSE_BYTES}\n"
        f"Read-only: enforced (GET only)"
    )


# ---------------------------------------------------------------------------
# Signal handling & entrypoint
# ---------------------------------------------------------------------------


def _handle_sigterm(_sig: int, _frame: Any) -> None:
    sys.exit(0)


signal.signal(signal.SIGTERM, _handle_sigterm)


if __name__ == "__main__":
    mcp.run()
