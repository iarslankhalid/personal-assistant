"""MCP hub tests. These start the real demo MCP server over stdio: a true MCP round trip."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from assistant.config import ROOT
from assistant.mcp_client import (MCPConfigError, MCPHub, ServerConfig, load_config, prune_defs,
                                  result_to_text)

# Relative path + cwd, exactly how config/mcp_servers.json runs servers
DEMO = ServerConfig("demo", sys.executable, ["mcp_servers/demo_server.py"], {},
                    ["current_time", "list_notes"], cwd=str(ROOT))


@pytest.fixture(scope="module")
def hub(tmp_path_factory):
    broken = ServerConfig("broken", sys.executable, ["-c", "import sys; sys.exit(3)"])
    with MCPHub([DEMO, broken], startup_timeout=30,
                log_dir=tmp_path_factory.mktemp("logs")) as h:
        yield h


def test_discovers_only_allowlisted_tools(hub):
    names = sorted(t.public_name for t in hub.tools)
    assert names == ["demo__current_time", "demo__list_notes"]   # save_note is not allowlisted
    time_tool = next(t for t in hub.tools if t.name == "current_time")
    assert time_tool.read_only is True
    assert time_tool.schema["properties"]["timezone"]["type"] == "string"


def test_calls_a_real_tool(hub):
    result = hub.call("demo__current_time", {"timezone": "Asia/Tokyo"})
    assert not result.is_error
    assert "Asia/Tokyo" in result.text


def test_blocked_and_unknown_tools_return_errors(hub):
    assert hub.call("demo__save_note", {"text": "x"}).is_error
    assert hub.call("made__up", {}).is_error


def test_broken_server_does_not_take_down_the_others(hub):
    assert "broken" in hub.failed
    assert any(t.server == "demo" for t in hub.tools)


# ------------------------------------------------------------------------ config
def write(tmp_path, data):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_config_fills_env_vars_and_skips_servers_missing_them(tmp_path):
    path = write(tmp_path, {"mcpServers": {
        "notion": {"command": "npx", "args": ["-y", "pkg"], "env": {"NOTION_TOKEN": "${NOTION_TOKEN}"}},
        "other": {"command": "npx", "env": {"KEY": "${OTHER_KEY}"}},
        "off": {"command": "npx", "enabled": False},
        "demo": {"command": "python", "args": ["demo.py"]},
    }})
    configs = {c.name: c for c in load_config(path, {"NOTION_TOKEN": "ntn_123"})}
    assert set(configs) == {"notion", "demo"}                  # 'other' lacks its var, 'off' is disabled
    assert configs["notion"].env == {"NOTION_TOKEN": "ntn_123"}
    assert configs["demo"].command == sys.executable           # 'python' means this interpreter


def test_config_errors_are_clear(tmp_path):
    with pytest.raises(MCPConfigError, match="not found"):
        load_config(tmp_path / "nope.json", {})
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    with pytest.raises(MCPConfigError, match="not valid JSON"):
        load_config(bad, {})
    with pytest.raises(MCPConfigError, match="allowed_tools"):
        load_config(write(tmp_path, {"mcpServers": {"x": {"command": "a", "allowed_tools": "all"}}}), {})


def test_shipped_config_is_valid():
    configs = {c.name: c for c in load_config(ROOT / "config" / "mcp_servers.json", {"NOTION_TOKEN": "t"}, cwd=ROOT)}
    assert configs["notion"].env == {"NOTION_TOKEN": "t"}
    assert "API-post-search" in configs["notion"].allowed_tools
    assert not any("patch" in t or "post-page" in t or "update" in t or "delete" in t
                   for t in configs["notion"].allowed_tools), "Notion allowlist must stay read-only"
    assert all(c.cwd == str(ROOT) for c in configs.values())
    for name in ("demo", "tasks"):
        assert (ROOT / configs[name].args[0]).exists(), f"{name} server file missing"


# ----------------------------------------------------------------------- helpers
def test_prune_defs_keeps_only_what_is_referenced():
    schema = {
        "type": "object",
        "properties": {"parent": {"$ref": "#/$defs/parent"}},
        "$defs": {
            "parent": {"type": "object", "properties": {"id": {"$ref": "#/$defs/uuid"}}},
            "uuid": {"type": "string"},
            "unused": {"type": "object", "properties": {"huge": {"type": "string"}}},
        },
    }
    pruned = prune_defs(schema)
    assert set(pruned["$defs"]) == {"parent", "uuid"}
    assert prune_defs({"type": "object", "$defs": {"x": {}}}) == {"type": "object"}


def test_result_to_text_handles_blocks_and_truncates():
    result = NS(content=[NS(type="text", text="a" * 50), NS(type="image")], structured_content=None,
                is_error=False)
    out = result_to_text(result, max_chars=20)
    assert out.text.startswith("a" * 20) and "truncated" in out.text
    assert "[image content omitted]" in result_to_text(result, 1000).text
    assert result_to_text(NS(content=[], structured_content={"k": 1}, is_error=True), 100).text == '{"k": 1}'
    assert result_to_text(NS(), 100).is_error
