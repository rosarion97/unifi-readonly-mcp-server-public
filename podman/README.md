# Ubiquiti UniFi Read-Only MCP Server (Podman)

A production-grade, **read-only** Model Context Protocol (MCP) server for a
single Ubiquiti UniFi OS console. Packaged as a Podman container and
designed to be launched directly by an MCP client (Claude Desktop or Claude
Code) over the **stdio** transport.

---

## 1. Overview

This server exposes a curated set of `GET`-only tools that wrap **two API
surfaces on the same console**:

* **Network Integration API** (official, `X-API-KEY`) — modern UUID-keyed
  endpoints under `/proxy/network/integration/v1/...`. Tools prefixed
  `unifi_int_*`.
* **Classic / Internal Controller API** (cookie session) — broader legacy
  coverage under `/proxy/network/api/...`. Tools prefixed
  `unifi_classic_*`.

It is intentionally constrained to be safe in production environments: no
tool will ever issue a `POST`, `PUT`, `PATCH`, or `DELETE` against the
console.

**Coverage**

| Area | What you can read |
|------|-------------------|
| Application & sites | Network version/capabilities, local sites |
| Devices | Adopted devices + latest statistics, pending devices, classic full/basic inventory, per-MAC device detail |
| Clients | Active, historical (`within` window), known/named, per-client detail (UUID or MAC) |
| Networks / VLANs | Integration networks + references, classic `networkconf` |
| WiFi (SSIDs) | Integration WiFi broadcasts, classic `wlanconf` and WLAN groups |
| Firewall | Integration zones / policies / ordering; classic legacy rules & groups |
| Access Control | Integration ACL rules + ordering |
| DNS | Integration DNS policies (A/AAAA/CNAME/MX/TXT/SRV/FORWARD_DOMAIN) |
| Traffic matching | Integration PORTS / IPV4_ADDRESSES / IPV6_ADDRESSES lists |
| Switching | Integration switch stacks, MC-LAG domains, LAGs |
| Routing / NAT | Classic static routes, port forwards, traffic routes |
| VPN / WAN | Integration WANs, site-to-site VPN tunnels, VPN servers |
| RADIUS / 802.1X | Integration RADIUS profiles |
| Events & alarms | Classic event log, alarms (active + archived) |
| Wireless intelligence | Classic rogue APs (detected + acknowledged) |
| DPI / Traffic intel | Integration DPI catalogs (categories + applications), classic site + per-client DPI |
| Reports | Classic daily / hourly / 5-minute site reports, gateway stats, sessions |
| Hotspot | Integration vouchers, classic vouchers + payments + operators |
| Reference data | Integration ISO countries, device tags |

**Why read-only?**

* Safe to expose to LLM agents — accidental writes are physically
  impossible because the corresponding HTTP verbs are never used.
* Predictable blast radius for shared / production environments.
* Encourages using a dedicated read-only local UniFi admin for the API key.

---

## 2. Prerequisites

* **Podman 4.x or newer** installed and on `$PATH`.
  Verify with `podman --version`.
* **A UniFi OS console**: UDM Pro, UDM SE, UDR, UCG Ultra, UDW, or UniFi OS
  Server (software install on Linux/macOS). UniFi Network application
  **≥ 9.0** is required for the Integration API; 9.3+ is recommended for
  the richer schema.
* (Strongly recommended) **A dedicated local Limited Admin (Read-Only)
  account** on the console. In the UniFi Network UI:
  1. *Settings → Control Plane → Admins & Users → Add Admin*
  2. Choose **Restrict to Local Access Only** (never use your UI.com cloud
     account — UI.com has enforced MFA since July 2024, which breaks
     non-interactive logins with HTTP 499).
  3. Role: **Limited Admin → Read-Only**.
  4. Save.
* **A Network Integration API key** generated *as that local admin*.
  *Settings → Control Plane → Integrations → Create API Key*. The key
  inherits the parent admin's permissions, so a read-only admin produces a
  read-only key — defense in depth on top of the codebase guarantee. The
  key is shown only once at creation; store it immediately.

> The Classic-API tools additionally need the same admin's username and
> password (`UNIFI_CLASSIC_USERNAME` / `UNIFI_CLASSIC_PASSWORD`). If you
> only need Integration coverage, leave them blank — Classic tools will
> raise a clear `ValueError` on first call, but the server still starts.

