"""A minimal GitHub GraphQL client: token lookup, one POST, retries."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any

import httpx

GRAPHQL_URL = "https://api.github.com/graphql"


class GitHubError(RuntimeError):
    pass


class TransientGitHubError(GitHubError):
    """A failure worth retrying, possibly with a smaller page: a timeout or a 5xx."""


def find_token() -> str:
    """Return a GitHub token from the environment, or from the `gh` CLI if it is logged in."""
    for var in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise GitHubError("No GitHub token: set GH_TOKEN or GITHUB_TOKEN, or run `gh auth login`.") from exc
    return result.stdout.strip()


class Client:
    def __init__(self, token: str | None = None, *, verbose: bool = True) -> None:
        self._http = httpx.Client(
            headers={
                "Authorization": f"Bearer {token or find_token()}",
                "User-Agent": "uberwatch",
            },
            timeout=60,
        )
        self.verbose = verbose
        self.requests = 0
        self.rate_remaining: int | None = None

    def close(self) -> None:
        self._http.close()

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Run one query. Raises TransientGitHubError once the retries for a 5xx/timeout run out."""
        last: Exception | None = None
        for attempt in range(3):
            if attempt:
                time.sleep(2 * attempt)
            try:
                response = self._http.post(GRAPHQL_URL, json={"query": query, "variables": variables})
            except httpx.TransportError as exc:
                last = exc
                continue
            self.requests += 1
            if response.status_code in (502, 503, 504):
                last = TransientGitHubError(f"HTTP {response.status_code}")
                continue
            if response.status_code in (403, 429) and "retry-after" in response.headers:
                # Secondary rate limit: GitHub says how long to wait.
                wait = int(response.headers["retry-after"])
                self._log(f"rate limited, waiting {wait}s")
                time.sleep(wait + 1)
                last = TransientGitHubError("secondary rate limit")
                continue
            if response.status_code != 200:
                raise GitHubError(f"HTTP {response.status_code}: {response.text[:500]}")
            payload = response.json()
            errors = payload.get("errors")
            if errors:
                messages = "; ".join(e.get("message", str(e)) for e in errors)
                # GitHub reports its own 10-second query timeout as a 200 with an error body.
                if "timeout" in messages.lower() or "something went wrong" in messages.lower():
                    last = TransientGitHubError(messages)
                    continue
                raise GitHubError(messages)
            data = payload["data"]
            if isinstance(data.get("rateLimit"), dict):
                self.rate_remaining = data["rateLimit"].get("remaining")
            return data
        raise TransientGitHubError(f"gave up after retries: {last}")

    def _log(self, message: str) -> None:
        if self.verbose:
            print(message, file=sys.stderr)
