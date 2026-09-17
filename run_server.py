"""Entry point for MCP clients (Claude Desktop, Claude Code, MCP Inspector)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reportguard.server import main  # noqa: E402

if __name__ == "__main__":
    main()
