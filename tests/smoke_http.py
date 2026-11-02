"""Smoke test for the FileForge MCP server's Streamable HTTP transport.

Why in-process? This dev sandbox intercepts loopback TCP between
separate processes, so the test drives the server's real ASGI app
(the exact Starlette app uvicorn serves in production) through an
httpx ASGI transport and the official MCP client. The full MCP
protocol path - initialize, tools/list, tools/call - is exercised;
only raw TCP is bypassed.

On an unrestricted machine you can equivalently boot
``python -m fileforge.server`` and point any MCP client at
http://127.0.0.1:8765/mcp.

Run from the project root:
    PYTHONPATH=src .venv/bin/python tests/smoke_http.py
Requires samples to exist (run tests/run_selftest.py first).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


async def run_client() -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    from fileforge.server import mcp

    app = mcp.streamable_http_app()

    # Host must be 127.0.0.1: FastMCP's DNS-rebinding protection only
    # allows localhost hosts by default. The ASGI transport intercepts
    # the request before any real network I/O happens.
    def client_factory(**kwargs) -> httpx.AsyncClient:
        kwargs.pop("follow_redirects", None)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            follow_redirects=True,
        )

    async with app.router.lifespan_context(app):
        async with streamablehttp_client(
            "http://127.0.0.1:8765/mcp", httpx_client_factory=client_factory
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools_result = await session.list_tools()
                names = sorted(t.name for t in tools_result.tools)
                print(f"tools listed over Streamable HTTP: {names}")
                assert names == ["images_to_pdf", "merge_pdfs", "pdf_tables_to_excel"], names
                for t in tools_result.tools:
                    assert t.title, f"tool {t.name} is missing a title"
                    assert len(t.name) <= 64, f"tool name too long: {t.name}"

                sample = PROJECT_ROOT / "samples" / "one.png"
                result = await session.call_tool(
                    "images_to_pdf", {"image_paths": [str(sample)]}
                )
                assert not result.isError, result.content
                print(f"remote images_to_pdf call OK: {result.content[0].text[:120]}...")


def main() -> int:
    asyncio.run(run_client())
    print("[PASS] streamable-http smoke test (ASGI transport, official MCP client)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
