"""
Text yourself on WhatsApp from anywhere: a script, a scheduled job, the end of a long command.

    assistant notify "Backup finished"
    assistant watch -- python train.py          # runs it, then texts you how it went

    from assistant.notify import notify          # or from your own Python code
    notify("Scraper done: 412 rows")

This works while the bot is running too: sending isn't polling, so it doesn't trip Meta's
"one poller per key" rule. It does share the agent's 12-messages-a-minute budget.

It needs your WhatsApp id, which the bot saves to data/state.json the first time you message
the agent (`assistant run`, then say anything).
"""

from __future__ import annotations

import collections
import logging
import subprocess
import time
from typing import Callable

from assistant.channels.whatsapp.client import AgentPlatform, split_text
from assistant.config import STATE_FILE, env
from assistant.state import State

log = logging.getLogger("assistant.notify")


class NotifyError(Exception):
    """Something the user has to fix before notifications can work."""


def notify(text: str, *, platform: AgentPlatform | None = None, state: State | None = None) -> list[str]:
    """Send `text` to you on WhatsApp. Returns the sent message ids."""
    text = text.strip()
    if not text:
        raise NotifyError("Nothing to send.")
    state = state or State(STATE_FILE)
    to = state.creator
    if not to:
        raise NotifyError("I don't know your WhatsApp id yet. Run `assistant run`, send your agent any "
                          "message once, then try again.")
    if platform is None:
        key = env("WHATSAPP_AGENT_API_KEY")
        if not key:
            raise NotifyError("Set WHATSAPP_AGENT_API_KEY in .env.")
        platform = AgentPlatform(key)
    return [platform.send_text(to, chunk) for chunk in split_text(text)]


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def watch(command: list[str], *, send: Callable[[str], object] = notify, label: str | None = None,
          tail_lines: int = 15) -> int:
    """Run `command`, show its output live, then text you whether it worked and how long it took.

    One argument is run through the shell (so `"npm run build && npm test"` works); several
    arguments are run directly. Returns the command's exit code.
    """
    if not command:
        raise NotifyError("No command given. Example: assistant watch -- python train.py")
    shell = len(command) == 1
    name = label or (command[0] if shell else " ".join(command))
    tail: collections.deque[str] = collections.deque(maxlen=tail_lines)
    started = time.monotonic()
    interrupted = False
    try:
        proc = subprocess.Popen(command[0] if shell else command, shell=shell, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                bufsize=1)
    except OSError as exc:
        code = 127
        tail.append(f"could not start: {exc}")
    else:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                print(line, end="", flush=True)
                tail.append(line.rstrip())
            code = proc.wait()
        except KeyboardInterrupt:
            interrupted = True
            proc.terminate()
            code = proc.wait()

    took = format_duration(time.monotonic() - started)
    if interrupted:
        message = f"⏹️ *{name[:100]}*\nstopped by you after {took}"
    elif code == 0:
        message = f"✅ *{name[:100]}*\nfinished after {took}"
    else:
        message = f"❌ *{name[:100]}*\nfailed (exit {code}) after {took}"
        if tail:
            message += "\n```\n" + "\n".join(tail)[-1500:] + "\n```"
    try:
        send(message)
    except Exception as exc:   # the command's result matters more than the notification
        log.error("Couldn't send the WhatsApp notification: %s", exc)
    return code
