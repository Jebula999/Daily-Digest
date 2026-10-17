"""Build the static site from content/ into _site/.

Usage: python scripts/build.py [--base /repo-name] [--today YYYY-MM-DD]
The base path can also come from the BASE_PATH environment variable (GitHub Pages project sites).
"""
import argparse
import json
import os
import re
import shutil
import sys
from datetime import date, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import markdown
import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "content"
OUT = ROOT / "_site"
CONFIG = json.loads((ROOT / "config" / "city.json").read_text(encoding="utf-8"))
TZ = ZoneInfo(CONFIG["timezone"])

STORY_TYPES = {
    "watch": "What to watch",
    "decided": "What they decided",
    "around": "Around town",
    "indepth": "In depth",
}
# Short labels used in the week-at-a-glance grid.
WEEK_KIND = {"brief": "Brief", "watch": "Preview", "decided": "Report", "around": "Around town",
             "indepth": "In depth", "meeting": "Meeting"}
# Section pages: url -> (title, intro, story types)
SECTIONS = {
    "/local-government/": ("Local government", "Previews of upcoming City meetings and what councillors, "
                           "committees and the planning tribunal decided.", ["watch", "decided"]),
    "/around-town/": ("Around town", "Planning applications, appeals and developments, neighbourhood by neighbourhood.",
                      ["around"]),
    "/in-depth/": ("In depth", "Longer explainers that pull a running story together.", ["indepth"]),
}
REQUIRED = {"brief": ["date", "headline", "dek"], "story": ["type", "title", "date", "summary"]}


def sky(code):
    """WMO weather code → plain words."""
    table = [((0,), "Clear"), ((1,), "Mostly clear"), ((2,), "Partly cloudy"), ((3,), "Overcast"),
             ((45, 48), "Fog"), ((51, 53, 55, 56, 57), "Drizzle"), ((61, 63, 66, 80, 81), "Rain"),
             ((65, 67, 82), "Heavy rain"), ((71, 73, 75, 77, 85, 86), "Snow"), ((95, 96, 99), "Thunderstorms")]
    return next((label for codes, label in table if code in codes), "Mixed")


def keep_hyphens(text):
    """Escape text and keep hyphenated words (year-end, Hill-Lewis) on one line."""
    out = re.sub(r"\w+(?:-\w+)+", lambda m: f'<span class="nw">{m.group(0)}</span>', str(escape(text or "")))
    return Markup(out)


class ContentError(Exception):
    pass


def parse(path):
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
    if not m:
        raise ContentError(f"{path}: missing front matter")
    meta = yaml.safe_load(m.group(1)) or {}
    md = markdown.Markdown(extensions=["toc", "attr_list", "tables", "smarty"])
    meta["html"] = md.convert(m.group(2))
    meta["path"] = path
    for k in ("date", "event_date"):
        if isinstance(meta.get(k), str):
            meta[k] = date.fromisoformat(meta[k])
    return meta


def check(meta, kind):
    missing = [k for k in REQUIRED[kind] if not meta.get(k)]
    if missing:
        raise ContentError(f"{meta['path'].name}: missing {', '.join(missing)}")
    if kind == "story" and meta["type"] not in STORY_TYPES:
        raise ContentError(f"{meta['path'].name}: unknown type {meta['type']!r}")


def load():
    briefs, stories, daily = [], [], {}
    for p in sorted((CONTENT / "briefs").glob("*.md")):
        b = parse(p)
        check(b, "brief")
        b["url"] = f"/briefs/{b['date']}/"
        briefs.append(b)
    for p in sorted((CONTENT / "stories").glob("*.md")):
        s = parse(p)
        check(s, "story")
        s["slug"] = p.stem
        s["url"] = f"/stories/{p.stem}/"
        s["label"] = STORY_TYPES[s["type"]]
        stories.append(s)
    for p in sorted((CONTENT / "daily").glob("*.json")):
        daily[date.fromisoformat(p.stem)] = json.loads(p.read_text(encoding="utf-8"))
    briefs.sort(key=lambda b: b["date"], reverse=True)
    stories.sort(key=lambda s: (s["date"], s.get("event_date") or s["date"]), reverse=True)
    return briefs, stories, daily


