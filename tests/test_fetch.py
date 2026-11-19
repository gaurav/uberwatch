"""The fetch, against a stand-in for GitHub: what it asks for, what it keeps, how it resumes."""

import json

import pytest

from uberwatch import fetch
from uberwatch.github import GitHubError

ONE_PAGE = {"hasNextPage": False, "endCursor": None}


def raw_issue(number: int, updated: str = "2024-01-01T00:00:00Z") -> dict:
    """An issue as GitHub's `node(id:)` query returns it, before `_flatten`."""
    return {
        "id": f"I{number}",
        "number": number,
        "updatedAt": updated,
        "timelineItems": {"nodes": [{"__typename": "ClosedEvent"}], "pageInfo": ONE_PAGE},
        "labels": {"nodes": [{"name": "bug"}]},
        "assignees": {"nodes": [{"login": "alice"}]},
        "comments": {"totalCount": 2},
    }


def raw_pull(number: int, **overrides) -> dict:
    node = raw_issue(number) | {
        "id": f"P{number}",
        "files": {"nodes": [{"path": "a.txt"}], "pageInfo": ONE_PAGE, "totalCount": 1},
        "commits": {"totalCount": 0, "nodes": []},
        "closingIssuesReferences": {"nodes": [{"number": 7}]},
    }
    return node | overrides


class FakeGitHub:
    """Answers the fetch's queries from `issues` and `pulls`, and records each item it is asked for."""

    def __init__(self, issues=(), pulls=(), fail_on=None, more=None):
        self.items = {"issues": list(issues), "pullRequests": list(pulls)}
        self.fail_on = fail_on  # an item ID that makes the request raise
        self.more = more or {}  # {(item ID, cursor): the next page of that item's connection}
        self.asked: list[str] = []
        self.requests = 0
        self.rate_remaining = 5000

    def graphql(self, query, variables):
        self.requests += 1
        if query == fetch._META_QUERY:
            return {"repository": {"milestones": {"nodes": []}, "labels": {"nodes": []}}}
        for connection, nodes in self.items.items():
            if query == fetch._IDS_QUERY % {"connection": connection}:
                listing = [{"id": node["id"], "updatedAt": node["updatedAt"]} for node in nodes]
                return {"repository": {connection: {"nodes": listing, "pageInfo": ONE_PAGE}}}
        if "cursor" in variables:
            return {"node": self.more[variables["id"], variables["cursor"]]}
        self.asked.append(variables["id"])
        if variables["id"] == self.fail_on:
            raise GitHubError("GitHub's hourly rate limit is used up.")
        nodes = self.items["issues"] + self.items["pullRequests"]
        return {"node": json.loads(json.dumps(next(n for n in nodes if n["id"] == variables["id"])))}


def stored(raw_dir, name="issues.jsonl"):
    return fetch.read_jsonl(raw_dir / name)


def test_fetch_writes_flattened_items_and_meta(tmp_path):
    github = FakeGitHub(issues=[raw_issue(1)], pulls=[raw_pull(2)])
    raw = fetch.fetch_repo("o/r", root=tmp_path, client=github)

    assert raw == tmp_path / "o" / "r" / "raw"
    [issue] = stored(raw)
    assert issue["labels"] == ["bug"] and issue["assignees"] == ["alice"] and issue["comments"] == 2
    assert issue["timeline"] == [{"__typename": "ClosedEvent"}] and "timelineItems" not in issue
    [pull] = stored(raw, "pulls.jsonl")
    assert pull["files"] == [{"path": "a.txt"}] and pull["filesTotal"] == 1
    assert pull["closingIssues"] == [7]
    assert json.loads((raw / "meta.json").read_text())["schema"] == fetch.RAW_SCHEMA
    assert not list(raw.glob("*.partial-*"))


