import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from mcp.server.fastmcp import FastMCP
from goodwe import OperationMode, SensorKind
from starlette.requests import Request
from starlette.responses import JSONResponse

from goodwe_mcp.connection import inverter_conn

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


def _err(exc: Exception) -> str:
    return f"Error ({type(exc).__name__}): {exc}"


@asynccontextmanager
async def _lifespan(_app: FastMCP) -> AsyncIterator[None]:
    if os.environ.get("GOODWE_HOST"):
        logger.info("Auto-connecting from GOODWE_HOST environment variable…")
        await inverter_conn.auto_connect_from_env()
    yield


def build_mcp(host: str = "127.0.0.1", port: int = 8000) -> FastMCP:
    """Create and return a fully configured FastMCP instance."""

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
            f"  Model:    {inv.model_name}\n"
            f"  Serial:   {inv.serial_number}\n"
            f"  Firmware: {inv.firmware_version}\n"
            f"  Address:  {inverter_conn.host}:{inverter_conn.port}"
        )

    @server.tool()
    async def get_device_info() -> str:
        """Get model name, serial number and firmware version of the connected inverter."""
        try:
            inv = inverter_conn.get_inverter()
            return (
                f"Model:    {inv.model_name}\n"
                f"Serial:   {inv.serial_number}\n"
                f"Firmware: {inv.firmware_version}\n"
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
            supported = await inv.get_operation_modes()
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
                "model": inv.model_name,
                "serial_number": inv.serial_number,
                "firmware": inv.firmware_version,
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

    return server
