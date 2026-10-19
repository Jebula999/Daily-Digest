"""Build this week's 'This weekend' list, a little more each morning.

The week runs Monday to Sunday. Each run finds events on the coming Friday–Sunday, applies the filters in
config/weekend.json, and adds only events not already on the list. A new list starts every Monday.

Writes:
  content/weekend/<monday>.json   the week's list (committed; the site reads it)
  data/raw/<today>-weekend.md     what's new today, plus event articles for the editor to mine

Usage:
  python scripts/weekend.py                     collect (normally run by collect.py)
  python scripts/weekend.py skip <id> [reason]  take an event off the list and never re-add it this week
"""
import html
import json
import math
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from hashlib import sha1
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config" / "weekend.json").read_text(encoding="utf-8-sig"))
TZ = ZoneInfo("Africa/Johannesburg")
UA = {"User-Agent": "Mozilla/5.0 (compatible; CapeTownDailyBrief/1.0; +https://github.com/Jebula999/Daily-Digest)"}


def words(keys):
    """Case-insensitive whole-word pattern for a list of phrases."""
    keys = [k.strip() for k in keys if k.strip()]
    return re.compile(r"\b(" + "|".join(re.escape(k) for k in keys) + r")\b", re.I) if keys else None


SKIP = words(CFG["skip_keywords"])
KEEP = words(CFG["keep_keywords"])
CATEGORIES = [(name, words(keys)) for name, keys in CFG["categories"].items()]
LOCALITIES = [l.lower() for l in CFG["area"]["localities"]]


def week_of(day):
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=4), monday + timedelta(days=6)  # Monday, Friday, Sunday


def path_for(monday):
    return ROOT / "content" / "weekend" / f"{monday}.json"


def load_week(monday, friday, sunday):
    p = path_for(monday)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8-sig"))
    return {"week_start": monday.isoformat(), "weekend_start": friday.isoformat(), "weekend_end": sunday.isoformat(),
            "events": [], "skipped": [], "articles_seen": []}


