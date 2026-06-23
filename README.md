# GoodWe Inverter MCP Server

MCP server for monitoring and controlling **GoodWe solar inverters** over the local network.

[![goodwe-inverter-mcp MCP server](https://glama.ai/mcp/servers/marcinn2/goodwe-inverter-mcp/badges/card.svg)](https://glama.ai/mcp/servers/marcinn2/goodwe-inverter-mcp)

Built on the [`goodwe`](https://pypi.org/project/goodwe/) library and the [Model Context Protocol](https://modelcontextprotocol.io/) Python SDK.

> **Based on** the [Home Assistant GoodWe integration](https://github.com/home-assistant/core/tree/dev/homeassistant/components/goodwe) — the sensor definitions, operation modes, settings, and inverter family support are modelled directly after that implementation.

## Features

- Read live runtime data: PV production, battery state, grid import/export, load consumption
- Read and write all configurable inverter settings
- Switch operation modes (general, eco, backup, peak-shaving, off-grid, …)
- Control grid export limit and battery depth-of-discharge
- 7 MCP resources: status, runtime, settings, power flow, daily energy, battery, sensor catalog
- 6 built-in prompt templates for common workflows (status overview, diagnostics, optimisation, …)
- Bearer token authentication for all HTTP transports
- Four transport modes: **stdio**, **SSE**, **Streamable HTTP**, and **server** (SSE + Streamable HTTP combined)
- Auto-connect via environment variables

## Requirements

- Python 3.10+
- GoodWe inverter reachable on the local network (UDP port 8899 or Modbus/TCP port 502)

## Installation

```bash
# with uv (recommended)
uv pip install .

# or editable install for development
uv pip install -e .
```

## Usage

### stdio (Claude Desktop)

```bash
goodwe-mcp
```

Add to `~/.claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "goodwe": {
      "command": "goodwe-mcp",
      "env": {
        "GOODWE_HOST": "192.168.1.100"
      }
    }
  }
}
```

### SSE transport

```bash
goodwe-mcp --transport sse --port 8080
# Server listens on http://0.0.0.0:8080/sse
```

### Streamable HTTP transport

```bash
goodwe-mcp --transport streamable-http --port 8080
# Server listens on http://0.0.0.0:8080/mcp
```

### Server transport (SSE + Streamable HTTP combined)

Serves both transports on a single port — useful when you need to support legacy SSE clients and modern Streamable HTTP clients simultaneously.

```bash
goodwe-mcp --transport server --host 0.0.0.0 --port 8080
# SSE:             http://0.0.0.0:8080/sse  (GET) and /messages/ (POST)
# Streamable HTTP: http://0.0.0.0:8080/mcp
```

### Options

```
--transport {stdio,sse,streamable-http,server}   Transport mode (default: stdio)
--host HOST                                      Bind address for SSE/HTTP (default: 127.0.0.1)
--port PORT                                      Listen port for SSE/HTTP (default: 8000)
--log-level {DEBUG,INFO,WARNING,ERROR}           Logging verbosity (default: INFO)
--auth-token TOKEN                               Bearer token required on all HTTP requests (env: MCP_AUTH_TOKEN)
--base-url URL                                   Public base URL, e.g. https://mcp.example.com (env: MCP_BASE_URL)
```

### Environment variables

| Variable | Description | Default |
|---|---|---|
| `GOODWE_HOST` | Inverter IP / hostname for auto-connect on startup | — |
| `GOODWE_PORT` | Inverter UDP/TCP port | `8899` |
| `GOODWE_FAMILY` | Inverter family override (ET, EH, BT, BH, ES, EM, BP, DT, MS, NS, XS) | auto-detect |
| `MCP_AUTH_TOKEN` | Bearer token required on all HTTP requests | — (auth disabled) |
| `MCP_BASE_URL` | Public base URL of the server (used as OAuth issuer URL) | `http://<host>:<port>` |

## Authentication

Bearer token authentication is supported for all HTTP transports (`sse`, `streamable-http`, `server`).
When enabled, every MCP request must include an `Authorization: Bearer <token>` header.
The `/health` endpoint is always unprotected so Kubernetes probes continue to work.

### Enable via environment variable (recommended)

```bash
export MCP_AUTH_TOKEN="$(openssl rand -hex 32)"
goodwe-mcp --transport server --host 0.0.0.0 --port 8080
```

### Enable via CLI flag

```bash
goodwe-mcp --transport streamable-http --port 8080 --auth-token my-secret-token
```

### Claude Desktop / MCP client configuration

Add the token to your client's MCP server configuration.  For example, with Claude Desktop
using the `streamable-http` transport via a proxy that injects the header, or with any
client that supports `Authorization` headers:

```json
{
  "mcpServers": {
    "goodwe": {
      "url": "http://localhost:8080/mcp",
      "headers": {
        "Authorization": "Bearer my-secret-token"
      }
    }
  }
}
```

### Docker / Docker Compose

Pass the token through the environment:

```bash
MCP_AUTH_TOKEN=my-secret-token GOODWE_HOST=192.168.1.100 \
  docker compose -f docs/docker-compose.yml up -d
```

### Kubernetes

Set the token in `docs/k8s/secret.yaml` before applying the manifests:

```yaml
stringData:
  GOODWE_HOST: "192.168.1.100"
  MCP_AUTH_TOKEN: "my-secret-token"
```

If `MCP_AUTH_TOKEN` is empty or not set, authentication is disabled and all HTTP endpoints are publicly accessible. The server will log a warning at startup when bound to a non-loopback address without a token.

### TLS / HTTPS

The MCP server itself does not terminate TLS. For any non-localhost deployment, place a TLS-terminating reverse proxy in front of it (nginx, Caddy, Traefik). Serving inverter data — which constitutes personal data under GDPR when linked to a household — over plain HTTP is a security risk.

Example with Caddy (simplest option):

```
mcp.example.com {
    reverse_proxy localhost:8000
}
```

## Data Processing Notice

This server processes data from a GoodWe solar inverter, including the inverter's IP address, serial number, and energy consumption metrics. When deployed in a home and operated by the homeowner for personal use, this processing falls under the GDPR household exemption (Art. 2(2)(c)) and GDPR does not apply. If deployed commercially — for example to monitor inverters belonging to third-party customers — the operator becomes a data controller under GDPR (EU) 2016/679 and must establish a lawful basis for processing (Art. 6), maintain records of processing activities (Art. 30), and ensure appropriate technical and organisational measures (Art. 32), including TLS encryption and access control.

## Docker

### Build

Build for the current machine's architecture:

```bash
docker build -t goodwe-inverter-mcp:latest .
```

#### Multi-platform builds (amd64 + arm64)

Use `docker buildx` to produce an image that runs on both x86-64 servers and ARM boards (Raspberry Pi, Apple Silicon, AWS Graviton, etc.).

**One-time setup** — create a builder that supports cross-compilation:

```bash
docker buildx create --name multi --driver docker-container --bootstrap --use
```

**Build both platforms and load into the local daemon** — requires the containerd image store (enabled by default in Docker Desktop 4.34+; on Linux run `dockerd --snapshotter=overlayfs` or enable it in `/etc/docker/daemon.json`):

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t goodwe-inverter-mcp:latest --load .
```

**Build both platforms and push to a registry** (e.g. Docker Hub or GHCR):

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t youruser/goodwe-inverter-mcp:latest \
  --push .
```

**Build both platforms and export as a local OCI tar** (no registry needed):

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t goodwe-inverter-mcp:latest \
  --output type=oci,dest=goodwe-inverter-mcp.tar .
```

### Run

```bash
docker run -d \
  --name goodwe-mcp \
  -e GOODWE_HOST=192.168.1.100 \
  -p 8000:8000 \
  goodwe-inverter-mcp:latest
```

The container defaults to `--transport server` (SSE + Streamable HTTP on port 8000).  
Override the transport or port via `CMD` args:

```bash
docker run -d -e GOODWE_HOST=192.168.1.100 -p 9000:9000 \
  goodwe-inverter-mcp:latest \
  goodwe-mcp --transport streamable-http --host 0.0.0.0 --port 9000
```

### Docker Compose

```bash
GOODWE_HOST=192.168.1.100 docker compose -f docs/docker-compose.yml up -d
```

`docs/docker-compose.yml` uses `network_mode: host` by default so the container can reach the inverter on the local LAN. Remove that line if your network already routes LAN traffic into containers.

## Kubernetes

### Prerequisites

The GoodWe inverter communicates over UDP/TCP on the local network. The pod needs to reach the inverter's IP. The simplest setup is `hostNetwork: true` on a node in the same subnet; remove it if your cluster has flat networking or another routing solution.

### Deploy

```bash
# 1. Edit the inverter IP
vi docs/k8s/secret.yaml

# 2. Apply all manifests
kubectl apply -f docs/k8s/

# 3. Check status
kubectl rollout status deployment/goodwe-mcp
kubectl logs -f deployment/goodwe-mcp
```

### Health endpoints

Both liveness and readiness probes hit `GET /health`, which returns:

```json
{ "status": "ok", "inverter_connected": true }
```

The pod becomes ready once the HTTP server is up. `inverter_connected` will be `false` until the server successfully connects to the inverter (auto-connect fires on the first MCP client session).

## Tools

| Tool | Description |
|---|---|
| `connect_inverter` | Connect to a GoodWe inverter by IP/host |
| `get_connection_status` | Check if connected and show device info |
| `get_device_info` | Model name, serial number, firmware version |
| `get_runtime_data` | All live sensor values (optional filter by kind: PV/AC/BAT/GRID/UPS/BMS) |
| `list_sensors` | List all sensor IDs and names |
| `read_sensor` | Read a single sensor by ID |
| `get_settings_data` | All configurable settings and current values |
| `read_setting` | Read a single setting by ID |
| `write_setting` | Write a value to a configurable setting |
| `get_operation_mode` | Current mode and supported modes |
| `set_operation_mode` | Set mode: general, eco, backup, off_grid, peak_shaving, eco_charge, eco_discharge |
| `get_grid_export_limit` | Grid export limit in watts |
| `set_grid_export_limit` | Set grid export limit (0 = disabled) |
| `get_battery_dod` | Battery depth-of-discharge setting |
| `set_battery_dod` | Set battery depth-of-discharge (0–99%) |

## Prompts

Pre-written prompt templates that MCP clients can fetch and use directly.

| Prompt | Arguments | Description |
|---|---|---|
| `status_overview` | — | Full status report: connection, live power flow, battery, grid |
| `battery_optimisation` | — | Review battery settings and suggest DoD / mode improvements |
| `grid_export_config` | — | Check and adjust the grid export power limit |
| `operation_mode_change` | — | Explain available modes and help switch to the right one |
| `diagnose_issue` | `symptom` (optional) | Collect full diagnostics and identify problems |
| `daily_energy_summary` | — | Today's energy counters as a human-readable table |

## Resources

| URI | Description |
|---|---|
| `inverter://status` | Connection status and device info (JSON) |
| `inverter://runtime` | All live sensor values (JSON) |
| `inverter://settings` | All configurable settings (JSON) |
| `inverter://power/now` | Real-time power flow — PV, battery, grid and load in watts, grouped by kind |
| `inverter://energy/today` | Today's energy counters — production, load, grid buy/sell, battery charge/discharge (kWh) |
| `inverter://battery` | Battery sensors, DoD limit and current operation mode in one payload |
| `inverter://sensors` | Static sensor catalog — id, name, unit and kind for every sensor (no live values) |

## Inverter families

| Family | Models | Notes |
|---|---|---|
| ET / EH / BT / BH | Hybrid 3-phase | Battery support, up to 4 PV strings |
| ES / EM / BP | Hybrid single-phase | Battery support |
| DT / MS / NS / XS | Grid-tie only | No battery |

## License

See LICENSE file in the root of the repository.

## Disclaimer

This software is not affiliated with or endorsed by GoodWe Inc. Use at your own risk.
This software is a personal project that I maintain in my free time.
Refer to the licence for more information.