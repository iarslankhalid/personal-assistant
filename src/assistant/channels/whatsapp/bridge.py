"""
The WhatsApp side of the assistant: poll for messages, check the sender is you, ask the brain,
send the answer back.

    WhatsApp chat --> Meta --> GET  /updates (long poll) --> Bridge --> brain
    WhatsApp chat <-- Meta <-- POST /messages            <-- Bridge <--
"""

from __future__ import annotations

import collections
import logging
import time
from typing import Any, Callable

import httpx

from assistant.brains import Brain
from assistant.channels.whatsapp.client import (AgentPlatform, ApiError, AuthFailed, NotCreator,
                                                PollConflict, RateLimited, split_text)
from assistant.state import State

BACKLOG_GRACE_S = 5   # messages older than bridge start (minus this) are skipped

log = logging.getLogger("assistant.whatsapp")


# ------------------------------------------------------------------------------ bridge
class Bridge:
    def __init__(self, platform: AgentPlatform, brain: Brain, *, state: State | None = None,
                 started_at: float | None = None, sleep: Callable[[float], None] = time.sleep):
        self.platform, self.brain, self.sleep = platform, brain, sleep
        self.state = state
        self.started_at = time.time() if started_at is None else started_at
        self.offset: int | None = None
        self._creator: str | None = state.creator if state else None
        self.non_creators: set[str] = set()
        self.seen: collections.deque[str] = collections.deque(maxlen=500)
        self.conflicts: collections.deque[float] = collections.deque()

    @property
    def creator(self) -> str | None:
        """Your WhatsApp id, as confirmed by Meta. Saved to data/state.json for `assistant notify`."""
        return self._creator

    @creator.setter
    def creator(self, value: str | None) -> None:
        self._creator = value
        if self.state is not None:
            self.state.creator = value

    def check_connection(self) -> None:
        """Instant poll to validate the key. Doesn't consume anything."""
        self.platform.get_updates(None, timeout=0, limit=1)

    def poll_once(self) -> int:
        """One long poll. Returns how many messages were handled."""
        next_offset, messages = self.platform.get_updates(self.offset)
        if next_offset is None:
            return 0
        handled = 0
        for msg in messages:
            try:
                if self.handle(msg):
                    handled += 1
            except (AuthFailed, PollConflict):
                raise
            except Exception:
                log.exception("Failed to handle message %s; skipping it", msg.get("id"))
        self.offset = next_offset
        return handled

    def _authorize(self, sender: str, wamid: str) -> bool:
        if sender == self.creator:
            try:
                self.platform.mark_read(wamid)  # blue ticks + typing dots; failures don't matter
            except ApiError:
                pass
            return True
        if sender in self.non_creators:
            return False
        try:
            self.platform.mark_read(wamid)  # succeeds only for the creator
        except NotCreator:
            self.non_creators.add(sender)
            log.warning("Ignored a message from %s: Meta says they are not this agent's creator", sender)
            return False
        is_new = self.creator is None
        self.creator = sender
        log.info("Creator confirmed by Meta: %s%s", sender,
                 " (saved: `assistant notify` can now reach you)" if is_new else "")
        return True

    def handle(self, msg: dict[str, Any]) -> bool:
        wamid, sender, mtype = msg.get("id"), msg.get("from"), msg.get("type")
        if not wamid or not sender or wamid in self.seen:
            return False
        self.seen.append(wamid)

        try:
            ts = float(msg.get("timestamp", "0"))
        except (TypeError, ValueError):
            ts = 0.0
        if ts and ts < self.started_at - BACKLOG_GRACE_S:
            log.debug("Skipping message %s sent before this script started", wamid)
            return False

        if not self._authorize(sender, wamid):
            return False

        if mtype != "text":
            self.platform.send_text(sender, f"I can only read text for now (got: {mtype}).", reply_to=wamid)
            return True

        text = ((msg.get("text") or {}).get("body") or "").strip()
        if not text:
            return False
        log.info("<- %s", text[:200])
        try:
            answer = self.brain.reply(sender, text)
        except Exception as exc:
            log.exception("Brain failed")
            answer = f"Sorry, something went wrong on my side ({type(exc).__name__})."
        chunks = split_text(answer)
        for i, chunk in enumerate(chunks):
            self.platform.send_text(sender, chunk, reply_to=wamid if i == 0 else None)
        log.info("-> %s", answer[:200].replace("\n", " "))
        return True

    def run_forever(self) -> None:
        failures = 0
        while True:
            try:
                self.poll_once()
                failures = 0
            except AuthFailed:
                raise
            except PollConflict:
                now = time.monotonic()
                self.conflicts.append(now)
                while self.conflicts and now - self.conflicts[0] > 600:
                    self.conflicts.popleft()
                if len(self.conflicts) >= 3:
                    raise
                log.warning("Another process polled this API key (HTTP 409). Pausing 60 s. "
                            "Only one script per key is allowed.")
                self.sleep(60)
            except RateLimited as exc:
                wait = exc.retry_after or 10
                log.warning("Rate limited by Meta; waiting %.0f s", wait)
                self.sleep(wait)
            except (ApiError, httpx.HTTPError, ValueError) as exc:
                failures += 1
                wait = min(60, 2 ** min(failures, 6))
                log.warning("Poll failed (%s); retrying in %d s", exc, wait)
                self.sleep(wait)


