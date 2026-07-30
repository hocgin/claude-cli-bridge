"""End-to-end smoke test: drive the MCP server over stdio like Codex would.

Usage:
    .venv/bin/python tests/smoke_mcp.py

Not part of the importable package; kept under tests/ for manual verification.
"""

from __future__ import annotations

import asyncio
import json
import sys

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

PYTHON = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1:] else None


async def call(session: ClientSession, name: str, args: dict) -> dict:
    result = await session.call_tool(name, args)
    # FastMCP returns a single TextContent; unpack its JSON.
    if result.content and hasattr(result.content[0], "text"):
        return json.loads(result.content[0].text)
    return {}


async def main() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "claude_cli_bridge"],
        env=None,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("tools:", [t.name for t in tools.tools])

            print("\n[1] ask_status (connectivity):")
            print(json.dumps(await call(session, "ask_status", {}), ensure_ascii=False, indent=2))

            print("\n[2] ask_start:")
            started = await call(
                session,
                "ask_start",
                {
                    "prompt": "Reply with exactly the word ONLINE and nothing else.",
                    "model": "glm-5.2",
                    "timeout_seconds": 120,
                },
            )
            print(json.dumps(started, ensure_ascii=False, indent=2))
            if not started.get("ok"):
                sys.exit(1)
            task_id = started["task_id"]

            print("\n[3] ask_wait:")
            final = await call(session, "ask_wait", {"task_id": task_id, "timeout_seconds": 55})
            print(json.dumps(final, ensure_ascii=False, indent=2))

            answer = final.get("answer", "")
            ok = final.get("state") == "completed" and "ONLINE" in answer
            print(
                "\nRESULT:",
                "PASS" if ok else "FAIL",
                "| state:",
                final.get("state"),
                "| answer:",
                repr(answer),
            )
            return ok


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
