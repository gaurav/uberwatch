"""Chart datasets, computed from replayed items. Everything here is a pure function of its
arguments: `now` is passed in (the time of the fetch), never read from the clock, so the same
data on disk always builds the same dashboard.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import median
from typing import Any

from .config import NO_MILESTONE, RepoConfig
from .events import Item

DAY = 86400.0


# --- time grid -------------------------------------------------------------------------------


def floor_period(when: datetime, unit: str) -> datetime:
    day = when.replace(hour=0, minute=0, second=0, microsecond=0)
    if unit == "week":
        return day - timedelta(days=day.weekday())
    if unit == "month":
        return day.replace(day=1)
    if unit == "quarter":
        return day.replace(month=3 * ((day.month - 1) // 3) + 1, day=1)
    if unit == "year":
        return day.replace(month=1, day=1)
    raise ValueError(unit)


def next_period(start: datetime, unit: str) -> datetime:
    if unit == "week":
        return start + timedelta(days=7)
    months = {"month": 1, "quarter": 3, "year": 12}[unit]
    total = start.year * 12 + (start.month - 1) + months
    return start.replace(year=total // 12, month=total % 12 + 1)


def grid(start: datetime, end: datetime, unit: str) -> list[datetime]:
    """The start of every period from the one containing `start` to the one containing `end`."""
    starts = [floor_period(start, unit)]
    while (following := next_period(starts[-1], unit)) <= end:
        starts.append(following)
    return starts


def fine_unit(start: datetime, end: datetime) -> str:
    """Weeks while that is at most a few hundred bars, months for a longer history."""
    return "week" if (end - start).days <= 8 * 365 else "month"


def coarse_unit(start: datetime, end: datetime) -> str:
    return "quarter" if (end - start).days <= 8 * 365 else "year"


def day(when: datetime | None) -> str | None:
    return when.strftime("%Y-%m-%d") if when else None


def period_label(start: datetime, unit: str) -> str:
    if unit == "quarter":
        return f"{start.year} Q{(start.month - 1) // 3 + 1}"
    if unit == "year":
        return str(start.year)
    return day(start) or ""


def days_between(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / DAY


def quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


# --- accumulation ----------------------------------------------------------------------------


def flow(items: list[Item], starts: list[datetime]) -> list[dict[str, Any]]:
    """Per period: items that became open (created or reopened), that stopped being open, and
    the number open at the period's end. For pull requests `merged` is the part of `closed`
    that was a merge."""
    opened = [0] * len(starts)
    closed = [0] * len(starts)
    merged = [0] * len(starts)
    for item in items:
        for index, (start, end) in enumerate(item.open_spans):
            opened[bisect_right(starts, start) - 1] += 1
            if end is not None:
                period = bisect_right(starts, end) - 1
                closed[period] += 1
                if item.state == "merged" and index == len(item.open_spans) - 1:
                    merged[period] += 1
    rows = []
    running = 0
    for index, start in enumerate(starts):
        running += opened[index] - closed[index]
        rows.append(
            {
                "date": day(start),
                "opened": opened[index],
                "closed": closed[index],
                "merged": merged[index],
                "open": running,
            }
        )
    return rows


def sample_times(starts: list[datetime], unit: str, now: datetime) -> list[datetime]:
    """The end of each period, the last one cut short at `now`."""
    return [min(next_period(start, unit), now) for start in starts]


def backlog_by_milestone(
    issues: list[Item], times: list[datetime], config: RepoConfig
) -> list[dict[str, Any]]:
    rows = []
    for when in times:
        counts = Counter(config.bucket(i.milestone_at(when)) for i in issues if i.is_open_at(when))
        rows.extend({"date": day(when), "band": band, "count": counts[band]} for band in config.bucket_order)
    return rows


def cohort_labeller(first_year: int, last_year: int, most: int = 5):
    """Group creation years into at most `most` cohorts of equal width.

    Five, because cohorts are drawn as steps of one hue, and five is as many steps as stay
    tellable apart between the lightest blue that shows on the page and the darkest.
    """
    width = math.ceil((last_year - first_year + 1) / most)

    def label(year: int) -> str:
        start = first_year + ((year - first_year) // width) * width
        end = min(start + width - 1, last_year)
        return str(start) if width == 1 else f"{start}–{str(end)[2:]}" if end > start else str(start)

    return label


def backlog_by_vintage(issues: list[Item], times: list[datetime], label) -> list[dict[str, Any]]:
    cohorts = sorted({label(i.created.year) for i in issues})
    rows = []
    for when in times:
        counts = Counter(label(i.created.year) for i in issues if i.is_open_at(when))
        rows.extend({"date": day(when), "cohort": c, "count": counts[c]} for c in cohorts)
    return rows


# --- speed -----------------------------------------------------------------------------------

CHECKPOINTS = [0, 1, 2, 3, 5, 7, 10, 14, 21, 30, 45, 60, 90, 120, 180, 270, 365, 545, 730, 1095, 1460]
CHECKPOINTS += [1825, 2555, 3650, 5475]


def survival(items: list[Item], now: datetime, label, smallest: int = 8) -> list[dict[str, Any]]:
    """Kaplan-Meier share of items still open N days after opening, per creation cohort.

    The median time to close of the items that did close flatters a repository with a large
    backlog, because the ones still open are not in it. This estimate keeps them in the
    denominator for as long as they have been observed. "Open" means until the first close.
    """
    cohorts: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    for item in items:
        start, end = item.open_spans[0]
        cohorts[label(item.created.year)].append((days_between(start, end or now), end is not None))
    rows = []
    for cohort, observed in sorted(cohorts.items()):
        if len(observed) < smallest:
            continue
        observed.sort()
        steps = [(0.0, 1.0)]
        at_risk = len(observed)
        share = 1.0
        index = 0
        while index < len(observed):
            time = observed[index][0]
            closed = censored = 0
            while index < len(observed) and observed[index][0] == time:
                closed += observed[index][1]
                censored += not observed[index][1]
                index += 1
            if closed:
                share *= 1 - closed / at_risk
                steps.append((time, share))
            at_risk -= closed + censored
        longest = observed[-1][0]
        for checkpoint in CHECKPOINTS:
            if checkpoint > longest:
                break
            value = [s for t, s in steps if t <= checkpoint][-1]
            rows.append({"cohort": cohort, "days": checkpoint, "share": round(value, 4), "n": len(observed)})
    return rows


def review_facts(pr: Item) -> dict[str, Any]:
    """Who looked at a pull request. A review or comment by its author, or by a bot, is not a
    human review."""
    human = [e for e in pr.events if e.type == "reviewed" and not e.actor_is_bot and e.actor != pr.author]
    bot = [e for e in pr.events if e.type == "reviewed" and e.actor_is_bot]
    if any(e.detail == "approved" for e in human):
        coverage = "Approved by a person"
    elif human:
        coverage = "Reviewed, not approved"
    elif bot:
        coverage = "Bot review only"
    else:
        coverage = "No review"
    verdicts = [e.detail for e in human if e.detail in ("approved", "changes_requested")]
    return {
        "first_human": human[0].at if human else None,
        "coverage": coverage,
        "verdict": verdicts[-1] if verdicts else None,
        "reviewers": sorted({e.actor for e in human if e.actor}),
    }


def stage(pr: Item, facts: dict[str, Any]) -> str:
    """Where an open pull request is waiting."""
    if pr.is_draft:
        return "Draft"
    if facts["verdict"] == "approved":
        return "Approved, not merged"
    if facts["verdict"] == "changes_requested":
        return "Changes requested"
    if facts["first_human"]:
        return "In review"
    return "Awaiting first review"


COVERAGE = ["Approved by a person", "Reviewed, not approved", "Bot review only", "No review"]


def pr_pipeline(prs: list[Item], unit: str) -> dict[str, list[dict[str, Any]]]:
    """Per period in which pull requests merged: how long they took and who had reviewed them.
    Bot-authored pull requests (dependency bumps) are left out; they would swamp both."""
    by_period: dict[datetime, list[Item]] = defaultdict(list)
    for pr in prs:
        if pr.merged and not pr.author_is_bot:
            by_period[floor_period(pr.merged, unit)].append(pr)
    timing, coverage = [], []
    for start in sorted(by_period):
        merged = by_period[start]
        label = day(start)
        to_merge = [days_between(pr.created, pr.merged) for pr in merged]
        facts = [review_facts(pr) for pr in merged]
        to_review = [
            days_between(pr.ready_at or pr.created, f["first_human"])
            for pr, f in zip(merged, facts, strict=True)
            if f["first_human"]
        ]
        timing.append(
            {
                "date": label,
                "n": len(merged),
                "p25": round(quantile(to_merge, 0.25), 2),
                "p50": round(quantile(to_merge, 0.5), 2),
                "p75": round(quantile(to_merge, 0.75), 2),
                "review_p50": round(max(0.0, median(to_review)), 2) if to_review else None,
                "self_merged": sum(pr.merged_by == pr.author for pr in merged),
            }
        )
        counts = Counter(f["coverage"] for f in facts)
        coverage.extend({"date": label, "coverage": c, "count": counts[c]} for c in COVERAGE)
    return {"timing": timing, "coverage": coverage}


# --- flow between milestones -----------------------------------------------------------------

NEW = "Opened in the period"
CLOSED = "Closed in the period"


def snapshot_times(first: datetime, now: datetime, most: int = 7) -> list[datetime]:
    """New Year's Days between `first` and `now`, thinned to at most `most`, then `now`."""
    years = list(range(first.year + 1, now.year + 1))
    step = max(1, math.ceil(len(years) / (most - 1)))
    kept = years[::-1][::step][::-1]  # thin from the recent end, so the last full year stays
    times = [first.replace(year=y, month=1, day=1, hour=0, minute=0, second=0, microsecond=0) for y in kept]
    return [t for t in times if t < now] + [now]


