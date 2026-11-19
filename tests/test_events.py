from datetime import UTC, datetime

from fixtures import closed, demilestoned, event, issue, milestoned, pull
from uberwatch.events import all_events, build_items


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def one(raw_issue=None, raw_pull=None):
    return build_items([raw_issue] if raw_issue else [], [raw_pull] if raw_pull else [])[0]


def test_open_issue_is_open_from_creation():
    item = one(issue(1, "2024-01-10T00:00:00Z"))
    assert item.open_spans == [(at("2024-01-10"), None)]
    assert not item.is_open_at(at("2024-01-09"))
    assert item.is_open_at(at("2025-01-01"))
    assert item.mismatches == []


def test_close_reopen_close_gives_two_spans():
    item = one(
        issue(
            1,
            "2024-01-10T00:00:00Z",
            [
                closed("2024-02-01T00:00:00Z"),
                event("ReopenedEvent", "2024-03-01T00:00:00Z"),
                closed("2024-04-01T00:00:00Z", closer="PullRequest"),
            ],
            state="CLOSED",
            stateReason="COMPLETED",
            closedAt="2024-04-01T00:00:00Z",
        )
    )
    assert item.open_spans == [(at("2024-01-10"), at("2024-02-01")), (at("2024-03-01"), at("2024-04-01"))]
    assert item.is_open_at(at("2024-01-20"))
    assert not item.is_open_at(at("2024-02-15"))
    assert item.is_open_at(at("2024-03-15"))
    assert not item.is_open_at(at("2024-04-01"))  # a span excludes its end
    assert item.closer == "pr"
    assert item.reason == "completed"


def test_closed_issue_without_a_closed_event_is_closed_from_the_snapshot():
    item = one(issue(1, "2024-01-10T00:00:00Z", state="CLOSED", closedAt="2024-02-01T00:00:00Z"))
    assert item.open_spans == [(at("2024-01-10"), at("2024-02-01"))]
    assert item.mismatches == ["closed now, but the timeline never closes it"]


def test_milestone_swap_in_the_same_second_leaves_no_gap():
    item = one(
        issue(
            1,
            "2024-01-10T00:00:00Z",
            [
                milestoned("2024-01-11T00:00:00Z", "Soon"),
                demilestoned("2024-03-01T00:00:00Z", "Soon"),
                milestoned("2024-03-01T00:00:00Z", "Later"),
            ],
            milestone={"title": "Later"},
        )
    )
    assert item.milestone_spans == [
        (at("2024-01-10"), at("2024-01-11"), None),
        (at("2024-01-11"), at("2024-03-01"), "Soon"),
        (at("2024-03-01"), None, "Later"),
    ]
    assert item.milestone_at(at("2024-02-01")) == "Soon"
    assert item.milestone_at(at("2024-03-01")) == "Later"
    assert item.mismatches == []


def test_renamed_milestone_takes_the_current_title_and_is_noted():
    item = one(
        issue(
            1,
            "2024-01-10T00:00:00Z",
            [milestoned("2024-01-11T00:00:00Z", "March release")],
            milestone={"title": "v1.0"},
        )
    )
    assert item.milestone_spans[-1] == (at("2024-01-11"), None, "v1.0")
    assert len(item.mismatches) == 1


def test_draft_start_is_inferred_from_the_first_change():
    started_draft = one(
        raw_pull=pull(1, "2024-01-10T00:00:00Z", [event("ReadyForReviewEvent", "2024-01-12T00:00:00Z")])
    )
    assert started_draft.started_as_draft
    assert started_draft.ready_at == at("2024-01-12")

    started_ready = one(
        raw_pull=pull(
            2, "2024-01-10T00:00:00Z", [event("ConvertToDraftEvent", "2024-01-12T00:00:00Z")], isDraft=True
        )
    )
    assert not started_ready.started_as_draft
    assert started_ready.ready_at == at("2024-01-10")

    still_draft = one(raw_pull=pull(3, "2024-01-10T00:00:00Z", isDraft=True))
    assert still_draft.started_as_draft
    assert still_draft.ready_at is None


def test_merged_pull_request_state():
    item = one(
        raw_pull=pull(
            1,
            "2024-01-10T00:00:00Z",
            [event("MergedEvent", "2024-01-15T00:00:00Z"), closed("2024-01-15T00:00:00Z")],
            state="MERGED",
            mergedAt="2024-01-15T00:00:00Z",
            closedAt="2024-01-15T00:00:00Z",
        )
    )
    assert item.state == "merged"
    assert item.open_spans == [(at("2024-01-10"), at("2024-01-15"))]


def test_assistance_from_bot_author_body_and_co_author():
    bot = one(raw_pull=pull(1, "2024-01-10T00:00:00Z", author={"__typename": "Bot", "login": "dependabot"}))
    body = one(raw_pull=pull(2, "2024-01-10T00:00:00Z", body="Generated with [Claude Code](https://x)"))
    trailer = one(
        raw_pull=pull(
            3,
            "2024-01-10T00:00:00Z",
            commits=[
                {"authors": [{"name": "Alice", "login": "alice"}, {"name": "Claude Opus 5", "login": None}]}
            ],
        )
    )
    plain = one(raw_pull=pull(4, "2024-01-10T00:00:00Z", body="Fixes the claudication term"))
    assert [bot.assisted, body.assisted, trailer.assisted, plain.assisted] == ["bot", "llm", "llm", "human"]


def test_all_events_includes_creation_and_is_ordered():
    items = build_items(
        [issue(1, "2024-01-10T00:00:00Z", [closed("2024-03-01T00:00:00Z")], state="CLOSED")],
        [pull(2, "2024-02-01T00:00:00Z")],
    )
    log = all_events(items)
    assert [(e.number, e.type) for e in log] == [(1, "opened"), (2, "opened"), (1, "closed")]
