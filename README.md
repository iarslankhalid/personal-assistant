# personal-assistant

A personal AI assistant you talk to on WhatsApp. It answers with Claude or OpenAI, uses tools
through MCP (your Notion, a task list, anything with an MCP server), and texts you when a job
you started has finished.

```
                    ┌──────────── assistant run ────────────┐
WhatsApp ─► Meta ─► │ channels/whatsapp ─► brains ─► mcp_client ─► MCP servers ─► Notion, tasks, …
WhatsApp ◄─ Meta ◄─ │ (poll, check sender)  (Claude/OpenAI + tool loop)                       │
                    └────────────────────────────────────────┘
any script ─► assistant notify / watch ─► Meta ─► WhatsApp
```

## Setup (Windows, PowerShell)

Needs Python 3.10+. Node.js 18+ is only needed for the Notion MCP server.

```powershell
git clone https://github.com/iarslankhalid/personal-assistant.git
cd personal-assistant
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
copy .env.example .env      # then fill it in; the old Whatsapp-bot .env works as-is
```

`pip install -e` installs the `assistant` command and points it at this folder, so code edits
take effect immediately.

## Commands

| Command | What it does |
|---|---|
| `assistant run --echo` | WhatsApp, replies "Echo: …". Tests the wiring. |
| `assistant run` | WhatsApp assistant (Claude or OpenAI) |
| `assistant run --mcp` | …that can use MCP tools |
| `assistant console --mcp` | Same brain and tools, in the terminal. Fastest way to try things. |
| `assistant tools` | Start the MCP servers, list their tools and what they cost in tokens |
| `assistant notify "Backup done"` | Text yourself |
| `assistant watch -- python train.py` | Run a command; when it ends, text you ✅/❌, how long it took, and the last lines on failure |
| `assistant watch "npm run build && npm test"` | One quoted string runs through the shell |

`--provider claude|openai` picks the LLM (else `LLM_PROVIDER` in `.env`, else whichever key is set).
`--debug` on any command shows everything. `/new` in a chat starts over.

**`notify` and `watch` need your WhatsApp id.** The bot saves it to `data/state.json` the first time
you message the agent, so run `assistant run` and say hi once. They work while the bot is running.

From your own Python code: `from assistant.notify import notify; notify("Scraper done: 412 rows")`

## Project layout

```
src/assistant/
  cli.py                  the `assistant` command (one function per subcommand)
  config.py               paths (project root, data/, logs/) and .env loading
  state.py                data/state.json: what the assistant learns while running
  notify.py               notify() and watch()
  channels/whatsapp/      client.py = Meta's API; bridge.py = poll → check sender → brain → reply
  brains/                 base.py = memory + tool-loop pieces; one module per LLM provider
  mcp_client/hub.py       starts MCP servers, lists tools, forwards tool calls
mcp_servers/              your own MCP servers (demo_server.py, tasks_server.py)
config/mcp_servers.json   which MCP servers start, and which of their tools the model may use
docs/                     mcp.md (how MCP works here), whatsapp-platform.md (Meta's protocol)
tests/                    offline: no network, no keys.  Run: pytest
data/  logs/              created at runtime, gitignored
```

## Extending it

- **A new MCP server of your own:** write `mcp_servers/<name>_server.py` (copy `tasks_server.py`),
  add it to `config/mcp_servers.json` with an `allowed_tools` list, then run `assistant tools`.
- **An existing MCP server:** add its command to `config/mcp_servers.json`, run `assistant tools`
  to see what it offers and what it costs, then allowlist only what you need. See `docs/mcp.md`.
- **Another LLM provider:** add `brains/<provider>_brain.py` with a `ChatBrain` subclass
  (implement `_complete`), and one line in `PROVIDERS` in `brains/__init__.py`.
- **Another channel** (Telegram, email…): add `channels/<name>/` next to `whatsapp/`. Brains and
  MCP don't care where messages come from.
- **A new command:** add a `cmd_<name>` function and a subparser in `cli.py`.

## Ideas for later

Voice notes (download → transcribe → same brain) · memory that survives restarts (SQLite +
running summary) · scheduled messages and reminders · remote (HTTP) MCP servers · tool calls that
ask you on WhatsApp before writing anything.

## Limits to know

- **WhatsApp agents are beta, and only you can use yours.** Meta delivers messages from, and
  allows replies to, only the person who created the agent. This is a personal tool, not
  something you can give other people yet.
- 12 messages a minute out (shared by the bot and `notify`). One poller per API key.
- Agent chats are **not end-to-end encrypted**: messages pass through Meta to this code and on to
  Anthropic or OpenAI. Keep Notion read-only and keep secrets out of chats.
- Notion's local MCP server is no longer maintained by Notion (pinned at 2.5.2). It's fine for
  learning; Notion's hosted MCP needs OAuth.