def milestone_flow(
    issues: list[Item], times: list[datetime], config: RepoConfig, smallest: int = 5
) -> dict[str, Any]:
    """An alluvial diagram's nodes and links: where the issues in each band at one snapshot
    were by the next one.

    Columns are snapshots, so links only ever run forward and an issue that bounces
    soon -> later -> soon is three honest links, not a cycle. Between two snapshots sit two
    extra nodes: issues opened in the period flow in from one, issues closed in it flow out
    to the other, which keeps every band's height equal to its count of open issues.
    """

    def band(issue: Item, when: datetime) -> str | None:
        return config.bucket(issue.milestone_at(when)) if issue.is_open_at(when) else None

    # Skip the early snapshots that predate any real backlog: a column a twelfth the height
    # of the tallest is a sliver nobody can read, and it costs a column's width.
    open_at = [sum(i.is_open_at(when) for i in issues) for when in times]
    floor = max(smallest, max(open_at) / 12)
    while len(times) > 2 and open_at[0] < floor:
        times, open_at = times[1:], open_at[1:]

    links: Counter[tuple[tuple[int, str], tuple[int, str]]] = Counter()
    for column in range(len(times) - 1):
        earlier, later = times[column], times[column + 1]
        for issue in issues:
            before, after = band(issue, earlier), band(issue, later)
            if before is None and after is None:
                # Not open at either end. Opened and closed in between is still a flow.
                if earlier <= issue.created < later and not issue.is_open_at(later):
                    links[(column, NEW), (column + 1, CLOSED)] += 1
                continue
            if before is None:
                # New, or closed before the period and reopened in it: either way an arrival.
                links[(column, NEW), (column + 1, after)] += 1
            elif after is None:
                links[(column, before), (column + 1, CLOSED)] += 1
            else:
                links[(column, before), (column + 1, after)] += 1

    order = [*config.bucket_order, NEW, CLOSED]
    keys = sorted({key for link in links for key in link}, key=lambda k: (k[0], order.index(k[1])))
    index = {key: position for position, key in enumerate(keys)}
    return {
        "columns": [day(t) for t in times],
        "nodes": [{"column": column, "band": name} for column, name in keys],
        "links": [
            {"source": index[source], "target": index[target], "value": value}
            for (source, target), value in sorted(
                links.items(), key=lambda kv: (index[kv[0][0]], index[kv[0][1]])
            )
        ],
    }