def week_view(today, briefs, stories, meetings):
    """Five columns: the previous working day, today, then the next working days.
    Weekend days only appear when something is happening on them."""
    covered = {s.get("meeting_id") for s in stories}

    def entries_for(d):
        out = [{"kind": WEEK_KIND["brief"], "title": b.get("alert") or b["headline"], "url": b["url"]}
               for b in briefs if b["date"] == d]
        for s in stories:
            when = s.get("event_date") if s["type"] == "watch" else s["date"]
            if when == d:
                out.append({"kind": WEEK_KIND[s["type"]], "title": s["title"], "url": s["url"]})
        for m in meetings:
            if m["date"] == d.isoformat() and m["id"] not in covered:
                out.append({"kind": WEEK_KIND["meeting"], "title": f"{m['title']}, {m['start']}",
                            "url": m["url"], "external": True})
        return out

    days, d = [], today - timedelta(days=1)
    while d.weekday() >= 5 and not entries_for(d):
        d -= timedelta(days=1)
    while len(days) < 5:
        e = entries_for(d)
        if d.weekday() < 5 or e or d == today:
            days.append({"date": d, "is_today": d == today, "entries": e})
        d += timedelta(days=1)
    return days


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("BASE_PATH", ""))
    ap.add_argument("--today", default=None, help="override today's date (YYYY-MM-DD)")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    try:
        briefs, stories, daily = load()
    except ContentError as e:
        sys.exit(f"Content error: {e}")

    # Root-relative links written in markdown ("/stories/...") need the base path too.
    for item in briefs + stories:
        item["html"] = re.sub(r'href="/(?!/)', f'href="{base}/', item["html"])

    today = date.fromisoformat(args.today) if args.today else datetime.now(TZ).date()
    if briefs and briefs[0]["date"] < today and not args.today:
        today = briefs[0]["date"]  # the latest edition stays "today" until a new one lands

    lead = briefs[0] if briefs else None
    conditions = daily.get(lead["date"]) if lead else None
    meetings = (conditions or {}).get("meetings", [])
    next_meeting = next((m for m in meetings if m["date"] >= today.isoformat()), None)
    meeting_ids = {m["id"] for d in daily.values() for m in d.get("meetings", [])}
    numbers = [
        (len(briefs), "daily briefs published"),
        (len(stories), "council and town stories"),
        (len(meeting_ids), "City meetings tracked"),
        (len(CONFIG["feeds"]), "newsroom feeds read each morning"),
        (0, "Google News or aggregator links used"),
    ]

    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html", "xml"]))
    env.globals.update(site=CONFIG, base=base, types=STORY_TYPES, build_time=datetime.now(TZ),
                       sections=SECTIONS, conditions=conditions, next_meeting=next_meeting,
                       numbers=numbers, lead_alert=lead.get("alert") if lead else None)
    env.filters["url"] = lambda p: base + p
    env.filters["long_date"] = lambda d: f"{d:%A} {d.day} {d:%B %Y}"
    env.filters["dmy"] = lambda d: f"{d.day} {d:%B %Y}"
    env.filters["short_date"] = lambda d: f"{d.day} {d:%b}"
    env.filters["weekday"] = lambda d: f"{d:%a}"
    env.filters["fulldayname"] = lambda d: f"{d:%A}"
    env.filters["sky"] = sky
    env.filters["keep"] = keep_hyphens
    env.filters["iso"] = lambda s: date.fromisoformat(s)
    env.filters["rfc822"] = lambda d: format_datetime(datetime(d.year, d.month, d.day, 6, 0, tzinfo=TZ))

    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "static").mkdir(parents=True)
    for f in (ROOT / "static").iterdir():
        shutil.copy(f, OUT / "static" / f.name)

    def render(tpl, out_path, **ctx):
        dest = OUT / out_path.lstrip("/")
        if out_path.endswith("/"):
            dest = dest / "index.html"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(env.get_template(tpl).render(page=out_path, **ctx), encoding="utf-8")

    latest_by_type = {t: next((s for s in stories if s["type"] == t), None) for t in STORY_TYPES}
    render("index.html", "/", lead=lead, today=today, latest_by_type=latest_by_type,
           week=week_view(today, briefs, stories, meetings), recent=briefs[1:9])
    for i, b in enumerate(briefs):
        render("brief.html", b["url"], brief=b, day=daily.get(b["date"]),
               newer=briefs[i - 1] if i > 0 else None, older=briefs[i + 1] if i + 1 < len(briefs) else None)
    for s in stories:
        related = [x for x in stories if x["type"] == s["type"] and x is not s][:4]
        render("story.html", s["url"], story=s, related=related)
    for path, (title, intro, kinds) in SECTIONS.items():
        render("section.html", path, title=title, intro=intro, kinds=kinds,
               stories=[s for s in stories if s["type"] in kinds])
    render("archive.html", "/briefs/", briefs=briefs)
    render("about.html", "/about/")
    render("404.html", "/404.html")
    render("feed.xml", "/feed.xml", items=sorted(
        [{"title": b["headline"], "url": b["url"], "date": b["date"], "summary": b["dek"]} for b in briefs] +
        [{"title": s["title"], "url": s["url"], "date": s["date"], "summary": s["summary"]} for s in stories],
        key=lambda x: x["date"], reverse=True)[:40])
    (OUT / ".nojekyll").write_text("")
    print(f"Built {len(briefs)} briefs, {len(stories)} stories -> {OUT.relative_to(ROOT)} (base '{base}')")


if __name__ == "__main__":
    main()
