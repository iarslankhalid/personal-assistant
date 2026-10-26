"""Brain tests with fake LLM clients and a fake MCP toolbox (no network, no keys)."""

import json
from types import SimpleNamespace as NS

import pytest

from assistant import brains
from assistant.mcp_client import ToolResult, ToolSpec

ME = "user:111"


# ------------------------------------------------------------------------- fakes
class FakeToolBox:
    def __init__(self, results=None):
        self.tools = [ToolSpec("notion__search", "notion", "search", "Search pages",
                               {"type": "object", "properties": {"query": {"type": "string"}}}, True)]
        self.results = results or {}
        self.calls = []

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        return self.results.get(name, ToolResult(f"result for {arguments}"))


def text_block(t):
    return NS(type="text", text=t)


def tool_block(id_, name, inp):
    return NS(type="tool_use", id=id_, name=name, input=inp)


class FakeAnthropic:
    """Returns the queued responses in order and records every request."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.requests = []
        self.messages = self

    def create(self, **kw):
        self.requests.append(json.loads(json.dumps(kw, default=str)))
        if self.queue:
            return NS(content=self.queue.pop(0))
        return NS(content=[text_block("final")])


class FakeOpenAI:
    def __init__(self, *responses):
        self.queue = list(responses)
        self.requests = []
        self.responses = self

    def create(self, **kw):
        self.requests.append(kw)
        if self.queue:
            return self.queue.pop(0)
        return oa_text("final", "resp_last")


def oa_text(t, rid="resp_x"):
    return NS(id=rid, output=[NS(type="message")], output_text=t)


def oa_call(call_id, name, args, rid):
    return NS(id=rid, output=[NS(type="function_call", call_id=call_id, name=name, arguments=args)],
              output_text="")


# ------------------------------------------------------------------------ Claude
def test_claude_plain_reply_history_and_reset():
    client = FakeAnthropic([text_block("a1")], [text_block("a2")], [text_block("a3")])
    brain = brains.ClaudeBrain("m", "sys", client=client)
    assert brain.reply(ME, "one") == "a1"
    assert brain.reply(ME, "two") == "a2"
    assert [m["role"] for m in client.requests[-1]["messages"]] == ["user", "assistant", "user"]
    assert "tools" not in client.requests[-1]
    assert brain.reply(ME, "/new") == "Started a new conversation."
    brain.reply(ME, "three")
    assert len(client.requests[-1]["messages"]) == 1


def test_claude_tool_loop_round_trip():
    box = FakeToolBox()
    client = FakeAnthropic(
        [text_block("Let me check."), tool_block("tu_1", "notion__search", {"query": "goals"})],
        [text_block("Your goals page says: ship it.")],
    )
    brain = brains.ClaudeBrain("m", "sys", client=client, toolbox=box)
    assert brain.reply(ME, "what are my goals?") == "Your goals page says: ship it."

    assert box.calls == [("notion__search", {"query": "goals"})]
    first, second = client.requests
    assert first["tools"][0]["name"] == "notion__search"
    assert "You have tools" in first["system"]
    assistant, results = second["messages"][-2], second["messages"][-1]
    assert assistant["role"] == "assistant"
    assert assistant["content"][1] == {"type": "tool_use", "id": "tu_1", "name": "notion__search",
                                       "input": {"query": "goals"}}
    assert results["role"] == "user"
    assert results["content"][0]["tool_use_id"] == "tu_1"
    assert results["content"][0]["is_error"] is False
    # Only plain text is remembered between replies
    assert brain.histories[ME] == [{"role": "user", "content": "what are my goals?"},
                                   {"role": "assistant", "content": "Your goals page says: ship it."}]


def test_claude_tool_error_is_flagged_to_model():
    box = FakeToolBox({"notion__search": ToolResult("server down", True)})
    client = FakeAnthropic([tool_block("tu_1", "notion__search", {})], [text_block("Notion is down.")])
    brains.ClaudeBrain("m", "sys", client=client, toolbox=box).reply(ME, "hi")
    assert client.requests[1]["messages"][-1]["content"][0]["is_error"] is True


def test_claude_stops_after_max_rounds():
    loops = [[tool_block(f"tu_{i}", "notion__search", {"query": str(i)})] for i in range(50)]
    client = FakeAnthropic(*loops)
    box = FakeToolBox()
    brains.ClaudeBrain("m", "sys", client=client, toolbox=box).reply(ME, "loop forever")
    assert len(box.calls) == brains.MAX_TOOL_ROUNDS
    assert client.requests[-1]["tool_choice"] == {"type": "none"}


# ------------------------------------------------------------------------ OpenAI
def test_openai_plain_reply_uses_responses_api():
    client = FakeOpenAI(oa_text("hello"))
    brain = brains.OpenAIBrain("gpt-test", "be brief", client=client)
    assert brain.reply(ME, "hi") == "hello"
    req = client.requests[0]
    assert req["model"] == "gpt-test"
    assert req["instructions"] == "be brief"
    assert req["input"] == [{"role": "user", "content": "hi"}]
    assert "tools" not in req


def test_openai_tool_loop_chains_previous_response():
    box = FakeToolBox()
    client = FakeOpenAI(oa_call("call_1", "notion__search", '{"query": "rent"}', "resp_1"),
                        oa_text("Rent is due on the 1st.", "resp_2"))
    brain = brains.OpenAIBrain("gpt-test", "sys", client=client, toolbox=box)
    assert brain.reply(ME, "when is rent due?") == "Rent is due on the 1st."
    assert box.calls == [("notion__search", {"query": "rent"})]
    first, second = client.requests
    assert first["tools"][0] == {"type": "function", "name": "notion__search", "description": "Search pages",
                                 "parameters": box.tools[0].schema}
    assert second["previous_response_id"] == "resp_1"
    assert second["instructions"].startswith("sys")
    assert second["input"] == [{"type": "function_call_output", "call_id": "call_1",
                                "output": "result for {'query': 'rent'}"}]


def test_openai_bad_json_arguments_go_back_as_error_text():
    box = FakeToolBox()
    client = FakeOpenAI(oa_call("call_1", "notion__search", "{not json", "resp_1"), oa_text("ok"))
    brains.OpenAIBrain("gpt-test", "sys", client=client, toolbox=box).reply(ME, "hi")
    assert box.calls == []
    assert "not valid JSON" in client.requests[1]["input"][0]["output"]


def test_openai_stops_after_max_rounds():
    loops = [oa_call(f"c{i}", "notion__search", "{}", f"r{i}") for i in range(50)]
    client = FakeOpenAI(*loops)
    box = FakeToolBox()
    brains.OpenAIBrain("gpt-test", "sys", client=client, toolbox=box).reply(ME, "loop")
    assert len(box.calls) == brains.MAX_TOOL_ROUNDS
    assert client.requests[-1]["tool_choice"] == "none"


def test_openai_error_keeps_history_clean():
    class Failing:
        class responses:
            @staticmethod
            def create(**kw):
                raise RuntimeError("quota")

    brain = brains.OpenAIBrain("gpt-test", "sys", client=Failing())
    with pytest.raises(RuntimeError):
        brain.reply(ME, "hi")
    assert brain.histories[ME] == []


# ----------------------------------------------------------------------- misc
def test_tools_prompt_only_when_tools_exist():
    assert "You have tools" not in brains.ClaudeBrain("m", "sys", client=FakeAnthropic()).system
    empty = FakeToolBox()
    empty.tools = []
    assert "You have tools" not in brains.ClaudeBrain("m", "sys", client=FakeAnthropic(), toolbox=empty).system


@pytest.mark.parametrize("requested, env, expected", [
    ("openai", {"ANTHROPIC_API_KEY": "a"}, "openai"),                     # explicit choice wins
    (None, {"OPENAI_API_KEY": "o"}, "openai"),                            # only OpenAI key set
    (None, {"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o"}, "claude"),  # both: Claude first
    (None, {}, "claude"),                                                 # none: main() asks for a key
])
def test_pick_provider(requested, env, expected):
    assert brains.pick_provider(requested, env) == expected
