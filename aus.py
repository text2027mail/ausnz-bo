#!/usr/bin/env python3
"""
Merged Hoyts + Event Cinemas AU scraper (compact day-wise output).

Output:
    australia boxoffice/YYYY/MM-DD.json   -> today's shows
    australia advance/YYYY/MM-DD.json     -> future shows

Record (array of values, no keys):
    [movie, id, time, gross, seats, sold, source]
    - movie  : str
    - id     : int
    - time   : ISO string
    - gross  : float
    - seats  : int
    - sold   : int
    - source : "H" | "E"
"""
import json, os, random, re, sys, threading, time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

import requests
import cloudscraper


# ------------------------------------------------------------------
# 1. Config
# ------------------------------------------------------------------
KEYWORDS        = {"hindi", "tamil", "telugu", "kannada", "malayalam"}
CINE_INDIA_ATTR = "cine india"
MAX_WORKERS     = 8
TIMEOUT         = 30
RETRIES         = 3
BACKOFF         = 2.0
EVENT_WINDOW    = 3          # days per Event movie

AU_TZ = ZoneInfo("Australia/Sydney") if ZoneInfo else timezone(timedelta(hours=10))

HOYTS_CINEMA = "https://apim-aea.hoyts.com.au/cinemaapi-au-live/api"
HOYTS_TICKET = "https://apim-aea.hoyts.com.au/ticketing-au-live/api/v1"
EVENT_BASE   = "https://www.eventcinemas.com.au"

PROXY_H  = os.environ.get("HOYTS_PROXY", "").strip()
PROXY_E  = [p.strip() for p in os.environ.get("EVENT_PROXIES", "").split(",") if p.strip()]
PROXIES_H = [PROXY_H] if PROXY_H else []
PROXIES_E = PROXY_E

# Index positions in the output record array
IDX_MOVIE, IDX_ID, IDX_TIME, IDX_GROSS, IDX_SEATS, IDX_SOLD, IDX_SRC = range(7)


# ------------------------------------------------------------------
# 2. Logging
# ------------------------------------------------------------------
def log(msg: str, level: str = "INFO"):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {level:5s} {msg}", flush=True)


# ==================================================================
#  HOYTS  - headers & fetch are EXACTLY from the working scraper
# ==================================================================
HOYTS_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
]

HOYTS_LANGS = [
    "en-AU,en;q=0.9",
    "en-US,en;q=0.9",
    "en-GB,en;q=0.9",
]


def hoyts_headers() -> Dict[str, str]:
    """EXACT copy of get_random_headers() from the working scraper."""
    ua = random.choice(HOYTS_UAS)
    platform = "Windows"
    mobile = "?0"
    if "iPhone" in ua or "iPad" in ua:
        platform, mobile = "iOS", "?1"
    elif "Android" in ua:
        platform, mobile = "Android", "?1"
    elif "Macintosh" in ua:
        platform = "macOS"
    elif "Linux" in ua:
        platform = "Linux"

    sec_ch_ua = '"Chromium";v="133", "Google Chrome";v="133", "Not-A.Brand";v="24"'
    m = re.search(r"Firefox/(\d+)", ua)
    if m:
        sec_ch_ua = f'"Firefox";v="{m.group(1)}", "Gecko";v="{m.group(1)}"'

    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": random.choice(HOYTS_LANGS),
        "Cache-Control": "no-cache",
        "Origin": "https://www.hoyts.com.au",
        "Pragma": "no-cache",
        "Priority": "u=1, i",
        "Referer": "https://www.hoyts.com.au/",
        "Sec-CH-UA": sec_ch_ua,
        "Sec-CH-UA-Mobile": mobile,
        "Sec-CH-UA-Platform": f'"{platform}"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "User-Agent": ua,
    }


