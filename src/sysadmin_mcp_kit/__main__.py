from .config import load_settings
from .server import build_server


def main() -> None:
    settings = load_settings()
    server = build_server(settings)
    server.run(transport="streamable-http")


if __name__ == "__main__":
    main()
