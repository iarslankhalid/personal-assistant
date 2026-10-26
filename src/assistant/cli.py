"""
The `assistant` command.

    assistant run [--mcp] [--provider P] [--echo]   the WhatsApp assistant
    assistant console [--mcp] [--provider P]        chat in the terminal (no WhatsApp needed)
    assistant tools                                 start the MCP servers, list tools and their token cost
    assistant notify TEXT                           send yourself a WhatsApp message
    assistant watch -- COMMAND ...                  run a command, text you when it ends

Add --debug to any of them for verbose logs.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

import httpx

from assistant import config
from assistant.brains import DEFAULT_SYSTEM_PROMPT, PROVIDERS, Brain, EchoBrain, pick_provider
from assistant.channels.whatsapp import AgentPlatform, ApiError, AuthFailed, Bridge, PollConflict
from assistant.config import env
from assistant.state import State

log = logging.getLogger("assistant")


# ------------------------------------------------------------------------------ building blocks
def build_hub(path: str) -> Any:
    from assistant.mcp_client import MCPHub, load_config   # only needed when MCP is used
    configs = load_config(path, cwd=config.ROOT)
    if not configs:
        log.warning("No MCP servers enabled in %s", path)
    else:
        log.info("Starting %d MCP server(s). The first npx run downloads packages; give it a minute.",
                 len(configs))
    return MCPHub(configs, log_dir=config.LOG_DIR).start()


def build_brain(args: argparse.Namespace, hub: Any) -> Brain | None:
    if getattr(args, "echo", False):
        log.info("Mode: echo")
        return EchoBrain()
    requested = args.provider or env("LLM_PROVIDER").lower() or None
    if requested and requested not in PROVIDERS:
        log.error("LLM_PROVIDER must be one of: %s", ", ".join(sorted(PROVIDERS)))
        return None
    provider = pick_provider(requested, dict(os.environ))
    brain_cls, key_var, model_var, default_model = PROVIDERS[provider]
    if not env(key_var):
        log.error("Set %s in .env for %s mode.", key_var, provider)
        return None
    model = env(model_var) or default_model
    toolbox = hub if hub is not None and hub.tools else None
    if args.mcp and toolbox is None:
        log.warning("--mcp: no MCP tools are available, continuing without tools")
    brain = brain_cls(model, env("SYSTEM_PROMPT") or DEFAULT_SYSTEM_PROMPT, toolbox=toolbox)
    log.info("Mode: %s (%s)%s", brain_cls.label, model, f" + {len(toolbox.tools)} MCP tools" if toolbox else "")
    return brain


# ------------------------------------------------------------------------------------- commands
def cmd_tools(args: argparse.Namespace) -> int:
    hub = build_hub(args.mcp_config)
    try:
        for name, why in hub.failed.items():
            print(f"! {name}: failed to start: {why}")
        tools = sorted(hub.tools, key=lambda t: t.public_name)
        if not tools:
            print("No tools available.")
            return 0
        server = None
        for t in tools:
            if t.server != server:
                server = t.server
                print(f"\n[{server}]")
            kind = {True: "read-only", False: "WRITES", None: "unmarked"}[t.read_only]
            first_line = (t.description.strip().splitlines() or [""])[0][:70]
            print(f"  {t.name:32} {kind:9}  {first_line}")
        sent = len(json.dumps([{"name": t.public_name, "description": t.description, "input_schema": t.schema}
                               for t in tools]))
        before = sent + sum(t.raw_schema_chars - len(json.dumps(t.schema)) for t in tools)
        print(f"\n{len(tools)} tools. Their definitions go to the model with EVERY message: "
              f"~{sent // 4:,} tokens (~{before // 4:,} before unused $defs were pruned).")
        return 0
    finally:
        hub.close()


def cmd_console(args: argparse.Namespace) -> int:
    hub = build_hub(args.mcp_config) if args.mcp else None
    try:
        brain = build_brain(args, hub)
        if brain is None:
            return 1
        print("Console chat (no WhatsApp). /new resets, /quit exits.\n")
        while True:
            try:
                text = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if text in ("/quit", "/exit"):
                return 0
            if not text:
                continue
            try:
                print(f"bot> {brain.reply('console', text)}\n")
            except Exception as exc:
                log.exception("Brain failed")
                print(f"error: {exc}\n")
    finally:
        if hub is not None:
            hub.close()


def cmd_run(args: argparse.Namespace) -> int:
    key = env("WHATSAPP_AGENT_API_KEY")
    if not key:
        log.error("Set WHATSAPP_AGENT_API_KEY in .env (WhatsApp > your agent's chat > Chat info > API key).")
        return 1
    if args.mcp and args.echo:
        log.warning("--mcp ignored: echo mode doesn't call an LLM")
    hub = build_hub(args.mcp_config) if args.mcp and not args.echo else None
    try:
        brain = build_brain(args, hub)
        if brain is None:
            return 1
        bridge = Bridge(AgentPlatform(key), brain, state=State(config.STATE_FILE))
        try:
            bridge.check_connection()
        except AuthFailed as exc:
            log.error("Meta rejected the API key (%s). Copy it again from WhatsApp: agent chat > Chat info > "
                      "API key. Regenerating the key or reinstalling WhatsApp invalidates the old one.", exc)
            return 1
        except PollConflict:
            log.error("Another process is already polling this key. Stop it first (one poller per key).")
            return 1
        except (ApiError, httpx.HTTPError) as exc:
            log.error("Could not reach the WhatsApp Agent Platform: %s", exc)
            return 1

        log.info("Connected. Send your agent a message on WhatsApp. Ctrl+C to stop.")
        try:
            bridge.run_forever()
        except AuthFailed as exc:
            log.error("API key stopped working (%s). Was it regenerated in WhatsApp?", exc)
            return 1
        except PollConflict:
            log.error("Another process keeps polling this key (409 three times in 10 min). Stopping.")
            return 1
        except KeyboardInterrupt:
            log.info("Stopped.")
        return 0
    finally:
        if hub is not None:
            hub.close()


def cmd_notify(args: argparse.Namespace) -> int:
    from assistant.notify import NotifyError, notify
    try:
        notify(" ".join(args.text))
    except NotifyError as exc:
        log.error("%s", exc)
        return 1
    except (ApiError, httpx.HTTPError) as exc:
        log.error("WhatsApp refused the message: %s", exc)
        return 1
    log.info("Sent.")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    from assistant.notify import NotifyError, watch
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        return watch(command, label=args.label)
    except NotifyError as exc:
        log.error("%s", exc)
        return 2


# ----------------------------------------------------------------------------------------- main
def make_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--debug", action="store_true", help="verbose logs")

    llm = argparse.ArgumentParser(add_help=False)
    llm.add_argument("--provider", choices=sorted(PROVIDERS),
                     help="which LLM answers (default: LLM_PROVIDER in .env, else whichever key is set)")
    llm.add_argument("--mcp", action="store_true", help="give the LLM the tools from the MCP config")
    llm.add_argument("--mcp-config", default=str(config.MCP_CONFIG), help="MCP config file")

    parser = argparse.ArgumentParser(prog="assistant", description="Your personal assistant on WhatsApp.")
    sub = parser.add_subparsers(dest="command_name", required=True, metavar="COMMAND")

    p = sub.add_parser("run", parents=[common, llm], help="run the WhatsApp assistant")
    p.add_argument("--echo", action="store_true", help="reply with an echo instead of calling an LLM")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("console", parents=[common, llm], help="chat in the terminal (no WhatsApp)")
    p.set_defaults(func=cmd_console)

    p = sub.add_parser("tools", parents=[common], help="list MCP tools and their token cost")
    p.add_argument("--mcp-config", default=str(config.MCP_CONFIG), help="MCP config file")
    p.set_defaults(func=cmd_tools)

    p = sub.add_parser("notify", parents=[common], help="send yourself a WhatsApp message")
    p.add_argument("text", nargs="+", help="the message")
    p.set_defaults(func=cmd_notify)

    p = sub.add_parser("watch", parents=[common], help="run a command, text you when it ends",
                       description='Examples:  assistant watch -- python train.py   |   '
                                   'assistant watch "npm run build && npm test"')
    p.add_argument("--label", help="name to use in the message (default: the command)")
    p.add_argument("command", nargs=argparse.REMAINDER, help="the command to run")
    p.set_defaults(func=cmd_watch)
    return parser


def setup_logging(debug: bool) -> None:
    for stream in (sys.stdout, sys.stderr):   # Windows consoles choke on emoji otherwise
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "mcp", "anthropic", "openai"):
        logging.getLogger(noisy).setLevel(logging.INFO if debug else logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    setup_logging(args.debug)
    config.load_env()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
