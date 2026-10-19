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
from html import unescape as html_unescape
from pathlib import Path
from zoneinfo import ZoneInfo

import markdown
import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "content"
OUT = ROOT / "_site"
CONFIG = json.loads((ROOT / "config" / "city.json").read_text(encoding="utf-8-sig"))
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
    text = path.read_text(encoding="utf-8-sig")
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
        daily[date.fromisoformat(p.stem)] = json.loads(p.read_text(encoding="utf-8-sig"))
    briefs.sort(key=lambda b: b["date"], reverse=True)
    stories.sort(key=lambda s: (s["date"], s.get("event_date") or s["date"]), reverse=True)
    return briefs, stories, daily


# Map colour groups, keyed by the words in a brief's section heading or a story's type.
MAP_GROUPS = [
    ("alerts", "Alerts & outages", ("alert", "outage")),
    ("safety", "Safety", ("safety",)),
    ("government", "Local government", ("government", "watch", "decided")),
    ("elections", "Elections", ("election",)),
    ("housing", "Housing & development", ("housing", "around")),
    ("transport", "Getting around", ("getting around", "transport")),
    ("community", "Health & community", ("health", "community")),
    ("environment", "Environment", ("environment",)),
    ("events", "Events & weekend", ("weekend", "coming up", "event", "indepth")),
]


def map_group(text):
    t = (text or "").lower()
    return next((key for key, _, words in MAP_GROUPS if any(w in t for w in words)), "events")


def strip_tags(s):
    return " ".join(re.sub(r"<[^>]+>", " ", s).split())


def map_points(briefs, stories, places):
    """Collect located items. Gives tagged brief paragraphs an id if they lack one, so the map can link to them."""
    points, unknown = [], set()

    def locate(raw):
        for name in (n.strip() for n in raw.split(";") if n.strip()):
            hit = places.get(name)
            if hit:
                yield name, hit
            else:
                unknown.add(name)

    for b in briefs:
        section, used = "", set(re.findall(r'id="([^"]+)"', b["html"]))

        def tag(m):
            nonlocal section
            if m.group("h2") is not None:
                section = strip_tags(m.group("h2"))
                return m.group(0)
            attrs, body = m.group("attrs"), m.group("body")
            lead_m = re.search(r"<strong>(.*?)</strong>", body, re.S)
            lead = strip_tags(lead_m.group(1)) if lead_m else strip_tags(body)[:120]
            pid = re.search(r'\bid="([^"]+)"', attrs)
            if pid:
                pid = pid.group(1)
            else:
                pid = re.sub(r"[^a-z0-9]+", "-", lead.lower()).strip("-")[:48] or "item"
                while pid in used:
                    pid += "-x"
                used.add(pid)
                attrs = f' id="{pid}"' + attrs
            rest = strip_tags(body[lead_m.end():] if lead_m else "")
            for name, hit in locate(html_unescape(re.search(r'data-place="([^"]+)"', attrs).group(1))):
                points.append({"lat": hit["lat"], "lon": hit["lon"], "place": name, "title": html_unescape(lead),
                               "text": html_unescape(rest[:180] + ("…" if len(rest) > 180 else "")),
                               "url": f"{b['url']}#{pid}", "date": b["date"].isoformat(),
                               "label": re.sub(r"^\W+", "", html_unescape(section)), "group": map_group(section)})
            return f"<p{attrs}>{body}</p>"

        b["html"] = re.sub(r"<h2[^>]*>(?P<h2>.*?)</h2>|<p(?P<attrs>[^>]*\bdata-place=\"[^\"]*\"[^>]*)>(?P<body>.*?)</p>",
                           tag, b["html"], flags=re.S)

    for s in stories:
        for name, hit in locate("; ".join(s.get("places") or [])):
            points.append({"lat": hit["lat"], "lon": hit["lon"], "place": name, "title": s["title"],
                           "text": s["summary"], "url": s["url"], "date": s["date"].isoformat(),
                           "label": s["label"], "group": map_group(s["type"])})
    for name in sorted(unknown):
        print(f"  ! no coordinates for place '{name}' (run scripts/geocode.py)")
    return points


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


