"""
MCP client hub: starts MCP servers and hands their tools to plain (synchronous) code.

How MCP works here
------------------

  brain ──► MCPHub ──JSON-RPC over stdin/stdout──► MCP server process ──► Notion API
  (sync)     (this file)                            (npx @notionhq/...)

An MCP *server* is just a program that answers three questions over stdin/stdout:
    initialize  -> "who are you, what can you do?"
    tools/list  -> "which tools do you have, and what arguments do they take?"
    tools/call  -> "run this tool with these arguments"

The *client* (this file) starts that program, asks those questions, and gives the tool list
to the LLM. The LLM never talks to MCP itself. It replies "please call notion__API-post-search
with {query: ...}", and we forward that call to the server and hand the result back.

Analogy: the LLM is a manager who can only write requests on sticky notes. MCP servers are
departments. This hub is the office runner: it knows which department handles which request,
walks the note over, and brings the answer back.

Why a background thread: the MCP Python SDK is async; the rest of this bot is simple sync code.
The hub runs its own event loop in a thread, and the sync methods (`call`, `tools`) pass work to it.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import sys
import threading
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import PaginatedRequestParams

try:
    _ExceptionGroup = BaseExceptionGroup                                  # Python 3.11+
except NameError:                                                         # Python 3.10
    from exceptiongroup import BaseExceptionGroup as _ExceptionGroup     # installed with anyio

log = logging.getLogger("assistant.mcp")

TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")   # what both Anthropic and OpenAI accept
SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")
SEPARATOR = "__"                                        # public tool name = <server>__<tool>
ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class MCPConfigError(Exception):
    pass


@dataclass
class ServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    allowed_tools: list[str] | None = None   # None = expose every tool the server offers
    cwd: str | None = None                   # working directory for the server process


@dataclass
class ToolSpec:
    public_name: str          # what the LLM sees, e.g. "notion__API-post-search"
    server: str               # "notion"
    name: str                 # the server's own name, e.g. "API-post-search"
    description: str
    schema: dict[str, Any]    # JSON Schema for the arguments
    read_only: bool | None    # the server's own hint; None = it didn't say
    raw_schema_chars: int = 0 # size before prune_defs, for --list-tools


@dataclass
class ToolResult:
    text: str
    is_error: bool = False


# ----------------------------------------------------------------------------- config
def load_config(path: str | os.PathLike, environ: dict[str, str] | None = None,
                cwd: str | os.PathLike | None = None) -> list[ServerConfig]:
    """Read mcp_servers.json (same shape as Claude Desktop / Cursor configs, plus two extras:
    "enabled" and "allowed_tools"). ${VAR} in env values is filled from .env / the environment,
    so secrets stay out of the JSON file. A server whose ${VAR} isn't set is skipped, not fatal.
    Servers run with `cwd` as their working directory, so relative paths in "args" resolve there."""
    environ = dict(os.environ) if environ is None else environ
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise MCPConfigError(f"{path} not found") from None
    except json.JSONDecodeError as exc:
        raise MCPConfigError(f"{path} is not valid JSON: {exc}") from None

    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        raise MCPConfigError(f'{path} needs a top-level "mcpServers" object')

    configs: list[ServerConfig] = []
    for name, raw in servers.items():
        if not isinstance(raw, dict) or raw.get("enabled", True) is False:
            continue
        if not SERVER_NAME_RE.match(name):
            raise MCPConfigError(f"server name {name!r}: use letters, digits, - or _ (max 20)")
        command = raw.get("command")
        if not command:
            raise MCPConfigError(f"server {name!r} has no command")
        if command in ("python", "python3"):
            command = sys.executable   # same interpreter (and venv) as the bot

        env: dict[str, str] = {}
        missing: list[str] = []
        for key, value in (raw.get("env") or {}).items():
            def fill(m: re.Match[str]) -> str:
                var = m.group(1)
                if not environ.get(var):
                    missing.append(var)
                    return ""
                return environ[var]
            env[key] = ENV_REF_RE.sub(fill, str(value))
        if missing:
            log.warning("MCP server '%s' skipped: set %s in .env", name, ", ".join(sorted(set(missing))))
            continue

        allowed = raw.get("allowed_tools")
        if allowed is not None and not (isinstance(allowed, list) and all(isinstance(t, str) for t in allowed)):
            raise MCPConfigError(f"server {name!r}: allowed_tools must be a list of tool names")
        configs.append(ServerConfig(name, command, [str(a) for a in raw.get("args") or []], env, allowed,
                                    str(cwd) if cwd is not None else None))
    return configs


# ----------------------------------------------------------------------------- helpers
def prune_defs(schema: dict[str, Any]) -> dict[str, Any]:
    """Drop $defs entries a schema never references.

    Some servers (Notion's included) paste one big shared $defs block into every tool. Tool
    definitions are re-sent to the LLM on every request, so unused definitions are paid for
    over and over. This keeps only what the tool's own properties reference, directly or not."""
    defs = schema.get("$defs")
    if not isinstance(defs, dict):
        return schema

    def refs(node: Any) -> set[str]:
        found: set[str] = set()
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                found.add(ref.split("/", 2)[2])
            for value in node.values():
                found |= refs(value)
        elif isinstance(node, list):
            for value in node:
                found |= refs(value)
        return found

    body = {k: v for k, v in schema.items() if k != "$defs"}
    keep: set[str] = set()
    todo = refs(body)
    while todo:
        name = todo.pop()
        if name in keep or name not in defs:
            continue
        keep.add(name)
        todo |= refs(defs[name])
    if keep:
        body["$defs"] = {k: defs[k] for k in defs if k in keep}
    return body


def result_to_text(result: Any, max_chars: int) -> ToolResult:
    """Flatten an MCP CallToolResult into text the LLM can read."""
    content = getattr(result, "content", None)
    if content is None:
        # e.g. a result type asking for interactive input, which a chat bot can't provide
        return ToolResult(f"Tool returned an unsupported result ({type(result).__name__}).", True)
    parts: list[str] = []
    for block in content:
        kind = getattr(block, "type", "")
        if kind == "text":
            parts.append(block.text)
        elif kind == "resource" and getattr(getattr(block, "resource", None), "text", None):
            parts.append(block.resource.text)
        else:
            parts.append(f"[{kind or type(block).__name__} content omitted]")
    text = "\n".join(parts).strip()
    structured = getattr(result, "structured_content", None)
    if not text and structured is not None:
        text = json.dumps(structured, ensure_ascii=False)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n[truncated: {len(text) - max_chars} more characters]"
    return ToolResult(text or "(empty result)", bool(getattr(result, "is_error", False)))


def describe(exc: BaseException) -> str:
    """Readable one-liner, unwrapping the ExceptionGroups that anyio raises."""
    while isinstance(exc, _ExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    msg = str(exc).strip() or type(exc).__name__
    return f"{type(exc).__name__}: {msg}"[:400]


# ------------------------------------------------------------------------------- hub
class MCPHub:
    def __init__(self, servers: list[ServerConfig], *, startup_timeout: float = 120.0,
                 call_timeout: float = 60.0, max_result_chars: int = 15_000,
                 log_dir: str | os.PathLike = "logs"):
        self.configs = servers
        self.startup_timeout = startup_timeout
        self.call_timeout = call_timeout
        self.max_result_chars = max_result_chars
        self.log_dir = Path(log_dir)
        self.failed: dict[str, str] = {}             # server -> why it didn't start
        self._sessions: dict[str, ClientSession] = {}
        self._alive: dict[str, bool] = {}
        self._tools: dict[str, ToolSpec] = {}        # public name -> spec
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._thread: threading.Thread | None = None
        self._fatal: BaseException | None = None

    # -- lifecycle --
    def start(self) -> "MCPHub":
        if not self.configs:
            self._ready.set()
            return self
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._thread_main, name="mcp-hub", daemon=True)
        self._thread.start()
        if not self._ready.wait(self.startup_timeout + 10):
            raise TimeoutError("MCP servers did not start in time")
        if self._fatal is not None:
            raise RuntimeError(f"MCP hub crashed: {describe(self._fatal)}")
        return self

    def close(self) -> None:
        if self._loop is not None and self._stop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None:
            self._thread.join(timeout=15)

    def __enter__(self) -> "MCPHub":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- what the brains use --
    @property
    def tools(self) -> list[ToolSpec]:
        return [t for t in self._tools.values() if self._alive.get(t.server)]

    def call(self, public_name: str, arguments: dict[str, Any] | None) -> ToolResult:
        spec = self._tools.get(public_name)
        if spec is None:
            # Also what a model gets if it invents a tool, or tries one that isn't allowlisted.
            return ToolResult(f"Unknown or disabled tool: {public_name}", True)
        if not self._alive.get(spec.server) or self._loop is None:
            return ToolResult(f"MCP server '{spec.server}' is not running.", True)
        future = asyncio.run_coroutine_threadsafe(self._call(spec, arguments or {}), self._loop)
        try:
            return future.result(timeout=self.call_timeout + 5)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return ToolResult(f"{public_name} timed out after {self.call_timeout:.0f}s.", True)
        except Exception as exc:
            # Some servers report failures as protocol errors (an exception) rather than an
            # is_error result. Either way the model gets a readable error, not a crash.
            return ToolResult(f"{public_name} failed: {describe(exc)}", True)

    # -- inside the hub's own thread / event loop --
    def _thread_main(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as exc:   # noqa: BLE001 - report anything that kills the loop
            self._fatal = exc
            for name in self._alive:
                self._alive[name] = False
            self._ready.set()

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        started: dict[str, asyncio.Future[None]] = {}
        tasks = []
        for cfg in self.configs:
            started[cfg.name] = self._loop.create_future()
            tasks.append(asyncio.create_task(self._run_server(cfg, started[cfg.name]),
                                             name=f"mcp-{cfg.name}"))
        for name, fut in started.items():
            try:
                await fut
            except Exception as exc:
                self.failed[name] = describe(exc)
                log.error("MCP server '%s' failed to start: %s (details: %s)", name, describe(exc),
                          self.log_dir / f"mcp-{name}.log")
        self._ready.set()
        await self._stop.wait()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_server(self, cfg: ServerConfig, started: asyncio.Future[None]) -> None:
        """Each server gets its own task, so one crashing doesn't take the others down."""
        try:
            async with AsyncExitStack() as stack:
                # The server's stderr (its own logs, npm noise) goes to a file, not your console.
                errlog = stack.enter_context(open(self.log_dir / f"mcp-{cfg.name}.log", "a", encoding="utf-8"))
                params = StdioServerParameters(command=cfg.command, args=cfg.args, env=cfg.env or None,
                                              cwd=cfg.cwd)
                log.info("Starting MCP server '%s': %s %s", cfg.name, Path(cfg.command).name, " ".join(cfg.args))
                read, write = await stack.enter_async_context(stdio_client(params, errlog=errlog))
                session = await stack.enter_async_context(ClientSession(read, write))
                with anyio.fail_after(self.startup_timeout):
                    init = await session.initialize()                  # step 1: handshake
                    listed = await self._list_all_tools(session)       # step 2: discover tools
                self._register(cfg, session, listed)
                info = getattr(init, "server_info", None)
                log.info("MCP server '%s' ready (%s %s): %d of %d tools enabled", cfg.name,
                         getattr(info, "name", "?"), getattr(info, "version", ""),
                         sum(1 for t in self._tools.values() if t.server == cfg.name), len(listed))
                started.set_result(None)
                await self._stop.wait()                                # stay connected until close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not started.done():
                started.set_exception(exc)
            else:
                log.error("MCP server '%s' stopped: %s", cfg.name, describe(exc))
        finally:
            self._alive[cfg.name] = False
            if not started.done():
                started.set_exception(RuntimeError("server task ended during startup"))

    @staticmethod
    async def _list_all_tools(session: ClientSession) -> list[Any]:
        tools, cursor = [], None
        while True:
            params = PaginatedRequestParams(cursor=cursor) if cursor else None
            page = await session.list_tools(params=params)
            tools.extend(page.tools)
            cursor = getattr(page, "next_cursor", None)
            if not cursor:
                return tools

    def _register(self, cfg: ServerConfig, session: ClientSession, listed: list[Any]) -> None:
        offered = {t.name for t in listed}
        if cfg.allowed_tools is not None:
            for name in sorted(set(cfg.allowed_tools) - offered):
                log.warning("MCP server '%s' has no tool named '%s' (typo in allowed_tools?)", cfg.name, name)
        else:
            log.warning("MCP server '%s' has no allowed_tools list: ALL its tools are enabled", cfg.name)
        for tool in listed:
            if cfg.allowed_tools is not None and tool.name not in cfg.allowed_tools:
                continue
            public = f"{cfg.name}{SEPARATOR}{tool.name}"
            if not TOOL_NAME_RE.match(public):
                public = re.sub(r"[^A-Za-z0-9_-]", "_", public)[:64]
            hints = getattr(tool, "annotations", None)
            raw = dict(tool.input_schema or {"type": "object", "properties": {}})
            self._tools[public] = ToolSpec(
                public_name=public, server=cfg.name, name=tool.name,
                description=(tool.description or tool.name)[:1024],
                schema=prune_defs(raw),
                read_only=getattr(hints, "read_only_hint", None) if hints else None,
                raw_schema_chars=len(json.dumps(raw)),
            )
        self._sessions[cfg.name] = session
        self._alive[cfg.name] = True

    async def _call(self, spec: ToolSpec, arguments: dict[str, Any]) -> ToolResult:
        session = self._sessions[spec.server]
        with anyio.fail_after(self.call_timeout):
            result = await session.call_tool(spec.name, arguments)    # step 3: run the tool
        return result_to_text(result, self.max_result_chars)
