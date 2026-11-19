"""Per-repository settings, read from repos.toml."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

NO_MILESTONE = "No milestone"


@dataclass
class RepoConfig:
    bots: frozenset[str] = frozenset()
    path_depth: int = 1
    # (band name, pattern) in stacking order; empty means "work the bands out from the data".
    milestone_buckets: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)
    other_milestones: str = "Other milestones"

    def bucket(self, milestone: str | None) -> str:
        if milestone is None:
            return NO_MILESTONE
        for name, pattern in self.milestone_buckets:
            if pattern.search(milestone):
                return name
        return self.other_milestones

    @property
    def bucket_order(self) -> list[str]:
        return [name for name, _ in self.milestone_buckets] + [self.other_milestones, NO_MILESTONE]


def load_config(repo: str, path: Path) -> RepoConfig:
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    settings = {**data.get("defaults", {}), **data.get(repo, {})}
    return RepoConfig(
        bots=frozenset(settings.get("bots", [])),
        path_depth=int(settings.get("path_depth", 1)),
        milestone_buckets=[
            (bucket["name"], re.compile(bucket["match"])) for bucket in settings.get("milestone_buckets", [])
        ],
        other_milestones=settings.get("other_milestones", "Other milestones"),
    )


def use_busiest_milestones(config: RepoConfig, issue_days: dict[str, float], keep: int = 6) -> None:
    """For a repository with no configured bands: one band for each of its busiest milestones.

    `issue_days` is, per milestone title, the total time open issues have spent in it.
    """
    if config.milestone_buckets:
        return
    busiest = sorted(issue_days, key=lambda title: issue_days[title], reverse=True)[:keep]
    config.milestone_buckets = [(title, re.compile(f"^{re.escape(title)}$")) for title in busiest]