def milestone_outcomes(
    issues: list[Item], meta: dict[str, Any], include, most: int = 30
) -> list[dict[str, Any]]:
    """For each milestone `include(title)` accepts: of the issues ever put in it, how many were
    closed while in it, how many were moved out still open, how many are open in it now."""
    due = {m["title"]: m.get("dueOn") for m in meta.get("milestones", [])}
    outcomes: dict[str, Counter[str]] = defaultdict(Counter)
    first_used: dict[str, datetime] = {}
    for issue in issues:
        last_span: dict[str, tuple[datetime, datetime | None]] = {}
        for start, end, title in issue.milestone_spans:
            if title is not None and include(title):
                last_span[title] = (start, end)
                first_used[title] = min(first_used.get(title, start), start)
        for title, (_, end) in last_span.items():
            if end is None:
                outcome = "Still open in it" if issue.state == "open" else "Closed in it"
            else:
                # Left the milestone: closed first, or carried out still open?
                outcome = "Moved out, still open" if issue.is_open_at(end) else "Closed in it"
            outcomes[title][outcome] += 1
    rows = []
    for title in sorted(outcomes, key=lambda t: first_used[t])[-most:]:
        if sum(outcomes[title].values()) < 3:
            continue
        for outcome in ("Closed in it", "Moved out, still open", "Still open in it"):
            rows.append(
                {
                    "milestone": title,
                    "due": (due.get(title) or "")[:10] or None,
                    "first": day(first_used[title]),
                    "outcome": outcome,
                    "count": outcomes[title][outcome],
                }
            )
    return rows


