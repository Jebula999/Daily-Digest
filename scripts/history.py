"""'This day in history': Cape Town and South Africa first, topped up with a few world events.

Sources: SA History Online's "This day in history" (sahistory.org.za) for South Africa, Wikipedia's
"On this day" feed (CC BY-SA) for the world. History doesn't change, so each day is fetched once.

Writes content/history/MM-DD.json for today and the next 7 days.

Usage: python scripts/history.py [--refresh]
"""
import html
import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "content" / "history"
TZ = ZoneInfo("Africa/Johannesburg")
UA = {"User-Agent": "CapeTownDailyBrief/1.0 (https://github.com/Jebula999/Daily-Digest)"}
DAYS_AHEAD = 7
WORLD_MAX = 5

SAHO = re.compile(r'<div class="tdih-event-date">\s*(.*?)\s*</div><h4 class="tdih-event-title"><a href="([^"]+)">'
                  r'(.*?)</a></h4><div class="tdih-event-body">\s*(.*?)</div>', re.S)
CAPE = re.compile(r"\b(cape town|table mountain|table bay|robben island|district six|bo-kaap|simon'?s ?town|"
                  r"stellenbosch|paarl|franschhoek|western cape|cape colony|cape peninsula|castle of good hope|"
                  r"langa|gugulethu|nyanga|khayelitsha|crossroads|athlone|mitchells plain|"
                  r"muizenberg|constantia|groote schuur|wynberg|observatory|sea point|woodstock|cape flats|"
                  r"at the cape|the cape)\b", re.I)
SA = re.compile(r"\b(south africa|south african|apartheid|soweto|johannesburg|pretoria|durban|natal|transvaal|"
                r"zulu|xhosa|mandela|anc|boer|afrikaner|cape)\b", re.I)


def fetch(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def clean(s, limit=None):
    s = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s or "")).split())
    if limit and len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return s


def year_of(text):
    m = re.search(r"\b(\d{3,4})\b", text)
    return int(m.group(1)) if m else None


def saho(mm, dd):
    page = fetch(f"https://sahistory.org.za/this-day-in-history?date={mm:02d}-{dd:02d}")
    items = []
    for when, href, title, body in SAHO.findall(page):
        title, body = clean(title), clean(body, 420)
        if not title:
            continue
        items.append({
            "year": year_of(when), "title": title, "text": body,
            "url": href if href.startswith("http") else "https://sahistory.org.za" + href,
            "cape_town": bool(CAPE.search(f"{title} {body}")),
        })
    # Cape Town first, then the rest of SA; oldest first within each.
    items.sort(key=lambda x: (not x["cape_town"], x["year"] or 0))
    return items


def world(mm, dd, sa_years):
    data = json.loads(fetch(f"https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/selected/{mm:02d}/{dd:02d}"))
    items = []
    for e in data.get("selected", []):
        text = re.sub(r"\s*\((pictured|shown|depicted)[^)]*\)", "", clean(e.get("text")))
        if not text or e.get("year") in sa_years:
            continue
        page = (e.get("pages") or [{}])[0]
        url = (page.get("content_urls") or {}).get("desktop", {}).get("page")
        items.append({"year": e.get("year"), "text": text, "url": url, "sa": bool(SA.search(text))})
    # Any South African items first, then an even spread across the centuries; at most WORLD_MAX.
    items.sort(key=lambda x: x["year"] or 0)
    picked = [x for x in items if x["sa"]][:WORLD_MAX]
    rest = [x for x in items if not x["sa"]]
    room = WORLD_MAX - len(picked)
    if room > 0 and rest:
        step = max(len(rest) / room, 1)
        picked += [rest[int(i * step)] for i in range(min(room, len(rest)))]
    return sorted(picked, key=lambda x: x["year"] or 0)


def build_day(day, refresh=False):
    p = OUT / f"{day:%m-%d}.json"
    if p.exists() and not refresh:
        return "kept"
    errors, sa, wd = [], [], []
    try:
        sa = saho(day.month, day.day)
    except Exception as ex:
        errors.append(f"SAHO: {ex}")
    try:
        wd = world(day.month, day.day, {x["year"] for x in sa})
    except Exception as ex:
        errors.append(f"Wikipedia: {ex}")
    if not sa and not wd:
        return "failed: " + "; ".join(errors)
    OUT.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"day": f"{day:%m-%d}", "south_africa": sa, "world": wd}, indent=1,
                            ensure_ascii=False) + "\n", encoding="utf-8")
    return f"{len(sa)} SA ({sum(x['cape_town'] for x in sa)} Cape Town), {len(wd)} world" + \
        (f" [{'; '.join(errors)}]" if errors else "")


if __name__ == "__main__":
    refresh = "--refresh" in sys.argv
    today = datetime.now(TZ).date()
    for i in range(DAYS_AHEAD + 1):
        d = today + timedelta(days=i)
        print(f"history {d:%m-%d}: {build_day(d, refresh)}")
