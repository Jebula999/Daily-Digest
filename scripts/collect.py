"""Collect today's raw source material for the daily brief.

Writes:
  data/raw/<date>.json     everything collected, machine-readable
  data/raw/<date>.md       a compact digest for the AI editor to read
  data/agendas/<id>.txt    extracted text of upcoming council agenda documents

Usage: python scripts/collect.py [--date YYYY-MM-DD]
"""
import argparse
import base64
import html
import io
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config" / "city.json").read_text(encoding="utf-8"))
TZ = ZoneInfo(CONFIG["timezone"])
UA = {"User-Agent": "Mozilla/5.0 (compatible; CapeTownDailyBrief/1.0; +https://github.com)"}
LOOKBACK_HOURS = 36
PROCEDURAL = re.compile(r"COVER PAGE|NOTICE OF (A )?MEETING|OPENING|LEAVE OF ABSENCE|APOLOGIES|DECLARATION OF INTEREST|"
                        r"CONFIRMATION OF (THE )?MINUTES|MOMENT OF SILENCE|GENERAL DISCUSSION", re.I)


def fetch(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_json(url, headers=None):
    return json.loads(fetch(url, headers))


def clean(text, limit=None):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = " ".join(text.split())
    return text[:limit] + "…" if limit and len(text) > limit else text


def is_local(text):
    t = text.lower()
    return any(k in t for k in CONFIG["local_keywords"])


def entry_time(e):
    for key in ("published_parsed", "updated_parsed"):
        if e.get(key):
            return datetime(*e[key][:6], tzinfo=timezone.utc)
    return None


def collect_feeds(errors):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    items = []
    feeds = list(CONFIG["feeds"])
    for q in CONFIG["google_news_queries"]:
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": q + " when:2d", "hl": "en-ZA", "gl": "ZA", "ceid": "ZA:en"})
        feeds.append({"name": f"Google News: {q}", "url": url, "local_filter": False})
    for f in feeds:
        try:
            parsed = feedparser.parse(fetch(f["url"]))
        except Exception as e:
            errors.append(f"feed {f['name']}: {e}")
            continue
        for e in parsed.entries:
            ts = entry_time(e)
            if ts and ts < cutoff:
                continue
            title = clean(e.get("title"))
            summary = clean(e.get("summary"), 240)
            if f.get("local_filter") and not is_local(title + " " + summary):
                continue
            source = f["name"]
            if source.startswith("Google News") and e.get("source"):
                source = e.source.get("title", source)
            items.append({
                "title": title,
                "summary": "" if source != f["name"] and summary.startswith(title[:30]) else summary,
                "url": e.get("link"),
                "source": source,
                "via": f["name"],
                "published": ts.astimezone(TZ).isoformat() if ts else None,
            })
    # De-duplicate by normalised title
    seen, unique = set(), []
    for it in sorted(items, key=lambda x: x["published"] or "", reverse=True):
        key = re.sub(r"[^a-z0-9]", "", it["title"].lower().split(" - ")[0])[:60]
        if key and key not in seen:
            seen.add(key)
            unique.append(it)
    return unique


def collect_city_media(errors):
    try:
        page = fetch(CONFIG["city_media_url"]).decode("utf-8", "replace")
    except Exception as e:
        errors.append(f"city media: {e}")
        return []
    out, seen = [], set()
    for m in re.finditer(r'<a[^>]+href="(/media-and-news/[^"]+)"[^>]*>(.*?)</a>', page, re.S | re.I):
        href, title = m.group(1), clean(m.group(2))
        if title and href not in seen:
            seen.add(href)
            out.append({"title": title, "url": "https://www.capetown.gov.za" + href,
                        "source": "City of Cape Town"})
    return out


def collect_eskom(errors):
    try:
        raw = int(fetch(CONFIG["eskom_status_url"]).decode().strip())
    except Exception as e:
        errors.append(f"eskom: {e}")
        return None
    stage = max(raw - 1, 0)
    return {"stage": stage, "label": "No load-shedding" if stage == 0 else f"Stage {stage} load-shedding",
            "source": "Eskom", "url": "https://loadshedding.eskom.co.za/"}


def collect_weather(errors):
    params = {
        "latitude": CONFIG["lat"], "longitude": CONFIG["lon"], "timezone": CONFIG["timezone"],
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
                 "precipitation_probability_max,wind_speed_10m_max,wind_gusts_10m_max,wind_direction_10m_dominant,uv_index_max",
        "forecast_days": 3, "wind_speed_unit": "kmh",
    }
    try:
        d = fetch_json("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params))["daily"]
    except Exception as e:
        errors.append(f"weather: {e}")
        return []
    keys = [k for k in d if k != "time"]
    return [{"date": t, **{k: d[k][i] for k in keys}} for i, t in enumerate(d["time"])]


class CouncilHub:
    def __init__(self):
        cfg = fetch_json(CONFIG["council_hub"]["settings_url"])
        self.base = cfg["apiBaseUrl"]
        self.headers = {"X-Api-Key": cfg["CouncillorsApiKey"]}

    def get(self, path):
        return fetch_json(self.base + path, self.headers)


def collect_council(today, errors):
    """Meetings from 7 days ago to 14 days ahead, with agenda items where published."""
    try:
        hub = CouncilHub()
    except Exception as e:
        errors.append(f"council hub settings: {e}")
        return []
    start, end = today - timedelta(days=7), today + timedelta(days=14)
    months = sorted({(start.year, start.month), (today.year, today.month), (end.year, end.month)})
    meetings = []
    for y, m in months:
        try:
            meetings += hub.get(f"Meetings/date?year={y}&month={m}&day=&publishedToWWW=true")["data"]
        except Exception as e:
            errors.append(f"council meetings {y}-{m}: {e}")
    agenda_dir = ROOT / "data" / "agendas"
    agenda_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for mt in meetings:
        d = date.fromisoformat(mt["meetingDate"][:10])
        if not (start <= d <= end):
            continue
        rec = {
            "id": mt["meetingId"], "title": mt["title"], "date": d.isoformat(),
            "start": mt["startTime"][:5], "category": mt["meetingCategory"], "status": mt["status"],
            "venue": (mt.get("meetingVenue") or {}).get("title"),
            "url": CONFIG["council_hub"]["app_url"] + "councilandCommitteemeetings",
            "items": [], "documents": [],
        }
        if mt["status"] != "Cancelled":
            try:
                enrich_meeting(hub, rec, d, today, agenda_dir)
            except Exception as e:
                errors.append(f"meeting {rec['id']} detail: {e}")
        out.append(rec)
    return sorted(out, key=lambda r: (r["date"], r["start"]))


def enrich_meeting(hub, rec, d, today, agenda_dir):
    cat = rec["category"]
    if cat == "MPTMeeting":
        det = hub.get(f"Serge/MPTMeetingItems/CouncillorsOnlineMeetingId/{rec['id']}?publishedToWWW=true")
        for it in det.get("mptMeetingItems") or []:
            rec["items"].append({"number": it.get("agendaItemNumber"), "subject": clean(it.get("subjectMatter")),
                                 "resolution": clean(it.get("resolutionDetails"), 600)})
    elif cat == "SubcouncilMeeting":
        det = hub.get(f"SubcouncilMeetings/id/{rec['id']}?publishedToWWW=true")
        rec["detail"] = {k: v for k, v in det.items() if isinstance(v, (str, int)) and v}
    elif cat == "ECSMeeting":
        det = hub.get(f"EcsCommitteeMeetings/id/{rec['id']}?showToWWW=true")
        docs = det.get("ecsCommitteeMeetingDocument") or []
        rec["documents"] = [{"id": x["ecscommitteeMeetingDocId"], "name": x["displayName"],
                             "type": x["documentType"]} for x in docs]
        # Pull the text of agendas/reports for meetings in the next week (cover pages skipped).
        if today - timedelta(days=7) <= d <= today + timedelta(days=7):
            wanted = [x for x in rec["documents"] if not PROCEDURAL.search(x["name"])]
            wanted.sort(key=lambda x: x["type"] not in ("Agenda", "Minutes"))
            wanted = wanted[:10]
            for doc in wanted:
                path = agenda_dir / f"{doc['id']}.txt"
                if not path.exists():
                    path.write_text(pdf_text(hub, doc["id"]), encoding="utf-8")
                doc["text_file"] = str(path.relative_to(ROOT)).replace("\\", "/")


def pdf_text(hub, doc_id, max_chars=40000):
    from pypdf import PdfReader
    data = hub.get(f"EcsMeetingDocuments/id/{doc_id}")
    reader = PdfReader(io.BytesIO(base64.b64decode(data["uploadedDocument"])))
    text = "\n".join((p.extract_text() or "") for p in reader.pages[:40])
    return text[:max_chars]


BOILERPLATE = [
    (r"\s*IN TERMS OF (SECTIONS? [\d()A-Z, AND]+ OF )?THE (CITY OF CAPE TOWN )?MUNIC[I]?PAL PLANNING BY[- ]?LAW,? ?(2015)?( \(MPBL\))?(,? ?(AS AMENDED|RESPECTIVELY))?", ""),
    (r"CASE ID:.*$", ""),
    (r"in terms of [Ss]ection [\w()]+ of the (City of Cape Town )?Municipal Planning By-?[Ll]aw,? ?(2015)?,?( \(as amended\))?,?", ""),
    (r"REASONS FOR DECISION:.*$", ""),
    (r"\s+", " "),
]


def squeeze(text, limit):
    """Strip legal boilerplate from planning items so the digest stays readable."""
    for pat, rep in BOILERPLATE:
        text = re.sub(pat, rep, text, flags=re.I if pat.startswith("in terms") else 0)
    text = text.strip(" .:")
    return text[:limit] + "…" if len(text) > limit else text


def digest(data):
    """Compact markdown digest for the editor."""
    L = [f"# Source digest for {data['date']} ({CONFIG['name']})", f"Collected {data['collected_at']}", ""]
    if data["eskom"]:
        L += ["## Load-shedding", f"- {data['eskom']['label']} (Eskom national status)", ""]
    if data["weather"]:
        L.append("## Weather forecast (Open-Meteo, city centre)")
        for w in data["weather"]:
            L.append(f"- {w['date']}: {w['temperature_2m_min']}–{w['temperature_2m_max']}°C, rain {w['precipitation_sum']}mm "
                     f"({w['precipitation_probability_max']}%), wind max {w['wind_speed_10m_max']} km/h gusting "
                     f"{w['wind_gusts_10m_max']} km/h from {w['wind_direction_10m_dominant']}°, UV {w['uv_index_max']}, code {w['weather_code']}")
        L.append("")
    L.append("## Council & committee meetings (past 7 days → next 14 days)")
    for m in data["council"]:
        L.append(f"- [{m['id']}] {m['date']} {m['start']} · {m['category']} · {m['status']} · {m['title']} · {m['venue']}")
        for it in m["items"]:
            if it["subject"] and it["number"] and not re.match(
                    r"(OPENING|APOLOGIES|DECLARATION|CONFIRMATION|COMMUNICATIONS|NOTING|MATTERS RECEIVING)", it["subject"]):
                res = f" → {squeeze(it['resolution'], 260)}" if it["resolution"] else ""
                L.append(f"    - {it['number'].replace(' ', '')}: {squeeze(it['subject'], 220)}{res}")
        for doc in m["documents"]:
            if PROCEDURAL.search(doc["name"]):
                continue
            extra = f" (text: {doc['text_file']})" if doc.get("text_file") else ""
            L.append(f"    - doc {doc['type']}: {doc['name']}{extra}")
    L += ["", "## City of Cape Town media releases (latest on the City site)"]
    L += [f"- {x['title']} — {x['url']}" for x in data["city_media"]]
    L += ["", f"## News items (last {LOOKBACK_HOURS}h, {len(data['news'])} items)",
          "Google News links are long redirects, so they are shown as #n. "
          "Get them with: python scripts/links.py <date> <n> [<n> ...]", ""]
    for i, n in enumerate(data["news"]):
        when = (n["published"] or "")[5:16].replace("T", " ")
        link = f"#{i}" if "news.google.com" in (n["url"] or "") else n["url"]
        L.append(f"- [{n['source']}] {n['title']} ({when}) {link}")
        if n["summary"]:
            L.append(f"    {n['summary']}")
    if data["errors"]:
        L += ["", "## Collector errors"] + [f"- {e}" for e in data["errors"]]
    return "\n".join(L) + "\n"


COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def write_daily(data):
    """Facts the site shows verbatim (no AI involved): conditions and the meeting calendar.
    Committed to the repo as content/daily/<date>.json."""
    w = data["weather"][0] if data["weather"] else None
    daily = {"date": data["date"], "loadshedding": data["eskom"], "weather": None, "meetings": []}
    if w:
        deg = w["wind_direction_10m_dominant"]
        daily["weather"] = {
            "min": round(w["temperature_2m_min"]), "max": round(w["temperature_2m_max"]),
            "rain_mm": w["precipitation_sum"], "rain_chance": w["precipitation_probability_max"],
            "wind_kmh": round(w["wind_speed_10m_max"]), "gust_kmh": round(w["wind_gusts_10m_max"]),
            "wind_deg": deg, "wind_from": COMPASS[round(deg / 22.5) % 16], "uv": round(w["uv_index_max"]),
            "code": w["weather_code"], "source": "Open-Meteo",
        }
    for m in data["council"]:
        if m["status"] != "Cancelled" and m["date"] >= data["date"]:
            daily["meetings"].append({k: m[k] for k in ("id", "title", "date", "start", "category", "venue", "url")})
    out = ROOT / "content" / "daily"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{data['date']}.json").write_text(json.dumps(daily, indent=1, ensure_ascii=False), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(TZ).date().isoformat())
    args = ap.parse_args()
    today = date.fromisoformat(args.date)
    errors = []
    data = {
        "date": today.isoformat(),
        "collected_at": datetime.now(TZ).isoformat(timespec="minutes"),
        "eskom": collect_eskom(errors),
        "weather": collect_weather(errors),
        "council": collect_council(today, errors),
        "city_media": collect_city_media(errors),
        "news": collect_feeds(errors),
        "errors": errors,
    }
    out = ROOT / "data" / "raw"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{today}.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    (out / f"{today}.md").write_text(digest(data), encoding="utf-8")
    write_daily(data)
    print(f"news={len(data['news'])} council={len(data['council'])} city_media={len(data['city_media'])} "
          f"errors={len(errors)} -> data/raw/{today}.md")
    for e in errors:
        print("  !", e, file=sys.stderr)


if __name__ == "__main__":
    main()
