"""
A tiny MCP server, so you can see the other side of the protocol.

Run it through the assistant (it's in config/mcp_servers.json as "demo"):
    assistant tools
    assistant console --mcp      then ask: "what time is it in Tokyo?" or "save a note: buy milk"

Everything an MCP server needs is here: a name, some functions marked @server.tool(), and
server.run(). The SDK turns each function's signature and docstring into the JSON schema and
description that the LLM sees, and speaks JSON-RPC over stdin/stdout for you.

Never print() to stdout in an MCP server: stdout *is* the protocol channel. Log to stderr.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("demo")
NOTES = Path(os.environ.get("DEMO_NOTES_FILE", "data/demo_notes.json"))   # relative to the project root


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def current_time(timezone: str = "Asia/Karachi") -> str:
    """Current date and time in an IANA timezone, e.g. 'Europe/Berlin' or 'Asia/Tokyo'."""
    try:
        now = datetime.now(ZoneInfo(timezone))
    except (ZoneInfoNotFoundError, ValueError):
        return f"Unknown timezone: {timezone}"
    return now.strftime(f"%A %d %B %Y, %H:%M ({timezone})")


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def list_notes() -> str:
    """List the notes saved with save_note."""
    notes = json.loads(NOTES.read_text(encoding="utf-8")) if NOTES.exists() else []
    return "\n".join(f"{i + 1}. {n}" for i, n in enumerate(notes)) or "No notes yet."


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
def save_note(text: str) -> str:
    """Save a short note to a local file. (A *write* tool: remove it from allowed_tools to block it.)"""
    notes = json.loads(NOTES.read_text(encoding="utf-8")) if NOTES.exists() else []
    notes.append(text.strip())
    NOTES.parent.mkdir(parents=True, exist_ok=True)
    NOTES.write_text(json.dumps(notes, indent=2, ensure_ascii=False), encoding="utf-8")
    return f"Saved. You have {len(notes)} note(s)."


if __name__ == "__main__":
    server.run("stdio")
