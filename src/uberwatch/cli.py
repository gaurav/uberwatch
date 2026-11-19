"""Command line: `uberwatch fetch|build|all owner/repo`."""

from __future__ import annotations

import argparse
from pathlib import Path

from .fetch import DATA_ROOT, fetch_repo


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="uberwatch", description=__doc__)
    parser.add_argument("command", choices=["fetch", "build", "all"])
    parser.add_argument("repo", help="owner/name, for example NCATSTranslator/Babel")
    parser.add_argument("--data", type=Path, default=DATA_ROOT, help="scratch directory (default: data)")
    parser.add_argument("--config", type=Path, default=Path("repos.toml"), help="per-repository settings")
    parser.add_argument("--full", action="store_true", help="re-fetch items even if they have not changed")
    args = parser.parse_args(argv)

    if args.command in ("fetch", "all"):
        fetch_repo(args.repo, root=args.data, full=args.full)
    if args.command in ("build", "all"):
        # Imported here so that `fetch` works while the dashboard code is broken or half-written.
        from .render import build_site

        out = build_site(args.repo, root=args.data, config_path=args.config)
        print(out)


if __name__ == "__main__":
    main()
