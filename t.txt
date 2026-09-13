#!/usr/bin/env python3
"""
Build per-movie summary files from day-wise show data.

Inputs (produced by scraper.py, both inside the data repo):
    <BASE>/australia boxoffice/YYYY/MM-DD.json
    <BASE>/australia advance/YYYY/MM-DD.json
    <BASE>/newzealand boxoffice/YYYY/MM-DD.json
    <BASE>/newzealand advance/YYYY/MM-DD.json

Outputs:
    <BASE>/australia data/<slug>.json
    <BASE>/newzealand data/<slug>.json

Movie summary format:
    {
      "movie": "<display name>",
      "days": {
        "YYYY-MM-DD": {
          "shows": N, "seats": N, "sold": N, "gross": F,
          "H": {"shows": N, "seats": N, "sold": N, "gross": F},
          "E": {"shows": N, "seats": N, "sold": N, "gross": F}
        }
      },
      "totals": { ...same shape as a day... }
    }

BASE defaults to "." — override via env var DATA_BASE.
"""
import json
import os
import re
import sys
from collections import defaultdict
from typing import Dict, List, Tuple

# Index positions in the input record array
IDX_MOVIE, IDX_ID, IDX_TIME, IDX_GROSS, IDX_SEATS, IDX_SOLD, IDX_SRC = range(7)

BASE = os.environ.get("DATA_BASE", ".").strip() or "."

# (country_label, boxoffice_dir, advance_dir, out_dir)
COUNTRIES = [
    ("australia",
     os.path.join(BASE, "australia boxoffice"),
     os.path.join(BASE, "australia advance"),
     os.path.join(BASE, "australia data")),
    ("newzealand",
     os.path.join(BASE, "newzealand boxoffice"),
     os.path.join(BASE, "newzealand advance"),
     os.path.join(BASE, "newzealand data")),
]


# ------------------------------------------------------------------
# Name → slug normalization
# ------------------------------------------------------------------
def normalize_slug(name: str) -> str:
    """
    Turn a raw movie name into a canonical slug so that
    'Mirzapur: The Movie (Hindi, Eng Sub)' and 'Mirzapur - The Movie'
    map to the same key: 'mirzapur-the-movie'.
    """
    if not name:
        return ""
    # Strip trailing parenthesized info like "(Hindi, Eng Sub)"
    name = re.sub(r'\s*\([^)]*\)\s*$', '', name).strip()
    # Lowercase
    name = name.lower()
    # Replace anything that isn't a-z0-9 with a space
    name = re.sub(r'[^a-z0-9]+', ' ', name)
    # Collapse whitespace and join with hyphens
    return '-'.join(name.split())


def pick_display_name(slug_names: Dict[str, int]) -> str:
    """
    Choose a clean, human-friendly display name from the observed raw names.
    Prefers: no trailing parens, most frequent, shortest, alphabetical.
    """
    cleaned: Dict[str, int] = {}
    for raw, count in slug_names.items():
        c = re.sub(r'\s*\([^)]*\)\s*$', '', raw).strip()
        if c:
            cleaned[c] = cleaned.get(c, 0) + count
    if not cleaned:
        return next(iter(slug_names))
    return max(cleaned.items(), key=lambda kv: (kv[1], -len(kv[0]), kv[0]))[0]


