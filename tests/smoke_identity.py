"""Identity + review-flow smoke test.

Verifies the differentiated capabilities beyond a plain prompt:
  - identity injection (S1 reviewer) with a constrained tool set
  - review_target binding to a real file (first review)
  - resume_review reusing the bound session (re-review), with SHA-256 diff

Usage: .venv/bin/python tests/smoke_identity.py
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


async def run_task_to_completion(
    session: ClientSession, args: dict
) -> dict:
    started = await call(session, "ask_start", args)
    assert started.get("ok"), f"ask_start failed: {started}"
    task_id = started["task_id"]
    for _ in range(8):  # up to ~8*55s
        final = await call(
            session, "ask_wait", {"task_id": task_id, "timeout_seconds": 55}
        )
        if final.get("state") in {"completed", "failed", "cancelled"}:
            return final
    return final  # type: ignore[name-defined]


async def main() -> bool:
    # A tiny file to review so S1 has something concrete to assess.
    tmp = Path(tempfile.mkdtemp(prefix="gc-review-"))
    target = tmp / "sample.py"
    target.write_text(
        "def add(a, b)\n"          # missing colon -- S1 should flag this
        "    return a + b\n",
        encoding="utf-8",
    )

    params = StdioServerParameters(
        command=sys.executable, args=["-m", "claude_cli_bridge"], env=None
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print("[1] First S1 review (binds session + baseline sha):")
            first = await run_task_to_completion(
                session,
                {
                    "prompt": "审查这个文件的语法问题，给出结论。",
                    "identity": "S1",
                    "review_target": str(target),
                    "model": "glm-5.2",
                    "timeout_seconds": 120,
                },
            )
            first_session = first.get("session_id")
            print("  state:", first.get("state"))
            print("  session_id:", first_session)
            print("  current_sha256:", (first.get("current_sha256") or "")[:12], "...")
            print("  answer head:", repr((first.get("answer") or "")[:120]))
            ok1 = (
                first.get("state") == "completed"
                and bool(first.get("session_id"))
                and bool(first.get("current_sha256"))
            )

            print("\n[2] Re-review (resume_review reuses the bound session):")
            second = await run_task_to_completion(
                session,
                {
                    "prompt": "重新审查同一目标，给出本轮结论。",
                    "identity": "S1",
                    "review_target": str(target),
                    "resume_review": True,
                    "model": "glm-5.2",
                    "timeout_seconds": 120,
                },
            )
            print("  state:", second.get("state"))
            print("  resumed:", second.get("resumed"))
            print("  resume_review:", second.get("resume_review"))
            print("  previous_sha256:", (second.get("previous_sha256") or "")[:12], "...")
            print("  answer head:", repr((second.get("answer") or "")[:120]))
            ok2 = (
                second.get("state") == "completed"
                and second.get("resumed") is True
                and bool(second.get("previous_sha256"))
            )

            print("\nRESULT:", "PASS" if ok1 and ok2 else "FAIL")
            return bool(ok1 and ok2)


if __name__ == "__main__":
    try:
        ok = asyncio.run(main())
    except BaseException as e:  # noqa: BLE001
        print("TEST CRASHED:", repr(e))
        ok = False
    sys.exit(0 if ok else 1)