---

## 3. Build the container

```bash
git clone <your-fork-or-repo-url>
cd unifi-readonly-mcp-server
podman build -t unifi-readonly-mcp:latest podman/
```

The build uses `python:3.12-slim`, installs `mcp[cli]`, `requests`, and
`urllib3`, copies in `server.py`, and switches to the unprivileged user
`unifi-mcp` (uid 1001).

---

## 4. Configure credentials

This server requires **four** values at minimum; the Classic-API credentials
are optional. Required values are validated at startup, and the server
refuses to launch if any is missing.

| Variable | Required | Purpose | Where to get it |
|----------|----------|---------|-----------------|
| `UNIFI_HOST` | **yes** | Console host or IP (no scheme). | LAN IP of your UniFi OS device. |
| `UNIFI_API_KEY` | **yes** | Integration-API bearer key. | *Settings → Control Plane → Integrations → Create API Key*, as the read-only local admin. |
| `UNIFI_SITE_ID` | **yes** | Integration-API site UUID. Pins this instance to one site. | Call `unifi_int_list_sites` once with a placeholder, copy the UUID, set it here. |
| `UNIFI_PORT` | no (default `443`) | TLS port. | `443` on UDM/UCG/UDR/UDW; `11443` on UniFi OS Server. |
| `UNIFI_SITE_NAME` | no (default `default`) | Classic-API short site name (distinct from the UUID). | Usually `default`; multi-site controllers expose other short names. |
| `UNIFI_CLASSIC_USERNAME` | no | Classic-API username. Required only for `unifi_classic_*` tools. | The local Limited Admin from §2. |
| `UNIFI_CLASSIC_PASSWORD` | no | Classic-API password. | Same admin. |
| `UNIFI_VERIFY_TLS` | no (default `false`) | Verify the console's TLS certificate. | `false` for stock self-signed; `true` once you install a real cert. |
| `UNIFI_MAX_RESPONSE_BYTES` | no (default `120000`) | Hard cap on the JSON size of any one tool response. Over-cap responses become a truncation envelope with a `_hint`. | Tune for your model's context window. See [§10b](#10b-response-size--context-window-safety). |

The server reads all credentials as **plain environment variables only**.
There is no secret-file mode — a secret must be *injected into the
environment*, not mounted as a file at `/run/secrets`. Both options below
do exactly that. Pick **one**.

### Option 1 — podman secret (preferred)

Store each value once in Podman's encrypted secret store; nothing sensitive
sits in a plaintext file in your project, and it never appears in
`podman inspect` output.

```bash
printf '%s' 'YOUR_INTEGRATION_API_KEY' | podman secret create unifi_api_key -
printf '%s' 'YOUR_SITE_UUID'           | podman secret create unifi_site_id  -

# Optional — only if you want the Classic-API tools:
printf '%s' 'YOUR_LOCAL_ADMIN_USER'    | podman secret create unifi_classic_user -
printf '%s' 'YOUR_LOCAL_ADMIN_PASS'    | podman secret create unifi_classic_pass -

podman secret ls   # confirm (values are not shown)
```

Then inject each secret into the env var the server reads with
`--secret …,type=env`. Non-secret config (host, port, site name, TLS) goes
on plain `-e` flags:

```bash
podman run --rm -i \
  -e UNIFI_HOST=192.168.1.1 \
  -e UNIFI_PORT=443 \
  -e UNIFI_SITE_NAME=default \
  -e UNIFI_VERIFY_TLS=false \
  --secret unifi_api_key,type=env,target=UNIFI_API_KEY \
  --secret unifi_site_id,type=env,target=UNIFI_SITE_ID \
  --secret unifi_classic_user,type=env,target=UNIFI_CLASSIC_USERNAME \
  --secret unifi_classic_pass,type=env,target=UNIFI_CLASSIC_PASSWORD \
  unifi-readonly-mcp:latest
```

> Use `type=env`, not the default `type=mount`. The server reads env vars,
> not files under `/run/secrets`.

### Option 2 — `.env` file (fallback)

