import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from mcp.server.auth.middleware.bearer_auth import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from goodwe import OperationMode, SensorKind
from starlette.requests import Request
from starlette.responses import JSONResponse

from goodwe_mcp.connection import inverter_conn, inverter_attr

logger = logging.getLogger(__name__)

_SENSOR_KIND_LABEL = {
    SensorKind.PV: "PV (Solar)",
    SensorKind.AC: "AC (Grid)",
    SensorKind.UPS: "UPS (Backup)",
    SensorKind.BAT: "Battery",
    SensorKind.GRID: "Grid Meter",
    SensorKind.BMS: "Battery Management",
}

_SENSOR_KIND_BY_NAME = {
    "PV": SensorKind.PV,
    "AC": SensorKind.AC,
    "UPS": SensorKind.UPS,
    "BAT": SensorKind.BAT,
    "GRID": SensorKind.GRID,
    "BMS": SensorKind.BMS,
}

_MODE_TO_NAME = {
    OperationMode.GENERAL: "general",
    OperationMode.OFF_GRID: "off_grid",
    OperationMode.BACKUP: "backup",
    OperationMode.ECO: "eco",
    OperationMode.PEAK_SHAVING: "peak_shaving",
    OperationMode.ECO_CHARGE: "eco_charge",
    OperationMode.ECO_DISCHARGE: "eco_discharge",
}

_NAME_TO_MODE = {v: k for k, v in _MODE_TO_NAME.items()}