# ------------------------------------------------------------------
# Loading
# ------------------------------------------------------------------
def load_records(folder: str) -> List[List]:
    """Load every record from <folder>/YYYY/MM-DD.json."""
    records: List[List] = []
    if not os.path.isdir(folder):
        return records
    for year in sorted(os.listdir(folder)):
        ypath = os.path.join(folder, year)
        if not os.path.isdir(ypath):
            continue
        for fname in sorted(os.listdir(ypath)):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(ypath, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    for r in data:
                        if isinstance(r, list) and len(r) >= 7:
                            records.append(r)
            except Exception as e:
                print(f"  ⚠️  Skipping {fpath}: {e}", flush=True)
    return records


def load_country_records(boxoffice_dir: str, advance_dir: str) -> List[List]:
    """
    Load boxoffice + advance. Dedupe by (source, id).
    Boxoffice wins when the same show appears in both — it is the fresher copy.
    """
    by_key: Dict[Tuple[str, int], List] = {}

    # Lower priority: advance
    for r in load_records(advance_dir):
        key = (r[IDX_SRC], r[IDX_ID])
        by_key[key] = r

    # Higher priority: boxoffice overwrites
    for r in load_records(boxoffice_dir):
        key = (r[IDX_SRC], r[IDX_ID])
        by_key[key] = r

    return list(by_key.values())


# ------------------------------------------------------------------
# Aggregation
# ------------------------------------------------------------------
def _empty_stats() -> Dict:
    return {
        "shows": 0, "seats": 0, "sold": 0, "gross": 0.0,
        "H": {"shows": 0, "seats": 0, "sold": 0, "gross": 0.0},
        "E": {"shows": 0, "seats": 0, "sold": 0, "gross": 0.0},
    }


def _add_to_stats(stats: Dict, src: str, seats: int, sold: int, gross: float):
    stats["shows"] += 1
    stats["seats"] += seats
    stats["sold"] += sold
    stats["gross"] += gross
    if src in ("H", "E"):
        stats[src]["shows"] += 1
        stats[src]["seats"] += seats
        stats[src]["sold"] += sold
        stats[src]["gross"] += gross


def _finalize_stats(stats: Dict) -> Dict:
    stats["gross"] = round(stats["gross"], 2)
    for s in ("H", "E"):
        stats[s]["gross"] = round(stats[s]["gross"], 2)
    return stats


def build_movie_summaries(records: List[List]) -> Dict[str, Dict]:
    """
    Group records by normalized slug and aggregate:
      - per-date (daywise) stats
      - overall totals across every date
    """
    slug_names: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    slug_days:  Dict[str, Dict[str, Dict]] = defaultdict(
        lambda: defaultdict(_empty_stats)
    )

    for r in records:
        raw_name = (r[IDX_MOVIE] or "").strip()
        if not raw_name:
            continue
        slug = normalize_slug(raw_name)
        if not slug:
            continue
        slug_names[slug][raw_name] += 1

        date = (r[IDX_TIME] or "").split("T")[0]
        if not date:
            continue

        src   = r[IDX_SRC] or "?"
        gross = float(r[IDX_GROSS] or 0)
        seats = int(r[IDX_SEATS] or 0)
        sold  = int(r[IDX_SOLD] or 0)

        _add_to_stats(slug_days[slug][date], src, seats, sold, gross)

    summaries: Dict[str, Dict] = {}

    for slug, days_map in slug_days.items():
        display = pick_display_name(slug_names[slug])

        days_out: Dict[str, Dict] = {}
        totals = _empty_stats()

        for date in sorted(days_map.keys()):
            day_stats = _finalize_stats(days_map[date])
            days_out[date] = day_stats

            # accumulate totals
            totals["shows"] += day_stats["shows"]
            totals["seats"] += day_stats["seats"]
            totals["sold"]  += day_stats["sold"]
            totals["gross"] += day_stats["gross"]
            for s in ("H", "E"):
                totals[s]["shows"] += day_stats[s]["shows"]
                totals[s]["seats"] += day_stats[s]["seats"]
                totals[s]["sold"]  += day_stats[s]["sold"]
                totals[s]["gross"] += day_stats[s]["gross"]

        _finalize_stats(totals)

        summaries[slug] = {
            "movie":  display,
            "days":   days_out,
            "totals": totals,
        }

    return summaries


# ------------------------------------------------------------------
# Saving
# ------------------------------------------------------------------
def save_summaries(summaries: Dict[str, Dict], out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)

    # Remove stale movie files (files whose slug is no longer present)
    for fname in os.listdir(out_dir):
        if fname.endswith(".json"):
            try:
                os.remove(os.path.join(out_dir, fname))
            except OSError:
                pass

    for slug, data in summaries.items():
        path = os.path.join(out_dir, f"{slug}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"), ensure_ascii=False)

    print(f"  💾 {out_dir}: wrote {len(summaries)} movie file(s)", flush=True)


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def main():
    print("🚀 Building per-movie summaries...", flush=True)
    for country, boxoffice_dir, advance_dir, out_dir in COUNTRIES:
        print(f"\n── {country.upper()} ──", flush=True)
        records = load_country_records(boxoffice_dir, advance_dir)
        print(f"  Loaded {len(records)} unique show record(s)", flush=True)
        if not records:
            print(f"  No records found — skipping {country}", flush=True)
            continue
        summaries = build_movie_summaries(records)
        save_summaries(summaries, out_dir)
    print("\n✅ Done.", flush=True)


if __name__ == "__main__":
    main()
