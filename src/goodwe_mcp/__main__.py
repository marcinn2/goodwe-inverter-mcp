import argparse
import logging
import sys
from contextlib import asynccontextmanager

from goodwe_mcp.server import build_mcp


async def _run_server_async(mcp, host: str, port: int) -> None:
    """Serve SSE and Streamable HTTP on the same uvicorn instance."""
    import uvicorn
    from starlette.applications import Starlette

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

    combined_app = Starlette(
        debug=mcp.settings.debug,
        routes=combined_routes,
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

examples:
  # stdio (Claude Desktop)
  goodwe-mcp

  # SSE only
  goodwe-mcp --transport sse --host 0.0.0.0 --port 8080

  # Streamable HTTP only
  GOODWE_HOST=192.168.1.100 goodwe-mcp --transport streamable-http --port 8080

  # Both SSE and Streamable HTTP on one port
  GOODWE_HOST=192.168.1.100 goodwe-mcp --transport server --host 0.0.0.0 --port 8080
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

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    mcp = build_mcp(host=args.host, port=args.port)

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
