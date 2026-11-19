"""Build data/<owner>/<repo>/site/index.html: one self-contained page with its data inside it.

The data is embedded rather than fetched so the page opens straight from disk (a `file://`
page may not fetch a neighbouring JSON file). The chart libraries still come from a CDN.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from .config import load_config, use_busiest_milestones
from .events import load_items, parse_time
from .fetch import DATA_ROOT, repo_dir
from .metrics import build_dashboard, milestone_issue_days


def build_site(repo: str, *, root: Path = DATA_ROOT, config_path: Path = Path("repos.toml")) -> Path:
    base = repo_dir(repo, root)
    meta = json.loads((base / "raw" / "meta.json").read_text())
    config = load_config(repo, config_path)
    items = load_items(base / "raw", bots=config.bots)
    now = parse_time(meta["fetchedAt"])
    assert now is not None
    use_busiest_milestones(config, milestone_issue_days([i for i in items if i.kind == "issue"], now))

    dashboard = build_dashboard(items, meta, config, now)
    (base / "dashboard.json").write_text(json.dumps(dashboard, ensure_ascii=False))

    site = base / "site"
    site.mkdir(exist_ok=True)
    out = site / "index.html"
    out.write_text(render_page(dashboard))
    return out


def render_page(dashboard: dict) -> str:
    templates = resources.files("uberwatch") / "templates"
    page = (templates / "dashboard.html").read_text()
    # "</" would end the <script> element early if a title happened to contain "</script>".
    data = json.dumps(dashboard, ensure_ascii=False).replace("</", "<\\/")
    return (
        page.replace("{{TITLE}}", _escape(dashboard.get("repo") or "repository"))
        .replace("/*{{CSS}}*/", (templates / "dashboard.css").read_text())
        .replace("/*{{JS}}*/", (templates / "dashboard.js").read_text())
        .replace("/*{{DATA}}*/", data)
    )


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
