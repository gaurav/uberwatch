"""Download every issue and pull request of a repository, with its timeline, as JSON lines.

Output, under data/<owner>/<repo>/raw/:

    issues.jsonl   one issue per line, as GitHub's GraphQL API returns it, except that the
    pulls.jsonl    paged connections are flattened: `timeline`, `files`, `commits` are plain lists
    meta.json      when the fetch ran, the repository's milestones and labels, GitHub's own totals

Two passes. The first lists every item's node ID and `updatedAt`, a hundred to a request. The
second loads each item that is new or has changed since the copy on disk, ONE ITEM PER REQUEST,
a few requests at a time. A first fetch therefore costs one request per item; later ones cost
about one per item touched since.

Do not batch the second pass. Selecting `timelineItems` for many items in one request, whether
inside `repository.issues(first: 50)` or `nodes(ids: [...50 ids])`, silently returns incomplete
timelines: the newest events are left out, `hasNextPage` is false, and for issues even
`totalCount` is reduced to match, so nothing in the response says anything is missing. On
NCATSTranslator/Babel that hid the latest milestone change of 168 of 770 issues. Batches of 25
issues happened to come back whole and batches of 20 pull requests did not, so there is no batch
size to trust; a request for a single item returns everything. Checked 2026-11-19.

`timelineItems.totalCount` is no use as a completeness check either: on a pull request it counts
every timeline item, ignoring the `itemTypes` filter. What does check the fetch is the replay in
events.py, which has to arrive at the state GitHub reports now.
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .github import Client

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
    _COMMON_TYPES + ", MERGED_EVENT, READY_FOR_REVIEW_EVENT, CONVERT_TO_DRAFT_EVENT, REVIEW_REQUESTED_EVENT, "
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

_IDS_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    %(connection)s(first: 100, after: $cursor, orderBy: {field: CREATED_AT, direction: ASC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes { id number updatedAt }
    }
  }
  rateLimit { cost remaining resetAt }
}
"""

_NODE_QUERY = """
query($id: ID!) {
  node(id: $id) { ... on %(type)s { %(node)s } }
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


# Bump when the selection changes, so copies fetched with the old one are not reused.
RAW_SCHEMA = 2

# Simultaneous requests. GitHub's secondary rate limit allows many more, but this is polite and
# already finishes a thousand items in about a minute.
WORKERS = 6


def fetch_repo(
    repo: str, *, root: Path = DATA_ROOT, client: Client | None = None, full: bool = False
) -> Path:
    owner, name = split_repo(repo)
    own_client = client is None
    client = client or Client()
    raw = repo_dir(repo, root) / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    reuse = not full and _stored_schema(raw) == RAW_SCHEMA
    try:
        meta = client.graphql(_META_QUERY, {"owner": owner, "name": name})["repository"]
        meta["milestones"] = meta["milestones"]["nodes"]
        meta["labels"] = meta["labels"]["nodes"]
        meta["fetchedAt"] = datetime.now(UTC).isoformat(timespec="seconds")
        meta["schema"] = RAW_SCHEMA

        issues = _fetch_all(client, owner, name, "issues", raw / "issues.jsonl", reuse)
        pulls = _fetch_all(client, owner, name, "pullRequests", raw / "pulls.jsonl", reuse)
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


def _stored_schema(raw: Path) -> int | None:
    try:
        return json.loads((raw / "meta.json").read_text()).get("schema")
    except (OSError, ValueError):
        return None


def _fetch_all(
    client: Client, owner: str, name: str, connection: str, stored_path: Path, reuse: bool
) -> list[dict[str, Any]]:
    is_pr = connection == "pullRequests"
    listing: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        variables = {"owner": owner, "name": name, "cursor": cursor}
        page = client.graphql(_IDS_QUERY % {"connection": connection}, variables)["repository"][connection]
        listing.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]

    stored = {}
    if reuse and stored_path.exists():
        stored = {item["id"]: item for item in read_jsonl(stored_path)}
    query = _NODE_QUERY % {
        "type": "PullRequest" if is_pr else "Issue",
        "node": _PR_NODE if is_pr else _ISSUE_NODE,
    }

    def load(entry: dict[str, Any]) -> dict[str, Any] | None:
        kept = stored.get(entry["id"])
        if kept and kept["updatedAt"] == entry["updatedAt"]:
            return kept
        item = client.graphql(query, {"id": entry["id"]})["node"]
        if item:  # None if it was deleted between the two passes
            _flatten(client, item, is_pr)
        return item

    items: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for done, item in enumerate(pool.map(load, listing), start=1):
            if item:
                items.append(item)
            if done % 100 == 0 or done == len(listing):
                _log(f"  {connection}: {done}/{len(listing)}")
    fetched = sum(1 for item in items if stored.get(item["id"]) is not item)
    _log(f"  {connection}: {fetched} fetched, {len(items) - fetched} unchanged since the last fetch")
    return items


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
        more = client.graphql(_MORE_FILES_QUERY, {"id": item["id"], "cursor": info["endCursor"]})["node"][
            "files"
        ]
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
