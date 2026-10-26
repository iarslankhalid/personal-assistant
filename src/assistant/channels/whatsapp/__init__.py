"""WhatsApp, through Meta's Agent Platform (beta). See docs/whatsapp-platform.md."""

from assistant.channels.whatsapp.bridge import Bridge
from assistant.channels.whatsapp.client import (AgentPlatform, ApiError, AuthFailed, NotCreator,
                                                PollConflict, RateLimited, split_text)

__all__ = ["AgentPlatform", "ApiError", "AuthFailed", "Bridge", "NotCreator", "PollConflict",
           "RateLimited", "split_text"]