```bash
cp podman/.env.example podman/.env
# Edit podman/.env and fill in UNIFI_HOST, UNIFI_API_KEY, UNIFI_SITE_ID.
# Optionally also UNIFI_CLASSIC_USERNAME / UNIFI_CLASSIC_PASSWORD.
chmod 600 podman/.env
```

`.env` is gitignored and excluded from the image by `.containerignore` — it
is mounted at runtime with `--env-file`, never baked into the image. Do not
commit it.

**Why pin the site statically?**

* The model cannot accidentally enumerate or query an unrelated site that
  the admin can also see.
* Site-scoped tools take no `site_id` / `site_name` parameter at all —
  there is no surface for the model to get it wrong.
* One container = one site. Run a second container with a different
  `.env` if you need to serve a second site.

---

## 5. Test the container manually

Using `.env` (Option 2):

```bash
podman run --rm -i --env-file podman/.env unifi-readonly-mcp:latest
```

The container starts and silently waits on stdin — that is correct
behaviour for an MCP stdio server. Press **Ctrl-C** to exit.

If any required value is missing, the server fails fast with a clear error
on stderr and exits with status 1.

---

## 6. Claude Desktop configuration

Edit your Claude Desktop config file:

* **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
* **Linux:** `~/.config/Claude/claude_desktop_config.json`
* **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

### Using a `.env` file (Option 2)

```json
{
  "mcpServers": {
    "unifi": {
      "command": "podman",
      "args": [
        "run", "--rm", "-i",
        "--env-file", "/absolute/path/to/unifi-readonly-mcp-server/podman/.env",
        "unifi-readonly-mcp:latest"
      ]
    }
  }
}
```

### Using podman secrets (Option 1)

Swap `--env-file` for `--secret` flags; non-secret config stays on `-e`:

```json
{
  "mcpServers": {
    "unifi": {
      "command": "podman",
      "args": [
        "run", "--rm", "-i",
        "-e", "UNIFI_HOST=192.168.1.1",
        "-e", "UNIFI_PORT=443",
        "-e", "UNIFI_SITE_NAME=default",
        "--secret", "unifi_api_key,type=env,target=UNIFI_API_KEY",
        "--secret", "unifi_site_id,type=env,target=UNIFI_SITE_ID",
        "--secret", "unifi_classic_user,type=env,target=UNIFI_CLASSIC_USERNAME",
        "--secret", "unifi_classic_pass,type=env,target=UNIFI_CLASSIC_PASSWORD",
        "unifi-readonly-mcp:latest"
      ]
    }
  }
}
```

Notes:

* `-i` (`--interactive`) is **required** — MCP stdio needs stdin attached.
* `--rm` cleans up the container after each session.
* The path passed to `--env-file` **must be absolute**. Claude Desktop does
  not expand `~` or run commands from your shell's working directory.

Restart Claude Desktop after editing the config. The `unifi` server should
appear in the MCP tools list.

---

## 7. Claude Code configuration

Claude Code uses the same `command` / `args` schema as §6, just in a
different file. Three scopes:

| Scope | File | Sharing |
|---|---|---|
| **local** (default) | `~/.claude.json`, under this project's entry | just you, just this project |
| **project** | `.mcp.json` at the project root | shared via git with collaborators |
| **user** (global) | `~/.claude.json`, top level | just you, every project |

**Easiest path — let the CLI write it for you.** Pick the scope and option
that matches §4:

Option 1 (Podman secret store, preferred):

```bash
claude mcp add -s user unifi -- \
  podman run --rm -i \
  -e UNIFI_HOST=192.168.1.1 -e UNIFI_PORT=443 -e UNIFI_SITE_NAME=default \
  --secret unifi_api_key,type=env,target=UNIFI_API_KEY \
  --secret unifi_site_id,type=env,target=UNIFI_SITE_ID \
  --secret unifi_classic_user,type=env,target=UNIFI_CLASSIC_USERNAME \
  --secret unifi_classic_pass,type=env,target=UNIFI_CLASSIC_PASSWORD \
  unifi-readonly-mcp:latest
```

Option 2 (plaintext `.env` file):

```bash
claude mcp add -s user unifi -- \
  podman run --rm -i \
  --env-file /absolute/path/to/podman/.env \
  unifi-readonly-mcp:latest
```

