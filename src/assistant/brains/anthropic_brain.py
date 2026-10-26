"""Claude via Anthropic's Messages API, with the MCP tool loop."""

from __future__ import annotations

from typing import Any

from assistant.brains.base import MAX_TOOL_ROUNDS, ChatBrain, ToolBox


class ClaudeBrain(ChatBrain):
    """Claude via Anthropic's Messages API."""

    label = "Claude"

    def __init__(self, model: str, system_prompt: str, max_turns: int = 20,
                 client: Any = None, toolbox: ToolBox | None = None, max_tokens: int = 2048):
        super().__init__(model, system_prompt, max_turns, toolbox)
        if client is None:
            import anthropic  # imported here so --echo works without the package
            client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        self.client = client
        self.max_tokens = max_tokens

    def _complete(self, history: list[dict[str, str]]) -> str:
        tools = [{"name": t.public_name, "description": t.description, "input_schema": t.schema}
                 for t in self._tool_specs()]
        messages: list[dict[str, Any]] = list(history)
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            kwargs: dict[str, Any] = dict(model=self.model, max_tokens=self.max_tokens,
                                          system=self.system, messages=messages)
            if tools:
                kwargs["tools"] = tools
                if round_no == MAX_TOOL_ROUNDS:
                    kwargs["tool_choice"] = {"type": "none"}   # out of rounds: answer with what you have
            resp = self.client.messages.create(**kwargs)

            text = "".join(b.text for b in resp.content if b.type == "text")
            calls = [b for b in resp.content if b.type == "tool_use"]
            if not calls or round_no == MAX_TOOL_ROUNDS:   # never run tools past the limit
                return text

            # Echo the assistant turn back (built by hand: only the fields the API needs),
            # then answer every tool_use with a tool_result carrying the same id.
            assistant: list[dict[str, Any]] = []
            for b in resp.content:
                if b.type == "text" and b.text:
                    assistant.append({"type": "text", "text": b.text})
                elif b.type == "tool_use":
                    assistant.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
            messages.append({"role": "assistant", "content": assistant})
            results = []
            for call in calls:
                r = self._run_tool(call.name, call.input)
                results.append({"type": "tool_result", "tool_use_id": call.id,
                                "content": r.text, "is_error": r.is_error})
            messages.append({"role": "user", "content": results})
        return ""  # not reached: the last round always returns above
