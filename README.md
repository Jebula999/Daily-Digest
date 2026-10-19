# Cape Town Daily Brief

A local news site for Cape Town that is researched, written and published by AI every morning.
Inspired by the [Tucson Daily Brief](https://tucsondailybrief.com/).

## How it works

```
scheduled Claude Code task (daily, on this PC)
  ├─ scripts/collect.py   → pulls news feeds, City council meetings/agendas, Eskom, weather
  ├─ reads EDITOR.md       → verifies sources, writes content/briefs + content/stories
  ├─ scripts/geocode.py    → looks up tagged places on OpenStreetMap for the map
  ├─ scripts/build.py      → checks the content builds
  └─ git push              → GitHub Actions builds _site/ and deploys to GitHub Pages
```

| Path | What it is |
|---|---|
| `config/city.json` | City settings: name, vetted outlet feeds, blocked domains, clickbait filters, keywords, coordinates |
| `scripts/collect.py` | Collects raw sources into `data/` (git-ignored) and `content/daily/` |
| `scripts/geocode.py` | Geocodes places tagged in briefs (`data-place`) and stories (`places:`) via Nominatim |
| `scripts/build.py` | Renders `content/` with `templates/` into `_site/`, including the `/map/` page |
| `EDITOR.md` | The AI editor's handbook: selection, verification and style rules |
| `content/briefs/` | One markdown file per daily brief |
| `content/stories/` | Council desk stories: `watch`, `decided`, `around`, `indepth` |
| `content/places.json` | Cached coordinates for every tagged place (edit by hand to fix a bad match) |
| `content/daily/` | Weather, load-shedding and meeting calendar (written by code, not AI) |

## Sources

Only outlets with their own newsroom, plus official records. No Google News or other aggregators. Outlets whose robots.txt blocks AI agents are excluded (News24, EWN) or used as headlines only (IOL). See `EDITOR.md` section 2.

## Run it locally

```bash
pip install -r requirements.txt
python scripts/collect.py
python scripts/geocode.py
python scripts/build.py
python -m http.server 8765 --directory _site
```

## Another city

Copy `config/city.json`, then change the name, coordinates, outlet feeds and keywords.
The council collector (`collect_council`) is specific to the City of Cape Town's Council Hub API.
Another municipality needs its own version of that function, or can skip it.
