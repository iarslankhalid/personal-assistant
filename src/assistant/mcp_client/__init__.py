"""MCP client: start MCP servers and hand their tools to the brains. See docs/mcp.md."""

from assistant.mcp_client.hub import (MCPConfigError, MCPHub, ServerConfig, ToolResult, ToolSpec,
                                      load_config, prune_defs, result_to_text)

__all__ = ["MCPConfigError", "MCPHub", "ServerConfig", "ToolResult", "ToolSpec", "load_config",
           "prune_defs", "result_to_text"]
