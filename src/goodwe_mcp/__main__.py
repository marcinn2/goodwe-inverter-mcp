import argparse
import logging
import sys
from contextlib import asynccontextmanager

from goodwe_mcp.server import build_mcp


class _SuppressSessionTeardownNoise(logging.Filter):
    """Suppress the cascade of harmless errors produced when the MCP
    Streamable HTTP session is cleanly terminated (DELETE /mcp → 200 OK)
    while concurrent requests are still in flight.

    Affected log messages (all races in the MCP SDK / uvicorn, not real errors):
      • "Error in standalone SSE writer"    — anyio.ClosedResourceError
      • "Error handling POST request"       — anyio.ClosedResourceError
      • "Exception in ASGI application"    — RuntimeError: response already completed
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not (
            self._exc_chain_has_closed_resource(record)
            or "after response already completed" in record.getMessage()
        )

    @staticmethod
    def _exc_chain_has_closed_resource(record: logging.LogRecord) -> bool:
        if "ClosedResourceError" in record.getMessage():
            return True
        if not (record.exc_info and record.exc_info[1] is not None):
            return False
        exc: BaseException | None = record.exc_info[1]
        seen: set[int] = set()
        while exc is not None and id(exc) not in seen:
            seen.add(id(exc))
            if type(exc).__name__ == "ClosedResourceError":
                return True
            exc = exc.__cause__ or exc.__context__
        return False


class _DowngradeSettingReadErrors(logging.Filter):
    """Downgrade 'Error reading setting <x>' records from ERROR to DEBUG.

    The goodwe ET class defines settings for the entire ET inverter family.
    Individual models silently reject Modbus registers they don't support
    (ILLEGAL DATA ADDRESS → ValueError: Unknown sensor/setting).  The library
    handles this correctly in read_settings_data() — it catches and skips the
    value — but logs every rejected register at ERROR level, which floods
    production logs.  These are expected and not actionable; downgrading to
    DEBUG keeps them available for troubleshooting without polluting INFO logs.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if "Error reading setting" in record.getMessage():
            record.levelno = logging.DEBUG
            record.levelname = "DEBUG"
            record.exc_info = None
            record.exc_text = None
        return True


async def _run_server_async(mcp, host: str, port: int) -> None:
    """Serve SSE and Streamable HTTP on the same uvicorn instance."""
    import uvicorn
    from starlette.applications import Starlette
    from starlette.middleware import Middleware

    # Build both sub-apps.  streamable_http_app() also initialises the
    # session manager that the lifespan below needs.
    sse_starlette = mcp.sse_app()
    http_starlette = mcp.streamable_http_app()

    # Routes don't overlap: /sse + /messages/ (SSE) and /mcp (Streamable HTTP)
    combined_routes = list(sse_starlette.routes) + list(http_starlette.routes)

    @asynccontextmanager
    async def lifespan(_app):
        async with mcp.session_manager.run():
            yield

    # When auth is enabled, each route handler already has RequireAuthMiddleware
    # applied by FastMCP, but the token-parsing AuthenticationMiddleware lives at
    # the Starlette app level and is lost when we extract routes.  Re-add it here.
    middleware: list[Middleware] = []
    if mcp._token_verifier:
        from starlette.middleware.authentication import AuthenticationMiddleware
        from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
        from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
        middleware = [
            Middleware(AuthenticationMiddleware, backend=BearerAuthBackend(mcp._token_verifier)),
            Middleware(AuthContextMiddleware),
        ]

    combined_app = Starlette(
        debug=mcp.settings.debug,
        routes=combined_routes,
        middleware=middleware,
        lifespan=lifespan,
    )

    config = uvicorn.Config(
        combined_app,
        host=host,
        port=port,
        log_level=mcp.settings.log_level.lower(),
    )
    await uvicorn.Server(config).serve()


def main() -> None:
    import os

    parser = argparse.ArgumentParser(
        prog="goodwe-mcp",
        description="MCP server for GoodWe solar inverters",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
transport modes:
  stdio              Standard I/O — for Claude Desktop and pipe-based clients (default)
  sse                Server-Sent Events over HTTP  (/sse, /messages/)
  streamable-http    Streamable HTTP — MCP 2025-03-26 spec  (/mcp)
  server             SSE + Streamable HTTP on the same port  (/sse, /messages/, /mcp)

environment variables (optional):
  GOODWE_HOST        Inverter IP / hostname for auto-connect on startup
  GOODWE_PORT        Inverter port (default: 8899)
  GOODWE_FAMILY      Inverter family override: ET, EH, BT, BH, ES, EM, BP, DT, MS, NS, XS
  MCP_AUTH_TOKEN     Bearer token required on all HTTP requests (HTTP transports only)
  MCP_BASE_URL       Public base URL of this server, e.g. https://mcp.example.com
                     Used as the OAuth issuer URL when --auth-token is set.
                     Defaults to http://<host>:<port> when not provided.

examples:
  # stdio (Claude Desktop)
  goodwe-mcp

  # SSE only
  goodwe-mcp --transport sse --host 0.0.0.0 --port 8080

  # Streamable HTTP only
  GOODWE_HOST=192.168.1.100 goodwe-mcp --transport streamable-http --port 8080

  # Both SSE and Streamable HTTP on one port
  GOODWE_HOST=192.168.1.100 goodwe-mcp --transport server --host 0.0.0.0 --port 8080

  # With bearer token authentication
  MCP_AUTH_TOKEN=my-secret-token goodwe-mcp --transport server --host 0.0.0.0 --port 8080
""",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http", "server"],
        default="stdio",
        help="Transport mode (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address for SSE / HTTP transports (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Listen port for SSE / HTTP transports (default: 8000)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging verbosity (default: INFO)",
    )
    parser.add_argument(
        "--auth-token",
        default=os.environ.get("MCP_AUTH_TOKEN"),
        metavar="TOKEN",
        help="Bearer token required on all HTTP requests. Env: MCP_AUTH_TOKEN",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("MCP_BASE_URL"),
        metavar="URL",
        help=(
            "Public base URL of this server (e.g. https://mcp.example.com). "
            "Used as the OAuth issuer URL when --auth-token is set. "
            "Defaults to http://<host>:<port>. Env: MCP_BASE_URL"
        ),
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    _teardown_filter = _SuppressSessionTeardownNoise()
    logging.getLogger("mcp.server.streamable_http").addFilter(_teardown_filter)
    logging.getLogger("uvicorn.error").addFilter(_teardown_filter)
    logging.getLogger("goodwe.et").addFilter(_DowngradeSettingReadErrors())

    _LOOPBACK = {"127.0.0.1", "localhost", "::1"}
    if args.transport != "stdio" and args.host not in _LOOPBACK and not args.auth_token:
        logging.getLogger("goodwe_mcp").warning(
            "Security warning: server bound to %s:%d with no MCP_AUTH_TOKEN set — "
            "all endpoints are publicly accessible. Set --auth-token or MCP_AUTH_TOKEN "
            "to require bearer authentication.",
            args.host,
            args.port,
        )

    mcp = build_mcp(
        host=args.host,
        port=args.port,
        auth_token=args.auth_token or None,
        base_url=args.base_url or None,
    )

    transport = args.transport
    if transport == "stdio":
        mcp.run(transport="stdio")
    elif transport == "sse":
        mcp.run(transport="sse")
    elif transport == "streamable-http":
        mcp.run(transport="streamable-http")
    elif transport == "server":
        import anyio
        anyio.run(_run_server_async, mcp, args.host, args.port)


if __name__ == "__main__":
    main()
