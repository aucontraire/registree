"""registree: an anti-hallucination class registry served over MCP."""

__version__ = "0.2.0"


def main() -> None:
    import sys

    from registree.cli import main as cli_main

    sys.exit(cli_main())
