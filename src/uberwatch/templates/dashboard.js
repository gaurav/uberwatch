// Draws every chart on the page from the JSON embedded by render.py.
//
// Each entry in `charts` takes the width it has to fill and returns
//   { node, legend?: [{label, color, line?}] | {ramp: [low, high], max}, table?: rows }
// `draw()` puts the node, a legend and a "data table" disclosure into the card whose
// data-chart attribute names the entry. Everything is redrawn on resize and on a light/dark
// switch, because colours are read from the CSS custom properties at draw time.
(function () {
  "use strict";

  const DATA = JSON.parse(document.getElementById("data").textContent);
  const NOW = new Date(DATA.fetchedAt);
  const DAY = 86400000;
  const date = (text) => (text ? new Date(text + "T00:00:00Z") : null);
  const monthYear = d3.utcFormat("%b %Y");
  const fullDay = d3.utcFormat("%-d %b %Y");
  const UNIT_NAMES = { week: "week", month: "month", quarter: "quarter", year: "year" };

  let C = {};
  function tokens() {
    const style = getComputedStyle(document.documentElement);
    const get = (name) => style.getPropertyValue(name).trim();
    const dark = matchMedia("(prefers-color-scheme: dark)").matches;
    return {
      dark,
      surface: get("--surface-1"),
      text: get("--text-primary"),
      text2: get("--text-secondary"),
      muted: get("--muted"),
      grid: get("--grid"),
      axis: get("--axis"),
      series: [1, 2, 3, 4, 5, 6, 7].map((n) => get("--series-" + n)),
      neutral: get("--neutral"),
      neutralStrong: get("--neutral-strong"),
      ordinal: [1, 2, 3, 4, 5].map((n) => get("--ordinal-" + n)),
      seq: [get("--seq-low"), get("--seq-mid"), get("--seq-high")],
      // Ink for a number printed on the dark end of the heatmap ramp.
      onHigh: dark ? "#0b0b0b" : "#ffffff",
    };
  }

  // ---- shared pieces ------------------------------------------------------------------------

  const NEW = "Opened in the period";
  const CLOSED = "Closed in the period";

  function bandColor(band) {
    if (band === DATA.checks.noMilestone || band === NEW) return C.neutral;
    if (band === CLOSED) return C.neutralStrong;
    return C.series[DATA.bands.indexOf(band) % C.series.length];
  }
  const bandLegend = () => DATA.bands.map((band) => ({ label: band, color: bandColor(band) }));

  // Cohorts are ordered, so they take steps of one hue, spread across the five available.
  // A cohort keeps its step in every chart, whether or not all cohorts appear in it.
  const COHORTS = [...new Set(DATA.backlogByVintage.map((d) => d.cohort))];
  function cohortScale(cohorts) {
    const n = COHORTS.length;
    const step = (cohort) => C.ordinal[n === 1 ? 2 : Math.round((COHORTS.indexOf(cohort) * 4) / (n - 1))];
    return { domain: cohorts, range: cohorts.map(step) };
  }
  const scaleLegend = (scale) => scale.domain.map((label, i) => ({ label, color: scale.range[i] }));

  function plot(options) {
    return Plot.plot({
      style: { color: C.text2, background: "transparent", fontSize: "11px", overflow: "visible" },
      ...options,
    });
  }

  const TIP = () => ({ fill: C.surface, stroke: C.axis, fontSize: 12, textPadding: 8, lineHeight: 1.25 });

  // A vertical rule that follows the pointer, with one tooltip for everything at that x.
  function crosshair(rows, x, title) {
    return [
      Plot.ruleX(rows, Plot.pointerX({ x, stroke: C.muted, strokeWidth: 1 })),
      Plot.tip(rows, Plot.pointerX({ x, frameAnchor: "top", title, ...TIP() })),
    ];
  }

  function addUnit(start, unit) {
    const next = new Date(start);
    if (unit === "week") next.setUTCDate(next.getUTCDate() + 7);
    else next.setUTCMonth(next.getUTCMonth() + { month: 1, quarter: 3, year: 12 }[unit]);
    return next;
  }
  const periodName = (start, unit) =>
    unit === "week" ? "Week of " + fullDay(start)
    : unit === "month" ? monthYear(start)
    : unit === "quarter" ? `${start.getUTCFullYear()} Q${Math.floor(start.getUTCMonth() / 3) + 1}`
    : String(start.getUTCFullYear());

  // Bars are capped at 24px; the rest of each slot is left as air.
  const barInset = (plotWidth, count) => Math.max(1, (plotWidth / Math.max(1, count) - 24) / 2);

  // The gap between hundreds of thin bars: half a pixel a side where there is room, none where
  // the bars are already under two pixels wide (a fixed inset would erase them on a phone).
  function hairGap(plotWidth, count) {
    const inset = plotWidth / Math.max(1, count) >= 2 ? 0.5 : 0;
    return { insetLeft: inset, insetRight: inset };
  }

  // Every period label from the first item to now, so an empty quarter still gets its column.
  function coarsePeriods() {
    const unit = DATA.units.coarse;
    const first = d3.min(DATA.items, (d) => d.created);
    let start = date(first);
    start = unit === "year"
      ? new Date(Date.UTC(start.getUTCFullYear(), 0, 1))
      : new Date(Date.UTC(start.getUTCFullYear(), Math.floor(start.getUTCMonth() / 3) * 3, 1));
    const labels = [];
    for (; start <= NOW; start = addUnit(start, unit)) labels.push(periodName(start, unit));
    return labels;
  }
  // On a quarterly axis, label only each year's first quarter.
  const coarseTick = (label) => (label.includes(" Q") ? (label.endsWith("Q1") ? label.slice(0, 4) : "") : label);

  const sequential = () => d3.interpolateRgbBasis(C.seq);

  // A logarithmic axis in days, done by hand: positions are log10(days) on a linear scale.
  // Plot's own log scale drops the labels of ticks that are not near a power of ten, which
  // leaves a week, a month and a year unlabelled.
  const DAY_TICKS = [1, 7, 30, 90, 365, 1095, 2190, 3650];
  const dayLabel = (days) => (days >= 365 ? Math.round(days / 365) + " yr" : days + " d");
  function logDays(lowest, highest, label) {
    return {
      domain: [Math.log10(lowest), Math.log10(highest)],
      ticks: DAY_TICKS.filter((v) => v >= lowest && v <= highest).map(Math.log10),
      tickFormat: (v) => dayLabel(Math.round(10 ** v)),
      label, grid: true,
    };
  }

  // ---- floating tooltip, for the one chart Plot does not draw -----------------------------

  const floatTip = document.createElement("div");
  floatTip.className = "float-tip";
  floatTip.style.cssText =
    "position:fixed;display:none;pointer-events:none;z-index:10;padding:6px 9px;border-radius:6px;" +
    "font-size:12.5px;line-height:1.35;white-space:pre-line;max-width:320px;" +
    "background:var(--surface-1);color:var(--text-primary);border:1px solid var(--axis);" +
    "box-shadow:0 2px 10px rgba(0,0,0,.15)";
  document.body.append(floatTip);
  function hover(selection, text) {
    selection
      .on("pointermove", (event, d) => {
        floatTip.textContent = text(d);
        floatTip.style.display = "block";
        const box = floatTip.getBoundingClientRect();
        floatTip.style.left = Math.min(event.clientX + 14, innerWidth - box.width - 8) + "px";
        floatTip.style.top = Math.max(8, event.clientY - box.height - 12) + "px";
      })
      .on("pointerleave", () => (floatTip.style.display = "none"));
  }

  // ---- data prepared once -----------------------------------------------------------------

  function withEnds(rows, unit) {
    return rows.map((row) => {
      const start = date(row.date);
      const end = addUnit(start, unit);
      return { ...row, start, end: end > NOW ? NOW : end, period: periodName(start, unit) };
    });
  }
  const FLOW = { issue: withEnds(DATA.flow.issue, DATA.units.fine), pr: withEnds(DATA.flow.pr, DATA.units.fine) };
  const ISSUES = DATA.items.filter((d) => d.kind === "issue").map(prepare);
  const PRS = DATA.items.filter((d) => d.kind === "pr").map(prepare);
  function prepare(item, rank) {
    return { ...item, rank, from: date(item.created), to: item.closed ? date(item.closed) : NOW, readyAt: date(item.ready) };
  }
  const describe = (d) =>
    `#${d.number} ${d.title}\n${d.outcome}${d.stage ? " · " + d.stage : ""} · opened ${d.created}` +
    (d.closed ? ` · closed ${d.closed}` : ` · ${Math.round(d.age)} days ago`) + (d.author ? `\nby ${d.author}` : "");

  // ---- the charts ---------------------------------------------------------------------------

  const charts = {
    openCount(width) {
      const rows = [
        ...FLOW.issue.map((d) => ({ at: d.end, kind: "Issues", open: d.open })),
        ...FLOW.pr.map((d) => ({ at: d.end, kind: "Pull requests", open: d.open })),
      ];
      const color = { domain: ["Issues", "Pull requests"], range: [C.series[0], C.series[1]] };
      const pairs = FLOW.issue.map((d, i) => ({ at: d.end, issues: d.open, prs: FLOW.pr[i].open }));
      return {
        node: plot({
          width, height: 280, marginRight: 120, color,
          x: { label: null }, y: { label: "open", grid: true, nice: true },
          marks: [
            Plot.ruleY([0], { stroke: C.axis }),
            Plot.line(rows, { x: "at", y: "open", stroke: "kind", strokeWidth: 2 }),
            Plot.text(rows, Plot.selectLast({
              x: "at", y: "open", z: "kind", text: (d) => `${d.kind} ${d.open}`,
              dx: 8, textAnchor: "start", fill: C.text, fontSize: 12,
            })),
            ...crosshair(pairs, "at", (d) => `${fullDay(d.at)}\n${d.issues} issues open\n${d.prs} pull requests open`),
          ],
        }),
        legend: scaleLegend(color).map((item) => ({ ...item, line: true })),
        table: pairs.map((d) => ({ date: fullDay(d.at), "issues open": d.issues, "pull requests open": d.prs })),
      };
    },

    flowIssue(width) {
      const rows = FLOW.issue;
      const gap = hairGap(width, rows.length);
      return {
        node: plot({
          width, height: 260,
          x: { label: null }, y: { label: "issues", grid: true, tickFormat: (v) => Math.abs(v) },
          marks: [
            Plot.rect(rows, { x1: "start", x2: "end", y1: 0, y2: "opened", fill: C.series[0], ...gap }),
            Plot.rect(rows, { x1: "start", x2: "end", y1: 0, y2: (d) => -d.closed, fill: C.series[1], ...gap }),
            Plot.ruleY([0], { stroke: C.axis }),
            ...crosshair(rows, (d) => new Date((+d.start + +d.end) / 2),
              (d) => `${d.period}\n${d.opened} opened\n${d.closed} closed\n${d.open} open at the end`),
          ],
        }),
        legend: [{ label: "Opened or reopened", color: C.series[0] }, { label: "Closed", color: C.series[1] }],
        table: rows.map((d) => ({ period: d.period, opened: d.opened, closed: d.closed, "open at end": d.open })),
      };
    },

    flowPr(width) {
      const rows = FLOW.pr;
      const gap = hairGap(width, rows.length);
      return {
        node: plot({
          width, height: 260,
          x: { label: null }, y: { label: "pull requests", grid: true, tickFormat: (v) => Math.abs(v) },
          marks: [
            Plot.rect(rows, { x1: "start", x2: "end", y1: 0, y2: "opened", fill: C.series[0], ...gap }),
            Plot.rect(rows, { x1: "start", x2: "end", y1: 0, y2: (d) => -d.merged, fill: C.series[1], ...gap }),
            Plot.rect(rows, { x1: "start", x2: "end", y1: (d) => -d.merged, y2: (d) => -d.closed, fill: C.series[2], ...gap }),
            Plot.ruleY([0], { stroke: C.axis }),
            ...crosshair(rows, (d) => new Date((+d.start + +d.end) / 2),
              (d) => `${d.period}\n${d.opened} opened\n${d.merged} merged\n${d.closed - d.merged} closed unmerged\n${d.open} open at the end`),
          ],
        }),
        legend: [
          { label: "Opened or reopened", color: C.series[0] },
          { label: "Merged", color: C.series[1] },
          { label: "Closed without merging", color: C.series[2] },
        ],
        table: rows.map((d) => ({ period: d.period, opened: d.opened, merged: d.merged, "closed unmerged": d.closed - d.merged, "open at end": d.open })),
      };
    },

    backlogMilestone(width) {
      return stackedBacklog(width, DATA.backlogByMilestone, "band",
        { domain: DATA.bands, range: DATA.bands.map(bandColor) });
    },

    backlogVintage(width) {
      return stackedBacklog(width, DATA.backlogByVintage, "cohort", cohortScale(COHORTS));
    },

    lifelineIssue(width) {
      const color = { domain: ["Open", "Completed", "Not planned or duplicate"], range: C.series.slice(0, 3) };
      return {
        node: plot({
          width, height: Math.min(520, Math.max(240, ISSUES.length * 0.55)), color,
          x: { label: null }, y: { reverse: true, label: "↓ in the order opened", ticks: 5 },
          marks: [
            Plot.link(ISSUES, { x1: "from", x2: "to", y1: "rank", y2: "rank", stroke: "outcome", strokeWidth: 1, href: "url", target: "_blank" }),
            Plot.tip(ISSUES, Plot.pointerY({ x: "from", y: "rank", title: describe, maxRadius: 2000, ...TIP() })),
          ],
        }),
        legend: scaleLegend(color).map((item) => ({ ...item, line: true })),
        table: ISSUES.map((d) => ({ issue: "#" + d.number, title: d.title, opened: d.created, closed: d.closed || "", outcome: d.outcome, milestone: d.milestone || "" })),
      };
    },

    lifelinePr(width) {
      const color = { domain: ["Open", "Merged", "Closed unmerged"], range: C.series.slice(0, 3) };
      const state = (d) => (d.outcome === "Open draft" ? "Open" : d.outcome);
      // The stretch spent as a draft, and the stretch after it.
      const drafts = PRS.filter((d) => d.draft).map((d) => ({ ...d, to: d.readyAt || d.to }));
      const ready = PRS.filter((d) => !d.draft || d.readyAt).map((d) => ({ ...d, from: d.draft ? d.readyAt : d.from }));
      return {
        node: plot({
          width, height: Math.min(460, Math.max(240, PRS.length * 0.6)), color,
          x: { label: null }, y: { reverse: true, label: "↓ in the order opened", ticks: 5 },
          marks: [
            Plot.link(drafts, { x1: "from", x2: "to", y1: "rank", y2: "rank", stroke: state, strokeWidth: 1, strokeOpacity: 0.3, href: "url", target: "_blank" }),
            Plot.link(ready, { x1: "from", x2: "to", y1: "rank", y2: "rank", stroke: state, strokeWidth: 1, href: "url", target: "_blank" }),
            Plot.tip(PRS, Plot.pointerY({ x: "from", y: "rank", title: describe, maxRadius: 2000, ...TIP() })),
          ],
        }),
        legend: [...scaleLegend(color).map((item) => ({ ...item, line: true })), { label: "Faint: while a draft", color: C.neutral, line: true }],
        table: PRS.map((d) => ({ "pull request": "#" + d.number, title: d.title, opened: d.created, closed: d.closed || "", outcome: d.outcome, "LLM tool": d.assisted })),
      };
    },

    stale(width) {
      const rows = ISSUES.filter((d) => d.outcome === "Open").map((d) => ({
        ...d, x: Math.log10(Math.max(0.5, d.age)), y: Math.log10(Math.max(0.5, d.quiet)),
        talk: d.quiet >= d.age - 0.05 ? "Never commented on" : "Has comments",
      }));
      const color = { domain: ["Has comments", "Never commented on"], range: [C.series[0], C.neutral] };
      const oldest = Math.max(2, d3.max(rows, (d) => d.age) || 2);
      const extent = [Math.log10(0.5), Math.log10(oldest)];
      return {
        node: plot({
          width, height: 420, color,
          x: logDays(0.5, oldest, "age →"),
          y: logDays(0.5, oldest, "↑ since the last comment"),
          marks: [
            Plot.link([extent], { x1: (d) => d[0], y1: (d) => d[0], x2: (d) => d[1], y2: (d) => d[1], stroke: C.axis }),
            Plot.dot(rows, { x: "x", y: "y", r: 3.5, fill: "talk", fillOpacity: 0.75, stroke: C.surface, strokeWidth: 1, href: "url", target: "_blank" }),
            Plot.tip(rows, Plot.pointer({
              x: "x", y: "y", ...TIP(),
              title: (d) => `#${d.number} ${d.title}\nopen ${Math.round(d.age)} days · ${d.talk === "Has comments" ? "last comment " + Math.round(d.quiet) + " days ago" : "no comments"}\n${d.band}`,
            })),
          ],
        }),
        legend: scaleLegend(color),
        table: rows.map((d) => ({ issue: "#" + d.number, title: d.title, "age (days)": Math.round(d.age), "days since last comment": d.talk === "Has comments" ? Math.round(d.quiet) : "never", comments: d.comments, milestone: d.milestone || "" })),
      };
    },

    survivalIssue: (width) => survival(width, DATA.survival.issue),
    survivalPr: (width) => survival(width, DATA.survival.pr),

    mergeTime(width) {
      const rows = DATA.pipeline.timing.map((d) => {
        const start = date(d.date);
        return { ...d, at: new Date((+start + +addUnit(start, DATA.units.coarse)) / 2), period: periodName(start, DATA.units.coarse) };
      });
      return {
        node: plot({
          width, height: 280,
          x: { label: null }, y: { label: "days", grid: true, nice: true },
          marks: [
            Plot.ruleY([0], { stroke: C.axis }),
            Plot.areaY(rows, { x: "at", y1: "p25", y2: "p75", fill: C.series[0], fillOpacity: 0.14 }),
            Plot.line(rows, { x: "at", y: "p50", stroke: C.series[0], strokeWidth: 2 }),
            Plot.dot(rows, { x: "at", y: "p50", r: 4, fill: C.series[0], stroke: C.surface, strokeWidth: 2 }),
            ...crosshair(rows, "at", (d) =>
              `${d.period}: ${d.n} merged\nmedian ${d.p50} days (middle half ${d.p25}–${d.p75})\n` +
              `${d.self_merged} merged by their author` +
              (d.review_p50 == null ? "" : `\nmedian ${d.review_p50} days to a first human review`)),
          ],
        }),
        legend: [{ label: "Median", color: C.series[0], line: true }, { label: "Middle half (25th to 75th percentile)", color: C.series[0], faint: true }],
        table: rows.map((d) => ({ period: d.period, merged: d.n, "25th pct (days)": d.p25, "median (days)": d.p50, "75th pct (days)": d.p75, "merged by author": d.self_merged, "median days to first human review": d.review_p50 ?? "" })),
      };
    },

    coverage(width) {
      const order = ["Approved by a person", "Reviewed, not approved", "Bot review only", "No review"];
      const color = { domain: order, range: [C.series[0], C.series[1], C.series[2], C.neutral] };
      return stackedColumns(width, DATA.pipeline.coverage, "coverage", color, "pull requests merged");
    },

    waiting(width) {
      const order = ["Draft", "Awaiting first review", "In review", "Changes requested", "Approved, not merged"];
      const rows = PRS.filter((d) => d.stage).map((d) => ({ ...d, x: Math.log10(Math.max(0.2, d.age)) }));
      const oldest = Math.max(2, d3.max(rows, (d) => d.age) || 2);
      const present = order.filter((stage) => rows.some((d) => d.stage === stage));
      const counts = d3.rollup(rows, (v) => v.length, (d) => d.stage);
      return {
        node: plot({
          width, height: 60 + present.length * 90, marginLeft: 170,
          x: logDays(0.2, oldest, "open for →"),
          fy: { domain: present, axis: "left", label: null, tickFormat: (stage) => `${stage} (${counts.get(stage)})` },
          marks: [
            Plot.frame({ stroke: C.grid }),
            Plot.dot(rows, Plot.dodgeY({
              x: "x", fy: "stage", anchor: "middle", r: 4, padding: 1.5, fill: C.series[0], stroke: C.surface, strokeWidth: 1,
              href: "url", target: "_blank", title: describe, tip: TIP(),
            })),
          ],
        }),
        table: rows.map((d) => ({ "pull request": "#" + d.number, title: d.title, stage: d.stage, "days open": Math.round(d.age), author: d.author || "" })),
      };
    },

    alluvial,

    outcomes(width) {
      const rows = DATA.milestoneOutcomes;
      if (!rows.length) return { node: message("No milestones here beyond the ones charted as bands above.") };
      const names = [...new Set(rows.map((d) => d.milestone))];
      const order = ["Closed in it", "Moved out, still open", "Still open in it"];
      const color = { domain: order, range: [C.series[1], C.series[2], C.series[0]] };
      return {
        node: plot({
          width, height: 50 + names.length * 24, marginLeft: 250, color,
          x: { label: "issues →", grid: true }, y: { domain: names, label: null, padding: 0.35 },
          marks: [
            Plot.barX(rows.filter((d) => d.count), {
              y: "milestone", x: "count", fill: "outcome", insetLeft: 1, insetRight: 1,
              title: (d) => `${d.milestone}${d.due ? " (due " + d.due + ")" : ""}\n${d.outcome}: ${d.count}`, tip: TIP(),
            }),
            Plot.ruleX([0], { stroke: C.axis }),
          ],
        }),
        legend: scaleLegend(color),
        table: names.map((name) => {
          const of = (outcome) => rows.find((d) => d.milestone === name && d.outcome === outcome);
          return { milestone: name, due: of(order[0]).due || "", "closed in it": of(order[0]).count, "moved out": of(order[1]).count, "still open in it": of(order[2]).count };
        }),
      };
    },

    closure(width) {
      const order = ["Closed by a pull request or commit", "Closed by hand", "Not planned", "Duplicate"];
      const color = { domain: order, range: C.series.slice(0, 4) };
      const rows = DATA.closureModes;
      const span = d3.extent(rows, (d) => +d.year);
      const years = d3.range(span[0], span[1] + 1).map(String);
      const chartWidth = Math.min(width, 120 + years.length * 70);
      return {
        node: plot({
          width: chartWidth, height: 280, color,
          x: { domain: years, label: null, padding: 0.6 }, y: { label: "issues closed", grid: true },
          marks: [
            Plot.barY(rows, { x: "year", y: "count", fill: "mode", insetTop: 1, title: (d) => `${d.year}\n${d.mode}: ${d.count}`, tip: TIP() }),
            Plot.ruleY([0], { stroke: C.axis }),
          ],
        }),
        legend: scaleLegend(color),
        table: rows.map((d) => ({ year: d.year, how: d.mode, issues: d.count })),
      };
    },

    labelHeat(width) {
      const labels = orderedLabels();
      return heat(width, DATA.labels.cells, "label", labels, "issues opened");
    },

    labelState(width) {
      const labels = orderedLabels();
      const color = { domain: ["Open", "Closed"], range: [C.series[0], C.series[1]] };
      return {
        node: plot({
          width, height: 50 + labels.length * 20, marginLeft: 220, color,
          x: { label: "issues →", grid: true }, y: { domain: labels, label: null, padding: 0.35 },
          marks: [
            Plot.barX(DATA.labels.state.filter((d) => d.count), { y: "label", x: "count", fill: "state", insetLeft: 1, insetRight: 1, title: (d) => `${d.label}\n${d.state}: ${d.count}`, tip: TIP() }),
            Plot.ruleX([0], { stroke: C.axis }),
          ],
        }),
        legend: scaleLegend(color),
        table: labels.map((label) => {
          const of = (state) => DATA.labels.state.find((d) => d.label === label && d.state === state).count;
          return { label, open: of("Open"), closed: of("Closed") };
        }),
      };
    },

    pathHeat(width) {
      const totals = new Map(DATA.paths.map((d) => [d.area, d.total]));
      const areas = [...totals.keys()].sort((a, b) => totals.get(b) - totals.get(a));
      return heat(width, DATA.paths, "area", areas, "merged pull requests");
    },

    people(width) {
      const rows = DATA.people.activity;
      const roles = ["Issues opened", "Issues closed", "Pull requests opened", "Pull requests merged", "Reviews given"];
      const years = [...new Set(rows.map((d) => d.year))].sort();
      const max = d3.max(rows, (d) => d.count) || 1;
      const bots = new Set(DATA.people.bots);
      const LABELS = 190;
      const CELL = 22; // the narrowest a year's cell that is still worth putting side by side
      const ROOMY = 30; // what a cell gets when the roles are stacked and width is not scarce
      // One grid of `shown` roles side by side. Plot shrinks an SVG wider than its card, which
      // makes the counts unreadable, so a grid is never asked to be wider than the card.
      const grid = (shown, gridWidth) =>
        plot({
          width: gridWidth, height: 70 + DATA.people.order.length * 24, marginLeft: LABELS, marginTop: 34,
          color: { type: "sqrt", domain: [0, max], interpolate: sequential() },
          fx: { domain: shown, label: null, padding: 0.05 },
          x: { domain: years, label: null, tickSize: 0, tickFormat: (y) => "’" + y.slice(2) },
          y: { domain: DATA.people.order, label: null, tickSize: 0, tickFormat: (login) => (bots.has(login) ? login + " (bot)" : login) },
          marks: [
            Plot.cell(rows.filter((d) => shown.includes(d.role)), { fx: "role", x: "year", y: "login", fill: "count", inset: 1, rx: 2, title: (d) => `${d.login}, ${d.year}\n${d.role}: ${d.count}`, tip: TIP() }),
            Plot.text(rows.filter((d) => shown.includes(d.role)), { fx: "role", x: "year", y: "login", text: "count", fontSize: 9.5, fill: (d) => (Math.sqrt(d.count / max) > 0.5 ? C.onHigh : C.text), pointerEvents: "none" }),
          ],
        });
      // A short history fits every role in one row. A long one (Uberon has twenty years) gets
      // as many roles per row as fit at a readable cell width, and at least one.
      const perRow = Math.max(1, Math.min(roles.length, Math.floor((width - LABELS) / (years.length * CELL))));
      const node = document.createElement("div");
      for (let i = 0; i < roles.length; i += perRow) {
        const shown = roles.slice(i, i + perRow);
        node.append(grid(shown, perRow === roles.length ? width : Math.min(width, LABELS + shown.length * years.length * ROOMY)));
      }
      return {
        node,
        legend: { ramp: C.seq, max, label: "events in the year" },
        table: rows.map((d) => ({ account: d.login, year: d.year, activity: d.role, count: d.count })),
      };
    },

    reviewPairs(width) {
      const rows = DATA.people.reviews.slice(0, 14).map((d) => ({ ...d, pair: `${d.author}  ←  ${d.reviewer}` }));
      if (!rows.length) return { node: message("No reviews recorded.") };
      return {
        node: plot({
          width: Math.min(width, 760), height: 40 + rows.length * 24, marginLeft: 280, marginRight: 40,
          x: { label: "pull requests reviewed →", grid: true }, y: { domain: rows.map((d) => d.pair), label: null, padding: 0.35 },
          marks: [
            Plot.barX(rows, { y: "pair", x: "count", fill: C.series[0], title: (d) => `${d.reviewer} reviewed ${d.count} of ${d.author}'s pull requests`, tip: TIP() }),
            Plot.text(rows, { y: "pair", x: "count", text: "count", dx: 6, textAnchor: "start", fill: C.text }),
            Plot.ruleX([0], { stroke: C.axis }),
          ],
        }),
        table: DATA.people.reviews.map((d) => ({ author: d.author, reviewer: d.reviewer, "pull requests": d.count })),
      };
    },

    assistance(width) {
      const order = ["No LLM tool named", "Names an LLM tool", "Bot account"];
      const color = { domain: order, range: C.series.slice(0, 3) };
      return stackedColumns(width, DATA.assistance, "kind", color, "pull requests opened");
    },
  };

  // ---- chart builders shared by several entries ---------------------------------------------

  function stackedBacklog(width, source, key, color) {
    const rows = source.map((d) => ({ ...d, at: date(d.date) }));
    const byDate = d3.groups(rows, (d) => d.date).map(([, group]) => ({
      at: group[0].at,
      total: d3.sum(group, (d) => d.count),
      lines: group.filter((d) => d.count).map((d) => `${d[key]}: ${d.count}`).reverse(),
    }));
    return {
      node: plot({
        width, height: 320, color,
        x: { label: null }, y: { label: "open issues", grid: true },
        marks: [
          Plot.areaY(rows, { x: "at", y: "count", fill: key, fillOpacity: 0.9, stroke: C.surface, strokeWidth: 1 }),
          Plot.ruleY([0], { stroke: C.axis }),
          ...crosshair(byDate, "at", (d) => `${fullDay(d.at)}: ${d.total} open\n${d.lines.join("\n")}`),
        ],
      }),
      legend: scaleLegend(color),
      table: d3.groups(rows, (d) => d.date).filter((_, i, all) => i % Math.ceil(all.length / 120) === 0 || i === all.length - 1)
        .map(([day, group]) => Object.fromEntries([["date", day], ...group.map((d) => [d[key], d.count])])),
    };
  }

  function survival(width, source) {
    if (!source.length) return { node: message("Not enough items yet.") };
    const cohorts = [...new Set(source.map((d) => d.cohort))];
    const color = cohortScale(cohorts);
    const rows = source.map((d) => ({ ...d, pct: d.share * 100 }));
    const longest = d3.max(rows, (d) => d.days);
    const ticks = [0, 1, 7, 30, 90, 365, 1095, 2555].filter((v) => v <= longest);
    return {
      node: plot({
        width, height: 300, color,
        x: { type: "symlog", constant: 1, ticks, tickFormat: (v) => (v >= 365 ? Math.round(v / 365) + " yr" : v + " d"), label: "since opening →", grid: true },
        y: { domain: [0, 100], label: "still open", tickFormat: (v) => v + "%", grid: true },
        marks: [
          Plot.ruleY([0], { stroke: C.axis }),
          Plot.line(rows, { x: "days", y: "pct", z: "cohort", stroke: "cohort", strokeWidth: 2 }),
          Plot.dot(rows, Plot.pointer({ x: "days", y: "pct", r: 4, fill: "cohort", stroke: C.surface, strokeWidth: 2, maxRadius: 120 })),
          Plot.tip(rows, Plot.pointer({ x: "days", y: "pct", maxRadius: 120, ...TIP(), title: (d) => `Opened ${d.cohort} (${d.n} items)\n${d.pct.toFixed(0)}% still open after ${d.days} days` })),
        ],
      }),
      legend: scaleLegend(color).map((item) => ({ ...item, line: true })),
      table: rows.map((d) => ({ opened: d.cohort, items: d.n, "days since opening": d.days, "still open": d.pct.toFixed(1) + "%" })),
    };
  }

  // Counts per coarse period, stacked by `key`.
  function stackedColumns(width, source, key, color, yLabel) {
    const unit = DATA.units.coarse;
    const rows = source.map((d) => {
      const start = date(d.date);
      return { ...d, start, end: addUnit(start, unit), period: periodName(start, unit) };
    });
    const periods = new Set(rows.map((d) => d.date)).size;
    const inset = barInset(width - 60, Math.max(periods, (d3.max(rows, (d) => +d.end) - d3.min(rows, (d) => +d.start)) / (addUnit(new Date(0), unit) - 0)));
    return {
      node: plot({
        width, height: 280, color,
        x: { label: null }, y: { label: yLabel, grid: true },
        marks: [
          Plot.rectY(rows.filter((d) => d.count), { x1: "start", x2: "end", y: "count", fill: key, insetLeft: inset, insetRight: inset, insetTop: 1, title: (d) => `${d.period}\n${d[key]}: ${d.count}`, tip: TIP() }),
          Plot.ruleY([0], { stroke: C.axis }),
        ],
      }),
      legend: scaleLegend(color),
      table: d3.groups(rows, (d) => d.period).map(([period, group]) => Object.fromEntries([["period", period], ...group.map((d) => [d[key], d.count])])),
    };
  }

  function orderedLabels() {
    const info = DATA.labels.labels;
    const familySize = d3.rollup(info, (v) => d3.sum(v, (d) => d.total), (d) => d.family);
    return [...info]
      .sort((a, b) => familySize.get(b.family) - familySize.get(a.family) || a.family.localeCompare(b.family) || b.total - a.total)
      .map((d) => d.label);
  }

  function heat(width, cells, key, rowsOrder, what) {
    const periods = coarsePeriods();
    const max = d3.max(cells, (d) => d.count) || 1;
    return {
      node: plot({
        width, height: 60 + rowsOrder.length * 20, marginLeft: 220,
        color: { type: "sqrt", domain: [0, max], interpolate: sequential() },
        x: { domain: periods, label: null, tickFormat: coarseTick, tickSize: 0 },
        y: { domain: rowsOrder, label: null, tickSize: 0 },
        marks: [
          Plot.cell(cells, { x: "period", y: key, fill: "count", inset: 1, rx: 2, title: (d) => `${d[key]}, ${d.period}\n${what}: ${d.count}`, tip: TIP() }),
        ],
      }),
      legend: { ramp: C.seq, max, label: what + " in the period" },
      table: cells.map((d) => ({ [key]: d[key], period: d.period, [what]: d.count })),
    };
  }

  // The flow of open issues between milestone bands, one column per snapshot. Drawn with
  // d3-sankey; columns are fixed by snapshot, so links only ever run left to right.
  function alluvial(width) {
    const flow = DATA.milestoneFlow;
    if (!flow.links.length) return { node: message("No milestone history to show.") };
    const order = [NEW, ...DATA.bands, CLOSED];
    const height = 560, top = 30;
    const nodes = flow.nodes.map((n) => ({ ...n, order: order.indexOf(n.band) }));
    const links = flow.links.map((l) => ({ ...l }));
    const layout = d3.sankey()
      .nodeWidth(12).nodePadding(9)
      .nodeAlign((node) => node.column)
      .nodeSort((a, b) => a.order - b.order)
      .extent([[2, top], [width - (width < 700 ? 40 : 170), height - 4]]);
    layout({ nodes, links });

    const svg = d3.create("svg").attr("width", width).attr("height", height).attr("viewBox", [0, 0, width, height])
      .style("font", "11px system-ui, sans-serif");
    const last = flow.columns.length - 1;
    const columnX = d3.rollup(nodes, (v) => v[0].x0, (d) => d.column);
    const when = (column) => (column === last ? "now" : monthYear(date(flow.columns[column])));

    svg.append("g").selectAll("text").data(flow.columns).join("text")
      .attr("x", (_, i) => columnX.get(i))
      .attr("y", 14).attr("text-anchor", "start")
      .attr("fill", C.text).attr("font-weight", 600)
      .text((_, i) => (i === last ? "Now" : monthYear(date(flow.columns[i]))));

    const linkText = (d) => {
      const span = `${when(d.source.column)} → ${when(d.target.column)}`;
      if (d.source.band === NEW) return `${d.value} issues opened ${span}\n${d.target.band === CLOSED ? "and closed in the same period" : "in “" + d.target.band + "” at the end"}`;
      if (d.target.band === CLOSED) return `${d.value} issues in “${d.source.band}”\nclosed ${span}`;
      return `${d.value} issues\n“${d.source.band}” → “${d.target.band}”\n${span}`;
    };
    svg.append("g").attr("fill", "none").selectAll("path").data(links).join("path")
      .attr("d", d3.sankeyLinkHorizontal())
      .attr("stroke", (d) => bandColor(d.source.band === NEW ? d.target.band : d.source.band))
      .attr("stroke-opacity", (d) => (d.source.band === NEW || d.target.band === CLOSED ? 0.22 : 0.42))
      .attr("stroke-width", (d) => Math.max(1, d.width))
      .call(hover, linkText)
      .on("pointerenter", function () { d3.select(this).attr("stroke-opacity", 0.75); })
      .on("pointerout", function (_, d) { d3.select(this).attr("stroke-opacity", d.source.band === NEW || d.target.band === CLOSED ? 0.22 : 0.42); });

    const nodeText = (d) =>
      d.band === NEW ? `${d.value} issues opened after ${when(d.column)}`
      : d.band === CLOSED ? `${d.value} issues closed before ${when(d.column)}`
      : `${d.band}\n${d.value} open issues, ${when(d.column)}`;
    svg.append("g").selectAll("rect").data(nodes).join("rect")
      .attr("x", (d) => d.x0).attr("y", (d) => d.y0).attr("rx", 2)
      .attr("width", (d) => d.x1 - d.x0).attr("height", (d) => Math.max(1, d.y1 - d.y0))
      .attr("fill", (d) => bandColor(d.band))
      .call(hover, nodeText);

    // Name a block only where it is tall enough to carry the text; the legend names the rest.
    svg.append("g").selectAll("text").data(nodes.filter((d) => d.y1 - d.y0 >= 13)).join("text")
      .attr("x", (d) => d.x1 + 6)
      .attr("y", (d) => (d.y0 + d.y1) / 2).attr("dy", "0.35em")
      .attr("text-anchor", "start")
      .attr("fill", C.text).attr("stroke", C.surface).attr("stroke-width", 3).attr("paint-order", "stroke")
      .attr("pointer-events", "none")
      .text((d) => (d.band === NEW ? `${d.value} opened` : d.band === CLOSED ? `${d.value} closed` : width < 700 ? d.value : `${d.band} ${d.value}`));

    return {
      node: svg.node(),
      legend: [...bandLegend(), { label: "Opened in the period", color: C.neutral }, { label: "Closed in the period", color: C.neutralStrong }],
      table: links.map((d) => ({ from: when(d.source.column), to: when(d.target.column), "was in": d.source.band, "then in": d.target.band, issues: d.value })),
    };
  }

  // ---- page assembly ------------------------------------------------------------------------

  function message(text) {
    const p = document.createElement("p");
    p.className = "error";
    p.textContent = text;
    return p;
  }

  function legendNode(legend) {
    const box = document.createElement("div");
    box.className = "legend";
    if (!Array.isArray(legend)) {
      const span = document.createElement("span");
      const ramp = document.createElement("i");
      ramp.className = "ramp";
      ramp.style.background = `linear-gradient(to right, ${legend.ramp.join(",")})`;
      span.append("1", ramp, `${legend.max} ${legend.label}`);
      box.append(span);
      return box;
    }
    for (const item of legend) {
      const span = document.createElement("span");
      const swatch = document.createElement("i");
      if (item.line) swatch.className = "line";
      swatch.style.background = item.color;
      if (item.faint) swatch.style.opacity = 0.25;
      span.append(swatch, item.label);
      box.append(span);
    }
    return box;
  }

  const TABLE_LIMIT = 500;
  function tableNode(rows) {
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "Data table";
    details.append(summary);
    details.addEventListener("toggle", () => {
      if (!details.open || details.querySelector("table")) return;
      const columns = [...new Set(rows.flatMap((row) => Object.keys(row)))];
      const table = document.createElement("table");
      const head = table.createTHead().insertRow();
      const numeric = (column) => rows.every((row) => row[column] === "" || row[column] == null || typeof row[column] === "number");
      for (const column of columns) {
        const th = document.createElement("th");
        th.textContent = column;
        if (numeric(column)) th.className = "num";
        head.append(th);
      }
      const body = table.createTBody();
      for (const row of rows.slice(0, TABLE_LIMIT)) {
        const tr = body.insertRow();
        for (const column of columns) {
          const td = tr.insertCell();
          td.textContent = row[column] ?? "";
          if (numeric(column)) td.className = "num";
        }
      }
      const wrap = document.createElement("div");
      wrap.className = "table-wrap";
      wrap.append(table);
      details.append(wrap);
      if (rows.length > TABLE_LIMIT) details.append(`First ${TABLE_LIMIT} of ${rows.length} rows; the rest are in dashboard.json.`);
    });
    return details;
  }

  function draw() {
    C = tokens();
    for (const card of document.querySelectorAll("[data-chart]")) {
      card.querySelectorAll(".chart, .legend, details").forEach((old) => old.remove());
      const holder = document.createElement("div");
      holder.className = "chart";
      card.append(holder);
      const width = Math.max(320, Math.floor(holder.getBoundingClientRect().width));
      try {
        const result = charts[card.dataset.chart](width);
        holder.append(result.node);
        if (result.legend) card.append(legendNode(result.legend));
        if (result.table && result.table.length) card.append(tableNode(result.table));
      } catch (error) {
        console.error(card.dataset.chart, error);
        holder.append(message(`This chart could not be drawn: ${error.message}`));
      }
    }
  }

  function chrome() {
    document.getElementById("repo-link").href = DATA.url;
    document.getElementById("fetched").textContent =
      `${DATA.checks.issues} issues and ${DATA.checks.pullRequests} pull requests, as fetched from GitHub on ${fullDay(NOW)}.`;
    for (const span of document.querySelectorAll("[data-unit]")) span.textContent = UNIT_NAMES[DATA.units[span.dataset.unit]];

    const tiles = document.getElementById("tiles");
    for (const tile of DATA.tiles) {
      const box = document.createElement("div");
      box.className = "tile";
      for (const [cls, text] of [["label", tile.label], ["value", tile.value], ["sub", tile.note]]) {
        const div = document.createElement("div");
        div.className = cls;
        div.textContent = text;
        box.append(div);
      }
      tiles.append(box);
    }

    const toc = document.getElementById("toc");
    for (const section of document.querySelectorAll("main section[id]")) {
      const heading = section.querySelector("h2");
      if (!heading) continue;
      const link = document.createElement("a");
      link.href = "#" + section.id;
      link.textContent = heading.textContent;
      toc.append(link);
    }

    const checks = DATA.checks;
    const foot = document.getElementById("checks");
    const lines = [
      `History is rebuilt by replaying each item's GitHub timeline. GitHub counts ${checks.githubIssues} issues and ${checks.githubPullRequests} pull requests; this page has ${checks.issues} and ${checks.pullRequests}.`,
      checks.replayMismatches
        ? `For ${checks.replayMismatches} items the replay ends somewhere other than the item's present state, usually because a milestone was renamed or deleted after they were put in it; those take their present state from GitHub.`
        : "Every replay ends in the item's present state.",
      "Timeline events record what a milestone was called at the time, so a renamed milestone appears under both names.",
    ];
    for (const line of lines) {
      const p = document.createElement("p");
      p.textContent = line;
      foot.append(p);
    }
  }

  chrome();
  draw();
  let pending;
  let lastWidth = innerWidth;
  addEventListener("resize", () => {
    if (innerWidth === lastWidth) return;
    lastWidth = innerWidth;
    clearTimeout(pending);
    pending = setTimeout(draw, 150);
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
})();