EVENT_CATEGORIES = {
    "music": "Music", "stage": "Stage & comedy", "food": "Food & drink", "markets": "Markets",
    "festivals": "Festivals", "outdoors": "Outdoors", "sport": "Sport", "art": "Art", "family": "Family",
    "film-talks": "Film & talks", "other": "Out & about",
}


def load_weekends():
    """content/weekend/<monday>.json, newest first."""
    weeks = []
    for p in sorted((CONTENT / "weekend").glob("*.json"), reverse=True):
        w = json.loads(p.read_text(encoding="utf-8-sig"))
        w["monday"] = date.fromisoformat(w["week_start"])
        w["url"] = f"/weekend/{w['week_start']}/"
        weeks.append(w)
    return weeks


def where(venue, area):
    """'Venue, Area', leaving out the area when it's just 'Cape Town' or already in the venue's name."""
    venue = venue or ""
    if area and area != "Cape Town" and area.lower() not in venue.lower():
        return f"{venue}, {area}" if venue else area
    return venue or area or ""


def weekend_view(week, today):
    """Group a week's events into Friday, Saturday, Sunday and 'on all weekend'."""
    fri = date.fromisoformat(week["weekend_start"])
    days = [fri + timedelta(days=i) for i in range(3)]
    by_day, multi = {d: [] for d in days}, []
    for e in week["events"]:
        start = datetime.fromisoformat(e["start"])
        end = datetime.fromisoformat(e["end"]) if e.get("end") else start
        if end.time() == datetime.min.time() and end.date() > start.date():
            end -= timedelta(minutes=1)  # ends at midnight: the day before
        on = {datetime.fromisoformat(x).date() for x in e.get("sessions") or [e["start"]]}
        if end - start >= timedelta(hours=20):  # a real multi-day run, not an evening that ends after midnight
            on |= {d for d in days if start.date() <= d <= end.date()}
        on = sorted(d for d in on if d in days)
        if not on:
            continue
        ev = dict(e, days=on, is_new=e.get("added") == today.isoformat(),
                  label=EVENT_CATEGORIES.get(e["category"], "Out & about"),
                  time=None if e.get("all_day") or len(on) > 1 else f"{start:%H:%M}",
                  where=where(e.get("venue"), e.get("area")))
        if len(on) > 1:
            span = f"{on[0]:%a}–{on[-1]:%a}" if len(on) == 3 else " & ".join(f"{d:%a}" for d in on)
            ev["when"] = f"{span}" + (f", until {end.day} {end:%b}" if end.date() > days[-1] else "")
            multi.append(ev)
        else:
            by_day[on[0]].append(ev)
    order = lambda e: (not e.get("highlight"), e.get("time") or "00:00", e["title"])
    groups = [(d, sorted(by_day[d], key=order)) for d in days]
    everything = [e for _, v in groups for e in v] + multi
    # Homepage: the editor's picks first, then one from each category so it isn't five wine tastings.
    picks, seen = [e for e in everything if e.get("highlight")], set()
    for e in everything:
        if len(picks) >= 5:
            break
        if e not in picks and e["category"] not in seen:
            picks.append(e)
            seen.add(e["category"])
    return {"days": groups, "multi": sorted(multi, key=lambda e: (not e.get("highlight"), e["title"])),
            "count": sum(len(v) for v in by_day.values()) + len(multi),
            "new": sum(1 for v in by_day.values() for e in v if e["is_new"]) + sum(e["is_new"] for e in multi),
            "fri": fri, "sun": days[-1], "picks": picks[:5]}