def hoyts_request(url: str) -> Dict:
    """GET + retry + JSON. Matches the working scraper's flow."""
    last = None
    for i in range(RETRIES):
        try:
            proxies = None
            if PROXIES_H:
                p = random.choice(PROXIES_H)
                proxies = {"http": p, "https": p}
            r = requests.get(url, headers=hoyts_headers(),
                             proxies=proxies, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            log(f"HOYTS attempt {i+1}/{RETRIES} failed {url}: {e}", "WARN")
            if i < RETRIES - 1:
                time.sleep(BACKOFF * (2 ** i))
    raise RuntimeError(f"HOYTS GET failed {url}: {last}")


def hoyts_fetch_movies() -> List[Dict]:
    return hoyts_request(f"{HOYTS_CINEMA}/movies")


def hoyts_fetch_sessions() -> List[Dict]:
    data = hoyts_request(f"{HOYTS_CINEMA}/sessions")
    if isinstance(data, dict):
        for k in ("sessions", "items", "data"):
            if k in data and isinstance(data[k], list):
                return data[k]
        return []
    return data if isinstance(data, list) else []


def hoyts_fetch_seat_map(cinema_id, session_id) -> Dict:
    return hoyts_request(f"{HOYTS_TICKET}/ticket/seats/{cinema_id}/{session_id}/")


def hoyts_parse_seat_map(seat_map: Dict) -> Tuple[int, int]:
    total = 0
    sold = 0
    for row in seat_map.get("rows", []):
        for seat in row.get("seats", []):
            if seat.get("typeId") == "gap":
                continue
            total += 1
            if seat.get("sold", False):
                sold += 1
    return total, sold


def run_hoyts() -> List[List]:
    log("HOYTS: fetching movies", "STEP")
    movies = hoyts_fetch_movies()
    log(f"HOYTS: {len(movies)} movies total")

    filtered = [m for m in movies
                if m.get("vistaId") and any(
                    kw in (m.get("name", "") or "").lower() for kw in KEYWORDS)]
    log(f"HOYTS: {len(filtered)} Indian-language matches", "OK")
    if not filtered:
        return []
    by_id = {m["vistaId"]: m for m in filtered}

    log("HOYTS: fetching sessions", "STEP")
    sessions = hoyts_fetch_sessions()
    log(f"HOYTS: {len(sessions)} sessions total")
    matched = [s for s in sessions if s.get("movieId") in by_id]
    log(f"HOYTS: {len(matched)} sessions for matching movies", "OK")
    if not matched:
        return []

    log(f"HOYTS: fetching {len(matched)} seat maps", "STEP")
    results: List[List] = []

    def task(s):
        try:
            sm = hoyts_fetch_seat_map(s["cinemaId"], s["id"])
            total, sold = hoyts_parse_seat_map(sm)
            return [
                by_id[s["movieId"]].get("name", ""),   # movie
                s["id"],                                # id
                s.get("date", ""),                      # time
                0.0,                                    # gross
                total,                                  # seats
                sold,                                   # sold
                "H",                                    # source
            ]
        except Exception as e:
            log(f"HOYTS: seat map {s.get('id')} failed: {e}", "WARN")
            return None

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = [ex.submit(task, s) for s in matched]
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            if r:
                results.append(r)
            if i % 25 == 0 or i == len(futs):
                log(f"HOYTS: {i}/{len(futs)} seat maps", "DATA")

    log(f"HOYTS: {len(results)} shows OK", "OK")
    return results


# ==================================================================
#  EVENT CINEMAS
# ==================================================================
UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
]
LANGS = ["en-AU,en;q=0.9", "en-US,en;q=0.9", "en-GB,en;q=0.9"]


def build_headers(host: str) -> Dict[str, str]:
    ua = random.choice(UAS)
    platform, mobile = "Windows", "?0"
    if "Macintosh" in ua:  platform = "macOS"
    elif "Linux" in ua:    platform = "Linux"
    sec = '"Chromium";v="133", "Google Chrome";v="133", "Not-A.Brand";v="24"'
    m = re.search(r"Firefox/(\d+)", ua)
    if m: sec = f'"Firefox";v="{m.group(1)}", "Gecko";v="{m.group(1)}"'
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": random.choice(LANGS),
        "Cache-Control": "no-cache",
        "Origin": f"https://{host}",
        "Pragma": "no-cache",
        "Referer": f"https://{host}/",
        "Sec-CH-UA": sec,
        "Sec-CH-UA-Mobile": mobile,
        "Sec-CH-UA-Platform": f'"{platform}"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "User-Agent": ua,
    }


