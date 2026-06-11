# unifi-readonly-mcp-server

A production-grade, **read-only** Model Context Protocol (MCP) server for a
single **Ubiquiti UniFi OS** console. Designed to be launched directly by an
MCP client (Claude Desktop, Claude Code, Gemini CLI, etc.) over the **stdio**
transport.

## Scope — local APIs only

The server wraps **two API surfaces on the same UniFi OS console**:

| Surface | Auth | Base path |
|---|---|---|
| **Network Integration API** (official) | `X-API-KEY` | `/proxy/network/integration/v1/...` |
| **Classic / Internal Controller API** | cookie session (local admin) | `/proxy/network/api/...` |

The **Site Manager (cloud) API is intentionally NOT wrapped.** This server
talks only to a console you can reach on the LAN. If your console is behind
CGNAT and you need the cloud proxy, that belongs in a separate server.

## Container backends

This repository ships a containerised build for both Podman and Docker. They
share a byte-for-byte identical `server.py`; only the runtime tooling differs.

| Backend | Status | Folder |
|---|---|---|
| Podman | stable | [`podman/`](./podman) |
| Docker | stable | [`docker/`](./docker) |

See [`podman/README.md`](./podman/README.md) or
[`docker/README.md`](./docker/README.md) for full build, configuration, and
Claude Desktop / Claude Code / Codex integration steps. The Docker guide also
covers the **Docker Desktop MCP Toolkit**.

### Credentials: secret first, `.env` fallback

The server reads `UNIFI_API_KEY`, `UNIFI_SITE_ID`, and optionally
`UNIFI_CLASSIC_USERNAME` / `UNIFI_CLASSIC_PASSWORD` as **plain environment
variables only** (no secret-file mode). Both backends prefer a container
secret *injected as an env var* over a plaintext `.env`:

* **Podman:** `podman secret create` + `--secret <name>,type=env` (see
  [podman §4](./podman/README.md#4-configure-credentials)).
* **Docker:** Docker Desktop MCP Toolkit (`docker mcp secret set`), which
  injects the secret as an env var (see
  [docker §4](./docker/README.md#4-configure-credentials)).
* **Either:** a gitignored `.env` mounted with `--env-file` as the fallback.

`.env` is gitignored and excluded from both image builds (`.dockerignore` /
`.containerignore`), so credentials are never baked into an image.

## Hard read-only guarantee

Every tool maps to an HTTP `GET` against the configured UniFi console. The
shared `_request()` helper refuses any verb other than `GET`. The only
`POST` anywhere in the codebase is the Classic-API login, performed via a
one-off `requests.request("POST", ...)` (not the shared session) so the
read-only grep lint stays empty:

```bash
grep -nE 'session\.(post|put|patch|delete)|_session\.(post|put|patch|delete)' podman/server.py
# → no output
```

A dedicated `attempt_write_operation` tool returns the canonical refusal
string and makes the stance visible in tool catalogs.

See [§11 of the podman README](./podman/README.md#11-security-notes) for the
full security model.

## Coverage at a glance

**Network Integration API** (`unifi_int_*`):

* Application info & site discovery
* Adopted devices + latest statistics, pending-adoption devices
* Connected clients (with optional filter-expression support)
* Networks (VLANs) + references
* WiFi broadcasts (SSIDs)
* Firewall zones, policies, and policy ordering
* ACL rules + ordering
* DNS policies
* Traffic matching lists (PORTS / IPV4 / IPV6)
* Switching: stacks, MC-LAG domains, LAGs
* WAN interfaces, site-to-site VPN tunnels, VPN servers, RADIUS profiles
* Device tags, hotspot vouchers
* Global reference data: DPI categories, DPI applications, ISO countries

**Classic Controller API** (`unifi_classic_*`):

* Per-subsystem health, controller sysinfo, admins
* Full and basic device inventory + per-MAC device detail
* Active clients, historical clients, known/named clients
* Network / WLAN / WLAN group configuration
* Firewall rules & groups, port forwards, static routes
* Traffic rules (L7 / bandwidth), traffic routes (policy routing)
* Port profiles, dynamic DNS, IDS/IPS settings
* Events, alarms, rogue APs (detected & acknowledged)
* DPI aggregates (site + per client), gateway stats, sessions
* Daily / hourly / 5-minute site reports
* Hotspot vouchers, payments, operators

## Configuration

| Variable | Required | Purpose |
|---|---|---|
| `UNIFI_HOST` | yes | Console host or IP (no scheme). |
| `UNIFI_PORT` | no (default `443`) | `443` on UniFi OS hardware; `11443` on UniFi OS Server (software install). |
| `UNIFI_API_KEY` | yes | Integration-API key. |
| `UNIFI_SITE_ID` | yes | Integration-API site UUID. Pins this instance to one site. |
| `UNIFI_SITE_NAME` | no (default `default`) | Classic-API short site name. |
| `UNIFI_CLASSIC_USERNAME` | no | Classic-API user. Without it, only `unifi_int_*` tools work. |
| `UNIFI_CLASSIC_PASSWORD` | no | Classic-API password. |
| `UNIFI_VERIFY_TLS` | no (default `false`) | UniFi OS ships self-signed; leave off for homelab. |
| `UNIFI_MAX_RESPONSE_BYTES` | no (default `120000`) | JSON byte cap per response. |

See [`podman/.env.example`](./podman/.env.example) and the podman README for
details.

## License

Provided as-is for internal use. Not affiliated with or endorsed by
Ubiquiti. Pull requests that add additional **read-only** endpoints (either
API surface) are welcome; any change that introduces a write-capable verb
against the console will be rejected.