def load_history():
    """content/history/MM-DD.json -> {"MM-DD": {...}}."""
    out = {}
    for p in sorted((CONTENT / "history").glob("*.json")):
        h = json.loads(p.read_text(encoding="utf-8-sig"))
        h["cape"] = [x for x in h["south_africa"] if x.get("cape_town")]
        h["sa"] = [x for x in h["south_africa"] if not x.get("cape_town")]
        h["url"] = f"/history/{p.stem}/"
        out[p.stem] = h
    return out


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

    places_file = CONTENT / "places.json"
    places = {k: v for k, v in json.loads(places_file.read_text(encoding="utf-8-sig")).items() if v} \
        if places_file.exists() else {}
    points = map_points(briefs, stories, places)

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
    weeks = load_weekends()
    current_week = next((w for w in weeks if w["monday"] <= today), None)
    weekend = weekend_view(current_week, today) if current_week else None
    history = load_history()
    history_today = history.get(f"{today:%m-%d}")
    numbers = [
        (len(briefs), "daily briefs published"),
        (len(stories), "council and town stories"),
        (len(meeting_ids), "City meetings tracked"),
        (len(CONFIG["feeds"]), "newsroom feeds read each morning"),
    ]
    if weekend and weekend["count"]:
        numbers.insert(2, (weekend["count"], "things to do this weekend"))

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
    env.filters["md"] = lambda k: f"{int(k[3:])} {date(2000, int(k[:2]), 1):%B}"  # "10-21" -> "21 October"
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
           week=week_view(today, briefs, stories, meetings), recent=briefs[1:9],
           weekend=weekend, weekend_url=current_week["url"] if current_week else None, history_today=history_today)
    for w in weeks:
        view = weekend_view(w, today)
        others = [x for x in weeks if x is not w][:8]
        render("weekend.html", w["url"], week=w, view=view, others=others, current=w is current_week)
        if w is current_week:
            render("weekend.html", "/weekend/", week=w, view=view, others=others, current=True)
    for key, h in history.items():
        d = date(2000, int(key[:2]), int(key[3:]))  # a leap year, so 29 February works
        keys = list(history)
        i = keys.index(key)
        nav = dict(prev=history[keys[i - 1]] if i > 0 else None,
                   next=history[keys[i + 1]] if i + 1 < len(keys) else None)
        render("history.html", h["url"], h=h, day=d, **nav)
        if h is (history_today or next(iter(history.values()))):
            render("history.html", "/history/", h=h, day=d, **nav)
    for i, b in enumerate(briefs):
        render("brief.html", b["url"], brief=b, day=daily.get(b["date"]),
               newer=briefs[i - 1] if i > 0 else None, older=briefs[i + 1] if i + 1 < len(briefs) else None)
    for s in stories:
        related = [x for x in stories if x["type"] == s["type"] and x is not s][:4]
        render("story.html", s["url"], story=s, related=related)
    for path, (title, intro, kinds) in SECTIONS.items():
        render("section.html", path, title=title, intro=intro, kinds=kinds,
               stories=[s for s in stories if s["type"] in kinds])
    for p in points:
        p["url"] = base + p["url"]
    render("map.html", "/map/", today=today, groups=[(k, label) for k, label, _ in MAP_GROUPS],
           points_json=Markup(json.dumps(points, ensure_ascii=False).replace("</", "<\\/")))
    render("archive.html", "/briefs/", briefs=briefs)
    render("404.html", "/404.html")
    render("feed.xml", "/feed.xml", items=sorted(
        [{"title": b["headline"], "url": b["url"], "date": b["date"], "summary": b["dek"]} for b in briefs] +
        [{"title": s["title"], "url": s["url"], "date": s["date"], "summary": s["summary"]} for s in stories],
        key=lambda x: x["date"], reverse=True)[:40])
    (OUT / ".nojekyll").write_text("")
    print(f"Built {len(briefs)} briefs, {len(stories)} stories -> {OUT.relative_to(ROOT)} (base '{base}')")


if __name__ == "__main__":
    main()