def event_session() -> cloudscraper.CloudScraper:
    try:
        return cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False},
            enable_stealth=True,
            stealth_options={"min_delay": 1.0, "max_delay": 4.0,
                             "human_like_delays": True,
                             "randomize_headers": True,
                             "browser_quirks": True},
            allow_brotli=True,
        )
    except TypeError:
        return cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows"})


def event_get(url: str, session) -> Dict:
    last = None
    for i in range(RETRIES):
        try:
            proxies = None
            if PROXIES_E:
                p = random.choice(PROXIES_E)
                proxies = {"http": p, "https": p}
            r = session.get(url, headers=build_headers("www.eventcinemas.com.au"),
                            proxies=proxies, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            log(f"EVENT attempt {i+1}/{RETRIES} failed {url}: {e}", "WARN")
            if i < RETRIES - 1:
                time.sleep(BACKOFF * (2 ** i))
    raise RuntimeError(f"EVENT GET failed {url}: {last}")


def event_all_movies(session) -> List[Dict]:
    merged: Dict[int, Dict] = {}
    for path in ("Movies/GetNowShowing", "Movies/GetComingSoon"):
        d = event_get(f"{EVENT_BASE}/{path}", session)
        for m in ((d.get("Data") or {}).get("Movies") or []):
            if m.get("Id") is not None:
                merged[m["Id"]] = m
    return list(merged.values())


def event_langs(m: Dict) -> Set[str]:
    s: Set[str] = set()
    for a in m.get("Attributes") or []:
        s.add(str(a).strip().lower())
    for f in m.get("AllFilters") or []:
        if f.get("code"): s.add(f["code"].strip().lower())
        if f.get("name"): s.add(f["name"].strip().lower())
    return s


def is_indian(m: Dict) -> bool:
    langs = event_langs(m)
    if CINE_INDIA_ATTR in langs:
        return True
    return any(kw in langs for kw in KEYWORDS)


def event_sessions(session, cinema_ids: List[int], date_str: str) -> List[Dict]:
    if not cinema_ids:
        return []
    params = "&".join(f"cinemaIds={c}" for c in cinema_ids)
    d = event_get(f"{EVENT_BASE}/Cinemas/GetSessions?{params}&date={date_str}", session)
    return (d.get("Data") or {}).get("Movies") or []


def event_seat_map(session, sid: int) -> Dict:
    return event_get(f"{EVENT_BASE}/api/ticketing/session?sessionId={sid}", session)


def parse_event_seats(sd: Dict) -> Tuple[int, int, float]:
    total = sold = 0
    price = 0.0
    rows = ((sd.get("Data") or {}).get("Seats") or {}).get("Rows") or []
    for row in rows:
        for seat in row.get("Seats") or []:
            st = seat.get("Status")
            if not st or st == "Spacer":
                continue
            total += 1
            if st == "Booked":
                sold += 1
    for t in (sd.get("Data") or {}).get("Tickets") or []:
        if (t.get("Name") or "").strip().lower() == "adult":
            try:
                price = float(t.get("Price") or 0)
            except (TypeError, ValueError):
                price = 0.0
            break
    return total, sold, price


def _release_dt(m: Dict) -> Optional[datetime]:
    raw = m.get("ReleasedAt")
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=AU_TZ)
        except ValueError:
            continue
    return None


