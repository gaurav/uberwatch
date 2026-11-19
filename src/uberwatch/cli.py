"""Command line: `uberwatch fetch owner/repo`."""

from __future__ import annotations

import argparse
from pathlib import Path

from .fetch import DATA_ROOT, fetch_repo


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="uberwatch", description=__doc__)
    parser.add_argument("command", choices=["fetch"])
    parser.add_argument("repo", help="owner/name, for example NCATSTranslator/Babel")
    parser.add_argument("--data", type=Path, default=DATA_ROOT, help="scratch directory (default: data)")
    parser.add_argument("--full", action="store_true", help="re-fetch items even if they have not changed")
    args = parser.parse_args(argv)

    fetch_repo(args.repo, root=args.data, full=args.full)


if __name__ == "__main__":
    main()