def test_null_connections_and_null_nodes_are_empty():
    # obophenotype/uberon has pull requests whose file list GitHub returns as null.
    pull = raw_pull(1, files=None, commits=None, closingIssuesReferences=None)
    pull["timelineItems"]["nodes"] = [None, {"__typename": "MergedEvent"}]
    fetch._flatten(FakeGitHub(), pull, is_pr=True)

    assert pull["timeline"] == [{"__typename": "MergedEvent"}]
    assert pull["files"] == [] and pull["filesTotal"] is None
    assert pull["commits"] == [] and pull["commitsTotal"] == 0
    assert pull["closingIssues"] == []


def test_a_long_timeline_is_followed_to_its_last_page():
    issue = raw_issue(1)
    issue["timelineItems"]["pageInfo"] = {"hasNextPage": True, "endCursor": "c1"}
    second = {"timelineItems": {"nodes": [{"__typename": "ReopenedEvent"}], "pageInfo": ONE_PAGE}}
    fetch._flatten(FakeGitHub(more={("I1", "c1"): second}), issue, is_pr=False)

    assert [event["__typename"] for event in issue["timeline"]] == ["ClosedEvent", "ReopenedEvent"]


def test_only_items_whose_updated_time_changed_are_fetched_again(tmp_path):
    fetch.fetch_repo("o/r", root=tmp_path, client=FakeGitHub(issues=[raw_issue(1), raw_issue(2)]))

    later = FakeGitHub(issues=[raw_issue(1), raw_issue(2, updated="2024-06-01T00:00:00Z")])
    raw = fetch.fetch_repo("o/r", root=tmp_path, client=later)

    assert later.asked == ["I2"]
    assert [item["updatedAt"] for item in stored(raw)] == ["2024-01-01T00:00:00Z", "2024-06-01T00:00:00Z"]


def test_full_fetches_everything_again(tmp_path):
    issues = [raw_issue(1), raw_issue(2)]
    fetch.fetch_repo("o/r", root=tmp_path, client=FakeGitHub(issues=issues))

    again = FakeGitHub(issues=issues)
    fetch.fetch_repo("o/r", root=tmp_path, client=again, full=True)

    assert sorted(again.asked) == ["I1", "I2"]


def test_a_failed_fetch_keeps_what_it_got_and_the_next_run_carries_on(tmp_path):
    issues = [raw_issue(number) for number in (1, 2, 3, 4)]
    with pytest.raises(GitHubError):
        fetch.fetch_repo("o/r", root=tmp_path, client=FakeGitHub(issues=issues, fail_on="I3"))

    raw = tmp_path / "o" / "r" / "raw"
    assert not (raw / "issues.jsonl").exists()  # never a half-written final file
    [partial] = raw.glob("issues.partial-*.jsonl")
    assert [item["id"] for item in fetch.read_jsonl(partial)] == ["I1", "I2"]

    resumed = FakeGitHub(issues=issues)
    fetch.fetch_repo("o/r", root=tmp_path, client=resumed)

    assert sorted(resumed.asked) == ["I3", "I4"]
    assert [item["number"] for item in stored(raw)] == [1, 2, 3, 4]
    assert not list(raw.glob("*.partial-*"))


def test_a_failed_fetch_leaves_the_last_good_copy_alone(tmp_path):
    fetch.fetch_repo("o/r", root=tmp_path, client=FakeGitHub(issues=[raw_issue(1)]))

    changed = [raw_issue(1, updated="2024-06-01T00:00:00Z")]
    with pytest.raises(GitHubError):
        fetch.fetch_repo("o/r", root=tmp_path, client=FakeGitHub(issues=changed, fail_on="I1"))

    assert [item["updatedAt"] for item in stored(tmp_path / "o" / "r" / "raw")] == ["2024-01-01T00:00:00Z"]


def test_a_line_cut_short_by_a_crash_is_skipped_only_when_asked(tmp_path):
    path = tmp_path / "items.jsonl"
    path.write_text('{"id": "I1"}\n{"id": "I2", "tit')

    assert fetch.read_jsonl(path, skip_bad_lines=True) == [{"id": "I1"}]
    with pytest.raises(ValueError):
        fetch.read_jsonl(path)