def event_window(m: Dict, days: int = EVENT_WINDOW) -> List[str]:
    today = datetime.now(AU_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    rel = _release_dt(m)
    start = rel if (rel and rel > today) else today
    return [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]


def collect_event_sessions(session, movie: Dict, cinemas: List[int],
                           dates: List[str]) -> List[Dict]:
    name = (movie.get("Name") or "").strip()
    out: List[Dict] = []
    for d in dates:
        try:
            days = event_sessions(session, cinemas, d)
        except Exception as e:
            log(f"EVENT sessions {name}@{d} failed: {e}", "WARN")
            continue
        for m in days:
            if (m.get("Name") or "").strip() != name:
                continue
            for cinema in m.get("CinemaModels") or []:
                for s in cinema.get("Sessions") or []:
                    out.append({
                        "sessionId": s.get("Id"),
                        "startTime": s.get("StartTime"),
                    })
    return out


def run_event() -> List[List]:
    log("EVENT: fetching movies", "STEP")
    main = event_session()
    all_movies = event_all_movies(main)
    indian = [m for m in all_movies if is_indian(m)]
    log(f"EVENT: {len(indian)} Indian-language matches", "OK")
    if not indian:
        return []

    log("EVENT: discovering sessions", "STEP")
    tasks: List[Tuple[Dict, Dict]] = []
    for m in indian:
        cinemas = m.get("CinemaIds") or []
        if not cinemas:
            continue
        dates = event_window(m)
        sess = collect_event_sessions(main, m, cinemas, dates)
        for s in sess:
            if s.get("sessionId") is not None:
                tasks.append((m, s))
    log(f"EVENT: {len(tasks)} sessions discovered", "OK")
    if not tasks:
        return []

    tl = threading.local()

    def get_session():
        if not hasattr(tl, "s"):
            tl.s = event_session()
        return tl.s

    log(f"EVENT: fetching {len(tasks)} seat maps", "STEP")
    results: List[List] = []

    def task(m, s):
        sid = s["sessionId"]
        try:
            sd = event_seat_map(get_session(), sid)
            total, sold, price = parse_event_seats(sd)
            return [
                m.get("Name", ""),                      # movie
                sid,                                    # id
                s.get("startTime", ""),                 # time
                round(sold * price, 2),                 # gross
                total,                                  # seats
                sold,                                   # sold
                "E",                                    # source
            ]
        except Exception as e:
            log(f"EVENT: seat map {sid} failed: {e}", "WARN")
            return None

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = [ex.submit(task, m, s) for m, s in tasks]
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            if r:
                results.append(r)
            if i % 25 == 0 or i == len(futs):
                log(f"EVENT: {i}/{len(futs)} seat maps", "DATA")

    log(f"EVENT: {len(results)} shows OK", "OK")
    return results


# ==================================================================
#  STORAGE  (merge + save)
# ==================================================================
def merge_and_save(path: str, new_records: List[List]) -> None:
    existing: Dict[str, List] = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                for r in data:
                    if isinstance(r, list) and len(r) >= 7:
                        key = f"{r[IDX_SRC]}:{r[IDX_ID]}"
                        existing[key] = r
        except Exception as e:
            log(f"Read failed {path}: {e} — starting fresh", "WARN")
            existing = {}

    added = updated = 0
    for rec in new_records:
        key = f"{rec[IDX_SRC]}:{rec[IDX_ID]}"
        if key in existing:
            old = existing[key]
            old[IDX_MOVIE] = rec[IDX_MOVIE]
            old[IDX_TIME]  = rec[IDX_TIME]
            old[IDX_GROSS] = rec[IDX_GROSS]
            old[IDX_SEATS] = rec[IDX_SEATS]
            old[IDX_SOLD]  = rec[IDX_SOLD]
            updated += 1
        else:
            existing[key] = rec
            added += 1

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(list(existing.values()), f, separators=(",", ":"),
                  ensure_ascii=False)
    log(f"{path}: +{added} new, ~{updated} updated, total {len(existing)}", "OK")


# ==================================================================
#  MAIN
# ==================================================================
def main():
    started = time.time()
    log("Merged AU scraper starting", "STEP")
    today = datetime.now(AU_TZ).strftime("%Y-%m-%d")
    log(f"Reference today (AU/Sydney): {today}")

    records: List[List] = []

    try:
        records += run_hoyts()
    except Exception as e:
        log(f"HOYTS aborted: {e}", "ERR")

    try:
        records += run_event()
    except Exception as e:
        log(f"EVENT aborted: {e}", "ERR")

    if not records:
        log("No new records fetched — existing files left untouched.", "WARN")
        return

    by_date: Dict[str, List[List]] = defaultdict(list)
    for r in records:
        d = (r[IDX_TIME] or "").split("T")[0]
        if d:
            by_date[d].append(r)

    for date, recs in sorted(by_date.items()):
        if date == today:
            base = "australia boxoffice"
        elif date > today:
            base = "australia advance"
        else:
            log(f"Skipping past date {date}", "INFO")
            continue
        path = os.path.join(base, date[:4], f"{date[5:]}.json")
        merge_and_save(path, recs)

    log(f"Done in {time.time() - started:.1f}s", "OK")


if __name__ == "__main__":
    main()
