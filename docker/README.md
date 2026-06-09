# Ubiquiti UniFi Read-Only MCP Server (Docker)

A production-grade, **read-only** Model Context Protocol (MCP) server for a
single Ubiquiti UniFi OS console. Packaged as a Docker container and
designed to be launched directly by an MCP client (Claude Desktop, Claude
Code, or the **Docker Desktop MCP Toolkit**) over the **stdio** transport.

This is the Docker sibling of [`../podman`](../podman). The `server.py` is
byte-for-byte identical to the Podman build; only the runtime tooling
differs.

---

## 1. Overview

The server wraps **two API surfaces on the same console**:

* **Network Integration API** (official, `X-API-KEY`) — modern UUID-keyed
  endpoints. Tools prefixed `unifi_int_*`.
* **Classic / Internal Controller API** (cookie session) — broader legacy
  coverage. Tools prefixed `unifi_classic_*`.

The **Site Manager (cloud) API is intentionally NOT wrapped.** This server
talks only to a console you can reach on the LAN.

It is intentionally constrained to be safe in production: no tool will ever
issue a `POST`, `PUT`, `PATCH`, or `DELETE` against the console.

**Coverage** — see [§8 of the Podman README](../podman/README.md#8-available-tools).
The tool surface is identical between Podman and Docker.

---

## 2. Prerequisites

* **Docker Engine 24+ / Docker Desktop 4.27+** on `$PATH`.
  Verify with `docker --version`.
* For the MCP Toolkit path (§4 Option 1): **Docker Desktop** with the
  **MCP Toolkit** enabled (Settings → Beta features → *Enable Docker MCP
  Toolkit*), which provides the `docker mcp` CLI plugin.
* **A UniFi OS console** (UDM/UCG/UDR/UDW or UniFi OS Server) running
  Network application **≥ 9.0** (9.3+ recommended).
* (Strongly recommended) **A dedicated local Limited Admin (Read-Only)**
  account on the console. *Settings → Control Plane → Admins & Users →
  Add Admin*; **Restrict to Local Access Only**; role **Limited Admin →
  Read-Only**.
  Do **not** use your UI.com cloud account: UI.com enforces MFA, which
  breaks non-interactive logins with HTTP 499.
* **A Network Integration API key** generated *as that local admin*:
  *Settings → Control Plane → Integrations → Create API Key*. Shown only
  once at creation; copy immediately.

> The Classic-API tools additionally need the same admin's username and
> password (`UNIFI_CLASSIC_USERNAME` / `UNIFI_CLASSIC_PASSWORD`). If you
> only need Integration coverage, leave them blank — Classic tools raise
> a clear `ValueError` on first call but the server still starts.

---

## 3. Build the image

```bash
cd docker
docker build -t unifi-readonly-mcp:latest .
```

The build uses `python:3.12-slim`, installs `mcp[cli]`, `requests`, and
`urllib3`, copies in `server.py`, and switches to the unprivileged user
`unifi-mcp` (uid 1001).

`.env` is excluded from the build context by [`.dockerignore`](./.dockerignore),
so credentials are **never** baked into the image — they are supplied at
runtime only.

---

## 4. Configure credentials

This server requires **four** values at minimum; the Classic-API
credentials are optional. Required values are validated at startup; the
server refuses to launch if any is missing.

| Variable | Required | Purpose | Where to get it |
|----------|----------|---------|-----------------|
| `UNIFI_HOST` | **yes** | Console host or IP (no scheme). | LAN IP of your UniFi OS device. |
| `UNIFI_API_KEY` | **yes** | Integration-API bearer key. | *Settings → Control Plane → Integrations → Create API Key*. |
| `UNIFI_SITE_ID` | **yes** | Integration-API site UUID. Pins this instance to one site. | Call `unifi_int_list_sites` once with a placeholder, copy the UUID, set it here. |
| `UNIFI_PORT` | no (default `443`) | TLS port. | `443` on UDM/UCG/UDR/UDW; `11443` on UniFi OS Server. |
| `UNIFI_SITE_NAME` | no (default `default`) | Classic-API short site name. | Usually `default`. |
| `UNIFI_CLASSIC_USERNAME` | no | Classic-API username. | The local Limited Admin from §2. |
| `UNIFI_CLASSIC_PASSWORD` | no | Classic-API password. | Same admin. |
| `UNIFI_VERIFY_TLS` | no (default `false`) | TLS cert verification. | `false` for stock self-signed; `true` once you install a real cert. |
| `UNIFI_MAX_RESPONSE_BYTES` | no (default `120000`) | JSON byte cap per tool response. | See [Podman §10b](../podman/README.md#10b-response-size--context-window-safety). |

The server reads all credentials as **plain environment variables only**.
There is no secret-file mode — a secret must be *injected into the
environment*, not mounted as a file at `/run/secrets`. Both options below
do exactly that. Pick **one**.

### Option 1 — Docker secret store via the MCP Gateway (preferred)

The Docker MCP gateway resolves secrets from Docker Desktop's encrypted
store and injects them into the server **as environment variables** at
launch — no plaintext `.env` on disk, and no GUI registration step.

**Step A — Store the secrets**

```bash
docker mcp secret set UNIFI_API_KEY
docker mcp secret set UNIFI_SITE_ID
# Optional — only if you want the unifi_classic_* tools:
docker mcp secret set UNIFI_CLASSIC_USERNAME
docker mcp secret set UNIFI_CLASSIC_PASSWORD

# List what's stored (values are masked):
docker mcp secret ls
```

Non-secret values (`UNIFI_HOST`, `UNIFI_PORT`, `UNIFI_SITE_NAME`,
`UNIFI_VERIFY_TLS`, `UNIFI_MAX_RESPONSE_BYTES`) go in the catalog entry's
env block rather than the secret store.

**Step B — Install the custom catalog**

```bash
mkdir -p ~/.docker/mcp/catalogs
cp docker/custom-catalog.yaml ~/.docker/mcp/catalogs/custom.yaml
```

Open the copied file and set the non-secret env values (`UNIFI_HOST`,
`UNIFI_PORT`, etc.) on the `unifi-readonly` entry. The shipped catalog
declares all secret bindings; you only need to fill in the runtime config.

**Step C — Enable the server in the registry**

`~/.docker/mcp/registry.yaml` lists active servers under a single top-level
`registry:` key. Add the `unifi-readonly` entry — do **not** overwrite the
file if it already exists.

```yaml
registry:
  unifi-readonly:
    catalog: custom
    enabled: true
  # ... any other servers you already had stay here
```

Connecting Claude Desktop is covered in §6 (the explicit gateway JSON
block); that block replaces the GUI "register as a custom server" step you
may have used with the Toolkit.

> Plain `docker run` has no `.env`-free secret mechanism without Swarm,
> and this server does not read file-mounted secrets (`/run/secrets`). For
> a secret-backed setup, use the gateway path above; otherwise use `.env`.

### Option 2 — `.env` file (fallback)

```bash
cd docker
cp .env.example .env
# Edit .env: UNIFI_HOST, UNIFI_API_KEY, UNIFI_SITE_ID required.
# Optionally also UNIFI_CLASSIC_USERNAME / UNIFI_CLASSIC_PASSWORD.
chmod 600 .env
```

`.env` is gitignored and excluded from the image via `.dockerignore`. It
is mounted at runtime with `--env-file`, never baked in. Use this when you
don't have Docker Desktop or just want the simplest setup.

**Why pin the site statically?** The model cannot enumerate or query an
unrelated site that the admin can also see. Site-scoped tools take no site
parameter at all. One container = one site; run a second container with
its own secret/`.env` to serve another site.

---

## 5. Test the container manually

Using `.env` (Option 2):

```bash
docker run --rm -i --env-file .env unifi-readonly-mcp:latest
```

You can also inject values inline for a quick check:

```bash
docker run --rm -i \
  -e UNIFI_HOST=192.168.1.1 \
  -e UNIFI_PORT=443 \
  -e UNIFI_API_KEY=YOUR_KEY \
  -e UNIFI_SITE_ID=YOUR_SITE_UUID \
  unifi-readonly-mcp:latest
```

The container starts and silently waits on stdin — correct behaviour for
an MCP stdio server. Press **Ctrl-C** to exit. If a required value is
missing the server fails fast on stderr and exits with status 1.

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
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "--env-file", "/absolute/path/to/unifi-readonly-mcp-server/docker/.env",
        "unifi-readonly-mcp:latest"
      ]
    }
  }
}
```

### Using the MCP Gateway (Option 1)

Replace `<your-username>` with your macOS username (run `whoami` to check):

```json
{
  "mcpServers": {
    "mcp-toolkit-gateway": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        "-v", "/Users/<your-username>/.docker/mcp:/mcp",
        "-v", "/Users/<your-username>/Library/Caches/docker-secrets-engine/engine.sock:/root/.cache/docker-secrets-engine/engine.sock",
        "docker/mcp-gateway:latest",
        "--catalog=/mcp/catalogs/custom.yaml",
        "--registry=/mcp/registry.yaml",
        "--transport=stdio"
      ]
    }
  }
}
```

All three bind-mounts are required:

1. **`/var/run/docker.sock`** — lets the gateway spawn the
   `unifi-readonly-mcp` container.
2. **`~/.docker/mcp`** — the gateway reads the catalog and registry from
   here.
3. **`docker-secrets-engine/engine.sock`** — the resolver socket Docker
   Desktop exposes for the secret store. Without it the gateway resolves
   your secret URLs to empty strings and `docker run -e ""` rejects the
   env flags, so the server never starts and only the gateway's internal
   admin tools show up. On Linux Docker Desktop the host path is
   `~/.docker/desktop/secrets-engine/engine.sock` instead; check with
   `find ~ -name engine.sock 2>/dev/null`.

`claude_desktop_config.json` never contains `UNIFI_API_KEY` — the gateway
resolves it from Docker's secret store at request time.

> **Shortcut alternative.** `docker mcp client connect claude-desktop` (or
> **MCP Toolkit > Clients** in Docker Desktop) will write a similar block
> for you automatically. The explicit JSON above gives you control over
> which catalogs load and survives Docker Desktop updates that may rewrite
> the auto-managed entry.

Notes:

* `-i` (`--interactive`) on `docker run` is **required** — MCP stdio needs
  stdin attached.
* `--rm` cleans up the container after each session.
* Every path must be **absolute**. Claude Desktop does not expand `~` or
  use your shell's working directory.

Restart Claude Desktop after editing the config.

---

## 7. Claude Code configuration

Claude Code uses the same `mcp-toolkit-gateway` block from §6 — same
`command`, same `args` — but reads it from a different file. There are
three scopes:

| Scope | File | Sharing |
|---|---|---|
| **local** (default) | `~/.claude.json`, under this project's entry | just you, just this project |
| **project** | `.mcp.json` at the project root | shared via git with collaborators |
| **user** (global) | `~/.claude.json`, top level | just you, every project |

**Easiest path — let the CLI write it for you.** Replace `<your-username>`
and pick the scope you want:

```bash
claude mcp add -s user mcp-toolkit-gateway -- \
  docker run -i --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /Users/<your-username>/.docker/mcp:/mcp \
  -v /Users/<your-username>/Library/Caches/docker-secrets-engine/engine.sock:/root/.cache/docker-secrets-engine/engine.sock \
  docker/mcp-gateway:latest \
  --catalog=/mcp/catalogs/custom.yaml \
  --registry=/mcp/registry.yaml \
  --transport=stdio
