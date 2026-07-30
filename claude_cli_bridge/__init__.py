"""claude_cli_bridge — expose the local Claude Code CLI as an MCP worker.

A drop-in replacement for the ACP layer of workbuddy_bridge: instead of driving
a WorkBuddy desktop host over JSON-RPC/SSE, every task spawns an isolated
``claude -p`` subprocess.  The CLI already owns the agent loop, tool execution,
session persistence and streaming, so the bridge only has to wrap it.
"""

__version__ = "0.1.0"
