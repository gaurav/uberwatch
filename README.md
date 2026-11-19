# uberwatch

Watching Uberon grow: graphs of how issues and pull requests move through a GitHub repository.

The goal is an automatically updated dashboard of how people change
[Uberon](https://github.com/obophenotype/uberon), as a baseline before LLM-based tools are brought
into that process. The tools that read a repository's history are not specific to Uberon, and the
first repository they were built against is
[NCATSTranslator/Babel](https://github.com/NCATSTranslator/Babel).

**Status: the first layer only.** It fetches a repository's issue and pull request history and
replays it, so that any item can be asked what state it was in on a past date. Nothing is drawn
or published yet; see [What is not here yet](#what-is-not-here-yet).

## Use

Needs [uv](https://docs.astral.sh/uv/) and a GitHub token: `GH_TOKEN` or `GITHUB_TOKEN` in the
environment, or a logged-in `gh` CLI.

```sh
uv run uberwatch fetch NCATSTranslator/Babel
```

The first fetch costs one API request per issue and pull request (about 1,300 for Babel, 3,800
for Uberon, against GitHub's 5,000 an hour). Later fetches only re-request items whose
`updatedAt` has changed, so they cost a few dozen. `--full` re-fetches everything. A fetch that
fails partway, or runs out of the hour's requests, keeps what it got; the same command carries
on from there.

Everything is written under `data/`, which git ignores:

```
data/<owner>/<repo>/raw/issues.jsonl   one issue per line, with its timeline
data/<owner>/<repo>/raw/pulls.jsonl    one pull request per line, with timeline, files, commits
data/<owner>/<repo>/raw/meta.json      fetch time, milestones, labels, GitHub's own totals
```

To read it back with its history replayed:

```python
from datetime import UTC, datetime
from pathlib import Path

from uberwatch.events import load_items

items = load_items(Path("data/NCATSTranslator/Babel/raw"))
then = datetime(2025, 1, 1, tzinfo=UTC)
open_then = [item for item in items if item.kind == "issue" and item.is_open_at(then)]
print(len(open_then), "issues were open;", open_then[0].milestone_at(then))
```

## How it works

Two stages, one module each in `src/uberwatch/`:

1. **`fetch.py`** downloads every issue and pull request with its timeline over GitHub's GraphQL
   API.
2. **`events.py`** replays each timeline. GitHub only reports what an item looks like now; the
   timeline says what happened to it, so replaying it gives the spans during which the item was
   open, which milestone it was in, and when a pull request stopped being a draft. Each replay
   is checked against the item's present state, and `Item.mismatches` says where they disagree.
   It also marks each pull request as a bot's, as naming an LLM tool, or as neither.

```sh
uv run pytest
uv run ruff check . && uv run ruff format .
```

## Things that will bite you

- **Never fetch timelines for several items in one request.** GitHub silently returns
  incomplete timelines when they are batched, with nothing in the response to show it. The
  details are at the top of `fetch.py`. This is why a first fetch is one request per item.
- **The replay is the fetch's test.** If many items have `mismatches`, suspect the fetch. A few
  are normal: a milestone renamed or deleted after issues were put in it, or a very old issue
  whose closing GitHub never recorded as an event.
- **Milestone events carry the title at the time.** A renamed milestone appears in the history
  under both names.
- **"Names an LLM tool" is a rough signal.** It means a commit co-author or the pull request
  description mentions one. Work done with a tool that left no such trace is not counted.

## What is not here yet

- Charts. Nothing here draws or summarises the history.
- A GitHub Action that fetches daily and publishes to GitHub Pages. Fetching only changed items
  was built with this in mind; the stored raw data will need a home between runs (a branch or a
  cache), since the default Actions token gets 1,000 requests an hour.
- Which parts of the ontology a change touches. The fetch records the files each pull request
  changed, which for Uberon is nearly always the one edit file; the real answer needs the terms
  changed in each diff.
- GitHub Projects. Babel's issues also move through a project board's status column, and those
  events are in the timeline, but reading them needs a token with the `read:project` scope.
- A library other repositories can install.