Use `-s user` for global, `-s project` to commit the entry to `.mcp.json`
for collaborators, or omit `-s` for the default local scope. Verify with
`claude mcp list`.

---

## 7b. Codex configuration

OpenAI Codex reads MCP server config from a TOML file instead of JSON. Two
scopes:

| Scope | File | Trust requirement |
|---|---|---|
| **global** | `~/.codex/config.toml` | none |
| **project** | `.codex/config.toml` at the project root | Codex only loads project files for **trusted** projects — confirm trust in Codex before relying on this scope |

The translation from the §6 JSON is mechanical: `mcpServers.foo` →
`[mcp_servers.foo]`; same `command`, same `args`.

Option 1 (Podman secret store):

```toml
[mcp_servers.unifi]
command = "podman"
args = [
  "run", "--rm", "-i",
  "-e", "UNIFI_HOST=192.168.1.1",
  "-e", "UNIFI_PORT=443",
  "-e", "UNIFI_SITE_NAME=default",
  "--secret", "unifi_api_key,type=env,target=UNIFI_API_KEY",
  "--secret", "unifi_site_id,type=env,target=UNIFI_SITE_ID",
  "--secret", "unifi_classic_user,type=env,target=UNIFI_CLASSIC_USERNAME",
  "--secret", "unifi_classic_pass,type=env,target=UNIFI_CLASSIC_PASSWORD",
  "unifi-readonly-mcp:latest",
]
```

Option 2 (`.env` file):

```toml
[mcp_servers.unifi]
command = "podman"
args = [
  "run", "--rm", "-i",
  "--env-file", "/absolute/path/to/podman/.env",
  "unifi-readonly-mcp:latest",
]
```

Restart Codex or open a new project thread so the MCP server loads.

---

## 8. Available Tools

