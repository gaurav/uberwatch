# uberwatch

Watching Uberon grow: graphs of how issues and pull requests move through a GitHub repository.

The goal is an automatically updated dashboard of how people change
[Uberon](https://github.com/obophenotype/uberon), as a baseline before LLM-based tools are brought
into that process. The tools that read and graph a repository's history are not specific to
Uberon, and the first repository they were built against is
[NCATSTranslator/Babel](https://github.com/NCATSTranslator/Babel).

**Status: a rough first version.** It fetches a repository's history and builds a dashboard on
your own machine. Nothing is published and nothing runs on a schedule yet; see
[What is not here yet](#what-is-not-here-yet).

## Use

Needs [uv](https://docs.astral.sh/uv/) and a GitHub token: `GH_TOKEN` or `GITHUB_TOKEN` in the
environment, or a logged-in `gh` CLI.

```sh
uv run uberwatch all NCATSTranslator/Babel     # fetch, then build
open data/NCATSTranslator/Babel/site/index.html
```

`all` is `fetch` followed by `build`; either can be run on its own. `build` needs no network.

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
data/<owner>/<repo>/dashboard.json     every chart's data
data/<owner>/<repo>/site/index.html    the dashboard, with that data embedded
```

## What the dashboard shows

| Section | Charts |
|---|---|
| Accumulation | open issues and pull requests over time; opened against closed per week; the open backlog split by milestone, and by the year issues were opened |
| Lifelines | every issue and pull request as a line from opened to closed; open issues by age against time since the last comment |
| Speed | the share of items still open N days after opening (survival curves); days to merge; who had reviewed a pull request when it merged; open pull requests by where they are waiting |
| Flow | where each milestone's open issues were a year later; what became of the issues put in each release milestone; how issues were closed |
| Where | issues by label; merged pull requests by the part of the tree they touched |
| Who | activity by person and year; who reviews whom; pull requests that name an LLM tool |

Each chart has a legend, a tooltip and a data table, and the page follows the system's light or
dark theme.

## How it works

Four stages, one module each in `src/uberwatch/`:

1. **`fetch.py`** downloads every issue and pull request with its timeline over GitHub's GraphQL
   API.
2. **`events.py`** replays each timeline. GitHub only reports what an item looks like now; the
   timeline says what happened to it, so replaying it gives the spans during which the item was
   open, which milestone it was in, and when a pull request stopped being a draft. Each replay
   is checked against the item's present state.
3. **`metrics.py`** turns the replayed items into each chart's rows. These are pure functions,
   and `now` is the time of the fetch, so the same data always builds the same dashboard.
4. **`render.py`** writes the page from `templates/`. Charts are drawn in the browser with
   [Observable Plot](https://observablehq.com/plot/) and d3-sankey, loaded from a CDN.

`repos.toml` holds per-repository settings: how milestones are grouped into the bands the
backlog and flow charts use, which accounts are bots, how deep a path counts as an "area". A
repository with no entry gets its six busiest milestones as bands.

```sh
uv run pytest
uv run ruff check . && uv run ruff format .
```

The page's JavaScript has no tests, so after changing anything in `templates/`, rebuild and
look. Three bugs in the first version only showed up this way:

- Build a second repository as well as Babel. One with a long history and few milestones
  (`obophenotype/uberon`) breaks charts that Babel does not.
- Load the page at desktop and at phone width, and in the light and dark themes. A chart that
  failed shows "This chart could not be drawn" in its card and logs the error to the console.
- At phone width the page itself must not scroll sideways.

## Things that will bite you

- **Never fetch timelines for several items in one request.** GitHub silently returns
  incomplete timelines when they are batched, with nothing in the response to show it. The
  details are at the top of `fetch.py`. This is why a first fetch is one request per item.
- **The replay is the fetch's test.** If `build` reports many items whose replay does not end
  in their present state, suspect the fetch. A few are normal: a milestone renamed or deleted
  after issues were put in it.
- **Milestone events carry the title at the time.** A renamed milestone appears in the history
  under both names, so the patterns in `repos.toml` have to match the old names too.
- **"Names an LLM tool" is a rough signal.** It means a commit co-author or the pull request
  description mentions one. Work done with a tool that left no such trace is not counted.

## What is not here yet

- A GitHub Action that rebuilds the dashboard daily and publishes it to GitHub Pages. Fetching
  only changed items was built with this in mind; the stored raw data will need a home between
  runs (a branch or a cache), since the default Actions token gets 1,000 requests an hour.
- Which parts of the ontology a change touches. Today's "where" chart groups changed files by
  directory, which for Uberon is nearly always the one edit file; the real answer needs the
  terms changed in each diff.
- Settings for Uberon. The dashboard builds for it, but nothing in `repos.toml` is tuned yet:
  - Uberon hardly uses milestones (four, all from 2013–14), so the milestone charts say almost
    nothing. Its issues move through labels instead (`Stale`, `autoclosed-unfixed`), and there
    is no chart of label history yet, though the label events are fetched.
  - `dragon-ai-agent` is an ordinary user account that looks to be driven by an LLM, going by
    its name and by most of its pull requests naming an LLM tool. It is not in `bots`, so it
    counts as a person in the review and people charts.
  - Two days dominate its history: 403 issues migrated from SourceForge were closed on
    3 July 2015, and 396 were closed as `autoclosed-unfixed` on 3 August 2021. Both are real,
    and both flatten every other month on the opened-and-closed chart.
- GitHub Projects. Babel's issues also move through a project board's status column, and those
  events are in the timeline, but reading them needs a token with the `read:project` scope.
- A library other repositories can install. The fetch and replay layers are the likeliest to
  survive a rewrite; the charts are a first guess at what is worth looking at.