def closure_modes(issues: list[Item]) -> list[dict[str, Any]]:
    """Per year, how closed issues ended."""
    counts: Counter[tuple[int, str]] = Counter()
    for issue in issues:
        if issue.state == "open" or issue.closed is None:
            continue
        if issue.reason == "not_planned":
            mode = "Not planned"
        elif issue.reason == "duplicate":
            mode = "Duplicate"
        elif issue.closer in ("pr", "commit"):
            mode = "Closed by a pull request or commit"
        else:
            mode = "Closed by hand"
        counts[issue.closed.year, mode] += 1
    return [
        {"year": str(year), "mode": mode, "count": count} for (year, mode), count in sorted(counts.items())
    ]


# --- where and who ---------------------------------------------------------------------------


def label_heat(issues: list[Item], unit: str, most: int = 28) -> dict[str, list[dict[str, Any]]]:
    totals = Counter(label for issue in issues for label in issue.labels)
    kept = [label for label, _ in totals.most_common(most)]

    def family(label: str) -> str:
        return label.split(":")[0].strip() if ":" in label else "(no prefix)"

    cells: Counter[tuple[str, datetime]] = Counter()
    state: Counter[tuple[str, str]] = Counter()
    for issue in issues:
        for label in issue.labels:
            if label in kept:
                cells[label, floor_period(issue.created, unit)] += 1
                state[label, "Open" if issue.state == "open" else "Closed"] += 1
    return {
        "labels": [{"label": label, "family": family(label), "total": totals[label]} for label in kept],
        "cells": [
            {"label": label, "date": day(start), "period": period_label(start, unit), "count": count}
            for (label, start), count in sorted(cells.items(), key=lambda kv: (kv[0][1], kv[0][0]))
        ],
        "state": [
            {"label": label, "state": s, "count": state[label, s]}
            for label in kept
            for s in ("Open", "Closed")
        ],
    }


def area(path: str, depth: int) -> str:
    directories = path.split("/")[:-1]
    return "/".join(directories[:depth]) + "/" if directories else "(top-level files)"


def path_heat(prs: list[Item], unit: str, depth: int, most: int = 14) -> list[dict[str, Any]]:
    """Merged pull requests touching each area of the tree, per period. A pull request counts
    once for each area it touches."""
    cells: Counter[tuple[str, datetime]] = Counter()
    totals: Counter[str] = Counter()
    for pr in prs:
        if not pr.merged:
            continue
        for touched in {area(path, depth) for path in pr.files}:
            cells[touched, floor_period(pr.merged, unit)] += 1
            totals[touched] += 1
    kept = {name for name, _ in totals.most_common(most)}
    return [
        {
            "area": name,
            "date": day(start),
            "period": period_label(start, unit),
            "count": count,
            "total": totals[name],
        }
        for (name, start), count in sorted(cells.items(), key=lambda kv: (kv[0][1], kv[0][0]))
        if name in kept
    ]


ROLES = ["Issues opened", "Issues closed", "Pull requests opened", "Pull requests merged", "Reviews given"]


