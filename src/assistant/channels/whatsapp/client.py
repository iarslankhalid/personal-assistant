"""
Client for Meta's WhatsApp Agent Platform API (/agent/v1): the four calls this assistant needs.

    GET  /updates   long poll for new messages (polling does NOT consume them; you pass the offset)
    POST /messages  send a message (only to the agent's creator, 12 per minute)
    POST /statuses  read receipt + typing dots (accepted only for the creator's own messages)

Protocol details: docs/whatsapp-platform.md
"""

from __future__ import annotations

import collections
import logging
import time
from typing import Any, Callable

import httpx

API_BASE = "https://api.whatsapp.com/agent/v1"
POLL_TIMEOUT_S = 20        # how long Meta holds a poll open when nothing arrives (Meta's max is 25)
MAX_CHUNK = 4000           # Meta caps a text message at 4096 characters; leave headroom

# Meta's per-agent budgets, rolling 60 s window. Polls get one slot of headroom under Meta's 15.
SEND_LIMIT_PER_MIN = 12
STATUS_LIMIT_PER_MIN = 12
POLL_LIMIT_PER_MIN = 14

# Documented error codes
CODE_INVALID_PARAMETER = 100
CODE_AUTH_HEADER = 190
CODE_RATE_LIMITED = 130429
CODE_NOT_CREATOR = 131005
CODE_POLL_REPLACED = 1752041

log = logging.getLogger("assistant.whatsapp")


# --------------------------------------------------------------------------- errors
class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None, code: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code

    def __str__(self) -> str:
        return f"{self.args[0]} (http={self.status}, code={self.code})"


class AuthFailed(ApiError):
    """The API key is wrong, or was regenerated in WhatsApp."""


class NotCreator(ApiError):
    """Meta says this sender is not the agent's creator. Only the creator can use the agent."""


class PollConflict(ApiError):
    """Another process started polling the same API key. Meta allows one poller per key."""


class RateLimited(ApiError):
    def __init__(self, message: str, retry_after: float | None = None, **kw: Any):
        super().__init__(message, **kw)
        self.retry_after = retry_after


