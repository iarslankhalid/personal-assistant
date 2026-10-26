"""
Lesson 2: an MCP server over your own data (a to-do list in data/tasks.json).

What this adds over demo_mcp_server.py:
  - read tools vs write tools, marked with annotations
  - validating the model's arguments (never trust them)
  - ToolError: the ONLY way your error message reaches the model.
    A plain `raise ValueError("...")` shows the model just "Error executing tool X".
  - compact, model-friendly output instead of raw JSON dumps

Try it (it's already in config/mcp_servers.json):
  1. assistant tools
  2. assistant console --mcp
       "add a task: send the client proposal, due 2026-11-02"
       "what's on my list?"   "mark the proposal one done"
"""

import json
import os
import re
from datetime import date
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

server = MCPServer("tasks", instructions="A personal to-do list. Use list_tasks to find task ids.")
STORE = Path(os.environ.get("TASKS_FILE", "data/tasks.json"))   # relative to the project root
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _load() -> list[dict]:
    return json.loads(STORE.read_text(encoding="utf-8")) if STORE.exists() else []


def _save(tasks: list[dict]) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(tasks, indent=2, ensure_ascii=False), encoding="utf-8")


def _line(t: dict) -> str:
    box = "x" if t["done"] else " "
    due = f" (due {t['due']})" if t.get("due") else ""
    return f"#{t['id']} [{box}] {t['title']}{due}"


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def list_tasks(include_done: bool = False) -> str:
    """List to-do tasks with their ids, newest last. Open tasks only unless include_done is true."""
    tasks = [t for t in _load() if include_done or not t["done"]]
    if not tasks:
        return "No tasks." if include_done else "No open tasks."
    return "\n".join(_line(t) for t in tasks[-50:])   # cap the output: the model pays for every line


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
def add_task(title: str, due: str | None = None) -> str:
    """Add a task. `due` is optional, format YYYY-MM-DD."""
    title = title.strip()
    if not title:
        raise ToolError("Title is empty. Ask the user what the task is.")
    if len(title) > 200:
        raise ToolError("Title is over 200 characters. Shorten it.")
    if due is not None:
        if not DATE_RE.match(due):
            raise ToolError(f"Due date '{due}' must be YYYY-MM-DD, e.g. {date.today().isoformat()}.")
        try:
            date.fromisoformat(due)
        except ValueError:
            raise ToolError(f"'{due}' is not a real date.") from None
    tasks = _load()
    task = {"id": max((t["id"] for t in tasks), default=0) + 1, "title": title, "due": due, "done": False}
    tasks.append(task)
    _save(tasks)
    return f"Added {_line(task)}"


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True))
def complete_task(task_id: int) -> str:
    """Mark a task done by its id (get ids from list_tasks)."""
    tasks = _load()
    for t in tasks:
        if t["id"] == task_id:
            t["done"] = True
            _save(tasks)
            return f"Done: {_line(t)}"
    raise ToolError(f"No task #{task_id}. Call list_tasks to see the ids.")


if __name__ == "__main__":
    server.run("stdio")
