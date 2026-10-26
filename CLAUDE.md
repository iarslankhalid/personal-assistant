# Notes for AI coding assistants

Personal assistant over WhatsApp (Meta's Agent Platform, beta) with Claude/OpenAI brains and MCP
tools. Owner runs it on Windows; keep everything Windows-friendly (no bash-only scripts, utf-8 I/O).

- Layout and extension points: README.md. MCP design notes: docs/mcp.md.
  WhatsApp protocol and limits: docs/whatsapp-platform.md.
- Install: `pip install -e ".[dev]"`. Tests: `pytest`. They are offline (fake Meta API, fake LLM
  clients, real local MCP servers) and must stay offline. Add a test with every behaviour change.
- Python ≥ 3.10. MCP SDK v2 (`mcp.server.mcpserver.MCPServer`, snake_case fields). OpenAI uses the
  Responses API. Anthropic tool messages are built by hand, not by echoing SDK objects back.
- Never commit `.env`, `data/` or `logs/`. Never put secrets in `config/mcp_servers.json`; use
  `${VAR}` there and the value in `.env`.
- Keep the Notion allowlist read-only (a test enforces this). Any new write tool needs a reason.
- WhatsApp: only the creator can be messaged; 12 sends/min shared by the bot and `notify`;
  one poller per key.
