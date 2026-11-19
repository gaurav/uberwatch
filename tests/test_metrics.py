import re
from collections import Counter
from datetime import UTC, datetime

from fixtures import closed, demilestoned, event, issue, milestoned, pull, review
from uberwatch import metrics
from uberwatch.config import NO_MILESTONE, RepoConfig
from uberwatch.events import build_items


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


NOW = at("2024-07-01")

CONFIG = RepoConfig(
    milestone_buckets=[("Soon", re.compile("^Soon$")), ("Later", re.compile("^Later$"))],
    other_milestones="Releases",
)


def sample_issues():
    """Four issues: one closed in its release, one bumped out of it, one untouched, one reopened."""
    return build_items(
        [
            issue(
                1,
                "2023-03-01T00:00:00Z",
                [
                    milestoned("2023-03-02T00:00:00Z", "v1"),
                    closed("2023-06-01T00:00:00Z", closer="PullRequest"),
                ],
                state="CLOSED",
                stateReason="COMPLETED",
                closedAt="2023-06-01T00:00:00Z",
                milestone={"title": "v1"},
            ),
            issue(
                2,
                "2023-03-01T00:00:00Z",
                [
                    milestoned("2023-03-02T00:00:00Z", "v1"),
                    demilestoned("2024-02-01T00:00:00Z", "v1"),
                    milestoned("2024-02-01T00:00:00Z", "Later"),
                ],
                milestone={"title": "Later"},
            ),
            issue(3, "2024-03-01T00:00:00Z"),
            issue(
                4,
                "2023-05-01T00:00:00Z",
                [closed("2023-05-10T00:00:00Z"), event("ReopenedEvent", "2024-05-01T00:00:00Z")],
            ),
        ],
        [],
    )


def test_grid_covers_both_ends():
    weeks = metrics.grid(at("2024-01-03"), at("2024-01-20"), "week")
    assert [metrics.day(w) for w in weeks] == ["2024-01-01", "2024-01-08", "2024-01-15"]
    quarters = metrics.grid(at("2023-11-15"), at("2024-04-02"), "quarter")
    assert [metrics.day(q) for q in quarters] == ["2023-10-01", "2024-01-01", "2024-04-01"]


def test_flow_counts_reopening_as_inflow_and_ends_at_the_open_count():
    issues = sample_issues()
    rows = metrics.flow(issues, metrics.grid(at("2023-03-01"), NOW, "month"))
    assert sum(r["opened"] for r in rows) == 5  # four created, one reopened
    assert sum(r["closed"] for r in rows) == 2
    assert rows[-1]["open"] == 3 == sum(i.state == "open" for i in issues)
    may_2023 = next(r for r in rows if r["date"] == "2023-05-01")
    assert (may_2023["opened"], may_2023["closed"]) == (1, 1)


def test_backlog_by_milestone_uses_the_milestone_of_the_day():
    issues = sample_issues()
    rows = metrics.backlog_by_milestone(issues, [at("2023-04-01"), at("2024-06-01")], CONFIG)
    counts = {(r["date"], r["band"]): r["count"] for r in rows}
    assert counts["2023-04-01", "Releases"] == 2  # issues 1 and 2, both in v1
    assert counts["2024-06-01", "Releases"] == 0
    assert counts["2024-06-01", "Later"] == 1
    assert counts["2024-06-01", NO_MILESTONE] == 2  # issue 3, and issue 4 reopened


def test_survival_keeps_open_issues_in_the_denominator():
    issues = build_items(
        [
            issue(n, "2024-01-01T00:00:00Z", [closed("2024-01-11T00:00:00Z")], state="CLOSED")
            for n in range(1, 5)
        ]
        + [issue(n, "2024-01-01T00:00:00Z") for n in range(5, 9)],
        [],
    )
    rows = metrics.survival(issues, NOW, str)
    share = {r["days"]: r["share"] for r in rows}
    assert share[7] == 1.0
    assert share[10] == 0.5 == share[180]  # half closed on day 10; the rest are still open
    assert max(share) == 180  # stops at the longest observed follow-up, 182 days


def test_milestone_flow_conserves_issues():
    issues = sample_issues()
    times = [at("2023-04-01"), at("2024-01-01"), NOW]
    result = metrics.milestone_flow(issues, times, CONFIG, smallest=0)
    nodes = result["nodes"]
    flows = Counter()
    for link in result["links"]:
        source, target = nodes[link["source"]], nodes[link["target"]]
        assert target["column"] == source["column"] + 1
        flows[source["band"], target["band"], source["column"]] += link["value"]
    # 2023-04 -> 2024-01: issue 1 closes, issue 2 stays in the release, issue 4 opens and closes.
    assert flows["Releases", metrics.CLOSED, 0] == 1
    assert flows["Releases", "Releases", 0] == 1
    assert flows[metrics.NEW, metrics.CLOSED, 0] == 1
    # 2024-01 -> now: issue 2 is bumped to Later, issue 3 is new, issue 4 comes back.
    assert flows["Releases", "Later", 1] == 1
    assert flows[metrics.NEW, NO_MILESTONE, 1] == 2
    # What flows into the last column's open bands is exactly what is open now.
    arriving = sum(
        link["value"]
        for link in result["links"]
        if nodes[link["target"]]["column"] == 2 and nodes[link["target"]]["band"] != metrics.CLOSED
    )
    assert arriving == 3


def test_milestone_outcomes():
    rows = metrics.milestone_outcomes(sample_issues() * 2, {"milestones": []}, lambda title: title == "v1")
    counts = {r["outcome"]: r["count"] for r in rows}
    assert counts == {"Closed in it": 2, "Moved out, still open": 2, "Still open in it": 0}


def test_review_coverage_ignores_the_author_and_bots():
    prs = build_items(
        [],
        [
            pull(1, "2024-01-01T00:00:00Z", [review("2024-01-02T00:00:00Z", "alice", "COMMENTED")]),
            pull(
                2, "2024-01-01T00:00:00Z", [review("2024-01-02T00:00:00Z", "copilot", "COMMENTED", bot=True)]
            ),
            pull(
                3,
                "2024-01-01T00:00:00Z",
                [
                    review("2024-01-02T00:00:00Z", "bob", "CHANGES_REQUESTED"),
                    review("2024-01-03T00:00:00Z", "bob", "APPROVED"),
                ],
            ),
        ],
    )
    facts = [metrics.review_facts(pr) for pr in prs]
    assert [f["coverage"] for f in facts] == ["No review", "Bot review only", "Approved by a person"]
    assert [metrics.stage(pr, f) for pr, f in zip(prs, facts, strict=True)] == [
        "Awaiting first review",
        "Awaiting first review",
        "Approved, not merged",
    ]


def test_area_groups_by_leading_directories():
    assert metrics.area("README.md", 1) == "(top-level files)"
    assert metrics.area("src/ontology/uberon-edit.obo", 1) == "src/"
    assert metrics.area("src/ontology/uberon-edit.obo", 2) == "src/ontology/"
    assert metrics.area("src/x.py", 2) == "src/"