def people(items: list[Item], most: int = 10) -> dict[str, Any]:
    counts: Counter[tuple[str, int, str]] = Counter()
    bots: set[str] = set()

    def add(login: str | None, year: int, role: str, is_bot: bool = False) -> None:
        if login:
            counts[login, year, role] += 1
            if is_bot:
                bots.add(login)

    pairs: Counter[tuple[str, str]] = Counter()
    for item in items:
        opened = "Issues opened" if item.kind == "issue" else "Pull requests opened"
        add(item.author, item.created.year, opened, item.author_is_bot)
        if item.kind == "issue":
            closes = [e for e in item.events if e.type == "closed"]
            if closes and item.state != "open":
                add(closes[-1].actor, closes[-1].at.year, "Issues closed", closes[-1].actor_is_bot)
        else:
            if item.merged:
                add(item.merged_by, item.merged.year, "Pull requests merged")
            reviewers = set()
            for event in item.events:
                if event.type == "reviewed" and event.actor != item.author:
                    add(event.actor, event.at.year, "Reviews given", event.actor_is_bot)
                    reviewers.add(event.actor)
            for reviewer in reviewers:
                if item.author and reviewer:
                    pairs[item.author, reviewer] += 1

    totals: Counter[str] = Counter()
    for (login, _, _), count in counts.items():
        totals[login] += count
    kept = [login for login, _ in totals.most_common(most)]
    rows = [
        {"login": login, "year": str(year), "role": role, "count": count}
        for (login, year, role), count in sorted(counts.items())
        if login in kept
    ]
    others: Counter[tuple[int, str]] = Counter()
    for (login, year, role), count in counts.items():
        if login not in kept:
            others[year, role] += count
    other_label = f"{len(totals) - len(kept)} others"
    if others:
        rows += [
            {"login": other_label, "year": str(year), "role": role, "count": count}
            for (year, role), count in sorted(others.items())
        ]
    return {
        "activity": rows,
        "order": kept + ([other_label] if others else []),
        "bots": sorted(bots),
        "reviews": [
            {"author": author, "reviewer": reviewer, "count": count}
            for (author, reviewer), count in pairs.most_common(40)
        ],
    }


ASSISTANCE = {"human": "No LLM tool named", "llm": "Names an LLM tool", "bot": "Bot account"}


def assistance(prs: list[Item], unit: str) -> list[dict[str, Any]]:
    counts: Counter[tuple[datetime, str]] = Counter()
    for pr in prs:
        counts[floor_period(pr.created, unit), pr.assisted] += 1
    periods = sorted({start for start, _ in counts})
    return [
        {"date": day(start), "kind": ASSISTANCE[kind], "count": counts[start, kind]}
        for start in periods
        for kind in ASSISTANCE
    ]


# --- the item table and the headline numbers -------------------------------------------------


def item_rows(items: list[Item], config: RepoConfig, now: datetime) -> list[dict[str, Any]]:
    """One row per item for the charts that draw every item: lifelines, the stale map, the
    waiting pull requests."""
    rows = []
    for item in items:
        row: dict[str, Any] = {
            "kind": item.kind,
            "number": item.number,
            "title": item.title[:140],
            "url": item.url,
            "author": item.author,
            "created": day(item.created),
            "closed": day(item.closed) if item.state != "open" else None,
            "age": round(days_between(item.created, now), 1),
            "comments": item.comments,
        }
        last = item.last_comment
        row["quiet"] = round(days_between(last or item.created, now), 1)
        if item.kind == "issue":
            if item.state == "open":
                row["outcome"] = "Open"
            elif item.reason in ("not_planned", "duplicate"):
                row["outcome"] = "Not planned or duplicate"
            else:
                row["outcome"] = "Completed"
            row["band"] = config.bucket(item.milestone)
            row["milestone"] = item.milestone
        else:
            facts = review_facts(item)
            if item.state == "open":
                row["outcome"] = "Open draft" if item.is_draft else "Open"
                row["stage"] = stage(item, facts)
            else:
                row["outcome"] = "Merged" if item.state == "merged" else "Closed unmerged"
            row["ready"] = day(item.ready_at) if item.started_as_draft else None
            row["draft"] = item.started_as_draft
            row["assisted"] = ASSISTANCE[item.assisted]
            row["size"] = item.additions + item.deletions
        rows.append(row)
    return rows


