#!/usr/bin/env python3
"""Builds data.json for the morning dashboard: news briefing, sports, Wikipedia, fantasy injuries.
Standard library only, so the GitHub Action needs no installs."""
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Los_Angeles")
UA = "morning-dashboard/1.0 (personal GitHub Pages project; Mozilla/5.0 compatible)"

# ============================ EDIT ME: NEWS ============================
HAWAII_TERMS = [r"\bhawai", r"\bhonolulu", r"\boahu", r"\bmaui", r"\bkauai", r"\blanai", r"\bmolokai",
                r"\bbig island", r"\bhilo\b", r"\bkona\b", r"\bwaikiki", r"\blihue", r"\blahaina",
                r"\bpearl harbor"]

# Section key -> (title on the page, [(source, RSS url) or (source, RSS url, required_terms)])
# required_terms: a story from that source is kept only if it matches one of these patterns.
# That is how national wire stories get dropped from a local feed.
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
        ("Star-Advertiser", "https://www.staradvertiser.com/feed/", HAWAII_TERMS),
    ]),
}

# What you care about in each section. The AI is told to pick only stories that fit,
# and to return fewer (or none) rather than pad the list. Rewrite these in your own words.
INTERESTS = {
    "general": "Major national and world news with broad consequences: the economy, markets and interest rates, major policy, elections, wars, big court rulings, large disasters. Skip celebrity, lifestyle, sports recaps, and puzzles.",
    "cre": "Commercial real estate valuation and markets: cap rates, interest rates and lending, large sales and financings, trends in office, multifamily and industrial (especially San Francisco, the Bay Area and California), distress and defaults, zoning and development policy, appraisal and valuation.",
    "sf": "San Francisco and Bay Area news that matters to a resident and a real estate professional: city government and policy, housing and development, downtown and office recovery, transit, public safety trends, major business news. Skip minor crime blotter, event listings, and lifestyle.",
    "hawaii": "Hawaii news of real consequence: state and county government and policy, housing and cost of living, the tourism economy, land and development, Maui recovery, natural hazards, major incidents. Skip puzzles, lifestyle, event listings, and national stories with no Hawaii angle.",
}

# Any story whose title or link matches one of these is dropped before anything else happens.
EXCLUDE_PATTERNS = [r"word search", r"crossword", r"sudoku", r"horoscope", r"obituar", r"\brecipe",
                    r"\bquiz\b", r"\bpuzzle", r"letters? to the editor", r"photos?:", r"\bwatch:", r"/puzzles?/"]

ITEMS_PER_FEED = 8          # most recent items kept from each feed
MAX_AGE_HOURS = 40          # ignore anything older than this
MAX_PICKS = 4               # most stories shown per section
# ======================================================================

# ============================ EDIT ME: SPORTS ==========================
TEAM = "Warriors"                                          # always shown when they play
NBA_NATIONAL = ["ABC", "NBC", "ESPN", "Peacock", "Prime Video"]   # "primetime" NBA = on one of these
NBA_NATIONAL_MAX = 2                                       # most non-Warriors NBA games shown
FAVORITE_NBA_TEAMS = []                                    # e.g. ["Lakers"] to bump their games up
NFL_PRIMETIME_START_HOUR_PT = 17                           # NFL games starting at or after 5pm PT
NFL_MAX = 3
# ======================================================================

AI = {"state": "no_key", "detail": ""}


