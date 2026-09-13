#!/usr/bin/env python3
"""
Merged Hoyts + Event Cinemas AU + NZ scraper (compact day-wise output).

Output:
    australia boxoffice/YYYY/MM-DD.json    -> today's AU shows
    australia advance/YYYY/MM-DD.json      -> future AU shows
    newzealand boxoffice/YYYY/MM-DD.json   -> today's NZ shows
    newzealand advance/YYYY/MM-DD.json     -> future NZ shows

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
# 1. Country configuration
# ------------------------------------------------------------------
COUNTRIES: Dict[str, Dict] = {
    "AU": {
        "label":        "Australia",
        "dir":          "australia",
        "tz":           "Australia/Sydney",
        "hoyts_cinema": "https://apim-aea.hoyts.com.au/cinemaapi-au-live/api",
        "hoyts_ticket": "https://apim-aea.hoyts.com.au/ticketing-au-live/api/v1",
        "event_base":   "https://www.eventcinemas.com.au",
        "hoyts_host":   "www.hoyts.com.au",
        "event_host":   "www.eventcinemas.com.au",
    },
    "NZ": {
        "label":        "New Zealand",
        "dir":          "newzealand",
        "tz":           "Pacific/Auckland",
        "hoyts_cinema": "https://apim-aea.hoyts.co.nz/cinemaapi-nz-live/api",
        "hoyts_ticket": "https://apim-aea.hoyts.co.nz/ticketing-nz-live/api/v1",
        "event_base":   "https://www.eventcinemas.co.nz",
        "hoyts_host":   "www.hoyts.co.nz",
        "event_host":   "www.eventcinemas.co.nz",
    },
}

# ------------------------------------------------------------------
# 2. Global config
# ------------------------------------------------------------------
KEYWORDS        = {"hindi", "tamil", "telugu", "kannada", "malayalam"}
CINE_INDIA_ATTR = "cine india"
MAX_WORKERS     = 8
TIMEOUT         = 30
RETRIES         = 3
BACKOFF         = 2.0
EVENT_WINDOW    = 3          # days per Event movie

# Index positions in the output record array
IDX_MOVIE, IDX_ID, IDX_TIME, IDX_GROSS, IDX_SEATS, IDX_SOLD, IDX_SRC = range(7)


# ------------------------------------------------------------------
# 3. Timezone helper
# ------------------------------------------------------------------
def get_tz(country_key: str):
    """Return a tzinfo for the given country, with a hardcoded fallback."""
    name = COUNTRIES[country_key]["tz"]
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    offsets = {"AU": 10, "NZ": 12}
    return timezone(timedelta(hours=offsets.get(country_key, 0)))


# ------------------------------------------------------------------
# 4. Logging
# ------------------------------------------------------------------
def log(msg: str, level: str = "INFO"):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {level:5s} {msg}", flush=True)


# ------------------------------------------------------------------
# 5. Proxy helpers (country-aware)
# ------------------------------------------------------------------
def get_hoyts_proxies(country_key: str) -> List[str]:
    if country_key == "AU":
        raw = os.environ.get("HOYTS_PROXY", "").strip()
    else:
        raw = (os.environ.get("HOYTS_NZ_PROXY", "").strip()
               or os.environ.get("HOYTS_PROXY", "").strip())
    return [raw] if raw else []


def get_event_proxies(country_key: str) -> List[str]:
    if country_key == "AU":
        raw = os.environ.get("EVENT_PROXIES", "").strip()
    else:
        raw = (os.environ.get("EVENT_NZ_PROXIES", "").strip()
               or os.environ.get("EVENT_PROXIES", "").strip())
    return [p.strip() for p in raw.split(",") if p.strip()]


# ------------------------------------------------------------------
# 6. Shared UA / Accept-Language lists
# ------------------------------------------------------------------
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


# ==================================================================
#  HOYTS  (AU + NZ)
# ==================================================================
def hoyts_headers(country_key: str) -> Dict[str, str]:
    """
    Exact header builder from the working scraper, but Origin / Referer
    are derived from the country's Hoyts host.
    """
    cfg = COUNTRIES[country_key]
    host = cfg["hoyts_host"]

    ua = random.choice(UAS)
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
        "Accept-Language": random.choice(LANGS),
        "Cache-Control": "no-cache",
        "Origin": f"https://{host}",
        "Pragma": "no-cache",
        "Priority": "u=1, i",
        "Referer": f"https://{host}/",
        "Sec-CH-UA": sec_ch_ua,
        "Sec-CH-UA-Mobile": mobile,
        "Sec-CH-UA-Platform": f'"{platform}"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "User-Agent": ua,
    }


def hoyts_request(url: str, country_key: str) -> Dict:
    cfg = COUNTRIES[country_key]
    proxies_pool = get_hoyts_proxies(country_key)
    last = None
    for i in range(RETRIES):
        try:
            proxies = None
            if proxies_pool:
                p = random.choice(proxies_pool)
                proxies = {"http": p, "https": p}
            r = requests.get(url, headers=hoyts_headers(country_key),
                             proxies=proxies, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            log(f"HOYTS[{country_key}] attempt {i+1}/{RETRIES} failed {url}: {e}", "WARN")
            if i < RETRIES - 1:
                time.sleep(BACKOFF * (2 ** i))
    raise RuntimeError(f"HOYTS[{country_key}] GET failed {url}: {last}")


def hoyts_fetch_movies(country_key: str) -> List[Dict]:
    url = f"{COUNTRIES[country_key]['hoyts_cinema']}/movies"
    return hoyts_request(url, country_key)


def hoyts_fetch_sessions(country_key: str) -> List[Dict]:
    url = f"{COUNTRIES[country_key]['hoyts_cinema']}/sessions"
    data = hoyts_request(url, country_key)
    if isinstance(data, dict):
        for k in ("sessions", "items", "data"):
            if k in data and isinstance(data[k], list):
                return data[k]
        return []
    return data if isinstance(data, list) else []


def hoyts_fetch_seat_map(cinema_id, session_id, country_key: str) -> Dict:
    url = f"{COUNTRIES[country_key]['hoyts_ticket']}/ticket/seats/{cinema_id}/{session_id}/"
    return hoyts_request(url, country_key)


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


def run_hoyts(country_key: str) -> List[List]:
    cfg = COUNTRIES[country_key]
    log(f"HOYTS[{country_key}]: fetching movies", "STEP")
    movies = hoyts_fetch_movies(country_key)
    log(f"HOYTS[{country_key}]: {len(movies)} movies total")

    filtered = [m for m in movies
                if m.get("vistaId") and any(
                    kw in (m.get("name", "") or "").lower() for kw in KEYWORDS)]
    log(f"HOYTS[{country_key}]: {len(filtered)} Indian-language matches", "OK")
    if not filtered:
        return []
    by_id = {m["vistaId"]: m for m in filtered}

    log(f"HOYTS[{country_key}]: fetching sessions", "STEP")
    sessions = hoyts_fetch_sessions(country_key)
    log(f"HOYTS[{country_key}]: {len(sessions)} sessions total")
    matched = [s for s in sessions if s.get("movieId") in by_id]
    log(f"HOYTS[{country_key}]: {len(matched)} sessions for matching movies", "OK")
    if not matched:
        return []

    log(f"HOYTS[{country_key}]: fetching {len(matched)} seat maps", "STEP")
    results: List[List] = []

    def task(s):
        try:
            sm = hoyts_fetch_seat_map(s["cinemaId"], s["id"], country_key)
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
            log(f"HOYTS[{country_key}]: seat map {s.get('id')} failed: {e}", "WARN")
            return None

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = [ex.submit(task, s) for s in matched]
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            if r:
                results.append(r)
            if i % 25 == 0 or i == len(futs):
                log(f"HOYTS[{country_key}]: {i}/{len(futs)} seat maps", "DATA")

    log(f"HOYTS[{country_key}]: {len(results)} shows OK", "OK")
    return results


# ==================================================================
#  EVENT CINEMAS  (AU + NZ)
# ==================================================================
def build_event_headers(country_key: str) -> Dict[str, str]:
    cfg = COUNTRIES[country_key]
    host = cfg["event_host"]

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


def event_get(url: str, session, country_key: str) -> Dict:
    proxies_pool = get_event_proxies(country_key)
    last = None
    for i in range(RETRIES):
        try:
            proxies = None
            if proxies_pool:
                p = random.choice(proxies_pool)
                proxies = {"http": p, "https": p}
            r = session.get(url, headers=build_event_headers(country_key),
                            proxies=proxies, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            log(f"EVENT[{country_key}] attempt {i+1}/{RETRIES} failed {url}: {e}", "WARN")
            if i < RETRIES - 1:
                time.sleep(BACKOFF * (2 ** i))
    raise RuntimeError(f"EVENT[{country_key}] GET failed {url}: {last}")


def event_all_movies(session, country_key: str) -> List[Dict]:
    base = COUNTRIES[country_key]["event_base"]
    merged: Dict[int, Dict] = {}
    for path in ("Movies/GetNowShowing", "Movies/GetComingSoon"):
        d = event_get(f"{base}/{path}", session, country_key)
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


def event_sessions(session, cinema_ids: List[int], date_str: str, country_key: str) -> List[Dict]:
    if not cinema_ids:
        return []
    base = COUNTRIES[country_key]["event_base"]
    params = "&".join(f"cinemaIds={c}" for c in cinema_ids)
    d = event_get(f"{base}/Cinemas/GetSessions?{params}&date={date_str}",
                  session, country_key)
    return (d.get("Data") or {}).get("Movies") or []


def event_seat_map(session, sid: int, country_key: str) -> Dict:
    base = COUNTRIES[country_key]["event_base"]
    return event_get(f"{base}/api/ticketing/session?sessionId={sid}",
                     session, country_key)


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


def _release_dt(m: Dict, tz) -> Optional[datetime]:
    raw = m.get("ReleasedAt")
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    return None


def event_window(m: Dict, country_key: str, days: int = EVENT_WINDOW) -> List[str]:
    tz = get_tz(country_key)
    today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    rel = _release_dt(m, tz)
    start = rel if (rel and rel > today) else today
    return [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]


def collect_event_sessions(session, movie: Dict, cinemas: List[int],
                           dates: List[str], country_key: str) -> List[Dict]:
    name = (movie.get("Name") or "").strip()
    out: List[Dict] = []
    for d in dates:
        try:
            days = event_sessions(session, cinemas, d, country_key)
        except Exception as e:
            log(f"EVENT[{country_key}] sessions {name}@{d} failed: {e}", "WARN")
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


def run_event(country_key: str) -> List[List]:
    log(f"EVENT[{country_key}]: fetching movies", "STEP")
    main = event_session()
    all_movies = event_all_movies(main, country_key)
    indian = [m for m in all_movies if is_indian(m)]
    log(f"EVENT[{country_key}]: {len(indian)} Indian-language matches", "OK")
    if not indian:
        return []

    log(f"EVENT[{country_key}]: discovering sessions", "STEP")
    tasks: List[Tuple[Dict, Dict]] = []
    for m in indian:
        cinemas = m.get("CinemaIds") or []
        if not cinemas:
            continue
        dates = event_window(m, country_key)
        sess = collect_event_sessions(main, m, cinemas, dates, country_key)
        for s in sess:
            if s.get("sessionId") is not None:
                tasks.append((m, s))
    log(f"EVENT[{country_key}]: {len(tasks)} sessions discovered", "OK")
    if not tasks:
        return []

    tl = threading.local()

    def get_session():
        if not hasattr(tl, "s"):
            tl.s = event_session()
        return tl.s

    log(f"EVENT[{country_key}]: fetching {len(tasks)} seat maps", "STEP")
    results: List[List] = []

    def task(m, s):
        sid = s["sessionId"]
        try:
            sd = event_seat_map(get_session(), sid, country_key)
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
            log(f"EVENT[{country_key}]: seat map {sid} failed: {e}", "WARN")
            return None

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = [ex.submit(task, m, s) for m, s in tasks]
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            if r:
                results.append(r)
            if i % 25 == 0 or i == len(futs):
                log(f"EVENT[{country_key}]: {i}/{len(futs)} seat maps", "DATA")

    log(f"EVENT[{country_key}]: {len(results)} shows OK", "OK")
    return results


# ==================================================================
#  STORAGE  (merge + save)
# ==================================================================
def merge_and_save(path: str, new_records: List[List]) -> None:
    """
    Merge new records into the existing file at `path`.
    Records are arrays: [movie, id, time, gross, seats, sold, source].
    Match key = f"{source}:{id}".
    """
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
#  MAIN  (loop over countries)
# ==================================================================
def main():
    started = time.time()
    log("Merged AU+NZ scraper starting", "STEP")

    for country_key in ("AU", "NZ"):
        cfg = COUNTRIES[country_key]
        tz = get_tz(country_key)
        today = datetime.now(tz).strftime("%Y-%m-%d")

        log(f"════════ {cfg['label']} ({country_key}) ════════", "STEP")
        log(f"Reference today ({cfg['tz']}): {today}")

        records: List[List] = []

        try:
            records += run_hoyts(country_key)
        except Exception as e:
            log(f"HOYTS[{country_key}] aborted: {e}", "ERR")

        try:
            records += run_event(country_key)
        except Exception as e:
            log(f"EVENT[{country_key}] aborted: {e}", "ERR")

        if not records:
            log(f"No {country_key} records fetched — existing files left untouched.", "WARN")
            continue

        by_date: Dict[str, List[List]] = defaultdict(list)
        for r in records:
            d = (r[IDX_TIME] or "").split("T")[0]
            if d:
                by_date[d].append(r)

        for date, recs in sorted(by_date.items()):
            if date == today:
                base = f"{cfg['dir']} boxoffice"
            elif date > today:
                base = f"{cfg['dir']} advance"
            else:
                log(f"Skipping past date {date}", "INFO")
                continue
            path = os.path.join(base, date[:4], f"{date[5:]}.json")
            merge_and_save(path, recs)

    log(f"Done in {time.time() - started:.1f}s", "OK")


if __name__ == "__main__":
    main()
