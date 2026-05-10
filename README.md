# GoodWe Inverter MCP Server

MCP server for monitoring and controlling **GoodWe solar inverters** over the local network.

Built on the [`goodwe`](https://pypi.org/project/goodwe/) library (same as the Home Assistant integration) and the [Model Context Protocol](https://modelcontextprotocol.io/) Python SDK.

## Features

- Read live runtime data: PV production, battery state, grid import/export, load consumption
- Read and write all configurable inverter settings
- Switch operation modes (general, eco, backup, peak-shaving, off-grid, …)
- Control grid export limit and battery depth-of-discharge
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
```

### Environment variables

| Variable | Description | Default |
|---|---|---|
| `GOODWE_HOST` | Inverter IP / hostname for auto-connect on startup | — |
| `GOODWE_PORT` | Inverter UDP/TCP port | `8899` |
| `GOODWE_FAMILY` | Inverter family override (ET, EH, BT, BH, ES, EM, BP, DT, MS, NS, XS) | auto-detect |

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

## Resources

| URI | Description |
|---|---|
| `inverter://status` | Connection status and device info (JSON) |
| `inverter://runtime` | All live sensor values (JSON) |
| `inverter://settings` | All configurable settings (JSON) |

## Inverter families

| Family | Models | Notes |
|---|---|---|
| ET / EH / BT / BH | Hybrid 3-phase | Battery support, up to 4 PV strings |
| ES / EM / BP | Hybrid single-phase | Battery support |
| DT / MS / NS / XS | Grid-tie only | No battery |

## License

MIT