def save_week(week, monday):
    p = path_for(monday)
    p.parent.mkdir(parents=True, exist_ok=True)
    week["events"].sort(key=lambda e: (e["start"], e["title"]))
    p.write_text(json.dumps(week, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def clean(s, limit=None):
    s = html.unescape(html.unescape(s or ""))
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    s = " ".join(s.replace("\\n", " ").split())
    return s[:limit].rstrip() + "…" if limit and len(s) > limit else s


def key_for(title, start):
    norm = re.sub(r"[^a-z0-9]+", "", title.lower())[:50]
    return sha1(f"{norm}|{start[:10]}".encode()).hexdigest()[:10]


def parse_when(value):
    """schema.org date or datetime → aware local datetime (date-only → midnight)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(TZ) if dt.tzinfo else dt.replace(tzinfo=TZ)


def jsonld_events(page):
    out = []

    def walk(o):
        if isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            t = o.get("@type")
            if any(isinstance(x, str) and x.endswith("Event") for x in (t if isinstance(t, list) else [t])):
                out.append(o)
            for k in ("@graph", "itemListElement", "item"):
                if k in o:
                    walk(o[k])

    for m in re.finditer(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", page, re.S | re.I):
        try:
            walk(json.loads(m.group(1)))
        except ValueError:
            pass
    return out


def normalise(e, source):
    loc = e.get("location") or {}
    loc = loc[0] if isinstance(loc, list) and loc else loc
    loc = loc if isinstance(loc, dict) else {"name": str(loc)}
    addr = loc.get("address") or {}
    addr = addr if isinstance(addr, dict) else {"streetAddress": str(addr)}
    geo = loc.get("geo") or {}
    offers = e.get("offers") or []
    offers = offers if isinstance(offers, list) else [offers]
    prices = []
    for o in offers:
        try:
            prices.append(float(o.get("price") or o.get("lowPrice") or 0))
        except (TypeError, ValueError):
            pass
    return {
        "title": clean(e.get("name")),
        "start": parse_when(e.get("startDate")),
        "end": parse_when(e.get("endDate")),
        "venue": clean(loc.get("name")),
        "locality": clean(addr.get("addressLocality")),
        "region": clean(addr.get("addressRegion")),
        "street": clean(addr.get("streetAddress")),
        "lat": float(geo["latitude"]) if geo.get("latitude") else None,
        "lon": float(geo["longitude"]) if geo.get("longitude") else None,
        "url": (e.get("url") or "").split("#")[0].split("?aff")[0],
        "price_from": min([p for p in prices if p > 0], default=None),
        "online": "Online" in str(e.get("eventAttendanceMode", "")),
        "description": clean(e.get("description"), 300),
        "source": source,
    }


def in_area(ev):
    a = CFG["area"]
    if ev["region"] and "western cape" not in ev["region"].lower():
        return False
    if ev["lat"] is not None and ev["lon"] is not None:
        dy = (ev["lat"] - a["lat"]) * 111.0
        dx = (ev["lon"] - a["lon"]) * 111.0 * math.cos(math.radians(a["lat"]))
        return math.hypot(dx, dy) <= a["radius_km"]
    if ev["region"] and "western cape" not in ev["region"].lower():
        return False
    place = f"{ev['locality']} {ev['street']}".lower()
    return any(l in place for l in LOCALITIES)


def categorise(title, text):
    """Title first (a wine farm venue shouldn't make a concert a food event), then the description."""
    for t in (title, text):
        hit = next((name for name, pat in CATEGORIES if pat and pat.search(t)), None)
        if hit:
            return hit
    return "other"


def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def collect(today=None):
    today = today or datetime.now(TZ).date()
    monday, friday, sunday = week_of(today)
    week = load_week(monday, friday, sunday)
    win_start = datetime.combine(friday, datetime.min.time(), TZ)
    win_end = datetime.combine(sunday + timedelta(days=1), datetime.min.time(), TZ)
    if today > sunday:
        return week, [], [], []

    known = {e["id"] for e in week["events"]} | {s["id"] for s in week["skipped"]}
    by_title = {re.sub(r"[^a-z0-9]+", "", e["title"].lower()): e for e in week["events"]}
    errors, added, filtered = [], [], {"area": 0, "dates": 0, "long-running": 0, "keyword": 0, "category": 0, "online": 0}

    raw = []
    for src in CFG["listing_sources"]:
        for page in range(1, src["pages"] + 1):
            url = src["url"].format(page=page)
            try:
                found = jsonld_events(fetch(url))
            except Exception as ex:
                errors.append(f"{src['name']} page {page}: {ex}")
                break
            if not found:
                break
            raw += [normalise(e, src["name"]) for e in found]
            time.sleep(1)

    for ev in raw:
        if not ev["title"] or not ev["start"]:
            continue
        end = ev["end"] or ev["start"] + timedelta(hours=3)
        if ev["start"] >= win_end or end <= win_start:
            filtered["dates"] += 1
            continue
        if (win_start - ev["start"]).days > CFG["max_days_running"]:
            filtered["long-running"] += 1
            continue
        if ev["online"]:
            filtered["online"] += 1
            continue
        if not in_area(ev):
            filtered["area"] += 1
            continue
        text = f"{ev['title']} {ev['description']}"  # not the venue: "Workshop 17" isn't a workshop
        if SKIP and SKIP.search(text) and not (KEEP and KEEP.search(text)):
            filtered["keyword"] += 1
            continue
        category = categorise(ev['title'], text)
        if category not in CFG["track_categories"]:
            filtered["category"] += 1
            continue

        start_s = ev["start"].isoformat(timespec="minutes")
        eid = key_for(ev["title"], start_s)
        norm_title = re.sub(r"[^a-z0-9]+", "", ev["title"].lower())
        same = by_title.get(norm_title)
        if same:  # same event, another session or another site
            day_s = start_s[:10]
            if day_s not in [s[:10] for s in same.setdefault("sessions", [same["start"]])] and ev["start"] >= win_start:
                same["sessions"].append(start_s)
            continue
        if eid in known:
            continue
        item = {
            "id": eid, "title": ev["title"], "category": category,
            "start": start_s, "end": ev["end"].isoformat(timespec="minutes") if ev["end"] else None,
            "all_day": ev["start"].hour == 0 and ev["start"].minute == 0,
            "venue": ev["venue"], "area": ev["locality"] or None, "price_from": ev["price_from"],
            "url": ev["url"], "source": ev["source"], "description": ev["description"],
            "added": today.isoformat(),
        }
        week["events"].append(item)
        known.add(eid)
        by_title[norm_title] = item
        added.append(item)

    # Event articles from editorial feeds: listed for the editor, who adds any weekend events they describe.
    articles = []
    seen = set(week["articles_seen"])
    for f in CFG["article_feeds"]:
        try:
            feed = feedparser.parse(fetch(f["url"]))
        except Exception as ex:
            errors.append(f"{f['name']}: {ex}")
            continue
        for e in feed.entries:
            link = e.get("link")
            if link and link not in seen:
                seen.add(link)
                articles.append({"source": f["name"], "title": clean(e.get("title")), "url": link,
                                 "summary": clean(e.get("summary"), 220)})
    week["articles_seen"] = sorted(seen)

    save_week(week, monday)
    write_digest(today, week, added, articles, filtered, errors, len(raw))
    return week, added, articles, errors


def write_digest(today, week, added, articles, filtered, errors, n_raw):
    L = [f"# This weekend: new on {today} (weekend {week['weekend_start']} to {week['weekend_end']})", "",
         f"Scanned {n_raw} listings. Filtered out: " + ", ".join(f"{k} {v}" for k, v in filtered.items() if v) + ".",
         f"The list now has {len(week['events'])} events ({len(added)} added today).", "",
         "## Added today (already on the list: skip any that are wrong or not worth it)",
         "Skip with: python scripts/weekend.py skip <id> \"reason\"", ""]
    for e in added:
        price = f" · from R{e['price_from']:.0f}" if e["price_from"] else ""
        L.append(f"- [{e['id']}] {e['start'][:16].replace('T', ' ')} · {e['category']} · {e['title']} · "
                 f"{e['venue']}, {e['area'] or '?'}{price} · {e['url']}")
        if e["description"]:
            L.append(f"    {e['description']}")
    L += ["", "## Event articles not seen before (add any events on this weekend to the list by hand)", ""]
    L += [f"- [{a['source']}] {a['title']} · {a['url']}\n    {a['summary']}" for a in articles]
    if errors:
        L += ["", "## Errors"] + [f"- {x}" for x in errors]
    out = ROOT / "data" / "raw"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{today}-weekend.md").write_text("\n".join(L) + "\n", encoding="utf-8")


def skip(eid, reason):
    today = datetime.now(TZ).date()
    monday, friday, sunday = week_of(today)
    week = load_week(monday, friday, sunday)
    hit = next((e for e in week["events"] if e["id"] == eid), None)
    if not hit:
        sys.exit(f"No event {eid} on this week's list")
    week["events"].remove(hit)
    week["skipped"].append({"id": eid, "title": hit["title"], "reason": reason})
    save_week(week, monday)
    print(f"Skipped {hit['title']}")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "skip":
        skip(sys.argv[2], " ".join(sys.argv[3:]) or "editor")
    else:
        week, added, articles, errors = collect()
        print(f"weekend: {len(week['events'])} events on the list, {len(added)} new, "
              f"{len(articles)} new articles, {len(errors)} errors")
        for x in errors:
            print("  !", x, file=sys.stderr)
