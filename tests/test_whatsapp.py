"""WhatsApp channel tests against a fake Meta API (no network, no keys)."""

import json
import time

import httpx
import pytest

from assistant.brains import EchoBrain
from assistant.channels.whatsapp import (AgentPlatform, AuthFailed, Bridge, PollConflict,
                                         split_text)
from assistant.state import State

CREATOR = "user:111"
STRANGER = "user:999"


class FakeMeta:
    """Just enough of /agent/v1 to drive the bridge."""

    def __init__(self, pages=None, creator=CREATOR):
        self.pages = list(pages or [])   # each: ("json", body) or (status, body)
        self.creator = creator
        self.sent = []                    # bodies POSTed to /messages
        self.statuses = []                # bodies POSTed to /statuses
        self.poll_params = []
        self.wamid_owner = {}             # inbound wamid -> sender

    def page(self, *messages, next_offset):
        for m in messages:
            self.wamid_owner[m["id"]] = m["from"]
        body = {"object": "whatsapp_agent_platform", "next_offset": next_offset,
                "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
                    "messaging_product": "whatsapp", "messages": list(messages), "statuses": []}}]}]}
        self.pages.append((200, body))

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        path = request.url.path
        if path.endswith("/updates"):
            self.poll_params.append(dict(request.url.params))
            if not self.pages:
                return httpx.Response(204)
            status, body = self.pages.pop(0)
            return httpx.Response(status, json=body)
        body = json.loads(request.content)
        if path.endswith("/statuses"):
            self.statuses.append(body)
            if self.wamid_owner.get(body["message_id"]) != self.creator:
                return httpx.Response(403, json={"error": {"code": 131005, "message": "not creator"}})
            return httpx.Response(200, json={"success": True})
        if path.endswith("/messages"):
            if body["to"] != self.creator:
                return httpx.Response(403, json={"error": {"code": 131005}})
            self.sent.append(body)
            return httpx.Response(200, json={"messaging_product": "whatsapp",
                                             "messages": [{"id": f"wamid.out{len(self.sent)}"}]})
        return httpx.Response(404)


def text_msg(wamid, sender, body, ts=None):
    return {"from": sender, "id": wamid, "timestamp": str(int(ts or time.time())),
            "type": "text", "text": {"body": body}}


def make(fake, brain=None):
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    platform = AgentPlatform("test-key", http=http, base_url="https://fake.test/agent/v1",
                                 sleep=lambda s: None)
    return Bridge(platform, brain or EchoBrain(), sleep=lambda s: None)


def test_echo_round_trip_and_offset_advances():
    fake = FakeMeta()
    fake.page(text_msg("wamid.1", CREATOR, "Hello agent"), next_offset=42)
    bridge = make(fake)

    assert bridge.poll_once() == 1
    assert fake.sent == [{"messaging_product": "whatsapp", "to": CREATOR, "type": "text",
                          "text": {"body": "Echo: Hello agent"},
                          "context": {"message_id": "wamid.1"}}]
    assert fake.statuses[0]["typing_indicator"] == {"type": "text"}
    assert bridge.creator == CREATOR
    assert bridge.offset == 42

    bridge.poll_once()  # 204: nothing new
    assert fake.poll_params[-1]["offset"] == "42"
    assert bridge.offset == 42


def test_stranger_is_ignored():
    fake = FakeMeta()
    fake.page(text_msg("wamid.s", STRANGER, "let me in"), next_offset=5)
    bridge = make(fake)
    assert bridge.poll_once() == 0
    assert fake.sent == []
    assert STRANGER in bridge.non_creators


def test_duplicate_and_backlog_messages_skipped():
    fake = FakeMeta()
    old = text_msg("wamid.old", CREATOR, "from yesterday", ts=time.time() - 86400)
    new = text_msg("wamid.new", CREATOR, "hi")
    fake.page(old, new, new, next_offset=7)
    bridge = make(fake)
    assert bridge.poll_once() == 1
    assert [m["text"]["body"] for m in fake.sent] == ["Echo: hi"]


def test_non_text_gets_a_notice():
    fake = FakeMeta()
    msg = {"from": CREATOR, "id": "wamid.img", "timestamp": str(int(time.time())), "type": "image",
           "image": {"id": "m1"}}
    fake.page(msg, next_offset=3)
    bridge = make(fake)
    bridge.poll_once()
    assert "only read text" in fake.sent[0]["text"]["body"]


def test_long_reply_is_split_under_meta_limit():
    class Long:
        def reply(self, sender, text):
            return ("word " * 3000).strip()  # ~15k chars

    fake = FakeMeta()
    fake.page(text_msg("wamid.l", CREATOR, "essay please"), next_offset=9)
    bridge = make(fake, Long())
    bridge.poll_once()
    assert len(fake.sent) >= 4
    assert all(len(m["text"]["body"]) <= 4096 for m in fake.sent)
    assert "context" in fake.sent[0] and "context" not in fake.sent[1]


def test_brain_error_sends_apology_instead_of_crashing():
    class Broken:
        def reply(self, sender, text):
            raise RuntimeError("boom")

    fake = FakeMeta()
    fake.page(text_msg("wamid.b", CREATOR, "hi"), next_offset=2)
    bridge = make(fake, Broken())
    bridge.poll_once()
    assert "went wrong" in fake.sent[0]["text"]["body"]
    assert bridge.offset == 2


def test_bad_key_raises_auth_failed():
    fake = FakeMeta([(400, {"error": {"code": 100, "message": "Invalid OAuth access token"}})])
    with pytest.raises(AuthFailed):
        make(fake).check_connection()


def test_second_poller_raises_conflict():
    fake = FakeMeta([(409, {"error": {"code": 1752041}})])
    with pytest.raises(PollConflict):
        make(fake).poll_once()


def test_run_forever_stops_after_repeated_conflicts():
    fake = FakeMeta([(409, {"error": {"code": 1752041}})] * 3)
    with pytest.raises(PollConflict):
        make(fake).run_forever()


def test_split_text_prefers_paragraphs():
    text = ("a" * 3000) + "\n\n" + ("b" * 3000)
    assert split_text(text) == ["a" * 3000, "b" * 3000]


def test_creator_is_saved_and_reloaded(tmp_path):
    state_file = tmp_path / "state.json"
    fake = FakeMeta()
    fake.page(text_msg("wamid.1", CREATOR, "hi"), next_offset=1)
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    platform = AgentPlatform("test-key", http=http, base_url="https://fake.test/agent/v1", sleep=lambda s: None)
    Bridge(platform, EchoBrain(), state=State(state_file), sleep=lambda s: None).poll_once()
    assert State(state_file).creator == CREATOR

    # a fresh bridge (after a restart) already knows you
    assert Bridge(platform, EchoBrain(), state=State(state_file)).creator == CREATOR
