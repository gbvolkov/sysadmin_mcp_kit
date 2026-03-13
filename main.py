from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sysadmin_mcp_kit.config import load_settings
from sysadmin_mcp_kit.server import build_server


def main() -> None:
    settings = load_settings()
    server = build_server(settings)
    server.run(transport="streamable-http")


if __name__ == "__main__":
    main()
