"""Download every issue and pull request of a repository, with its timeline, as JSON lines.

Output, under data/<owner>/<repo>/raw/:

    issues.jsonl   one issue per line, as GitHub's GraphQL API returns it, except that the
    pulls.jsonl    paged connections are flattened: `timeline`, `files`, `commits` are plain lists
    meta.json      when the fetch ran, the repository's milestones and labels, GitHub's own totals

This is a full re-fetch every time. At ~50 issues or ~20 pull requests a request that is a few
dozen requests for a repository with a thousand items, so it has not been worth building an
incremental sync (ask for items ordered by UPDATED_AT and stop at the last fetch's watermark).
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .github import Client, TransientGitHubError

DATA_ROOT = Path("data")


def repo_dir(repo: str, root: Path = DATA_ROOT) -> Path:
    owner, name = split_repo(repo)
    return root / owner / name


def split_repo(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    if not owner or not name or "/" in name:
        raise ValueError(f"Expected owner/name, got {repo!r}")
    return owner, name


# Timeline events both issues and pull requests have. The selections on different event types
# may share a field name only when the field has the same type everywhere, which holds here.
_COMMON_TIMELINE = """
  __typename
  ... on ClosedEvent { createdAt actor { login } stateReason
    closer { __typename ... on PullRequest { number } ... on Commit { abbreviatedOid } } }
  ... on ReopenedEvent { createdAt actor { login } }
  ... on LabeledEvent { createdAt actor { login } label { name } }
  ... on UnlabeledEvent { createdAt actor { login } label { name } }
  ... on MilestonedEvent { createdAt actor { login } milestoneTitle }
  ... on DemilestonedEvent { createdAt actor { login } milestoneTitle }
  ... on AssignedEvent { createdAt actor { login }
    assignee { __typename ... on User { login } ... on Bot { login } } }
  ... on UnassignedEvent { createdAt actor { login }
    assignee { __typename ... on User { login } ... on Bot { login } } }
  ... on CrossReferencedEvent { createdAt actor { login } willCloseTarget
    source { __typename
      ... on PullRequest { number repository { nameWithOwner } }
      ... on Issue { number repository { nameWithOwner } } } }
  ... on ConnectedEvent { createdAt actor { login }
    subject { __typename ... on PullRequest { number } ... on Issue { number } } }
  ... on IssueComment { createdAt author { __typename login } }
"""

_PR_TIMELINE = (
    _COMMON_TIMELINE
    + """
  ... on MergedEvent { createdAt actor { login } }
  ... on ReadyForReviewEvent { createdAt actor { login } }
  ... on ConvertToDraftEvent { createdAt actor { login } }
  ... on ReviewRequestedEvent { createdAt actor { login }
    requestedReviewer { __typename ... on User { login } ... on Bot { login } ... on Team { name } } }
  ... on PullRequestReview { createdAt submittedAt state author { __typename login } }
"""
)

_COMMON_TYPES = (
    "CLOSED_EVENT, REOPENED_EVENT, LABELED_EVENT, UNLABELED_EVENT, MILESTONED_EVENT, "
    "DEMILESTONED_EVENT, ASSIGNED_EVENT, UNASSIGNED_EVENT, CROSS_REFERENCED_EVENT, "
    "CONNECTED_EVENT, ISSUE_COMMENT"
)
_PR_TYPES = (
    _COMMON_TYPES
    + ", MERGED_EVENT, READY_FOR_REVIEW_EVENT, CONVERT_TO_DRAFT_EVENT, REVIEW_REQUESTED_EVENT, "
    "PULL_REQUEST_REVIEW"
)

_COMMON_FIELDS = """
  id number title url state createdAt closedAt updatedAt
  author { __typename login }
  authorAssociation
  labels(first: 50) { nodes { name } }
  milestone { title dueOn }
  assignees(first: 10) { nodes { login } }
  comments { totalCount }
"""

_ISSUE_NODE = (
    _COMMON_FIELDS
    + """
  stateReason
  issueType { name }
  timelineItems(first: 100, itemTypes: [%s]) {
    pageInfo { hasNextPage endCursor }
    nodes { %s }
  }
"""
    % (_COMMON_TYPES, _COMMON_TIMELINE)
)

# `commits` is here only for its author list: a co-author trailer such as
# "Co-Authored-By: Claude" shows up as a second author, which is the least bad signal available
# for LLM-assisted work. Names only, no e-mail addresses. The first 100 commits are enough for that.
_PR_NODE = (
    _COMMON_FIELDS
    + """
  isDraft mergedAt mergedBy { login }
  additions deletions changedFiles baseRefName headRefName reviewDecision
  body
  closingIssuesReferences(first: 20) { nodes { number } }
  commits(first: 100) { totalCount nodes { commit { committedDate
    authors(first: 4) { nodes { name user { login } } } } } }
  files(first: 100) {
    totalCount
    pageInfo { hasNextPage endCursor }
    nodes { path additions deletions }
  }
  timelineItems(first: 100, itemTypes: [%s]) {
    pageInfo { hasNextPage endCursor }
    nodes { %s }
  }
