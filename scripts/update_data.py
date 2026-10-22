#!/usr/bin/env python3
"""Builds data.json for the morning dashboard: a news briefing and today's sports.
Standard library only, so the GitHub Action needs no installs."""
import html
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Los_Angeles")
UA = "Mozilla/5.0 (compatible; morning-dashboard/1.0; personal use)"

# ============================ EDIT ME ============================
# Section key -> (title shown on the page, [(source name, RSS feed URL), ...])
# A feed that fails is skipped, so a wrong or dead URL won't break the run.
FEEDS = {
    "general": ("Top news", [
        ("NPR", "https://feeds.npr.org/1001/rss.xml"),
        ("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"),
        ("NYT", "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"),
    ]),
    "cre": ("Commercial real estate", [
        ("The Real Deal", "https://therealdeal.com/feed/"),
        ("GlobeSt", "https://www.globest.com/feed/"),
        ("Commercial Observer", "https://commercialobserver.com/feed/"),
    ]),
    "sf": ("San Francisco", [
        ("Mission Local", "https://missionlocal.org/feed/"),
        ("SF Standard", "https://sfstandard.com/feed/"),
    ]),
    "hawaii": ("Hawaii", [
        ("Civil Beat", "https://www.civilbeat.org/feed/"),
        ("Star-Advertiser", "https://www.staradvertiser.com/feed/"),
    ]),
}
ITEMS_PER_FEED = 6          # most recent items kept from each feed
MAX_AGE_HOURS = 40          # ignore anything older than this
PICKS_PER_SECTION = 4       # stories shown per section

SECTION_KEYWORDS = {
    "general": [
        "commercial real estate", "real estate", "san francisco", "sf", "bay area",
        "hawaii", "kauai", "housing", "economy", "california", "business"
    ],
    "cre": [
        "commercial real estate", "office", "industrial", "retail", "multifamily",
        "apartment", "development", "lease", "rent", "property", "real estate",
        "office market", "capital markets", "buildings"
    ],
    "sf": [
        "san francisco", "sf", "bay area", "oakland", "city hall", "california",
        "housing", "transit", "bart", "muni", "police", "school", "mayor"
    ],
    "hawaii": [
        "hawaii", "kauai", "oahu", "maui", "honolulu", "big island", "state",
        "tourism", "housing", "beach", "weather", "wildfire", "coast", "water",
        "school", "economy", "surf"
    ],
}
SECTION_EXCLUSIONS = {
    "hawaii": [
        "scary movie", "movie", "movies", "film", "vance", "ramaswamy", "ramaswamy",
        "trump", "election", "president", "meme", "podcast", "renovation"
    ],
    "sf": ["movie", "film", "entertainment", "celebrity"],
    "general": ["word search"],
}

TEAM = "Warriors"                                           # always shown when they play
NBA_NATIONAL = ["ESPN", "ABC", "NBC", "Peacock", "Prime Video"]   # "primetime" NBA = on one of these
NFL_PRIMETIME_START_HOUR_PT = 17                            # NFL games starting at or after 5pm PT
# =================================================================