> **About `verbose`, `limit`, and response size.** List tools with a
> `verbose` flag return a narrowed set of fields by default to keep
> responses small for the model's context window. Pass `verbose=True` to
> get the full record. `limit` / `per_page` is clamped server-side to
> `PER_PAGE_CAP` (default 200, matching the Integration API's own cap).
> Every response is additionally capped at `UNIFI_MAX_RESPONSE_BYTES` —
> over the cap, the server returns a truncation envelope with a `_hint`.
> See [§10b](#10b-response-size--context-window-safety).

### Network Integration API (`unifi_int_*`)

> All site-scoped Integration tools are pinned to `UNIFI_SITE_ID` from the
> `.env` file and take **no** `site_id` parameter.

#### Application & sites

| Tool | Parameters | Description |
|------|------------|-------------|
| `unifi_int_get_info` | — | Application version & capabilities. Call this first. |
| `unifi_int_list_sites` | — | Diagnostic: lists every site the API key can see. |

#### Devices

| Tool | Parameters | Description |
|------|------------|-------------|
| `unifi_int_list_devices` | `offset=0`, `limit=50`, `verbose=False` | Adopted devices in the pinned site. |
| `unifi_int_get_device` | `device_id` | Single device (UUID). |
| `unifi_int_get_device_statistics_latest` | `device_id` | Latest statistics snapshot. |
| `unifi_int_list_pending_devices` | `offset=0`, `limit=50` | Devices pending adoption (not site-scoped). |

#### Clients

| Tool | Parameters | Description |
|------|------------|-------------|
| `unifi_int_list_clients` | `offset=0`, `limit=50`, `verbose=False`, `filter=None` | Currently connected clients. `filter` accepts the Integration filter syntax, e.g. `type.eq('WIRELESS')`. |
| `unifi_int_get_client` | `client_id` | Single client (UUID). |

#### Networks (VLANs)

| Tool | Parameters | Description |
|------|------------|-------------|
| `unifi_int_list_networks` | `offset=0`, `limit=50`, `verbose=False` | Configured networks. |
| `unifi_int_get_network` | `network_id` | Single network. |
| `unifi_int_get_network_references` | `network_id` | Objects referencing the network (WLANs, rules, etc.). |

#### WiFi broadcasts (SSIDs)

| Tool | Parameters | Description |
|------|------------|-------------|
| `unifi_int_list_wifi_broadcasts` | `offset=0`, `limit=50`, `verbose=False` | All SSIDs. |
| `unifi_int_get_wifi_broadcast` | `wifi_broadcast_id` | Single SSID configuration. |

#### Firewall

| Tool | Parameters | Description |
|------|------------|-------------|
| `unifi_int_list_firewall_zones` | `offset=0`, `limit=50` | Firewall zones. |
| `unifi_int_get_firewall_zone` | `firewall_zone_id` | Single zone. |
| `unifi_int_list_firewall_policies` | `offset=0`, `limit=50` | Firewall policies. |
| `unifi_int_get_firewall_policy` | `firewall_policy_id` | Single policy. |
| `unifi_int_get_firewall_policy_ordering` | `source_firewall_zone_id`, `destination_firewall_zone_id` | User-defined ordering between two zones. |

#### Access Control (ACL Rules)

| Tool | Parameters | Description |
|------|------------|-------------|
| `unifi_int_list_acl_rules` | `offset=0`, `limit=50` | IPV4 / MAC ACL rules. |
| `unifi_int_get_acl_rule` | `acl_rule_id` | Single ACL rule. |
| `unifi_int_get_acl_rule_ordering` | — | User-defined ACL rule ordering. |

#### Switching

| Tool | Parameters | Description |
|------|------------|-------------|
| `unifi_int_list_switch_stacks` | `offset=0`, `limit=50` | Switch stacks. |
| `unifi_int_get_switch_stack` | `switch_stack_id` | Single switch stack. |
| `unifi_int_list_mc_lag_domains` | `offset=0`, `limit=50` | MC-LAG domains. |
| `unifi_int_get_mc_lag_domain` | `mc_lag_domain_id` | Single MC-LAG domain. |
| `unifi_int_list_lags` | `offset=0`, `limit=50` | LAGs (LOCAL / SWITCH_STACK / MULTI_CHASSIS). |
| `unifi_int_get_lag` | `lag_id` | Single LAG. |

#### DNS, traffic, supporting resources

| Tool | Parameters | Description |
|------|------------|-------------|
| `unifi_int_list_dns_policies` | `offset=0`, `limit=50` | DNS policies (A / AAAA / CNAME / MX / TXT / SRV / FORWARD_DOMAIN). |
| `unifi_int_get_dns_policy` | `dns_policy_id` | Single DNS policy. |
| `unifi_int_list_traffic_matching_lists` | `offset=0`, `limit=50` | PORTS / IPV4 / IPV6 matching lists. |
| `unifi_int_get_traffic_matching_list` | `traffic_matching_list_id` | Single list. |
| `unifi_int_list_wans` | `offset=0`, `limit=50` | WAN interfaces. |
| `unifi_int_list_vpn_site_to_site_tunnels` | `offset=0`, `limit=50` | Site-to-site VPN tunnels. |
| `unifi_int_list_vpn_servers` | `offset=0`, `limit=50` | VPN servers. |
| `unifi_int_list_radius_profiles` | `offset=0`, `limit=50` | RADIUS profiles. |
| `unifi_int_list_device_tags` | `offset=0`, `limit=50` | Device tags. |
| `unifi_int_list_hotspot_vouchers` | `offset=0`, `limit=100`, `verbose=False` | Hotspot vouchers. |
| `unifi_int_get_hotspot_voucher` | `voucher_id` | Single voucher. |
| `unifi_int_list_dpi_categories` | `offset=0`, `limit=50` | Global DPI category catalog. |
| `unifi_int_list_dpi_applications` | `offset=0`, `limit=50` | Global DPI application catalog. |
| `unifi_int_list_countries` | `offset=0`, `limit=50` | ISO countries. |

### Classic Controller API (`unifi_classic_*`)

> All site-scoped Classic tools are pinned to `UNIFI_SITE_NAME` (default
> `default`). Require `UNIFI_CLASSIC_USERNAME` / `UNIFI_CLASSIC_PASSWORD`.

#### Site & system

| Tool | Parameters | Description |
|------|------------|-------------|
| `unifi_classic_list_sites` | — | Diagnostic: sites visible to the configured admin. |
| `unifi_classic_health` | — | Per-subsystem health for the pinned site. |
| `unifi_classic_sysinfo` | — | Controller system info. |
| `unifi_classic_list_admins` | — | All admin accounts on the controller (not site-scoped). |

#### Devices

| Tool | Parameters | Description |
|------|------------|-------------|
| `unifi_classic_list_devices_basic` | — | Lightweight inventory (prefer this for listings). |
| `unifi_classic_list_devices` | `verbose=False` | Full device payload. |
| `unifi_classic_get_device` | `mac` | Device by MAC (normalized to lowercase, no separators). |

#### Clients

| Tool | Parameters | Description |
|------|------------|-------------|
| `unifi_classic_list_active_clients` | `verbose=False` | Currently connected clients. |
| `unifi_classic_list_all_clients` | `within=24`, `verbose=False` | Clients seen in the past N hours (includes offline). |
| `unifi_classic_get_client` | `mac` | Client by MAC. |
| `unifi_classic_list_known_clients` | — | Known / named (configured) clients. |

#### Networks, WLANs, firewall

| Tool | Parameters | Description |
|------|------------|-------------|
| `unifi_classic_list_networks` | — | Network / VLAN configurations. |
| `unifi_classic_list_wlans` | — | WLAN (SSID) configurations. |
| `unifi_classic_list_wlan_groups` | — | WLAN groups. |
| `unifi_classic_list_firewall_rules` | — | Legacy firewall rules. |
| `unifi_classic_list_firewall_groups` | — | Firewall groups. |
| `unifi_classic_get_ips_settings` | — | IDS / IPS settings. |
| `unifi_classic_list_port_forwards` | — | Port-forwarding rules. |
| `unifi_classic_list_port_profiles` | — | Switch port profiles. |
| `unifi_classic_list_traffic_rules` | — | L7 / bandwidth traffic rules. |
| `unifi_classic_list_traffic_routes` | — | Policy traffic routes. |
| `unifi_classic_list_static_routes` | — | Gateway static routes. |
| `unifi_classic_list_dynamic_dns` | — | Dynamic DNS configurations. |

#### Events, alarms, rogue APs

| Tool | Parameters | Description |
|------|------------|-------------|
| `unifi_classic_list_events` | `within=1`, `per_page=25` | Event log (`within` = hours). |
| `unifi_classic_list_alarms` | `archived=False`, `per_page=25` | Active or archived alarms. |
| `unifi_classic_list_rogue_aps` | `within=24` | Detected rogue / neighbouring APs. |
| `unifi_classic_list_known_rogues` | — | Acknowledged rogue APs. |

#### Stats, sessions, reports

| Tool | Parameters | Description |
|------|------------|-------------|
| `unifi_classic_dpi_aggregate` | — | Aggregate DPI statistics for the site. |
| `unifi_classic_dpi_per_client` | — | Per-client DPI breakdown. |
| `unifi_classic_gateway_stats` | — | Live gateway statistics. |
| `unifi_classic_list_sessions` | `start`, `end`, `type="all"` | Client sessions between two epochs. |
| `unifi_classic_report_daily_site` | `start=None`, `end=None` | Daily site report. |
| `unifi_classic_report_hourly_site` | `start=None`, `end=None` | Hourly site report. |
| `unifi_classic_report_5min_site` | `start=None`, `end=None` | 5-minute site report. |

#### Hotspot

| Tool | Parameters | Description |
|------|------------|-------------|
| `unifi_classic_list_vouchers` | — | Vouchers (Classic surface). |
| `unifi_classic_list_payments` | — | Payment records. |
| `unifi_classic_list_hotspot_operators` | — | Configured hotspot operators. |

### Read-only safety

| Tool | Parameters | Description |
|------|------------|-------------|
| `attempt_write_operation` | `method="POST"`, `path="/example"` | Always returns the standard refusal string. Useful for verifying the server's read-only stance. |

---

## 9. Example prompts to use with Claude

Once the server is wired in to Claude Desktop or Claude Code, try prompts
like these:

* "What UniFi Network version is running on the console?"
* "List every adopted device in the site."
* "Which clients are connected to the IoT VLAN right now?"
* "Show me the firewall policies between the LAN and Guest zones, in order."
* "What SSIDs are configured, and which networks do they sit on?"
* "Any new alarms in the last hour?"
* "List rogue APs detected in the last 24 hours."
* "What DNS policies are configured?"
* "Give me the daily site report for the last week."
* "What L7 traffic rules are configured?"
* "List port forwards on the gateway."
* "Show me the per-client DPI breakdown for the busiest user."
* "Which clients failed to connect to the WiFi today?" *(via classic events)*

Claude will pick the matching tool, hand it the required identifiers, and
summarise the JSON the API returns.

---

## 10. Rate limiting

UniFi does not publish a formal Integration-API quota, but the controller
will throttle bursts. When the API returns HTTP 429 this server surfaces:

> *"UniFi Integration API rate-limited. Retry after a moment."*

For the Classic API the controller is even less formal about quotas. If you
ask Claude to enumerate a large estate (e.g. "give me port status for every
switch and every client") expect to pace the requests or to retry once or
twice. The server does **not** auto-retry on your behalf — it returns the
error so the model and the user stay in control. The one exception is the
Classic-API session: on `api.err.LoginRequired` (stale cookie) the server
performs exactly one silent re-login before retrying.

---

## 10b. Response size & context-window safety

UniFi endpoints such as `/stat/event`, `/stat/sta`, `/stat/dpi`,
`/stat/alluser`, and the global DPI catalogs (`/dpi/applications`,
`/dpi/categories`) can return multi-megabyte JSON arrays. Handing that raw
payload to an LLM tool call will blow past the model's context window and
lead to truncation or hallucination. This server has four layers of
protection against that:

**1. Tighter default time windows.** Event endpoints default to `within=1`
(1 hour); historical-client and rogue-AP endpoints default to `within=24`.
The model can always ask for more by passing a larger value.

**2. Server-side `limit` / `per_page` cap.** Every tool that accepts a
pagination parameter is clamped to `PER_PAGE_CAP` (default 200, matching
the Integration API's own cap). The model can request less, never more.

**3. Field projection.** High-cardinality list endpoints
(`unifi_int_list_devices`, `unifi_int_list_clients`,
`unifi_int_list_networks`, `unifi_int_list_wifi_broadcasts`,
`unifi_int_list_hotspot_vouchers`, `unifi_classic_list_devices`,
`unifi_classic_list_active_clients`, `unifi_classic_list_all_clients`,
`unifi_classic_list_events`, `unifi_classic_list_alarms`) return a curated
subset of fields by default. Pass `verbose=True` to get the full record
back.

**4. A hard byte cap on every response.** `UNIFI_MAX_RESPONSE_BYTES`
(default `120000`, ~30k tokens) caps the JSON size of any single tool
response. When a payload would exceed the cap, the server returns a
truncation envelope instead:

```jsonc
{
  "_truncated": true,
  "_returned": 42,            // items included
  "_total": 1850,             // items the API actually returned
  "_bytes_cap": 120000,
  "_hint": "Shorten `within` or lower per_page; events accumulate fast.",
  "data": [ /* first 42 items */ ]
}
```

The `_hint` field is the important part: the model sees that data was cut
and gets a one-line nudge on how to re-query for what it actually needs.

**Tuning.** If you're running a big-window model and want to allow bigger
responses, raise the cap:

```
UNIFI_MAX_RESPONSE_BYTES=400000
```

If you're running a small-window model and getting "context full" errors
anyway, lower it:

```
UNIFI_MAX_RESPONSE_BYTES=60000
```

Restart the MCP client (which restarts the container) for the change to
take effect.

---

## 11. Security notes

* **Credential handling.** `UNIFI_API_KEY`, `UNIFI_CLASSIC_USERNAME`, and
  `UNIFI_CLASSIC_PASSWORD` are read once at process start from the
  container's environment. They are never written to stdout/stderr, never
  echoed back in a tool response, and never included in error strings.
  They live only inside the container's process memory.
* **Single-site pin.** `UNIFI_SITE_ID` (Integration UUID) and
  `UNIFI_SITE_NAME` (Classic short name) are read once at process start.
  Every site-scoped tool uses these values directly — the model cannot
  pass a different site even if a user asks it to. To serve a second site,
  run a second container with its own configuration.
* **Non-root runtime.** The container drops to `unifi-mcp` (uid 1001)
  before executing `server.py`.
* **No write paths.** Every tool routes through a shared `_request()`
  helper that refuses any verb other than `GET`. The only `POST` anywhere
  in the codebase is the Classic-API login, performed via a one-off
  `requests.request("POST", ...)` — not the shared `_classic_session` —
  so the read-only grep lint stays empty. A dedicated
  `attempt_write_operation` tool returns the canonical refusal string.
* **Recommended admin scope.** Generate the API key (and use the same
  credentials for the Classic surface) under a dedicated *local* Limited
  Admin with **Read-Only** role. Never use a UI.com cloud account: UI.com
  enforces MFA, which breaks non-interactive logins with HTTP 499.
* **Secret storage.** Prefer a `podman secret` injected with `type=env`
  (§4 Option 1) over `.env`: it keeps the credential out of a plaintext
  project file, out of the image, and out of `podman inspect` output. The
  server reads credentials as plain environment variables only. `.env` is
  gitignored and excluded from the build context by `.containerignore`; if
  you use it, don't commit it. If you rotate the API key, restart the MCP
  client so it relaunches the container with the new credentials.
* **TLS.** UniFi OS ships with a self-signed certificate. By default the
  server sets `verify=False` for both API surfaces and silences the
  urllib3 `InsecureRequestWarning`. For production, install a real
  certificate on the console and set `UNIFI_VERIFY_TLS=true`.

---

## 12. Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| Claude doesn't see the server | Confirm `podman` is in `$PATH` for the user running the client. Confirm the image exists: `podman images \| grep unifi-readonly-mcp`. |
| Exits with `UNIFI_HOST is not set` (or `_API_KEY`, `_SITE_ID`) | `.env` missing / wrong `--env-file` path (must be absolute), or the podman secret isn't mapped to the right `target=` env name. |
| `401 Unauthorized` from Integration tools | The API key was rejected. Re-generate under the right admin's Integrations panel. |
| `403 Forbidden` from Integration tools | Parent admin's role is too narrow. Read-Only is fine for every tool this server exposes. |
| `404 Not found` from Integration tools | Endpoint may require a newer Network application version. Call `unifi_int_get_info` and compare against the docs. |
| Classic tools raise `Classic-API tools require UNIFI_CLASSIC_USERNAME and UNIFI_CLASSIC_PASSWORD` | Expected if you didn't set them. Integration tools still work; add the variables to your `.env` or as secrets if you need Classic coverage. |
| Classic login fails with HTTP 499 | The admin has 2FA enabled (typically a UI.com cloud account). Create a new *local* Limited Admin without MFA. |
| Classic login returns 401/403 | Bad credentials, or you used a UI.com cloud account. Use a local admin. |
| Tool returns truncation envelope with `_truncated: true` | The endpoint exceeded `UNIFI_MAX_RESPONSE_BYTES`. Follow the `_hint` (shorter `within`, lower `per_page`, filter by id) or raise the cap. |
| TLS / certificate errors at startup | Self-signed cert. Either install a real cert on the console or leave `UNIFI_VERIFY_TLS=false`. |
| `meta.rc=error` with `api.err.LoginRequired` repeatedly | Session keeps expiring. Check that the Classic admin still exists and isn't being logged out by another process using the same credentials. |
| Changed `server.py`, old behaviour persists | Rebuild without cache: `podman build --no-cache -t unifi-readonly-mcp:latest podman/` and restart the client. |
| stdio framing errors | Ensure `-i` is present on `podman run`. Without it Podman closes stdin and the MCP handshake fails. |

---

## 13. Read-only invariant check

Before shipping any change to `server.py`, confirm no write verb exists on
the shared sessions:

```bash
grep -nE 'session\.(post|put|patch|delete)|_session\.(post|put|patch|delete)' server.py
# Must return no matches.
```

`podman/server.py` is kept byte-for-byte identical to `docker/server.py`.
Diff them after any edit:

```bash
diff ../docker/server.py server.py   # expect no output
```

---

## License & contributions

Provided as-is for internal use. Not affiliated with or endorsed by
Ubiquiti. PRs that add additional **read-only** UniFi endpoints (either API
surface) are welcome; any change that introduces a write-capable verb
against the console will be rejected on sight.