def fetch(url, headers=None, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def jget(url, headers=None, timeout=30):
    return json.loads(fetch(url, headers, timeout))


def strip(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def local(tag):
    return tag.rsplit("}", 1)[-1]


def matches(patterns, text):
    return any(re.search(p, text, re.I) for p in patterns)


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


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def parse_feed(xml_bytes, source, limit=ITEMS_PER_FEED, max_age=MAX_AGE_HOURS, require=None):
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
        if not (f["title"] and f["link"].startswith("http")):
            continue
        text = f["title"] + " " + f["link"]
        if matches(EXCLUDE_PATTERNS, text):
            continue
        if require and not matches(require, text + " " + f["desc"]):
            continue
        items.append(f)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age)
    items = [i for i in items if i["when"] is None or i["when"] >= cutoff]
    items.sort(key=lambda i: i["when"] or EPOCH, reverse=True)
    return items[:limit]


# ------------------------------ news ------------------------------

def collect_news():
    sections = []
    for key, (title, feeds) in FEEDS.items():
        pool, seen = [], set()
        for src in feeds:
            source, url = src[0], src[1]
            require = src[2] if len(src) > 2 else None
            try:
                for it in parse_feed(fetch(url), source, require=require):
                    k = it["title"].lower()
                    if k not in seen:
                        seen.add(k)
                        pool.append(it)
            except Exception as e:
                print(f"[feed failed] {source}: {e}", file=sys.stderr)
        pool.sort(key=lambda i: i["when"] or EPOCH, reverse=True)
        pool = pool[:14]
        for i, it in enumerate(pool):
            it["id"] = f"{key}-{i}"
        sections.append({"key": key, "title": title, "pool": pool})
    return sections


def news_prompt(sections):
    head = (
        "You write a short morning news briefing for one reader: a commercial real estate valuation "
        "analyst based in San Francisco with family ties to Kauai, Hawaii.\n\n"
        "Below are today's headlines grouped by section. Each line is: id | source | headline | snippet.\n"
        "Use ONLY these items. Do not add facts that are not in them. Do not speculate.\n\n"
        "What this reader cares about, by section:\n"
        + "\n".join(f"- {k}: {v}" for k, v in INTERESTS.items())
        + "\n\nReturn JSON in exactly this shape:\n"
        '{"overview": "...", "sections": {"general": [{"id": "general-0", "summary": "..."}], "cre": [], "sf": [], "hawaii": []}}\n\n'
        "Rules:\n"
        '- "overview": 4 to 6 plain sentences, no bullets or markdown, leading with the most important stories across all sections. '
        "Mention commercial real estate, San Francisco, or Hawaii only if the items contain real news about them.\n"
        f"- For each section pick up to {MAX_PICKS} items that clearly fit that section's interests, each with a one-sentence "
        '"summary" of at most 25 words restating what the item says.\n'
        "- Be strict. Return fewer items, or an empty list, rather than padding a section with stories that don't fit.\n"
        "- Use only ids that appear below.\n\nITEMS\n"
    )
    lines = []
    for s in sections:
        lines.append(f"## {s['key']} ({s['title']})")
        for it in s["pool"]:
            lines.append(f"{it['id']} | {it['source']} | {it['title']} | {it['desc'][:240]}")
    return head + "\n".join(lines)


def gemini_json(prompt):
    """Returns {"ok", "data", "state", "detail"}. Never raises."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        print("[ai skipped] GEMINI_API_KEY is empty or missing", file=sys.stderr)
        return {"ok": False, "data": None, "state": "no_key", "detail": ""}
    models = [os.environ.get("GEMINI_MODEL", "").strip(), "gemini-2.5-flash", "gemini-2.5-flash-lite"]
    details = []
    for model in dict.fromkeys(m for m in models if m):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3},
        }
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "x-goog-api-key": key},
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                resp = json.load(r)
            parts = resp["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
            print(f"[ai ok] model {model}")
            return {"ok": True, "data": json.loads(text), "state": "ok", "detail": model}
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")[:200].replace("\n", " ")
            details.append(f"{model}: HTTP {e.code} {msg}")
        except Exception as e:
            details.append(f"{model}: {e}")
        print(f"[ai failed] {details[-1]}", file=sys.stderr)
    return {"ok": False, "data": None, "state": "failed", "detail": " | ".join(details)[:400]}


def round_robin(pool, n):
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
    ai_ok = bool(ai and isinstance(ai.get("sections"), dict))
    for s in sections:
        by_id = {it["id"]: it for it in s["pool"]}
        items = []
        if ai_ok:
            # Trust the AI's selection, including "nothing notable today".
            for pick in ai["sections"].get(s["key"]) or []:
                it = by_id.get(pick.get("id")) if isinstance(pick, dict) else None
                if it and len(items) < MAX_PICKS:
                    items.append({"title": it["title"], "summary": strip(pick.get("summary", "")),
                                  "url": it["link"], "source": it["source"]})
        else:
            items = [{"title": it["title"], "summary": "", "url": it["link"], "source": it["source"]}
                     for it in round_robin(s["pool"], 3)]
        out.append({"key": s["key"], "title": s["title"], "items": items})
    return out


# ------------------------------ Wikipedia ------------------------------

def wiki():
    d = datetime.now(timezone.utc)
    path = f"{d.year}/{d.month:02d}/{d.day:02d}"
    urls = [f"https://en.wikipedia.org/api/rest_v1/feed/featured/{path}",
            f"https://api.wikimedia.org/feed/v1/wikipedia/en/featured/{path}"]
    last = None
    for u in urls:
        try:
            data = jget(u, timeout=40)
            t = data.get("tfa")
            if not t:
                raise ValueError("response had no featured article")
            page = lambda o: ((o or {}).get("content_urls") or {}).get("desktop", {}).get("page", "")
            otd = []
            for e in data.get("onthisday") or []:
                if e.get("text") and e.get("year") is not None and len(otd) < 2:
                    pages = e.get("pages") or [{}]
                    otd.append({"year": e["year"], "text": strip(e["text"]), "url": page(pages[0])})
            return {
                "title": (t.get("titles") or {}).get("normalized") or t.get("title", ""),
                "url": page(t),
                "extract": strip(t.get("extract", "")),
                "otd": otd,
            }
        except Exception as e:
            last = e
            print(f"[wiki failed] {u}: {e}", file=sys.stderr)
    raise last


# ------------------------------ sports ------------------------------

def parse_utc(s):
    for fmt in ("%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    raise ValueError(f"bad date {s}")


NET_BONUS = {"ABC": 0.15, "NBC": 0.15, "ESPN": 0.10, "Prime Video": 0.05, "Peacock": 0.05}


def parse_events(data):
    games = []
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        teams = {c.get("homeAway"): c for c in comp.get("competitors", [])}

        def name(c):
            t = (c or {}).get("team") or {}
            return t.get("shortDisplayName") or t.get("displayName") or t.get("abbreviation") or "?"

        def pct(c):
            for r in (c or {}).get("records") or []:
                m = re.match(r"(\d+)-(\d+)", r.get("summary", "") or "")
                if m:
                    w, l = int(m.group(1)), int(m.group(2))
                    return w / (w + l) if w + l else 0.5
            return 0.5

        start = parse_utc(ev["date"]).astimezone(TZ)
        nets = []
        for b in comp.get("broadcasts") or []:
            nets += b.get("names") or []
        for g in comp.get("geoBroadcasts") or []:
            m = (g.get("media") or {}).get("shortName")
            if m and ((g.get("market") or {}).get("type") or "").lower() == "national":
                nets.append(m)
        nets = list(dict.fromkeys(nets))
        matchup = f"{name(teams.get('away'))} at {name(teams.get('home'))}"
        score = pct(teams.get("away")) + pct(teams.get("home"))
        score += sum(0.5 for t in FAVORITE_NBA_TEAMS if t.lower() in matchup.lower())
        score += max([b for n, b in NET_BONUS.items() if any(n.lower() in x.lower() for x in nets)] or [0])
        games.append({
            "matchup": matchup, "time": start.strftime("%-I:%M %p"), "network": ", ".join(nets),
            "state": ((ev.get("status") or {}).get("type") or {}).get("state", "pre"),
            "start_hour": start.hour, "score": score,
        })
    games.sort(key=lambda g: (g["start_hour"], g["matchup"]))
    return games


def public(g):
    return {k: g[k] for k in ("matchup", "time", "network", "state")}


def golf(base):
    data = jget(base.format("golf", "pga"), timeout=30)
    events = data.get("events") or []
    if not events:
        return {"none": True}
    ev = events[0]
    st = (ev.get("status") or {}).get("type") or {}
    state = st.get("state", "pre")
    comp = (ev.get("competitions") or [{}])[0]
    leaders = []
    if state in ("in", "post"):
        comps = sorted(comp.get("competitors") or [], key=lambda c: c.get("order") or c.get("sortOrder") or 999)
        for c in comps[:3]:
            who = (c.get("athlete") or {}).get("displayName") or c.get("displayName") or ""
            sc = c.get("score")
            if isinstance(sc, dict):
                sc = sc.get("displayValue")
            if who:
                leaders.append({"name": who, "score": str(sc) if sc is not None else ""})
    start = ""
    try:
        start = parse_utc(ev["date"]).astimezone(TZ).strftime("%a %b %-d")
    except Exception:
        pass
    return {"none": False, "name": ev.get("name") or ev.get("shortName") or "PGA Tour event",
            "state": state, "detail": st.get("detail") or st.get("shortDetail") or "", "start": start,
            "leaders": leaders}


def sports():
    day = datetime.now(TZ).strftime("%Y%m%d")
    out = {"date": day, "nba": None, "nfl": None, "golf": None}
    base = "https://site.api.espn.com/apis/site/v2/sports/{}/{}/scoreboard"
    try:
        games = parse_events(jget(base.format("basketball", "nba") + "?dates=" + day))
        warriors = [g for g in games if TEAM.lower() in g["matchup"].lower()]
        cand = [g for g in games if g not in warriors
                and any(n.lower() in g["network"].lower() for n in NBA_NATIONAL)]
        top = sorted(cand, key=lambda g: g["score"], reverse=True)[:NBA_NATIONAL_MAX]
        top.sort(key=lambda g: g["start_hour"])
        out["nba"] = {"count": len(games), "warriors": [public(g) for g in warriors],
                      "national": [public(g) for g in top]}
    except Exception as e:
        print(f"[nba failed] {e}", file=sys.stderr)
    try:
        games = parse_events(jget(base.format("football", "nfl") + "?dates=" + day))
        prime = [g for g in games if g["start_hour"] >= NFL_PRIMETIME_START_HOUR_PT][:NFL_MAX]
        out["nfl"] = {"count": len(games), "primetime": [public(g) for g in prime],
                      "all": [public(g) for g in games] if len(games) <= NFL_MAX else []}
    except Exception as e:
        print(f"[nfl failed] {e}", file=sys.stderr)
    try:
        out["golf"] = golf(base)
    except Exception as e:
        print(f"[golf failed] {e}", file=sys.stderr)
    return out


# ------------------------------ fantasy ------------------------------

ESPN_POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "D/ST"}
ESPN_TEAMS = {0: "FA", 1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL", 7: "DEN", 8: "DET",
              9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV", 14: "LAR", 15: "MIA", 16: "MIN",
              17: "NE", 18: "NO", 19: "NYG", 20: "NYJ", 21: "PHI", 22: "ARI", 23: "PIT", 24: "LAC",
              25: "SF", 26: "SEA", 27: "TB", 28: "WSH", 29: "CAR", 30: "JAX", 33: "BAL", 34: "HOU"}
NFL_NEWS = [("ESPN", "https://www.espn.com/espn/rss/nfl/news"),
            ("CBS Sports", "https://www.cbssports.com/rss/headlines/nfl/"),
            ("Yahoo Sports", "https://sports.yahoo.com/nfl/rss/")]
SEVERITY = {"out": 0, "ir": 0, "pup": 0, "sus": 0, "suspension": 0, "doubtful": 1, "questionable": 2}


def season_year():
    now = datetime.now(TZ)
    return now.year if now.month >= 3 else now.year - 1


def pretty_status(st):
    s = str(st).replace("_", " ").strip()
    if s.lower() == "injury reserve":
        return "IR"
    return s if s.isupper() and len(s) <= 4 else s.title()


def sleeper_flagged(username, add):
    base = "https://api.sleeper.app/v1"
    uid = jget(f"{base}/user/{urllib.parse.quote(username)}")["user_id"]
    leagues = jget(f"{base}/user/{uid}/leagues/nfl/{season_year()}") or []
    players = jget(f"{base}/players/nfl", timeout=90)  # big file; Sleeper asks for at most one call a day
    for lg in leagues:
        rosters = jget(f"{base}/league/{lg['league_id']}/rosters") or []
        mine = next((r for r in rosters if str(r.get("owner_id")) == str(uid)), None)
        if not mine:
            continue
        for pid in mine.get("players") or []:
            p = players.get(pid) or {}
            st = p.get("injury_status")
            if st and str(st).lower() not in ("none", "healthy", "active"):
                nm = p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
                add(nm, p.get("position", ""), p.get("team") or "", pretty_status(st),
                    p.get("injury_body_part") or "", f"{lg.get('name', 'Sleeper league')} (Sleeper)",
                    p.get("injury_notes") or "")
    return len(leagues)


def espn_flagged(league_id, team_id, swid, s2, add):
    url = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season_year()}"
           f"/segments/0/leagues/{league_id}?view=mRoster&view=mTeam&view=mSettings")
    headers = {"Cookie": f"SWID={swid}; espn_s2={s2}"} if swid and s2 else {}
    data = jget(url, headers)
    lg_name = (data.get("settings") or {}).get("name") or "ESPN league"
    mine = next((t for t in data.get("teams") or [] if str(t.get("id")) == str(team_id)), None)
    if not mine:
        raise ValueError(f"team id {team_id} not found in league")
    for e in (mine.get("roster") or {}).get("entries") or []:
        pl = (e.get("playerPoolEntry") or {}).get("player") or {}
        st = pl.get("injuryStatus")
        if st and str(st).upper() != "ACTIVE":
            add(pl.get("fullName", "Unknown"), ESPN_POS.get(pl.get("defaultPositionId"), ""),
                ESPN_TEAMS.get(pl.get("proTeamId"), ""), pretty_status(st), "", f"{lg_name} (ESPN)", "")


def fantasy():
    sl = os.environ.get("SLEEPER_USERNAME", "").strip()
    lid = os.environ.get("ESPN_LEAGUE_ID", "").strip()
    tid = os.environ.get("ESPN_TEAM_ID", "").strip()
    out = {"configured": bool(sl or (lid and tid)), "players": [], "errors": []}
    if not out["configured"]:
        return out
    flagged = {}

    def add(name, pos, team, status, body, league, note):
        k = name.lower()
        p = flagged.setdefault(k, {"name": name, "pos": pos, "team": team, "status": status,
                                   "body": body, "note": note, "leagues": [], "news": [], "summary": ""})
        if league not in p["leagues"]:
            p["leagues"].append(league)

    if sl:
        try:
            n = sleeper_flagged(sl, add)
            if n == 0:
                out["errors"].append(f"Sleeper: no NFL leagues found for {sl} in {season_year()}.")
        except Exception as e:
            out["errors"].append(f"Sleeper: {e}")
    if lid and tid:
        try:
            espn_flagged(lid, tid, os.environ.get("ESPN_SWID", "").strip(), os.environ.get("ESPN_S2", "").strip(), add)
        except urllib.error.HTTPError as e:
            hint = " (private league: check the ESPN_SWID and ESPN_S2 secrets)" if e.code in (401, 403) else ""
            out["errors"].append(f"ESPN: HTTP {e.code}{hint}")
        except Exception as e:
            out["errors"].append(f"ESPN: {e}")

    players = list(flagged.values())
    if players:
        pool = []
        for source, url in NFL_NEWS:
            try:
                pool += parse_feed(fetch(url), source, limit=60, max_age=96)
            except Exception as e:
                print(f"[nfl news failed] {source}: {e}", file=sys.stderr)
        for p in players:
            p["link"] = "https://news.google.com/search?q=" + urllib.parse.quote_plus(p["name"] + " injury")
            for it in pool:
                if p["name"].lower() in (it["title"] + " " + it["desc"]).lower() and len(p["news"]) < 2:
                    p["news"].append({"title": it["title"], "url": it["link"], "source": it["source"]})
        with_news = [p for p in players if p["news"]]
        if with_news:
            lines = []
            for i, p in enumerate(players):
                if p["news"]:
                    heads = " / ".join(n["title"] for n in p["news"])
                    lines.append(f"p{i} | {p['name']} ({p['pos']}, {p['team']}) | status: {p['status']} {p['body']} | "
                                 f"note: {p['note'][:120]} | headlines: {heads}")
            prompt = ("Below are fantasy football players who are not fully healthy, with recent headlines found. "
                      "For each, write ONE sentence (at most 30 words) on what is going on, using ONLY the status and "
                      "headlines given. Do not guess. Return JSON like {\"p0\": \"...\"} using the ids given.\n\n"
                      + "\n".join(lines))
            res = gemini_json(prompt)
            if res["ok"] and isinstance(res["data"], dict):
                for i, p in enumerate(players):
                    p["summary"] = strip(str(res["data"].get(f"p{i}", "")))
        players.sort(key=lambda p: (SEVERITY.get(p["status"].lower(), 3), p["name"]))
        for p in players:
            p.pop("note", None)
    out["players"] = players
    return out


# ------------------------------ main ------------------------------

def main():
    now = datetime.now(TZ)
    try:
        sections = collect_news()
    except Exception as e:
        print(f"[news failed] {e}", file=sys.stderr)
        sections = []
    res = {"ok": False, "data": None, "state": "no_key", "detail": ""}
    if any(s["pool"] for s in sections):
        res = gemini_json(news_prompt(sections))
    ai = res["data"] if res["ok"] and isinstance(res["data"], dict) else None
    overview = strip(ai.get("overview", "")) if ai else ""
    if res["ok"] and not overview:
        res["ok"], res["state"], res["detail"] = False, "failed", "Gemini replied without an overview"
    data = {
        "generated": now.isoformat(),
        "ai": bool(overview),
        "ai_status": {"state": res["state"], "detail": res["detail"]},
        "overview": overview,
        "sections": compose_sections(sections, ai),
        "sports": sports(),
        "wiki": None,
        "fantasy": None,
    }
    try:
        data["wiki"] = wiki()
    except Exception as e:
        data["wiki_error"] = str(e)[:200]
    try:
        data["fantasy"] = fantasy()
    except Exception as e:
        print(f"[fantasy failed] {e}", file=sys.stderr)
        data["fantasy"] = {"configured": True, "players": [], "errors": [str(e)[:200]]}
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    print("wrote data.json")


if __name__ == "__main__":
    main()
