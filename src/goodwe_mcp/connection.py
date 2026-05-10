import asyncio
import logging
import os
from typing import Optional

import goodwe
from goodwe import Inverter

logger = logging.getLogger(__name__)


class InverterConnection:
    """Manages a single persistent connection to a GoodWe inverter."""

    def __init__(self) -> None:
        self._inverter: Optional[Inverter] = None
        self._lock = asyncio.Lock()
        self._host: Optional[str] = None
        self._port: int = 8899

    @property
    def is_connected(self) -> bool:
        return self._inverter is not None

    @property
    def host(self) -> Optional[str]:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    async def connect(
        self,
        host: str,
        port: int = 8899,
        family: Optional[str] = None,
        comm_addr: int = 0,
        timeout: int = 1,
        retries: int = 3,
    ) -> dict:
        async with self._lock:
            logger.info("Connecting to GoodWe inverter at %s:%d", host, port)
            self._inverter = await goodwe.connect(
                host=host,
                port=port,
                family=family,
                comm_addr=comm_addr,
                timeout=timeout,
                retries=retries,
            )
            self._host = host
            self._port = port
            logger.info("Connected to %s", self._inverter.model_name)
            return {
                "model": self._inverter.model_name,
                "serial_number": self._inverter.serial_number,
                "firmware": self._inverter.firmware_version,
                "host": host,
                "port": port,
            }

    def get_inverter(self) -> Inverter:
        if self._inverter is None:
            env_hint = ""
            host = os.environ.get("GOODWE_HOST")
            if host:
                env_hint = f" GOODWE_HOST is set to '{host}' — try connect_inverter(host='{host}')."
            raise RuntimeError(
                "Not connected to any inverter. Use the connect_inverter tool first." + env_hint
            )
        return self._inverter

    async def auto_connect_from_env(self) -> bool:
        """Try to connect using GOODWE_HOST / GOODWE_PORT / GOODWE_FAMILY env vars.

        No-ops if already connected (safe to call on every MCP session start).
        """
        if self._inverter is not None:
            return True
        host = os.environ.get("GOODWE_HOST")
        if not host:
            return False
        port = int(os.environ.get("GOODWE_PORT", "8899"))
        family = os.environ.get("GOODWE_FAMILY") or None
        try:
            await self.connect(host=host, port=port, family=family)
            return True
        except Exception as exc:
            logger.warning("Auto-connect from GOODWE_HOST failed: %s", exc)
            return False


inverter_conn = InverterConnection()
