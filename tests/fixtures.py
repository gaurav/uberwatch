"""Hand-written raw items in the shape fetch.py stores them, small enough to reason about."""

from __future__ import annotations

from typing import Any


def event(typename: str, at: str, **fields: Any) -> dict[str, Any]:
    return {"__typename": typename, "createdAt": at, "actor": {"login": "alice"}, **fields}


def issue(number: int, created: str, timeline: list[dict[str, Any]] | None = None, **fields: Any) -> dict:
    raw = {
        "id": f"I_{number}",
        "number": number,
        "title": f"Issue {number}",
        "url": f"https://example.org/issues/{number}",
        "state": "OPEN",
        "stateReason": None,
        "createdAt": created,
        "closedAt": None,
        "updatedAt": created,
        "author": {"__typename": "User", "login": "alice"},
        "authorAssociation": "MEMBER",
        "labels": [],
        "milestone": None,
        "assignees": [],
        "comments": 0,
        "issueType": None,
        "timeline": timeline or [],
    }
    raw.update(fields)
    return raw


def pull(number: int, created: str, timeline: list[dict[str, Any]] | None = None, **fields: Any) -> dict:
    raw = issue(number, created, timeline)
    raw.update(
        {
            "id": f"PR_{number}",
            "title": f"Pull request {number}",
            "isDraft": False,
            "mergedAt": None,
            "mergedBy": None,
            "additions": 10,
            "deletions": 2,
            "body": "",
            "files": [],
            "commits": [],
            "closingIssues": [],
        }
    )
    raw.update(fields)
    return raw


def closed(at: str, closer: str | None = None, reason: str = "COMPLETED") -> dict[str, Any]:
    return event("ClosedEvent", at, stateReason=reason, closer={"__typename": closer} if closer else None)


def milestoned(at: str, title: str) -> dict[str, Any]:
    return event("MilestonedEvent", at, milestoneTitle=title)


def demilestoned(at: str, title: str) -> dict[str, Any]:
    return event("DemilestonedEvent", at, milestoneTitle=title)


def review(at: str, login: str, state: str = "APPROVED", bot: bool = False) -> dict[str, Any]:
    return {
        "__typename": "PullRequestReview",
        "createdAt": at,
        "submittedAt": at,
        "state": state,
        "author": {"__typename": "Bot" if bot else "User", "login": login},
    }
