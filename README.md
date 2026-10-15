# Cape Town Daily Brief

A local news site for Cape Town that is researched, written and published by AI every morning.
Inspired by the [Tucson Daily Brief](https://tucsondailybrief.com/).

## How it works

```
scheduled Claude Code task (daily, on this PC)
  ├─ scripts/collect.py   → pulls news feeds, City council meetings/agendas, Eskom, weather
  ├─ reads EDITOR.md       → verifies sources, writes content/briefs + content/stories
  ├─ scripts/build.py      → checks the content builds
  └─ git push              → GitHub Actions builds _site/ and deploys to GitHub Pages
```

| Path | What it is |
|---|---|
| `config/city.json` | City settings: name, feeds, Google News queries, keywords, coordinates |
| `scripts/collect.py` | Collects raw sources into `data/` (git-ignored) and `content/daily/` |
| `scripts/links.py` | Resolves Google News `#n` items in the digest to real article URLs |
| `scripts/build.py` | Renders `content/` with `templates/` into `_site/` |
| `EDITOR.md` | The AI editor's handbook: selection, verification and style rules |
| `content/briefs/` | One markdown file per daily brief |
| `content/stories/` | Council desk stories: `watch`, `decided`, `around`, `indepth` |
| `content/daily/` | Weather, load-shedding and meeting calendar (written by code, not AI) |

## Run it locally

```bash
pip install -r requirements.txt
python scripts/collect.py
python scripts/build.py
python -m http.server 8765 --directory _site
```

## Another city

Copy `config/city.json`, then change the name, coordinates, feeds, keywords and Google News queries.
The council collector (`collect_council`) is specific to the City of Cape Town's Council Hub API.
Another municipality needs its own version of that function, or can skip it.
