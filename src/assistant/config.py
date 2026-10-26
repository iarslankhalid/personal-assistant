"""Where things live, and reading settings from .env.

The project root is found in this order:
  1. ASSISTANT_HOME, if set
  2. the source checkout this package runs from (normal case: `pip install -e .`)
  3. the current directory
"""

from __future__ import annotations

import os
from pathlib import Path


def _find_root() -> Path:
    if os.environ.get("ASSISTANT_HOME"):
        return Path(os.environ["ASSISTANT_HOME"]).resolve()
    checkout = Path(__file__).resolve().parents[2]          # src/assistant/config.py -> project root
    if (checkout / "pyproject.toml").exists():
        return checkout
    return Path.cwd()


ROOT = _find_root()
DATA_DIR = ROOT / "data"            # runtime state: who you are, task lists, notes (gitignored)
LOG_DIR = ROOT / "logs"             # MCP server logs (gitignored)
MCP_CONFIG = ROOT / "config" / "mcp_servers.json"
STATE_FILE = DATA_DIR / "state.json"


def load_env() -> None:
    """Load ROOT/.env into the environment (real environment variables win)."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT / ".env", override=False)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()
