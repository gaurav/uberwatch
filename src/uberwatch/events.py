"""Turn the raw GitHub items into `Item`s that know what state they were in at any past date.

GitHub only reports what an issue looks like now. Its timeline says what happened to it, so the
past is recovered by replaying that: an item is open from when it was created until a `closed`
event, in a milestone from a `milestoned` event until the next `demilestoned`, a draft until
`ready_for_review`. Each replay ends by checking itself against the present-day snapshot, and
`Item.mismatches` records where the two disagree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# GraphQL timeline type -> the event name used everywhere after this module.
_EVENT_NAMES = {
    "ClosedEvent": "closed",
    "ReopenedEvent": "reopened",
    "MergedEvent": "merged",
    "LabeledEvent": "labeled",
    "UnlabeledEvent": "unlabeled",
    "MilestonedEvent": "milestoned",
    "DemilestonedEvent": "demilestoned",
    "AssignedEvent": "assigned",
    "UnassignedEvent": "unassigned",
    "CrossReferencedEvent": "cross_referenced",
    "ConnectedEvent": "connected",
    "IssueComment": "commented",
    "ReadyForReviewEvent": "ready_for_review",
    "ConvertToDraftEvent": "converted_to_draft",
    "ReviewRequestedEvent": "review_requested",
    "PullRequestReview": "reviewed",
}

# Matched against commit co-author names and pull request bodies. A rough signal: it finds work
# that says an LLM tool helped, and misses work that doesn't say.
DEFAULT_LLM_PATTERN = (
    r"\bclaude\b|\bcopilot\b|\bcodex\b|\bcursor agent\b|\bdevin\b|\baider\b|\bgemini\b|chatgpt|"
    r"generated with \[?claude code"
)

Span = tuple[datetime, datetime | None]


@dataclass(frozen=True)
class Event:
    kind: str  # "issue" or "pr"
    number: int
    at: datetime
    type: str
    actor: str | None
    actor_is_bot: bool = False
    detail: Any = None


@dataclass
class Item:
    kind: str  # "issue" or "pr"
    number: int
    title: str
    url: str
    author: str | None
    author_is_bot: bool
    association: str
    created: datetime
    closed: datetime | None  # the current closure; None while open
    merged: datetime | None
    state: str  # "open", "closed" or "merged"
    reason: str | None  # issues: "completed", "not_planned", "duplicate"
    labels: list[str]
    milestone: str | None
    issue_type: str | None
    comments: int
    events: list[Event]
    # Replayed history. A span with no end is still running.
    open_spans: list[Span] = field(default_factory=list)
    milestone_spans: list[tuple[datetime, datetime | None, str | None]] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    # Pull requests only.
    is_draft: bool = False
    started_as_draft: bool = False
    ready_at: datetime | None = None  # first moment it was not a draft
    merged_by: str | None = None
    additions: int = 0
    deletions: int = 0
    files: list[str] = field(default_factory=list)
    closing_issues: list[int] = field(default_factory=list)
    assisted: str = "human"  # "bot", "llm" or "human": see classify_assistance

    def is_open_at(self, when: datetime) -> bool:
        return any(start <= when and (end is None or when < end) for start, end in self.open_spans)

    def milestone_at(self, when: datetime) -> str | None:
        for start, end, title in self.milestone_spans:
            if start <= when and (end is None or when < end):
                return title
        return None

    def first(self, event_type: str) -> Event | None:
        return next((e for e in self.events if e.type == event_type), None)

    @property
    def last_comment(self) -> datetime | None:
        times = [e.at for e in self.events if e.type in ("commented", "reviewed")]
        return max(times) if times else None

    @property
    def closer(self) -> str | None:
        """What the last `closed` event says closed it: "pr", "commit" or None for by hand."""
        closes = [e for e in self.events if e.type == "closed"]
        return closes[-1].detail.get("closer") if closes else None


def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def build_items(
    issues: list[dict[str, Any]],
    pulls: list[dict[str, Any]],
    *,
    llm_pattern: str = DEFAULT_LLM_PATTERN,
    bots: frozenset[str] = frozenset(),
) -> list[Item]:
    llm = re.compile(llm_pattern, re.IGNORECASE)
    items = [_build(raw, "issue", llm, bots) for raw in issues]
    items += [_build(raw, "pr", llm, bots) for raw in pulls]
    return items


def _build(raw: dict[str, Any], kind: str, llm: re.Pattern[str], bots: frozenset[str]) -> Item:
    author = raw.get("author") or {}
    created = parse_time(raw["createdAt"])
    assert created is not None
    merged = parse_time(raw.get("mergedAt"))
    item = Item(
        kind=kind,
        number=raw["number"],
        title=raw["title"],
        url=raw["url"],
        author=author.get("login"),
        author_is_bot=_is_bot(author, bots),
        association=raw.get("authorAssociation") or "NONE",
        created=created,
        closed=parse_time(raw.get("closedAt")),
        merged=merged,
        state="merged" if merged else raw["state"].lower(),
        reason=(raw.get("stateReason") or "").lower() or None,
        labels=list(raw.get("labels") or []),
        milestone=(raw.get("milestone") or {}).get("title"),
        issue_type=(raw.get("issueType") or {}).get("name"),
        comments=raw.get("comments") or 0,
        events=_events(raw, kind, bots),
    )
    if item.reason == "reopened":  # an open issue that was once closed, not a reason for closing
        item.reason = None
    _replay_open(item)
    _replay_milestones(item)
    if kind == "pr":
        item.is_draft = bool(raw.get("isDraft"))
        item.merged_by = (raw.get("mergedBy") or {}).get("login")
        item.additions = raw.get("additions") or 0
        item.deletions = raw.get("deletions") or 0
        item.files = [f["path"] for f in raw.get("files") or []]
        item.closing_issues = list(raw.get("closingIssues") or [])
        _replay_draft(item)
        item.assisted = classify_assistance(raw, item, llm)
    return item


def _is_bot(actor: dict[str, Any] | None, bots: frozenset[str]) -> bool:
    if not actor:
        return False
    login = actor.get("login") or ""
    return actor.get("__typename") == "Bot" or login in bots or login.endswith("[bot]")


def _events(raw: dict[str, Any], kind: str, bots: frozenset[str]) -> list[Event]:
    events = []
    for node in raw.get("timeline") or []:
        name = _EVENT_NAMES.get(node.get("__typename"))
        # A review that was started and never submitted has no time anyone else could see it.
        at = parse_time(node.get("submittedAt") or node.get("createdAt"))
        if name is None or at is None:
            continue
        actor = node.get("actor") or node.get("author") or {}
        events.append(
            Event(
                kind=kind,
                number=raw["number"],
                at=at,
                type=name,
                actor=actor.get("login"),
                actor_is_bot=_is_bot(actor, bots),
                detail=_detail(name, node),
            )
        )
    # GitHub returns the timeline in order already; the sort is stable, so events sharing a
    # timestamp (a milestone swap is a demilestone and a milestone in the same second) keep it.
    events.sort(key=lambda e: e.at)
    return events


def _detail(name: str, node: dict[str, Any]) -> Any:
    if name == "closed":
        closer = (node.get("closer") or {}).get("__typename")
        return {
            "closer": {"PullRequest": "pr", "Commit": "commit"}.get(closer),
            "reason": (node.get("stateReason") or "").lower() or None,
        }
    if name in ("labeled", "unlabeled"):
        return (node.get("label") or {}).get("name")
    if name in ("milestoned", "demilestoned"):
        return node.get("milestoneTitle")
    if name == "reviewed":
        return (node.get("state") or "").lower()
    if name in ("assigned", "unassigned"):
        return (node.get("assignee") or {}).get("login")
    if name == "review_requested":
        reviewer = node.get("requestedReviewer") or {}
        return reviewer.get("login") or reviewer.get("name")
    if name in ("cross_referenced", "connected"):
        source = node.get("source") or node.get("subject") or {}
        return {
            "kind": {"PullRequest": "pr", "Issue": "issue"}.get(source.get("__typename")),
            "number": source.get("number"),
            "repo": (source.get("repository") or {}).get("nameWithOwner"),
        }
    return None


def _replay_open(item: Item) -> None:
    spans: list[Span] = []
    opened: datetime | None = item.created
    for event in item.events:
        if event.type == "closed" and opened is not None:
            spans.append((opened, event.at))
            opened = None
        elif event.type == "reopened" and opened is None:
            opened = event.at
    if opened is not None:
        spans.append((opened, None))

    # Reconcile with the snapshot, which is the authority on the present.
    replayed_open = spans[-1][1] is None
    if item.state == "open" and not replayed_open:
        item.mismatches.append("open now, but the timeline ends closed")
        spans.append((spans[-1][1] or item.created, None))
    elif item.state != "open" and replayed_open:
        # Seen when the closing event is missing from the timeline (deleted actors, old imports).
        item.mismatches.append("closed now, but the timeline never closes it")
        spans[-1] = (spans[-1][0], item.closed or item.created)
    item.open_spans = spans


def _replay_milestones(item: Item) -> None:
    spans: list[tuple[datetime, datetime | None, str | None]] = []
    current: str | None = None
    since = item.created
    for event in item.events:
        if event.type == "milestoned":
            new = event.detail
        elif event.type == "demilestoned":
            new = None
        else:
            continue
        if new == current:
            continue
        if event.at > since:
            spans.append((since, event.at, current))
        current, since = new, event.at
    if current != item.milestone:
        # The events carry the milestone's title at the time; a later rename shows up here, and
        # is not an error. Trust the snapshot for the span that is still running.
        item.mismatches.append(f"milestone: timeline ends in {current!r}, now {item.milestone!r}")
        current = item.milestone
    spans.append((since, None, current))
    item.milestone_spans = spans


def _replay_draft(item: Item) -> None:
    """Work out whether a pull request started as a draft, and when it first stopped being one.

    GitHub records the changes (`ready_for_review`, `converted_to_draft`) but not the starting
    state, so that is inferred from the first change, or from the present if it never changed.
    """
    changes = [e for e in item.events if e.type in ("ready_for_review", "converted_to_draft")]
    if changes:
        item.started_as_draft = changes[0].type == "ready_for_review"
    else:
        item.started_as_draft = item.is_draft
    if not item.started_as_draft:
        item.ready_at = item.created
    else:
        ready = next((e for e in changes if e.type == "ready_for_review"), None)
        item.ready_at = ready.at if ready else None


def classify_assistance(raw: dict[str, Any], item: Item, llm: re.Pattern[str]) -> str:
    """ "bot" for a bot account's pull request, "llm" if it says an LLM tool took part, else "human"."""
    if item.author_is_bot:
        return "bot"
    if llm.search(raw.get("body") or ""):
        return "llm"
    for commit in raw.get("commits") or []:
        for author in commit.get("authors") or []:
            if llm.search(author.get("name") or ""):
                return "llm"
    return "human"


def all_events(items: list[Item]) -> list[Event]:
    """Every event of every item as one time-ordered log, each item's creation included."""
    log = [
        Event(item.kind, item.number, item.created, "opened", item.author, item.author_is_bot)
        for item in items
    ]
    for item in items:
        log.extend(item.events)
    log.sort(key=lambda e: e.at)
    return log


def load_items(raw_dir, **kwargs: Any) -> list[Item]:
    from .fetch import read_jsonl

    return build_items(read_jsonl(raw_dir / "issues.jsonl"), read_jsonl(raw_dir / "pulls.jsonl"), **kwargs)