def tiles(items: list[Item], now: datetime) -> list[dict[str, Any]]:
    issues = [i for i in items if i.kind == "issue"]
    prs = [i for i in items if i.kind == "pr"]
    open_issues = [i for i in issues if i.state == "open"]
    open_prs = [p for p in prs if p.state == "open"]

    def net(kind_items: list[Item], days: int) -> int:
        since = now - timedelta(days=days)
        opened = sum(1 for i in kind_items for start, _ in i.open_spans if start >= since)
        closed = sum(1 for i in kind_items for _, end in i.open_spans if end and end >= since)
        return opened - closed

    recent = [
        days_between(p.created, p.merged)
        for p in prs
        if p.merged and not p.author_is_bot and p.merged >= now - timedelta(days=90)
    ]
    waiting = [p for p in open_prs if stage(p, review_facts(p)) == "Awaiting first review"]
    ages = [days_between(i.created, now) for i in open_issues]
    return [
        {"label": "Open issues", "value": len(open_issues), "note": f"of {len(issues)} ever opened"},
        {
            "label": "Issue backlog, last 90 days",
            "value": f"{net(issues, 90):+d}",
            "note": f"{net(issues, 30):+d} in the last 30",
        },
        {
            "label": "Median age of an open issue",
            "value": f"{median(ages) / 365:.1f} yr" if ages else "–",
            "note": f"oldest {max(ages) / 365:.1f} yr" if ages else "",
        },
        {
            "label": "Open pull requests",
            "value": len(open_prs),
            "note": f"{sum(p.is_draft for p in open_prs)} are drafts",
        },
        {
            "label": "Awaiting a first human review",
            "value": len(waiting),
            "note": "open, not draft, no review by a person",
        },
        {
            "label": "Median days to merge, last 90 days",
            "value": f"{median(recent):.1f}" if recent else "–",
            "note": f"{len(recent)} merged, bots excluded",
        },
    ]


# --- everything ------------------------------------------------------------------------------


def milestone_issue_days(issues: list[Item], now: datetime) -> dict[str, float]:
    """Per milestone title, the total time open issues have spent in it."""
    totals: dict[str, float] = defaultdict(float)
    for issue in issues:
        for start, end, title in issue.milestone_spans:
            if title is None:
                continue
            for opened, closed in issue.open_spans:
                overlap = min(end or now, closed or now) - max(start, opened)
                totals[title] += max(0.0, overlap.total_seconds())
    return dict(totals)


def build_dashboard(
    items: list[Item], meta: dict[str, Any], config: RepoConfig, now: datetime
) -> dict[str, Any]:
    issues = [i for i in items if i.kind == "issue"]
    prs = [i for i in items if i.kind == "pr"]
    first = min(i.created for i in items)
    fine, coarse = fine_unit(first, now), coarse_unit(first, now)
    starts = grid(first, now, fine)
    times = sample_times(starts, fine, now)
    cohorts = cohort_labeller(first.year, now.year)
    configured = {name for name, _ in config.milestone_buckets}

    def is_release(title: str) -> bool:
        # With configured bands, the standing ones (Needed soon, ...) are not releases; without
        # any, there is nothing to tell a release from a standing bucket, so show them all.
        return config.bucket(title) not in configured

    return {
        "repo": meta.get("nameWithOwner"),
        "url": meta.get("url"),
        "fetchedAt": meta.get("fetchedAt"),
        "units": {"fine": fine, "coarse": coarse},
        "bands": config.bucket_order,
        "tiles": tiles(items, now),
        "flow": {"issue": flow(issues, starts), "pr": flow(prs, starts)},
        "backlogByMilestone": backlog_by_milestone(issues, times, config),
        "backlogByVintage": backlog_by_vintage(issues, times, cohorts),
        "survival": {"issue": survival(issues, now, cohorts), "pr": survival(prs, now, cohorts)},
        "pipeline": pr_pipeline(prs, coarse),
        "milestoneFlow": milestone_flow(issues, snapshot_times(first, now), config),
        "milestoneOutcomes": milestone_outcomes(issues, meta, is_release),
        "closureModes": closure_modes(issues),
        "labels": label_heat(issues, coarse),
        "paths": path_heat(prs, coarse, config.path_depth),
        "people": people(items),
        "assistance": assistance(prs, coarse),
        "items": item_rows(items, config, now),
        "checks": {
            "issues": len(issues),
            "pullRequests": len(prs),
            "githubIssues": (meta.get("issues") or {}).get("totalCount"),
            "githubPullRequests": (meta.get("pullRequests") or {}).get("totalCount"),
            "replayMismatches": sum(1 for i in items if i.mismatches),
            "noMilestone": NO_MILESTONE,
        },
    }
