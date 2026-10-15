"""Print the real article URL of news items listed as #n in a digest.

Google News RSS links only redirect via JavaScript, so this resolves them the way the
Google News page does (signature + batchexecute) before printing.

Usage: python scripts/links.py 2026-10-15 3 17 42
"""
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}


def resolve_google(url):
    m = re.search(r"/articles/([^?/]+)", url)
    if not m:
        return url
    gid = m.group(1)
    page = urllib.request.urlopen(urllib.request.Request(f"https://news.google.com/articles/{gid}", headers=UA),
                                  timeout=20).read().decode("utf-8", "replace")
    sig = re.search(r'data-n-a-sg="([^"]+)"', page)
    ts = re.search(r'data-n-a-ts="([^"]+)"', page)
    if not (sig and ts):
        return url
    inner = ["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
                            "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0], gid, int(ts.group(1)), sig.group(1)]
    payload = [[["Fbv4je", json.dumps(inner), None, "generic"]]]
    body = urllib.parse.urlencode({"f.req": json.dumps(payload)}).encode()
    req = urllib.request.Request("https://news.google.com/_/DotsSplashUi/data/batchexecute", data=body,
                                 headers={**UA, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    text = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
    chunk = json.loads(text.split("\n\n", 1)[1])[0][2]
    return json.loads(chunk)[1]


def main():
    day, *nums = sys.argv[1:]
    news = json.loads((ROOT / "data" / "raw" / f"{day}.json").read_text(encoding="utf-8"))["news"]
    for n in nums:
        n = n.lstrip("#")
        item = news[int(n)]
        url = item["url"]
        if "news.google.com" in url:
            try:
                url = resolve_google(url)
            except Exception as e:
                url += f"  (could not resolve: {e})"
        print(f"#{n} [{item['source']}] {item['title']}\n    {url}")


if __name__ == "__main__":
    main()
