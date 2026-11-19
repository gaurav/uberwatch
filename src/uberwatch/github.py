"""A minimal GitHub GraphQL client: token lookup, one POST, retries, rate-limit back-off."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from typing import Any

import httpx

GRAPHQL_URL = "https://api.github.com/graphql"

# How long to stand off after GitHub's secondary rate limit, per attempt. GitHub asks for at
# least a minute when it sends no Retry-After header, and longer each time after that.
SECONDARY_WAITS = (60, 120, 240, 480)


class GitHubError(RuntimeError):
    pass


class TransientGitHubError(GitHubError):
    """A failure that outlasted its retries but may pass on its own: a timeout or a 5xx."""


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
    """Safe to share between threads: a rate-limit pause in one holds the others back too."""

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
        self._lock = threading.Lock()
        self._paused_until = 0.0

    def close(self) -> None:
        self._http.close()

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Run one query. Raises TransientGitHubError once the retries for a 5xx/timeout run out."""
        last: Exception | None = None
        transient_tries = 0
        secondary_tries = 0
        while transient_tries < 3:
            self._wait_out_pause()
            try:
                response = self._http.post(GRAPHQL_URL, json={"query": query, "variables": variables})
            except httpx.TransportError as exc:
                last = exc
                transient_tries += 1
                time.sleep(2 * transient_tries)
                continue
            with self._lock:
                self.requests += 1
            if response.status_code in (502, 503, 504):
                last = TransientGitHubError(f"HTTP {response.status_code}")
                transient_tries += 1
                time.sleep(2 * transient_tries)
                continue

            try:
                payload = response.json()
            except ValueError:
                payload = {}
            errors = payload.get("errors") or []
            messages = "; ".join(e.get("message", str(e)) for e in errors) or payload.get("message", "")

            if "rate limit" in messages.lower() or response.status_code == 429:
                if response.headers.get("x-ratelimit-remaining") == "0":
                    raise GitHubError(
                        "GitHub's hourly rate limit is used up. What was fetched is saved: "
                        "run the same command again after the limit resets to carry on."
                    )
                # Hourly budget to spare, so this is the secondary limit: too much, too fast.
                # (The message does not tell the two apart. "API rate limit already exceeded
                # for user ID ..." is what the exhausted hourly limit says.)
                if secondary_tries == len(SECONDARY_WAITS):
                    raise GitHubError(
                        f"Still rate limited after {sum(SECONDARY_WAITS) // 60} minutes: {messages}"
                    )
                wait = int(response.headers.get("retry-after") or SECONDARY_WAITS[secondary_tries])
                secondary_tries += 1
                self._pause(wait)
                continue

            if response.status_code != 200:
                raise GitHubError(f"HTTP {response.status_code}: {response.text[:500]}")
            if errors:
                # GitHub reports its own 10-second query timeout as a 200 with an error body.
                if "timeout" in messages.lower() or "something went wrong" in messages.lower():
                    last = TransientGitHubError(messages)
                    transient_tries += 1
                    time.sleep(2 * transient_tries)
                    continue
                raise GitHubError(messages)
            data = payload["data"]
            if isinstance(data.get("rateLimit"), dict):
                self.rate_remaining = data["rateLimit"].get("remaining")
            return data
        raise TransientGitHubError(f"gave up after retries: {last}")

    def _pause(self, seconds: int) -> None:
        with self._lock:
            until = time.monotonic() + seconds
            if until > self._paused_until + 1:  # one message per pause, not one per thread
                self._log(f"  GitHub's secondary rate limit: pausing {seconds}s")
            self._paused_until = max(self._paused_until, until)

    def _wait_out_pause(self) -> None:
        while (remaining := self._paused_until - time.monotonic()) > 0:
            time.sleep(min(remaining, 5))

    def _log(self, message: str) -> None:
        if self.verbose:
            print(message, file=sys.stderr)