def fetch(url, headers=None, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def strip(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_date(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
    except Exception:
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def matches_topic(title, desc, key):
    text = f"{title or ''} {desc or ''}".lower()
    keywords = SECTION_KEYWORDS.get(key, [])
    if not keywords:
        return True
    if not any(kw in text for kw in keywords):
        return False
    ex = SECTION_EXCLUSIONS.get(key, [])
    if any(term in text for term in ex):
        return False
    return True


def parse_feed(xml_bytes, source):
    root = ET.fromstring(xml_bytes)
    items = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        f = {"source": source, "title": "", "link": "", "desc": "", "when": None}
        for ch in el:
            n = local(ch.tag)
            if n == "title":
                f["title"] = strip(ch.text)
            elif n == "link":
                href = ch.get("href")
                if href:
                    if ch.get("rel") in (None, "alternate"):
                        f["link"] = href.strip()
                elif ch.text:
                    f["link"] = ch.text.strip()
            elif n in ("description", "summary") and not f["desc"]:
                f["desc"] = strip(ch.text)
            elif n in ("pubDate", "published", "updated", "date") and f["when"] is None:
                f["when"] = parse_date(ch.text)
        if f["title"] and f["link"].startswith("http"):
            items.append(f)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    items = [i for i in items if i["when"] is None or i["when"] >= cutoff]
    items.sort(key=lambda i: i["when"] or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
    return items[:ITEMS_PER_FEED]


def collect_news():
    sections = []
    for key, (title, feeds) in FEEDS.items():
        pool, seen = [], set()
        for source, url in feeds:
            try:
                for it in parse_feed(fetch(url), source):
                    if not matches_topic(it["title"], it["desc"], key):
                        continue
                    k = it["title"].lower()
                    if k not in seen:
                        seen.add(k)
                        pool.append(it)
            except Exception as e:
                print(f"[feed failed] {source}: {e}", file=sys.stderr)
        pool.sort(key=lambda i: i["when"] or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
        pool = pool[:14]
        for i, it in enumerate(pool):
            it["id"] = f"{key}-{i}"
        sections.append({"key": key, "title": title, "pool": pool})
    return sections


PROMPT_HEAD = """You write a short morning news briefing for one reader: a commercial real estate valuation analyst based in San Francisco with family ties to Kauai, Hawaii.

Below are today's headlines grouped by section. Each line is: id | source | headline | snippet.
Use ONLY these items. Do not add facts that are not in them. Do not speculate. Prefer stories that directly match each section's topic; skip generic or off-topic items.

Return JSON in exactly this shape:
{"overview": "...", "sections": {"general": [{"id": "general-0", "summary": "..."}], "cre": [], "sf": [], "hawaii": []}}

Rules:
- "overview": 4 to 6 plain sentences, no bullets or markdown, leading with the most important stories across all sections. Mention commercial real estate, San Francisco, or Hawaii only if the item is actually relevant.
- For each section choose the 3 or 4 most important items and give each a one-sentence "summary" of at most 25 words, restating what the item says.
- Use only ids that appear below. If a section has no items, return an empty list for it.

ITEMS
"""


def build_prompt(sections):
    lines = []
    for s in sections:
        lines.append(f"## {s['key']} ({s['title']})")
        for it in s["pool"]:
            lines.append(f"{it['id']} | {it['source']} | {it['title']} | {it['desc'][:240]}")
    return PROMPT_HEAD + "\n".join(lines)


def gemini(prompt):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        print("[ai skipped] GEMINI_API_KEY is not set", file=sys.stderr)
        return None
    models = [os.environ.get("GEMINI_MODEL", "").strip(), "gemini-2.5-flash", "gemini-2.5-flash-lite"]
    for model in dict.fromkeys(m for m in models if m):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3},
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "x-goog-api-key": key},
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                resp = json.load(r)
            parts = resp["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
            print(f"[ai ok] model {model}")
            return json.loads(text)
        except Exception as e:
            print(f"[ai failed] {model}: {e}", file=sys.stderr)
    return None


def round_robin(pool, n):
    """Pick n items spread across sources so one outlet doesn't take every slot."""
    by_source = {}
    for it in pool:
        by_source.setdefault(it["source"], []).append(it)
    picks = []
    while len(picks) < n and any(by_source.values()):
        for src in list(by_source):
            if by_source[src] and len(picks) < n:
                picks.append(by_source[src].pop(0))
    return picks


def compose_sections(sections, ai):
    out = []
    ai_sections = (ai or {}).get("sections") if isinstance((ai or {}).get("sections"), dict) else {}
    for s in sections:
        by_id = {it["id"]: it for it in s["pool"]}
        items = []
        for pick in ai_sections.get(s["key"], []) or []:
            it = by_id.get(pick.get("id")) if isinstance(pick, dict) else None
            if it and len(items) < PICKS_PER_SECTION:
                items.append({"title": it["title"], "summary": strip(pick.get("summary", "")),
                              "url": it["link"], "source": it["source"]})
        if not items:
            items = [{"title": it["title"], "summary": "", "url": it["link"], "source": it["source"]}
                     for it in round_robin(s["pool"], PICKS_PER_SECTION)]
        out.append({"key": s["key"], "title": s["title"], "items": items})
    return out


# ------------------------------ sports ------------------------------

def parse_utc(s):
    for fmt in ("%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    raise ValueError(f"bad date {s}")


def parse_events(data):
    games = []
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        teams = {c.get("homeAway"): (c.get("team") or {}) for c in comp.get("competitors", [])}
        away, home = teams.get("away", {}), teams.get("home", {})
        name = lambda t: t.get("shortDisplayName") or t.get("displayName") or t.get("abbreviation") or "?"
        start = parse_utc(ev["date"]).astimezone(TZ)
        nets = []
        for b in comp.get("broadcasts") or []:
            nets += b.get("names") or []
        for g in comp.get("geoBroadcasts") or []:
            m = (g.get("media") or {}).get("shortName")
            if m and ((g.get("market") or {}).get("type") or "").lower() == "national":
                nets.append(m)
        games.append({
            "matchup": f"{name(away)} at {name(home)}",
            "time": start.strftime("%-I:%M %p"),
            "network": ", ".join(dict.fromkeys(nets)),
            "state": ((ev.get("status") or {}).get("type") or {}).get("state", "pre"),
            "start_hour": start.hour,
        })
    games.sort(key=lambda g: g["start_hour"])
    return games


def pga_events():
    candidates = [
        "https://site.api.espn.com/apis/site/v2/sports/golf/pga/scoreboard",
        "https://site.api.espn.com/apis/site/v2/sports/golf/pga/scoreboard?dates=" + datetime.now(TZ).strftime("%Y%m%d"),
        "https://site.api.espn.com/apis/site/v2/sports/golf/pga/schedule",
    ]
    seen = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        try:
            payload = json.loads(fetch(url))
            items = payload.get("events") or payload.get("items") or []
            out = []
            for ev in items[:5]:
                start = None
                for key in ("date", "startDate", "endDate"):
                    if key in ev:
                        start = parse_date(ev[key])
                        if start:
                            break
                name = ev.get("name") or ev.get("shortName") or ev.get("summary") or (ev.get("tournament") or {}).get("name") or "PGA event"
                if not name:
                    continue
                out.append({
                    "name": name,
                    "time": start.astimezone(TZ).strftime("%-I:%M %p") if start else "TBD",
                    "state": ((ev.get("status") or {}).get("type") or {}).get("state", "pre"),
                })
            if out:
                return out[:3]
        except Exception as e:
            print(f"[pga failed] {e}", file=sys.stderr)
    return []


def sports():
    day = datetime.now(TZ).strftime("%Y%m%d")
    out = {"date": day, "nba": None, "nfl": None, "pga": {"events": []}}
    base = "https://site.api.espn.com/apis/site/v2/sports/{}/{}/scoreboard?dates=" + day
    try:
        games = parse_events(json.loads(fetch(base.format("basketball", "nba"))))
        warriors = [g for g in games if TEAM.lower() in g["matchup"].lower()]
        national = [g for g in games if g not in warriors
                    and any(n.lower() in g["network"].lower() for n in NBA_NATIONAL)]
        out["nba"] = {"count": len(games), "warriors": warriors[:2], "national": national[:2]}
    except Exception as e:
        print(f"[nba failed] {e}", file=sys.stderr)
    try:
        games = parse_events(json.loads(fetch(base.format("football", "nfl"))))
        prime = [g for g in games if g["start_hour"] >= NFL_PRIMETIME_START_HOUR_PT]
        out["nfl"] = {"count": len(games), "primetime": prime[:3], "all": games[:3]}
    except Exception as e:
        print(f"[nfl failed] {e}", file=sys.stderr)
    events = pga_events()
    out["pga"] = {"events": events}
    return out


def main():
    now = datetime.now(TZ)
    try:
        sections = collect_news()
    except Exception as e:
        print(f"[news failed] {e}", file=sys.stderr)
        sections = []
    ai = None
    if any(s["pool"] for s in sections):
        try:
            ai = gemini(build_prompt(sections))
        except Exception as e:
            print(f"[ai failed] {e}", file=sys.stderr)
    overview = strip(ai.get("overview", "")) if isinstance(ai, dict) else ""
    data = {
        "generated": now.isoformat(),
        "ai": bool(overview),
        "overview": overview,
        "sections": compose_sections(sections, ai),
        "sports": sports(),
    }
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    print("wrote data.json")


if __name__ == "__main__":
    main()