"""
    % (_PR_TYPES, _PR_TIMELINE)
)

_LIST_QUERY = """
query($owner: String!, $name: String!, $first: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    %(connection)s(first: $first, after: $cursor, orderBy: {field: CREATED_AT, direction: ASC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes { %(node)s }
    }
  }
  rateLimit { cost remaining resetAt }
}
"""

_MORE_TIMELINE_QUERY = """
query($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on %(type)s {
      timelineItems(first: 100, after: $cursor, itemTypes: [%(types)s]) {
        pageInfo { hasNextPage endCursor }
        nodes { %(timeline)s }
      }
    }
  }
  rateLimit { cost remaining resetAt }
}
"""

_MORE_FILES_QUERY = """
query($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on PullRequest {
      files(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { path additions deletions }
      }
    }
  }
  rateLimit { cost remaining resetAt }
}
"""

_META_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    nameWithOwner url createdAt
    defaultBranchRef { name }
    issues { totalCount }
    openIssues: issues(states: OPEN) { totalCount }
    pullRequests { totalCount }
    openPullRequests: pullRequests(states: OPEN) { totalCount }
    mergedPullRequests: pullRequests(states: MERGED) { totalCount }
    milestones(first: 100) { totalCount nodes { title state dueOn createdAt closedAt } }
    labels(first: 100) { totalCount nodes { name color } }
  }
  rateLimit { cost remaining resetAt }
}
"""

# A pull request with thousands of changed files (a vendored directory, a data dump) is not worth
# paging to the end: the "where do changes land" chart only needs to know which areas it touched.
MAX_FILES = 1000


def fetch_repo(repo: str, *, root: Path = DATA_ROOT, client: Client | None = None) -> Path:
    owner, name = split_repo(repo)
    own_client = client is None
    client = client or Client()
    raw = repo_dir(repo, root) / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    try:
        meta = client.graphql(_META_QUERY, {"owner": owner, "name": name})["repository"]
        meta["milestones"] = meta["milestones"]["nodes"]
        meta["labels"] = meta["labels"]["nodes"]
        meta["fetchedAt"] = datetime.now(UTC).isoformat(timespec="seconds")

        issues = _fetch_all(client, owner, name, "issues", _ISSUE_NODE, page_size=50)
        pulls = _fetch_all(client, owner, name, "pullRequests", _PR_NODE, page_size=20)
    finally:
        if own_client:
            client.close()

    # Written only once everything is in hand, so a failed fetch leaves the last good copy alone.
    _write_jsonl(raw / "issues.jsonl", issues)
    _write_jsonl(raw / "pulls.jsonl", pulls)
    (raw / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    _log(
        f"{repo}: {len(issues)} issues, {len(pulls)} pull requests, "
        f"{client.requests} requests, {client.rate_remaining} left on the rate limit"
    )
    return raw


def _fetch_all(
    client: Client, owner: str, name: str, connection: str, node: str, *, page_size: int
) -> list[dict[str, Any]]:
    query = _LIST_QUERY % {"connection": connection, "node": node}
    is_pr = connection == "pullRequests"
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        variables = {"owner": owner, "name": name, "first": page_size, "cursor": cursor}
        try:
            page = client.graphql(query, variables)["repository"][connection]
        except TransientGitHubError:
            # GitHub times a query out after ten seconds; a page of large pull requests can do
            # that. Smaller pages get through.
            if page_size <= 2:
                raise
            page_size = max(2, page_size // 2)
            _log(f"  {connection}: page timed out, retrying with pages of {page_size}")
            continue
        for item in page["nodes"]:
            _flatten(client, item, is_pr)
            items.append(item)
        _log(f"  {connection}: {len(items)}/{page['totalCount']}")
        if not page["pageInfo"]["hasNextPage"]:
            return items
        cursor = page["pageInfo"]["endCursor"]


def _flatten(client: Client, item: dict[str, Any], is_pr: bool) -> None:
    """Replace the paged connections on one item with plain lists, fetching any further pages."""
    timeline = item.pop("timelineItems")
    events = list(timeline["nodes"])
    info = timeline["pageInfo"]
    while info["hasNextPage"]:
        query = _MORE_TIMELINE_QUERY % {
            "type": "PullRequest" if is_pr else "Issue",
            "types": _PR_TYPES if is_pr else _COMMON_TYPES,
            "timeline": _PR_TIMELINE if is_pr else _COMMON_TIMELINE,
        }
        more = client.graphql(query, {"id": item["id"], "cursor": info["endCursor"]})["node"]["timelineItems"]
        events.extend(more["nodes"])
        info = more["pageInfo"]
    item["timeline"] = events

    item["labels"] = [label["name"] for label in item["labels"]["nodes"]]
    item["assignees"] = [user["login"] for user in item["assignees"]["nodes"]]
    item["comments"] = item["comments"]["totalCount"]
    if not is_pr:
        return

    files = item.pop("files")
    paths = list(files["nodes"])
    info = files["pageInfo"]
    while info["hasNextPage"] and len(paths) < MAX_FILES:
        more = client.graphql(_MORE_FILES_QUERY, {"id": item["id"], "cursor": info["endCursor"]})["node"]["files"]
        paths.extend(more["nodes"])
        info = more["pageInfo"]
    item["files"] = paths
    item["filesTotal"] = files["totalCount"]

    commits = item.pop("commits")
    item["commitsTotal"] = commits["totalCount"]
    item["commits"] = [
        {
            "committedDate": node["commit"]["committedDate"],
            "authors": [
                {"name": author["name"], "login": (author["user"] or {}).get("login")}
                for author in node["commit"]["authors"]["nodes"]
            ],
        }
        for node in commits["nodes"]
    ]
    item["closingIssues"] = [ref["number"] for ref in item.pop("closingIssuesReferences")["nodes"]]


def _write_jsonl(path: Path, items: list[dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as out:
        for item in items:
            out.write(json.dumps(item, ensure_ascii=False) + "\n")
    tmp.replace(path)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open() as lines:
        return [json.loads(line) for line in lines if line.strip()]


def _log(message: str) -> None:
    print(message, file=sys.stderr)