```

Use `-s user` for global, `-s project` to commit the entry to `.mcp.json`
for collaborators, or omit `-s` for the default local scope. Everything
after `--` is the same docker invocation Claude Desktop uses — the schema
is byte-for-byte identical.

Verify with `claude mcp list`. The §4 secrets and catalog / registry setup
all carry over; nothing else changes.

### `.env` shortcut (Option 2 only)

If you set up secrets via Option 2 (plaintext `.env`) instead of the
gateway, skip the block above and use the env-file form directly. `-i` is
mandatory; paths must be absolute:

```bash
claude mcp add unifi -- \
  docker run --rm -i \
  --env-file /absolute/path/to/docker/.env \
  unifi-readonly-mcp:latest
```

---

## 7b. Codex configuration

OpenAI Codex reads MCP server config from a TOML file instead of JSON. Two
scopes:

| Scope | File | Trust requirement |
|---|---|---|
| **global** | `~/.codex/config.toml` | none |
| **project** | `.codex/config.toml` at the project root | Codex only loads project files for **trusted** projects — confirm trust in Codex before relying on this scope |

Same gateway invocation as §6, mechanically translated from JSON to TOML
(`mcpServers.foo` → `[mcp_servers.foo]`; same `command`, same `args`).
Replace `<your-username>` with your macOS username (run `whoami` to check):

```toml
[mcp_servers.mcp-toolkit-gateway]
command = "docker"
args = [
  "run",
  "-i",
  "--rm",
  "-v",
  "/var/run/docker.sock:/var/run/docker.sock",
  "-v",
  "/Users/<your-username>/.docker/mcp:/mcp",
  "-v",
  "/Users/<your-username>/Library/Caches/docker-secrets-engine/engine.sock:/root/.cache/docker-secrets-engine/engine.sock",
  "docker/mcp-gateway:latest",
  "--catalog=/mcp/catalogs/custom.yaml",
  "--registry=/mcp/registry.yaml",
  "--transport=stdio",
]
```

Restart Codex or open a new project thread so the MCP server loads. The §4
secrets and catalog / registry setup all carry over; nothing else changes.

---

## 8. Available tools, rate limiting, response-size safety

These are identical to the Podman build. See the Podman README:

* [§8 Available Tools](../podman/README.md#8-available-tools)
* [§9 Example prompts](../podman/README.md#9-example-prompts-to-use-with-claude)
* [§10 Rate Limiting](../podman/README.md#10-rate-limiting)
* [§10b Response size & context-window safety](../podman/README.md#10b-response-size--context-window-safety)

---

## 9. Security notes

* **Credential handling.** Read once at process start from the environment
  (plain env vars only — no secret-file mode). Never written to
  stdout/stderr, never echoed in a tool response, never included in error
  strings. Credentials live only in the container's process memory.
* **Secrets over `.env`.** Prefer an MCP Toolkit secret (Option 1), which
  injects the value as an env var and keeps the credential out of a
  plaintext file in your project, out of the image, and out of
  `docker inspect` output. `.env` remains supported as a fallback.
* **Never baked into the image.** `.env` is excluded from the build
  context by `.dockerignore` and is gitignored. Credentials are supplied
  at runtime only.
* **Single-site pin.** `UNIFI_SITE_ID` (Integration UUID) and
  `UNIFI_SITE_NAME` (Classic short name) are read once at start; every
  site-scoped tool uses them directly — the model cannot target a
  different site.
* **Non-root runtime.** The container drops to `unifi-mcp` (uid 1001)
  before executing `server.py`.
* **No write paths.** Every tool routes through a shared `_request()`
  helper that refuses any verb other than `GET`. The only `POST` anywhere
  is the Classic-API login, performed via a one-off
  `requests.request("POST", ...)` (not the shared session) so the
  read-only grep lint stays empty. A dedicated `attempt_write_operation`
  tool returns the canonical refusal string.
* **Recommended admin scope.** Generate the API key (and use the same
  account for Classic) under a dedicated *local* Limited Admin with
  **Read-Only** role. Never use a UI.com cloud account.
* **TLS.** UniFi OS ships with self-signed certs. The server sets
  `verify=False` by default and silences the urllib3
  `InsecureRequestWarning`. For production, install a real certificate and
  set `UNIFI_VERIFY_TLS=true`.

---

## 10. Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| Claude doesn't see the server | Confirm `docker` is in `$PATH` for the user running the client. Confirm the image exists: `docker images \| grep unifi-readonly-mcp`. |
| Exits with `UNIFI_HOST is not set` (or `_API_KEY`, `_SITE_ID`) | `.env` missing / wrong `--env-file` path (must be absolute), or the MCP Toolkit secret isn't mapped to the right env var name. |
| `401 Unauthorized` from Integration tools | The API key was rejected. Re-generate under the right admin's Integrations panel. |
| `403 Forbidden` from Integration tools | Parent admin's role is too narrow. Read-Only is fine for every tool. |
| `404 Not found` from Integration tools | Endpoint may require a newer Network application. Call `unifi_int_get_info`. |
| Classic tools raise `Classic-API tools require ...` | Expected if you didn't set the Classic credentials. |
| Classic login fails with HTTP 499 | Admin has 2FA enabled. Create a new *local* Limited Admin without MFA. |
| Classic login returns 401/403 | Bad credentials, or you used a UI.com cloud account. Use a local admin. |
| Truncation envelope with `_truncated: true` | Endpoint exceeded `UNIFI_MAX_RESPONSE_BYTES`. Follow the `_hint` or raise the cap. |
| TLS / certificate errors | Self-signed cert. Either install a real cert or leave `UNIFI_VERIFY_TLS=false`. |
| MCP Toolkit gateway starts but only its admin tools show up | The secret resolver socket isn't mounted. See §6 bullet 3 for the path on macOS vs Linux. |
| Changed `server.py`, old behaviour persists | Rebuild without cache: `docker build --no-cache -t unifi-readonly-mcp:latest .` and restart the client. |
| stdio framing errors | Ensure `-i` is present on `docker run`. Without it Docker detaches stdin and the MCP handshake fails. |

---

## 11. Read-only invariant check

Before shipping any change to `server.py`, confirm no write verb exists on
the shared sessions:

```bash
grep -nE 'session\.(post|put|patch|delete)|_session\.(post|put|patch|delete)' server.py
# Must return no matches.
```

`docker/server.py` is kept byte-for-byte identical to `podman/server.py`.
Diff them after any edit:

```bash
diff ../podman/server.py server.py   # expect no output
```

---

## License & contributions

Provided as-is for internal use. Not affiliated with or endorsed by
Ubiquiti. PRs that add additional **read-only** UniFi endpoints (either
API surface) are welcome; any change that introduces a write-capable verb
against the console will be rejected on sight.