# ----------------------------------------------------------------------- rate limits
class RateWindow:
    """Rolling-window limiter: allows bursts, waits only when the minute's budget is spent."""

    def __init__(self, limit: int, window: float = 60.0,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.limit, self.window = limit, window
        self._clock, self._sleep = clock, sleep
        self._stamps: collections.deque[float] = collections.deque()

    def _prune(self, now: float) -> None:
        while self._stamps and now - self._stamps[0] >= self.window:
            self._stamps.popleft()

    def acquire(self) -> None:
        while True:
            now = self._clock()
            self._prune(now)
            if len(self._stamps) < self.limit:
                self._stamps.append(now)
                return
            self._sleep(self.window - (now - self._stamps[0]) + 0.05)

    def try_acquire(self) -> bool:
        now = self._clock()
        self._prune(now)
        if len(self._stamps) >= self.limit:
            return False
        self._stamps.append(now)
        return True


# ------------------------------------------------------------------- Meta API client
class AgentPlatform:
    """Thin wrapper over the four calls this bridge needs."""

    def __init__(self, api_key: str, *, http: httpx.Client | None = None, base_url: str = API_BASE,
                 sleep: Callable[[float], None] = time.sleep):
        if not api_key:
            raise ValueError("WHATSAPP_AGENT_API_KEY is empty")
        self.base = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.http = http or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        self.send_window = RateWindow(SEND_LIMIT_PER_MIN, sleep=sleep)
        self.status_window = RateWindow(STATUS_LIMIT_PER_MIN, sleep=sleep)
        self.poll_window = RateWindow(POLL_LIMIT_PER_MIN, sleep=sleep)

    # -- error mapping (follows the developer manual's error codes) --
    @staticmethod
    def _error(resp: httpx.Response, action: str) -> ApiError:
        status, code = resp.status_code, None
        try:
            err = resp.json().get("error") or {}
            code = err.get("code") if isinstance(err.get("code"), int) else None
        except (ValueError, AttributeError):
            pass
        kw = {"status": status, "code": code}
        if status == 401 or code == CODE_AUTH_HEADER:
            return AuthFailed(f"{action}: API key missing or malformed", **kw)
        if action == "updates" and status == 400 and code == CODE_INVALID_PARAMETER:
            return AuthFailed(f"{action}: API key rejected", **kw)
        if status == 403 or code == CODE_NOT_CREATOR:
            return NotCreator(f"{action}: sender is not this agent's creator", **kw)
        if action == "updates" and (status == 409 or code == CODE_POLL_REPLACED):
            return PollConflict(f"{action}: another process is polling this API key", **kw)
        if status == 429 or code == CODE_RATE_LIMITED:
            raw = resp.headers.get("Retry-After")
            try:
                retry_after = float(raw) if raw is not None else None
            except ValueError:
                retry_after = None
            return RateLimited(f"{action}: rate limited", retry_after=retry_after, **kw)
        return ApiError(f"{action}: request failed", **kw)

    def get_updates(self, offset: int | None, *, timeout: int = POLL_TIMEOUT_S,
                    limit: int = 50) -> tuple[int | None, list[dict[str, Any]]]:
        """Long-poll. Returns (next_offset, messages). next_offset is None when nothing arrived (HTTP 204).

        Polling does not consume messages: you advance by passing next_offset back on the next call.
        """
        params: dict[str, int] = {"timeout": timeout, "limit": limit}
        if offset is not None:
            params["offset"] = offset
        self.poll_window.acquire()
        resp = self.http.get(f"{self.base}/updates", headers=self.headers, params=params,
                             timeout=httpx.Timeout(timeout + 15.0, connect=10.0))
        if resp.status_code == 204:
            return None, []
        if resp.status_code >= 400:
            raise self._error(resp, "updates")
        body = resp.json()
        next_offset = body.get("next_offset")
        if body.get("object") != "whatsapp_agent_platform" or not isinstance(next_offset, int):
            raise ApiError("updates: unexpected response shape", status=resp.status_code)
        messages: list[dict[str, Any]] = []
        for entry in body.get("entry") or []:
            for change in (entry or {}).get("changes") or []:
                if (change or {}).get("field") != "messages":
                    continue
                messages.extend(m for m in (change.get("value") or {}).get("messages") or []
                                if isinstance(m, dict))
        return next_offset, messages

    def send_text(self, to: str, body: str, *, reply_to: str | None = None) -> str:
        payload: dict[str, Any] = {"messaging_product": "whatsapp", "to": to,
                                   "type": "text", "text": {"body": body}}
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        self.send_window.acquire()
        resp = self.http.post(f"{self.base}/messages", headers=self.headers, json=payload)
        if resp.status_code >= 400:
            raise self._error(resp, "send")
        return resp.json()["messages"][0]["id"]

    def mark_read(self, message_id: str, *, typing: bool = True) -> None:
        """Read receipt (+ typing dots for ~25 s). Meta only accepts this for the creator's own
        messages, which makes it a reliable "is this the creator?" check."""
        if not self.status_window.try_acquire():
            raise RateLimited("statuses: local budget spent")
        payload: dict[str, Any] = {"messaging_product": "whatsapp", "status": "read",
                                   "message_id": message_id}
        if typing:
            payload["typing_indicator"] = {"type": "text"}
        resp = self.http.post(f"{self.base}/statuses", headers=self.headers, json=payload)
        if resp.status_code >= 400:
            raise self._error(resp, "statuses")


# ----------------------------------------------------------------------------- helpers
def split_text(text: str, limit: int = MAX_CHUNK) -> list[str]:
    """Split a long reply on paragraph, then line, then word boundaries."""
    chunks: list[str] = []
    rest = text.strip()
    while len(rest) > limit:
        cut = -1
        for sep in ("\n\n", "\n", " "):
            cut = rest.rfind(sep, 0, limit)
            if cut > limit // 2:
                break
        if cut <= 0:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return chunks


