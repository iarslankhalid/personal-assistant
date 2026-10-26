"""
Brains turn a message into a reply. To add a provider: write a ChatBrain subclass in its own
module (implement _complete), then add one line to PROVIDERS.
"""

from assistant.brains.anthropic_brain import ClaudeBrain
from assistant.brains.base import (DEFAULT_SYSTEM_PROMPT, MAX_TOOL_ROUNDS, TOOLS_PROMPT, Brain,
                                   ChatBrain, EchoBrain, ToolBox)
from assistant.brains.openai_brain import OpenAIBrain

PROVIDERS = {
    # name: (brain class, API key env var, model env var, default model)
    "claude": (ClaudeBrain, "ANTHROPIC_API_KEY", "CLAUDE_MODEL", "claude-sonnet-5-5"),
    "openai": (OpenAIBrain, "OPENAI_API_KEY", "OPENAI_MODEL", "gpt-6-luna"),
}


def pick_provider(requested: str | None, env: dict[str, str]) -> str:
    """Explicit choice wins; otherwise use whichever API key is set (Claude first)."""
    if requested:
        return requested
    for name, (_, key_var, _, _) in PROVIDERS.items():
        if env.get(key_var, "").strip():
            return name
    return "claude"


__all__ = ["Brain", "ChatBrain", "ClaudeBrain", "DEFAULT_SYSTEM_PROMPT", "EchoBrain", "MAX_TOOL_ROUNDS",
           "OpenAIBrain", "PROVIDERS", "TOOLS_PROMPT", "ToolBox", "pick_provider"]