class StaticBearerVerifier:
    """Verifies a single pre-shared bearer token using a timing-safe comparison."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not secrets.compare_digest(token.encode(), self._token.encode()):
            return None
        return AccessToken(token=token, client_id="static", scopes=[])


def _err(exc: Exception) -> str:
    return f"Error ({type(exc).__name__}): {exc}"


@asynccontextmanager
async def _lifespan(_app: FastMCP) -> AsyncIterator[None]:
    if os.environ.get("GOODWE_HOST") and not inverter_conn.is_connected:
        logger.info("Auto-connecting from GOODWE_HOST environment variable…")
        await inverter_conn.auto_connect_from_env()
    yield


def build_mcp(
    host: str = "127.0.0.1",
    port: int = 8000,
    auth_token: Optional[str] = None,
    base_url: Optional[str] = None,
) -> FastMCP:
    """Create and return a fully configured FastMCP instance."""

    token_verifier: Optional[StaticBearerVerifier] = None
    auth_settings: Optional[AuthSettings] = None
    if auth_token:
        token_verifier = StaticBearerVerifier(auth_token)
        issuer_url = base_url or f"http://{host}:{port}"
        auth_settings = AuthSettings(issuer_url=issuer_url, resource_server_url=None)

    server = FastMCP(
        "GoodWe Inverter",
        instructions=(
            "Monitor and control GoodWe solar inverters. "
            "Read live power data, battery state, grid metrics and adjust settings. "
            "Call connect_inverter first if GOODWE_HOST was not set at startup."
        ),
        host=host,
        port=port,
        lifespan=_lifespan,
        token_verifier=token_verifier,
        auth=auth_settings,
    )

    # ------------------------------------------------------------------
    # Health check (HTTP transports only)
    # ------------------------------------------------------------------

    @server.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        """Liveness probe — returns inverter connection status."""
        return JSONResponse(
            {
                "status": "ok",
                "inverter_connected": inverter_conn.is_connected,
            }
        )

    # ------------------------------------------------------------------
    # Connection tools
    # ------------------------------------------------------------------

    @server.tool()
    async def connect_inverter(
        host: str,
        port: int = 8899,
        family: Optional[str] = None,
        timeout: int = 1,
        retries: int = 3,
    ) -> str:
        """Connect to a GoodWe inverter over the local network.

        Args:
            host: IP address or hostname of the inverter (e.g. "192.168.1.100").
            port: UDP port — default 8899. Use 502 for Modbus/TCP.
            family: Optional inverter family override: ET, EH, BT, BH, ES, EM, BP, DT, MS, NS, XS.
                    Leave blank for auto-detection.
            timeout: Per-attempt timeout in seconds.
            retries: Number of retry attempts on failure.
        """
        try:
            info = await inverter_conn.connect(
                host=host, port=port, family=family, timeout=timeout, retries=retries
            )
            return (
                f"Connected successfully.\n"
                f"  Model:    {info['model']}\n"
                f"  Serial:   {info['serial_number']}\n"
                f"  Firmware: {info['firmware']}\n"
                f"  Address:  {info['host']}:{info['port']}"
            )
        except Exception as exc:
            return _err(exc)

    @server.tool()
    async def get_connection_status() -> str:
        """Return the current connection status and basic device info."""
        if not inverter_conn.is_connected:
            host_env = os.environ.get("GOODWE_HOST", "")
            hint = (
                f" GOODWE_HOST is set to '{host_env}'."
                if host_env
                else " Set GOODWE_HOST or call connect_inverter."
            )
            return "Not connected." + hint
        inv = inverter_conn.get_inverter()
        return (
            f"Connected to GoodWe inverter.\n"
            f"  Model:    {inverter_attr(inv, 'model_name')}\n"
            f"  Serial:   {inverter_attr(inv, 'serial_number')}\n"
            f"  Firmware: {inverter_attr(inv, 'firmware_version')}\n"
            f"  Address:  {inverter_conn.host}:{inverter_conn.port}"
        )

    @server.tool()
    async def get_device_info() -> str:
        """Get model name, serial number and firmware version of the connected inverter."""
        try:
            inv = inverter_conn.get_inverter()
            return (
                f"Model:    {inverter_attr(inv, 'model_name')}\n"
                f"Serial:   {inverter_attr(inv, 'serial_number')}\n"
                f"Firmware: {inverter_attr(inv, 'firmware_version')}\n"
                f"Address:  {inverter_conn.host}:{inverter_conn.port}"
            )
        except Exception as exc:
            return _err(exc)

    # ------------------------------------------------------------------
    # Sensor / runtime data tools
    # ------------------------------------------------------------------

    @server.tool()
    async def get_runtime_data(sensor_kind: Optional[str] = None) -> str:
        """Read current live data from all inverter sensors.

        Args:
            sensor_kind: Optional filter — one of PV, AC, UPS, BAT, GRID, BMS.
                         Omit to return all sensors.

        Returns power, voltage, current, energy, battery state, grid status and more.
        """
        try:
            inv = inverter_conn.get_inverter()

            kind_filter: Optional[SensorKind] = None
            if sensor_kind:
                kind_filter = _SENSOR_KIND_BY_NAME.get(sensor_kind.upper())
                if kind_filter is None:
                    return (
                        f"Unknown sensor_kind '{sensor_kind}'. "
                        f"Valid values: {', '.join(_SENSOR_KIND_BY_NAME)}"
                    )

            runtime_data = await inv.read_runtime_data()
            sensors = inv.sensors()

            lines: list[str] = ["Live Runtime Data:"]
            current_kind: Optional[SensorKind] = None

            for sensor in sensors:
                if sensor.id_ not in runtime_data:
                    continue
                if kind_filter is not None and sensor.kind != kind_filter:
                    continue
                if sensor.kind != current_kind:
                    current_kind = sensor.kind
                    label = _SENSOR_KIND_LABEL.get(sensor.kind, str(sensor.kind))
                    lines.append(f"\n[{label}]")
                value = runtime_data[sensor.id_]
                unit = f" {sensor.unit}" if sensor.unit else ""
                lines.append(f"  {sensor.name}: {value}{unit}  ({sensor.id_})")

            if len(lines) == 1:
                return "No data available" + (f" for kind '{sensor_kind}'" if sensor_kind else "")
            return "\n".join(lines)
        except Exception as exc:
            return _err(exc)

    @server.tool()
    async def list_sensors(sensor_kind: Optional[str] = None) -> str:
        """List all sensor IDs and names available on the connected inverter.

        Args:
            sensor_kind: Optional filter — one of PV, AC, UPS, BAT, GRID, BMS.
        """
        try:
            inv = inverter_conn.get_inverter()

            kind_filter: Optional[SensorKind] = None
            if sensor_kind:
                kind_filter = _SENSOR_KIND_BY_NAME.get(sensor_kind.upper())
                if kind_filter is None:
                    return (
                        f"Unknown sensor_kind '{sensor_kind}'. "
                        f"Valid values: {', '.join(_SENSOR_KIND_BY_NAME)}"
                    )

            lines: list[str] = ["Available Sensors:"]
            current_kind: Optional[SensorKind] = None
            count = 0

            for sensor in inv.sensors():
                if kind_filter is not None and sensor.kind != kind_filter:
                    continue
                if sensor.kind != current_kind:
                    current_kind = sensor.kind
                    label = _SENSOR_KIND_LABEL.get(sensor.kind, str(sensor.kind))
                    lines.append(f"\n[{label}]")
                unit = f" ({sensor.unit})" if sensor.unit else ""
                lines.append(f"  {sensor.id_}: {sensor.name}{unit}")
                count += 1

            lines.append(f"\nTotal: {count} sensors")
            return "\n".join(lines)
        except Exception as exc:
            return _err(exc)

    @server.tool()
    async def read_sensor(sensor_id: str) -> str:
        """Read the current value of a single sensor by its ID.

        Args:
            sensor_id: Sensor identifier, e.g. "pv1_voltage", "battery_soc", "grid_power".
                       Use list_sensors to discover available IDs.
        """
        try:
            inv = inverter_conn.get_inverter()
            meta = next((s for s in inv.sensors() if s.id_ == sensor_id), None)
            if meta is None:
                return f"Sensor '{sensor_id}' not found. Use list_sensors to see available IDs."
            value = await inv.read_sensor(sensor_id)
            unit = f" {meta.unit}" if meta.unit else ""
            kind = _SENSOR_KIND_LABEL.get(meta.kind, str(meta.kind))
            return f"{meta.name} [{kind}]: {value}{unit}"
        except Exception as exc:
            return _err(exc)

    # ------------------------------------------------------------------
    # Settings tools
    # ------------------------------------------------------------------

    @server.tool()
    async def get_settings_data() -> str:
        """Read all configurable inverter settings and their current values."""
        try:
            inv = inverter_conn.get_inverter()
            data = await inv.read_settings_data()
            lines: list[str] = ["Inverter Settings:"]
            for setting in inv.settings():
                if setting.id_ not in data:
                    continue
                unit = f" {setting.unit}" if setting.unit else ""
                lines.append(f"  {setting.name}: {data[setting.id_]}{unit}  ({setting.id_})")
            if len(lines) == 1:
                return "No settings available"
            return "\n".join(lines)
        except Exception as exc:
            return _err(exc)

    @server.tool()
    async def read_setting(setting_id: str) -> str:
        """Read the current value of a single configurable setting.

        Args:
            setting_id: Setting identifier. Use get_settings_data to see all IDs.
        """
        try:
            inv = inverter_conn.get_inverter()
            meta = next((s for s in inv.settings() if s.id_ == setting_id), None)
            if meta is None:
                return f"Setting '{setting_id}' not found. Use get_settings_data to see available IDs."
            value = await inv.read_setting(setting_id)
            unit = f" {meta.unit}" if meta.unit else ""
            return f"{meta.name}: {value}{unit}"
        except Exception as exc:
            return _err(exc)

    @server.tool()
    async def write_setting(setting_id: str, value: str) -> str:
        """Write a new value to a configurable inverter setting.

        Args:
            setting_id: Setting identifier. Use get_settings_data to see available IDs.
            value: New value as a string (converted to int/float automatically when possible).

        Warning: Incorrect values can affect inverter operation. Verify the value before writing.
        """
        try:
            inv = inverter_conn.get_inverter()
            meta = next((s for s in inv.settings() if s.id_ == setting_id), None)
            if meta is None:
                return f"Setting '{setting_id}' not found. Use get_settings_data to see available IDs."

            typed: Any
            try:
                typed = int(value)
            except ValueError:
                try:
                    typed = float(value)
                except ValueError:
                    typed = value

            await inv.write_setting(setting_id, typed)
            unit = f" {meta.unit}" if meta.unit else ""
            logger.info("WRITE setting=%s value=%r host=%s", setting_id, typed, inverter_conn.host)
            return f"OK — {meta.name} set to {typed}{unit}"
        except Exception as exc:
            return _err(exc)

    # ------------------------------------------------------------------
    # Operation mode tools
    # ------------------------------------------------------------------

    @server.tool()
    async def get_operation_mode() -> str:
        """Get the current operation mode and the list of modes supported by this inverter."""
        try:
            inv = inverter_conn.get_inverter()
            current = await inv.get_operation_mode()
            supported = await inv.get_operation_modes(include_emulated=False)
            current_name = _MODE_TO_NAME.get(current, str(current))
            supported_names = [_MODE_TO_NAME.get(m, str(m)) for m in supported]
            return (
                f"Current mode: {current_name}\n"
                f"Supported modes: {', '.join(supported_names)}"
            )
        except Exception as exc:
            return _err(exc)

    @server.tool()
    async def set_operation_mode(
        mode: str,
        eco_mode_power: int = 100,
        eco_mode_soc: int = 100,
    ) -> str:
        """Set the inverter operation mode.

        Args:
            mode: One of: general, off_grid, backup, eco, peak_shaving, eco_charge, eco_discharge.
            eco_mode_power: Power level % for eco modes (0–100, default 100).
            eco_mode_soc: Battery SOC % threshold for eco modes (0–100, default 100).
        """
        try:
            op_mode = _NAME_TO_MODE.get(mode.lower())
            if op_mode is None:
                valid = ", ".join(_NAME_TO_MODE)
                return f"Unknown mode '{mode}'. Valid modes: {valid}"
            inv = inverter_conn.get_inverter()
            await inv.set_operation_mode(
                op_mode,
                eco_mode_power=eco_mode_power,
                eco_mode_soc=eco_mode_soc,
            )
            logger.info("WRITE operation_mode=%s eco_power=%d eco_soc=%d host=%s", mode, eco_mode_power, eco_mode_soc, inverter_conn.host)
            return f"Operation mode set to: {mode}"
        except Exception as exc:
            return _err(exc)

    # ------------------------------------------------------------------
    # Grid export tools
    # ------------------------------------------------------------------

    @server.tool()
    async def get_grid_export_limit() -> str:
        """Get the current grid export power limit in watts."""
        try:
            inv = inverter_conn.get_inverter()
            limit = await inv.get_grid_export_limit()
            return f"Grid export limit: {limit} W"
        except Exception as exc:
            return _err(exc)

    @server.tool()
    async def set_grid_export_limit(limit_watts: int) -> str:
        """Set the maximum power the inverter may export to the grid.

        Args:
            limit_watts: Power limit in watts (0–10000). Use 0 to disable export.
        """
        try:
            inv = inverter_conn.get_inverter()
            await inv.set_grid_export_limit(limit_watts)
            logger.info("WRITE grid_export_limit=%d host=%s", limit_watts, inverter_conn.host)
            return f"Grid export limit set to {limit_watts} W"
        except Exception as exc:
            return _err(exc)

    # ------------------------------------------------------------------
    # Battery tools
    # ------------------------------------------------------------------

    @server.tool()
    async def get_battery_dod() -> str:
        """Get the battery depth-of-discharge setting (percentage of capacity that may be used)."""
        try:
            inv = inverter_conn.get_inverter()
            dod = await inv.get_ongrid_battery_dod()
            return f"Battery depth of discharge: {dod}%"
        except Exception as exc:
            return _err(exc)

    @server.tool()
    async def set_battery_dod(depth_percent: int) -> str:
        """Set the battery depth-of-discharge limit.

        Args:
            depth_percent: How much of the battery capacity may be discharged (0–99).
                           Higher values allow deeper cycling but can reduce battery lifespan.
        """
        try:
            if not 0 <= depth_percent <= 99:
                return "depth_percent must be between 0 and 99"
            inv = inverter_conn.get_inverter()
            await inv.set_ongrid_battery_dod(depth_percent)
            logger.info("WRITE battery_dod=%d host=%s", depth_percent, inverter_conn.host)
            return f"Battery depth of discharge set to {depth_percent}%"
        except Exception as exc:
            return _err(exc)

    # ------------------------------------------------------------------
    # Resources
    # ------------------------------------------------------------------

    @server.resource("inverter://status")
    async def resource_status() -> str:
        """Connection status and device info for the GoodWe inverter."""
        if not inverter_conn.is_connected:
            return json.dumps({"connected": False})
        inv = inverter_conn.get_inverter()
        return json.dumps(
            {
                "connected": True,
                "model": inverter_attr(inv, "model_name"),
                "serial_number": inverter_attr(inv, "serial_number"),
                "firmware": inverter_attr(inv, "firmware_version"),
                "host": inverter_conn.host,
                "port": inverter_conn.port,
            },
            indent=2,
        )

    @server.resource("inverter://runtime")
    async def resource_runtime() -> str:
        """Live runtime data from the inverter — all sensor values as JSON."""
        if not inverter_conn.is_connected:
            return json.dumps({"error": "not connected"})
        try:
            inv = inverter_conn.get_inverter()
            runtime_data = await inv.read_runtime_data()
            result: dict[str, Any] = {}
            for sensor in inv.sensors():
                if sensor.id_ in runtime_data:
                    result[sensor.id_] = {
                        "name": sensor.name,
                        "value": runtime_data[sensor.id_],
                        "unit": sensor.unit,
                        "kind": sensor.kind.name if sensor.kind else None,
                    }
            return json.dumps(result, indent=2)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    @server.resource("inverter://settings")
    async def resource_settings() -> str:
        """Current configurable settings of the inverter as JSON."""
        if not inverter_conn.is_connected:
            return json.dumps({"error": "not connected"})
        try:
            inv = inverter_conn.get_inverter()
            data = await inv.read_settings_data()
            result: dict[str, Any] = {}
            for setting in inv.settings():
                if setting.id_ in data:
                    result[setting.id_] = {
                        "name": setting.name,
                        "value": data[setting.id_],
                        "unit": setting.unit,
                    }
            return json.dumps(result, indent=2)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    @server.resource("inverter://power/now")
    async def resource_power_now() -> str:
        """Real-time power flow: PV, battery, grid and load — watts only."""
        if not inverter_conn.is_connected:
            return json.dumps({"error": "not connected"})
        try:
            inv = inverter_conn.get_inverter()
            runtime_data = await inv.read_runtime_data()
            result: dict[str, Any] = {}
            for sensor in inv.sensors():
                if sensor.id_ not in runtime_data:
                    continue
                if sensor.unit != "W":
                    continue
                kind_key = sensor.kind.name.lower() if sensor.kind else "other"
                result.setdefault(kind_key, {})[sensor.id_] = {
                    "name": sensor.name,
                    "value": runtime_data[sensor.id_],
                    "unit": "W",
                }
            return json.dumps(result, indent=2)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    @server.resource("inverter://energy/today")
    async def resource_energy_today() -> str:
        """Today's energy counters: production, load, grid buy/sell, battery charge/discharge (kWh)."""
        if not inverter_conn.is_connected:
            return json.dumps({"error": "not connected"})
        try:
            inv = inverter_conn.get_inverter()
            runtime_data = await inv.read_runtime_data()
            result: dict[str, Any] = {}
            for sensor in inv.sensors():
                if sensor.id_ not in runtime_data:
                    continue
                if sensor.unit not in ("kWh", "Wh"):
                    continue
                name_lower = sensor.name.lower()
                id_lower = sensor.id_.lower()
                if not any(k in name_lower or k in id_lower for k in ("today", "daily", "_day", "day_")):
                    continue
                result[sensor.id_] = {
                    "name": sensor.name,
                    "value": runtime_data[sensor.id_],
                    "unit": sensor.unit,
                }
            return json.dumps(result, indent=2)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    @server.resource("inverter://battery")
    async def resource_battery() -> str:
        """Battery state: SOC, power, temperature, BMS data, depth-of-discharge and operation mode."""
        if not inverter_conn.is_connected:
            return json.dumps({"error": "not connected"})
        try:
            inv = inverter_conn.get_inverter()
            runtime_data = await inv.read_runtime_data()

            sensors: dict[str, Any] = {}
            for sensor in inv.sensors():
                if sensor.id_ not in runtime_data:
                    continue
                if sensor.kind not in (SensorKind.BAT, SensorKind.BMS):
                    continue
                sensors[sensor.id_] = {
                    "name": sensor.name,
                    "value": runtime_data[sensor.id_],
                    "unit": sensor.unit,
                }

            dod: Any = None
            try:
                dod = await inv.get_ongrid_battery_dod()
            except Exception:
                pass

            mode: Any = None
            try:
                current = await inv.get_operation_mode()
                mode = _MODE_TO_NAME.get(current, str(current))
            except Exception:
                pass

            return json.dumps(
                {"sensors": sensors, "depth_of_discharge_pct": dod, "operation_mode": mode},
                indent=2,
            )
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    @server.resource("inverter://sensors")
    async def resource_sensors() -> str:
        """Static sensor catalog: id, name, unit and kind for every sensor (no live values)."""
        if not inverter_conn.is_connected:
            return json.dumps({"error": "not connected"})
        try:
            inv = inverter_conn.get_inverter()
            result: dict[str, Any] = {}
            for sensor in inv.sensors():
                result[sensor.id_] = {
                    "name": sensor.name,
                    "unit": sensor.unit,
                    "kind": sensor.kind.name if sensor.kind else None,
                }
            return json.dumps(result, indent=2)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    # ------------------------------------------------------------------
    # Prompts
    # ------------------------------------------------------------------

    @server.prompt(
        title="System status overview",
        description="Get a full status report: connection, live power flow, battery state, and grid metrics.",
    )
    def status_overview() -> str:
        return (
            "Use the available tools to give me a complete status overview of my GoodWe solar inverter.\n\n"
            "Please:\n"
            "1. Call get_connection_status to confirm the inverter is reachable.\n"
            "2. Call get_runtime_data to read all live sensor values.\n"
            "3. Call get_operation_mode to show the current mode.\n"
            "4. Summarise the results in a clear, human-readable report covering:\n"
            "   - PV production (current power and today's energy)\n"
            "   - Battery state (SOC %, charging/discharging power)\n"
            "   - Grid import/export\n"
            "   - House load\n"
            "   - Active operation mode\n"
            "Highlight anything unusual or worth attention."
        )

    @server.prompt(
        title="Battery optimisation advice",
        description="Review battery settings and suggest depth-of-discharge and operation mode adjustments.",
    )
    def battery_optimisation() -> str:
        return (
            "I want to optimise my GoodWe inverter's battery usage. "
            "Please use the available tools to analyse the current configuration and give concrete recommendations.\n\n"
            "Steps:\n"
            "1. Call get_runtime_data (filter: BAT) to see the current battery state.\n"
            "2. Call get_battery_dod to read the depth-of-discharge limit.\n"
            "3. Call get_operation_mode to check the current and available modes.\n"
            "4. Call get_settings_data to review all battery-related settings.\n\n"
            "Based on the data, advise me on:\n"
            "- Whether the depth-of-discharge limit is appropriate for long battery lifespan.\n"
            "- Which operation mode best fits my situation (self-consumption, backup, peak-shaving, eco).\n"
            "- Any settings that look misconfigured or suboptimal.\n"
            "- Concrete next steps, including which tool calls to make to apply your recommendations."
        )

    @server.prompt(
        title="Grid export configuration",
        description="Check and adjust the grid export power limit.",
    )
    def grid_export_config() -> str:
        return (
            "Help me configure the grid export limit on my GoodWe inverter.\n\n"
            "1. Call get_grid_export_limit to show the current limit.\n"
            "2. Call get_runtime_data (filter: PV) to show current PV production.\n"
            "3. Call get_runtime_data (filter: GRID) to show current grid flow.\n\n"
            "Explain what the export limit does, whether the current value looks correct, "
            "and ask me how much power I am allowed to export to the grid so you can "
            "call set_grid_export_limit with the right value if I want to change it."
        )

    @server.prompt(
        title="Operation mode change",
        description="Explain available operation modes and help pick the right one.",
    )
    def operation_mode_change() -> str:
        return (
            "I want to change my GoodWe inverter's operation mode.\n\n"
            "1. Call get_operation_mode to show the current mode and all supported modes.\n"
            "2. Call get_runtime_data to give context (battery SOC, PV output, grid state).\n\n"
            "Then explain each supported mode in plain language — when it is useful and what it does — "
            "and ask me which one I'd like to switch to. "
            "Once I confirm, call set_operation_mode with the chosen mode."
        )

    @server.prompt(
        title="Diagnose inverter issue",
        description="Collect full diagnostics and help identify problems with the inverter.",
    )
    def diagnose_issue(symptom: str = "") -> str:
        symptom_line = f"\nThe symptom I am seeing: {symptom}\n" if symptom.strip() else ""
        return (
            f"Help me diagnose a problem with my GoodWe solar inverter.{symptom_line}\n"
            "Please collect full diagnostics:\n"
            "1. Call get_connection_status.\n"
            "2. Call get_device_info.\n"
            "3. Call get_runtime_data (all sensors).\n"
            "4. Call get_operation_mode.\n"
            "5. Call get_settings_data.\n"
            "6. Call get_battery_dod and get_grid_export_limit.\n\n"
            "Review all the data and:\n"
            "- Identify any sensor readings that look abnormal (zero production on a sunny day, "
            "unexpected grid draw, battery not charging, etc.).\n"
            "- Highlight settings that could be misconfigured.\n"
            "- Suggest specific corrective actions, including which tool calls to make."
        )

    @server.prompt(
        title="Energy summary for today",
        description="Pull today's energy counters and produce a human-readable daily summary.",
    )
    def daily_energy_summary() -> str:
        return (
            "Give me a summary of today's energy figures from my GoodWe inverter.\n\n"
            "1. Call get_runtime_data to read all sensor values.\n"
            "2. Extract the daily energy counters "
            "(look for sensors whose names contain 'today', 'daily', or 'day_' — "
            "e.g. e_day, e_load_day, e_grid_buy_day, e_grid_sell_day, e_bat_charge_day, e_bat_discharge_day).\n\n"
            "Present the results as a clear daily energy table with columns: category, energy (kWh). "
            "Then add a brief interpretation: self-consumption ratio, grid dependency, battery utilisation."
        )

    return server
