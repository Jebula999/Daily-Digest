"""Look up coordinates for every place tagged in content/, using OpenStreetMap (Nominatim).

Places are tagged in briefs as `{: #id data-place="Fisher Avenue, Epping" }` (several separated by ";")
and in stories as a `places:` list. Results are cached in content/places.json, so each place is looked
up once and the site build never calls the network. Edit places.json by hand to fix a bad match.

Usage: python scripts/geocode.py
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "content" / "places.json"
CONFIG = json.loads((ROOT / "config" / "city.json").read_text(encoding="utf-8-sig"))
UA = {"User-Agent": "CapeTownDailyBrief/1.0 (+https://github.com/Jebula999/Daily-Digest)"}
# Cape Town metro, roughly: Atlantis to Gordon's Bay
VIEWBOX = "18.30,-33.45,19.00,-34.40"


def tagged_places():
    names = set()
    for p in (ROOT / "content" / "briefs").glob("*.md"):
        for m in re.finditer(r'data-place="([^"]+)"', p.read_text(encoding="utf-8-sig")):
            names.update(n.strip() for n in m.group(1).split(";") if n.strip())
    for p in (ROOT / "content" / "stories").glob("*.md"):
        m = re.match(r"^---\s*\n(.*?)\n---", p.read_text(encoding="utf-8-sig"), re.S)
        meta = yaml.safe_load(m.group(1)) if m else {}
        names.update(n.strip() for n in (meta or {}).get("places", []) or [] if n.strip())
    return names


AREA_TYPES = {"suburb", "neighbourhood", "quarter", "city_district", "town", "village", "hamlet", "city", "island"}
STREET_WORDS = re.compile(r"\b(road|rd|street|st|avenue|ave|drive|lane|close|crescent|way|boulevard|court|beach|"
                          r"stadium|hall|school|hospital|station|park|reserve|parliament|centre|center|mall)\b", re.I)


def search(q, bounded):
    params = {"q": q, "format": "jsonv2", "limit": 8, "countrycodes": "za"}
    if bounded:
        params.update({"viewbox": VIEWBOX, "bounded": 1})
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        hits = json.loads(r.read())
    time.sleep(1.1)  # Nominatim usage policy: at most one request per second
    return hits


def pick(name, hits):
    """For a bare area name ("Strandfontein"), prefer a suburb over a road or school that shares the name."""
    if not hits:
        return None
    if not STREET_WORDS.search(name):
        areas = [h for h in hits if h.get("addresstype") in AREA_TYPES]
        if areas:
            return areas[0]
    return hits[0]


def lookup(name):
    parts = [p.strip() for p in name.split(",")]
    queries = [(f"{name}, {CONFIG['name']}", True)]
    if len(parts) > 1 and STREET_WORDS.search(parts[0]):  # "De La Rey Road, Parow" → also try the street alone
        queries.append((f"{parts[0]}, {CONFIG['name']}", True))
    queries.append((f"{name}, {CONFIG['region']}, South Africa", False))
    for q, bounded in queries:
        h = pick(name, search(q, bounded))
        if h:
            return {"lat": round(float(h["lat"]), 5), "lon": round(float(h["lon"]), 5), "match": h["display_name"]}
    return None


def main():
    cache = json.loads(CACHE.read_text(encoding="utf-8-sig")) if CACHE.exists() else {}
    missing = sorted(n for n in tagged_places() if n not in cache)
    for name in missing:
        try:
            hit = lookup(name)
        except Exception as e:
            print(f"  ! {name}: {e}", file=sys.stderr)
            continue
        cache[name] = hit
        print(f"  {'✓' if hit else '✗'} {name}" + (f" → {hit['match'][:80]}" if hit else " (not found: make it more specific)"))
    CACHE.write_text(json.dumps(dict(sorted(cache.items())), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(missing)} looked up, {len(cache)} places cached")


if __name__ == "__main__":
    main()
