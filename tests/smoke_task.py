"""ask_task smoke test: synchronous arbitrary-task delegation.

Verifies the general-purpose entry point:
  - runs with the FULL default tool set (can Read a file we never named in the
    prompt, proving it has tool access, not just bare text completion)
  - returns synchronously (the final answer is in the same call)
  - honours a custom system_prompt
  - the task is queryable via ask_status afterwards

Usage: .venv/bin/python tests/smoke_task.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def call(session: ClientSession, name: str, args: dict) -> dict:
    result = await session.call_tool(name, args)
    if result.content and hasattr(result.content[0], "text"):
        return json.loads(result.content[0].text)
    return {}


async def main() -> bool:
    # A marker file with a secret token the worker must READ to report back.
    # Since we never put the token in the prompt, the worker can only know it
    # by actually using a file-reading tool -- proving full tool access.
    tmp = Path(tempfile.mkdtemp(prefix="gc-task-"))
    marker = tmp / "marker.txt"
    token = "BLUEBIRD-42"
    marker.write_text(f"secret token: {token}\n", encoding="utf-8")

    params = StdioServerParameters(
        command=sys.executable, args=["-m", "claude_cli_bridge"], env=None
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print("[1] ask_task (full tools, reads marker file):")
            res = await call(
                session,
                "ask_task",
                {
                    "prompt": (
                        f"读取文件 {marker} 的内容，找到其中的 secret token，"
                        "然后用一句话报告这个 token 是什么。"
                    ),
                    "cwd": str(tmp),
                    "system_prompt": "你是一个能读写文件、执行命令的全能助手。",
                    "model": "glm-5.2",
                    "timeout_seconds": 180,
                },
            )
            print("  state:", res.get("state"))
            print("  error:", res.get("error"))
            print("  cost_usd:", round(res.get("cost_usd", 0), 4))
            print("  answer:", repr((res.get("answer") or "")[:200]))
            ok1 = (
                res.get("state") == "completed"
                and token in (res.get("answer") or "")
            )

            print("\n[2] ask_status (task is queryable afterwards):")
            tid = res.get("task_id")
            status = await call(session, "ask_status", {"task_id": tid})
            print("  ok:", status.get("ok"), "| state:", status.get("state"))
            ok2 = bool(status.get("ok")) and status.get("task_id") == tid

            print("\nRESULT:", "PASS" if ok1 and ok2 else "FAIL")
            if not ok1:
                print("  (worker did not surface the token -> no tool access?)")
            return bool(ok1 and ok2)


if __name__ == "__main__":
    try:
        ok = asyncio.run(main())
    except BaseException as e:  # noqa: BLE001
        print("TEST CRASHED:", repr(e))
        ok = False
    sys.exit(0 if ok else 1)
