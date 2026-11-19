"""The client's retries and rate-limit handling, against canned HTTP responses and a fake clock."""

import httpx
import pytest

from uberwatch import github
from uberwatch.github import Client, GitHubError, TransientGitHubError

OK = httpx.Response(200, json={"data": {"viewer": {"login": "someone"}}})


class FakeClock:
    """Stands in for the `time` module: sleeping moves the clock on instead of waiting."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(github, "time", fake)
    return fake


def client_answering(*responses):
    """A client whose requests get `responses` in turn; the last one repeats."""
    queue = list(responses)

    def handler(request):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    client = Client(token="not-a-real-token", verbose=False)
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def rate_limited(remaining, **headers):
    body = {"message": "API rate limit already exceeded for user ID 1."}
    return httpx.Response(403, json=body, headers={"x-ratelimit-remaining": remaining, **headers})


def test_a_query_returns_its_data(clock):
    assert client_answering(OK).graphql("query", {}) == {"viewer": {"login": "someone"}}


def test_an_exhausted_hourly_limit_fails_at_once_and_says_the_fetch_can_resume(clock):
    client = client_answering(rate_limited("0"))

    with pytest.raises(GitHubError, match="run the same command again"):
        client.graphql("query", {})
    assert client.requests == 1 and clock.now == 0


def test_the_secondary_limit_is_waited_out_and_the_query_retried(clock):
    client = client_answering(rate_limited("4000"), OK)

    assert client.graphql("query", {}) == {"viewer": {"login": "someone"}}
    assert client.requests == 2
    assert clock.now >= github.SECONDARY_WAITS[0]


def test_retry_after_sets_the_wait(clock):
    client = client_answering(rate_limited("4000", **{"retry-after": "7"}), OK)

    client.graphql("query", {})
    assert 7 <= clock.now < github.SECONDARY_WAITS[0]


def test_a_secondary_limit_that_never_lifts_gives_up(clock):
    client = client_answering(rate_limited("4000"))

    with pytest.raises(GitHubError, match="Still rate limited"):
        client.graphql("query", {})
    assert client.requests == len(github.SECONDARY_WAITS) + 1


def test_a_server_error_is_retried(clock):
    client = client_answering(httpx.Response(502), OK)

    assert client.graphql("query", {}) == {"viewer": {"login": "someone"}}


def test_a_server_error_that_persists_is_reported_as_transient(clock):
    client = client_answering(httpx.Response(502))

    with pytest.raises(TransientGitHubError):
        client.graphql("query", {})
    assert client.requests == 3


def test_githubs_own_query_timeout_is_retried(clock):
    timed_out = httpx.Response(200, json={"errors": [{"message": "Something went wrong: timeout"}]})

    assert client_answering(timed_out, OK).graphql("query", {}) == {"viewer": {"login": "someone"}}


def test_any_other_graphql_error_is_raised_with_its_message(clock):
    wrong = httpx.Response(200, json={"errors": [{"message": "Field 'nope' doesn't exist"}]})

    with pytest.raises(GitHubError, match="Field 'nope'"):
        client_answering(wrong).graphql("query", {})
