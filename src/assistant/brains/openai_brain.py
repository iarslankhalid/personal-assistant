"""OpenAI via the Responses API, with the MCP tool loop."""

from __future__ import annotations

import json
from typing import Any

from assistant.brains.base import MAX_TOOL_ROUNDS, ChatBrain, ToolBox


class OpenAIBrain(ChatBrain):
    """OpenAI via the Responses API (OpenAI's recommended API; newer models need it for tools)."""

    label = "OpenAI"

    def __init__(self, model: str, system_prompt: str, max_turns: int = 20,
                 client: Any = None, toolbox: ToolBox | None = None):
        super().__init__(model, system_prompt, max_turns, toolbox)
        if client is None:
            import openai  # imported here so the other modes work without the package
            client = openai.OpenAI()  # reads OPENAI_API_KEY
        self.client = client

    def _complete(self, history: list[dict[str, str]]) -> str:
        tools = [{"type": "function", "name": t.public_name, "description": t.description,
                  "parameters": t.schema} for t in self._tool_specs()]
        kwargs: dict[str, Any] = dict(model=self.model, instructions=self.system,
                                      input=[{"role": m["role"], "content": m["content"]} for m in history])
        if tools:
            kwargs["tools"] = tools
        resp = self.client.responses.create(**kwargs)

        for round_no in range(MAX_TOOL_ROUNDS):
            calls = [o for o in resp.output if getattr(o, "type", None) == "function_call"]
            if not calls:
                break
            outputs = []
            for call in calls:
                try:
                    args = json.loads(call.arguments or "{}")
                except json.JSONDecodeError:
                    text = "Your arguments were not valid JSON. Try again."
                else:
                    text = self._run_tool(call.name, args).text
                outputs.append({"type": "function_call_output", "call_id": call.call_id, "output": text})
            # Chain onto the previous response instead of resending everything.
            # (OpenAI keeps the earlier response server-side for this; instructions must be resent.)
            nxt: dict[str, Any] = dict(model=self.model, instructions=self.system,
                                       previous_response_id=resp.id, input=outputs, tools=tools)
            if round_no == MAX_TOOL_ROUNDS - 1:
                nxt["tool_choice"] = "none"   # out of rounds: answer with what you have
            resp = self.client.responses.create(**nxt)
        return resp.output_text or ""
