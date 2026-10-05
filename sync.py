#!/usr/bin/env python3
"""Synchronise CNC films above one million French admissions to an MDBList list."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
STATE_FILE = ROOT / "cnc-state.json"
CATALOG_FILE = ROOT / "cnc-catalog.json"
DEFAULT_CNC_METADATA_URL = "https://www.data.gouv.fr/api/1/datasets/films-ayant-realise-plus-dun-million-dentrees"
MDB = "https://api.mdblist.com"
TMDB = "https://api.themoviedb.org/3"
TRAKT = "https://api.trakt.tv"


class SyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class Film:
    title: str
    year: int | None
    admissions: int | None

    @property
    def key(self) -> str:
        return f"{normalise(self.title)}|{self.year or ''}"


@dataclass
class Resolution:
    film: Film
    status: str
    tmdb_id: int | None = None
    candidates: list[dict[str, Any]] | None = None
    note: str | None = None


def normalise(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


def title_variants(value: Any) -> set[str]:
    text = str(value)
    variants = {normalise(text)}
    text = re.sub(r"\s*\([^)]*\)", "", text)
    variants.add(normalise(text))
    reordered = re.match(r"^(.+?)\s*\(([^)]+)\)\s*(.*)$", str(value))
    if reordered:
        suffix = reordered.group(2).strip()
        remainder = reordered.group(3).strip(" -")
        variants.add(normalise(f"{suffix} {reordered.group(1)} {remainder}"))
    return {variant for variant in variants if variant}


def search_title(value: Any) -> str:
    return re.sub(r"\s*\(\s*ex\s*:[^)]*\)", "", str(value), flags=re.IGNORECASE).strip()


def env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or value.startswith("replace_"):
        raise SyncError(f"Variable {name} missing in .env or GitHub Secrets.")
    return value


def env_any(*names: str, required: bool = True) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value and not value.startswith("replace_"):
            return value
    if required:
        joined = " or ".join(names)
        raise SyncError(f"Variable {joined} missing in .env or GitHub Secrets.")
    return ""


def request(session: requests.Session, method: str, url: str, **kwargs: Any) -> Any:
    for attempt in range(4):
        try:
            response = session.request(method, url, timeout=35, **kwargs)
            if response.status_code not in {429, 500, 502, 503, 504}:
                response.raise_for_status()
                return response.json() if response.content else None
            if attempt == 3:
                response.raise_for_status()
        except requests.RequestException:
            if attempt == 3:
                raise
        time.sleep(2**attempt)
    raise AssertionError("unreachable")


def canonical_column(columns: Iterable[Any], aliases: Iterable[str]) -> str | None:
    wanted = {normalise(alias) for alias in aliases}
    for column in columns:
        if normalise(column) in wanted:
            return str(column)
    for column in columns:
        candidate = normalise(column)
        if any(alias in candidate or candidate in alias for alias in wanted):
            return str(column)
    return None


def number(value: Any) -> int | None:
    if pd.isna(value):
        return None
    digits = re.sub(r"[^0-9]", "", str(value))
    return int(digits) if digits else None


def year(value: Any) -> int | None:
    found = re.search(r"(?:19|20)\d{2}", str(value))
    return int(found.group()) if found else None


def read_cnc(session: requests.Session, url: str) -> list[Film]:
    response = session.get(url, timeout=45)
    response.raise_for_status()
    source = REPORTS / "cnc-source.xlsx"
    source.write_bytes(response.content)
    sheets = pd.read_excel(source, sheet_name=None, header=None)
    films: dict[str, Film] = {}
    for sheet_name, raw in sheets.items():
        # The official workbook has one tab per year plus a summary/navigation tab.
        if not re.fullmatch(r"(?:19|20)\d{2}", str(sheet_name)):
            continue
        header_at = next((i for i in range(min(20, len(raw))) if canonical_column(raw.iloc[i].tolist(), ["titre", "titre du film"])), None)
        if header_at is None:
            continue
        frame = raw.iloc[header_at + 1 :].copy()
        frame.columns = raw.iloc[header_at].tolist()
        title_col = canonical_column(frame.columns, ["titre", "titre du film"])
        date_col = canonical_column(frame.columns, ["date de sortie", "sortie", "année", "annee"])
        admissions_col = canonical_column(frame.columns, ["entrées", "entrees", "nombre d entrées", "nombre d entrees"])
        if not title_col:
            continue
        for _, row in frame.iterrows():
            title = str(row[title_col]).strip()
            if not title or title.lower() == "nan":
                continue
            admissions = number(row[admissions_col]) if admissions_col else None
            # The CNC source is already the >1m list; an explicit lower value is ignored.
            if admissions is not None and admissions < 1_000_000:
                continue
            film = Film(title=title, year=year(row[date_col]) if date_col else None, admissions=admissions)
            films[film.key] = film
    if not films:
        raise SyncError("No films found: the CNC XLSX layout may have changed; inspect reports/cnc-source.xlsx.")
    return sorted(films.values(), key=lambda f: (f.year or 0, f.title))


def cnc_last_update(session: requests.Session, url: str) -> str:
    payload = request(session, "GET", url)
    value = payload.get("last_update") if isinstance(payload, dict) else None
    if not value:
        raise SyncError("CNC dataset metadata has no last_update field.")
    return str(value)


def previous_cnc_update() -> str:
    if not STATE_FILE.exists():
        return ""
    payload = json.loads(STATE_FILE.read_text())
    return str(payload.get("last_update", "")) if isinstance(payload, dict) else ""


def write_cnc_update(value: str) -> None:
    STATE_FILE.write_text(json.dumps({"last_update": value}, indent=2) + "\n")


def load_catalog() -> set[int] | None:
    if not CATALOG_FILE.exists():
        return None
    payload = json.loads(CATALOG_FILE.read_text())
    ids = payload.get("tmdb_ids") if isinstance(payload, dict) else None
    if not isinstance(ids, list) or not all(isinstance(value, int) for value in ids):
        raise SyncError("cnc-catalog.json must contain a tmdb_ids integer array.")
    return set(ids)


def write_catalog(update: str, ids: set[int]) -> None:
    CATALOG_FILE.write_text(json.dumps({"last_update": update, "tmdb_ids": sorted(ids)}, indent=2) + "\n")


def check_cnc_update(session: requests.Session) -> tuple[str, bool]:
    current = cnc_last_update(session, os.getenv("CNC_METADATA_URL") or DEFAULT_CNC_METADATA_URL)
    previous = previous_cnc_update()
    changed = current != previous
    needs_sync = changed or not CATALOG_FILE.exists()
    (REPORTS / "cnc-update.json").write_text(
        json.dumps({"current_update": current, "previous_update": previous, "changed": changed, "needs_sync": needs_sync}, indent=2) + "\n"
    )
    return current, needs_sync


def load_overrides() -> dict[str, int]:
    path = ROOT / "overrides.json"
    if not path.exists():
        return {}
    values = json.loads(path.read_text())
    if not isinstance(values, dict) or not all(isinstance(v, int) for v in values.values()):
        raise SyncError("overrides.json must map a film key to an integer TMDb ID.")
    return values


def resolve_tmdb(session: requests.Session, token: str, film: Film, overrides: dict[str, int]) -> Resolution:
    if film.key in overrides:
        return Resolution(film, "resolved_override", overrides[film.key])
    params: dict[str, Any] = {"query": search_title(film.title), "language": "fr-FR", "include_adult": "false"}
    if re.fullmatch(r"[0-9a-fA-F]{32}", token):
        params["api_key"] = token
        headers = {"accept": "application/json"}
    else:
        headers = {"Authorization": f"Bearer {token}", "accept": "application/json"}
    if film.year:
        params["year"] = film.year
    results = request(session, "GET", f"{TMDB}/search/movie", headers=headers, params=params).get("results", [])
    if not results and film.year:
        retry_params = dict(params)
        retry_params.pop("year")
        results = request(session, "GET", f"{TMDB}/search/movie", headers=headers, params=retry_params).get("results", [])
    candidates = [{"id": x.get("id"), "title": x.get("title"), "original_title": x.get("original_title"), "release_date": x.get("release_date")} for x in results[:5]]
    if len(candidates) == 1:
        return Resolution(film, "resolved", int(candidates[0]["id"]), candidates)
    same_year = [x for x in candidates if film.year and str(x.get("release_date", ""))[:4] == str(film.year)]
    if len(same_year) == 1:
        return Resolution(film, "resolved", int(same_year[0]["id"]), candidates)
    film_titles = title_variants(film.title)
    title_matches = [x for x in candidates if film_titles.intersection(title_variants(x.get("title", "")) | title_variants(x.get("original_title", "")))]
    year_matches = [x for x in title_matches if not film.year or str(x.get("release_date", ""))[:4] == str(film.year)]
    matches = year_matches if len(year_matches) == 1 else title_matches
    if len(matches) == 1:
        return Resolution(film, "resolved", int(matches[0]["id"]), candidates)
    if not candidates:
        return Resolution(film, "unresolved", candidates=candidates, note="TMDb returned no candidate")
    return Resolution(film, "ambiguous", candidates=candidates, note="No single exact title/year match")


def mdblist(session: requests.Session, key: str, method: str, path: str, **kwargs: Any) -> Any:
    params = dict(kwargs.pop("params", {}))
    params["apikey"] = key
    return request(session, method, f"{MDB}{path}", params=params, **kwargs)


def trakt_tmdb_ids(session: requests.Session, client_id: str, username: str, list_slug: str = "") -> set[int]:
    headers = {"trakt-api-version": "2", "trakt-api-key": client_id}
    ids: set[int] = set()
    page = 1
    endpoint = f"/users/{username}/lists/{list_slug}/items/movies" if list_slug else f"/users/{username}/watched/movies"
    while True:
        payload = request(
            session,
            "GET",
            f"{TRAKT}{endpoint}",
            headers=headers,
            params={"page": page, "limit": 1000},
        )
        items = payload if isinstance(payload, list) else []
        ids.update(
            int(item["movie"]["ids"]["tmdb"])
            for item in items
            if isinstance(item, dict)
            and isinstance(item.get("movie"), dict)
            and item["movie"].get("ids", {}).get("tmdb") is not None
        )
        if len(items) < 1000:
            return ids
        page += 1


def list_path(value: str) -> str:
    value = value.strip().rstrip("/")
    value = re.sub(r"^https?://mdblist\.com/lists/", "", value, flags=re.IGNORECASE)
    if value.isdigit():
        return f"/lists/{value}"
    if re.fullmatch(r"[^/]+/[^/]+", value):
        return f"/lists/{value}"
    raise SyncError("MDBList list ID must be numeric or in the form username/list-name.")


def numeric_list_id(session: requests.Session, key: str, value: str) -> int:
    if value.isdigit():
        return int(value)
    payload = mdblist(session, key, "GET", list_path(value))
    entries = payload if isinstance(payload, list) else [payload]
    list_id = entries[0].get("id") if entries and isinstance(entries[0], dict) else None
    if list_id is None:
        raise SyncError(f"Could not resolve MDBList output list: {value}")
    return int(list_id)


def list_tmdb_ids(payload: Any) -> set[int]:
    if isinstance(payload, dict):
        movies = payload.get("movies", payload.get("items", []))
    elif isinstance(payload, list):
        movies = payload
    else:
        movies = []
    return {int(item["id"]) for item in movies if isinstance(item, dict) and item.get("id") is not None}


def configured_source_lists() -> list[str]:
    """Read one or more MDBList URLs/paths from the environment."""
    raw = os.getenv("MDBLIST_SOURCE_LISTS", "")
    return [value.strip() for value in re.split(r"[,\n;]+", raw) if value.strip()]


def mdblist_source_tmdb_ids(session: requests.Session, key: str, lists: list[str]) -> set[int]:
    ids: set[int] = set()
    for source in lists:
        payload = mdblist(session, key, "GET", f"{list_path(source)}/items")
        source_ids = list_tmdb_ids(payload)
        logging.info("Source MDBList %s: %s film(s)", source, len(source_ids))
        ids.update(source_ids)
    return ids


def mdblist_watched_tmdb_ids(session: requests.Session, key: str, tmdb_ids: set[int]) -> set[int]:
    ids: set[int] = set()
    sorted_ids = sorted(tmdb_ids)
    for start in range(0, len(sorted_ids), 100):
        payload = mdblist(
            session,
            key,
            "POST",
            "/sync/state/movie/tmdb",
            json={"ids": sorted_ids[start : start + 100]},
        )
        movies = payload.get("items", []) if isinstance(payload, dict) else []
        ids.update(
            int(item["id"])
            for item in movies
            if isinstance(item, dict)
            and item.get("id") is not None
            and item.get("watched") is True
        )
    return ids


def modify_list(session: requests.Session, key: str, list_id: str, action: str, ids: set[int]) -> None:
    # Official MDBList static-list body: movie identifiers under the movies array.
    for batch in (list(sorted(ids))[i : i + 100] for i in range(0, len(ids), 100)):
        body = {"movies": [{"tmdb": tmdb_id} for tmdb_id in batch]}
        mdblist(session, key, "POST", f"{list_id}/items/{action}", json=body)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Report proposed changes without updating MDBList.")
    parser.add_argument("--check-update", action="store_true", help="Check whether the CNC dataset changed.")
    parser.add_argument("--force", action="store_true", help="Synchronize even when the CNC dataset did not change.")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    REPORTS.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    session = requests.Session()
    source_lists = configured_source_lists()
    if source_lists:
        current_update, needs_sync = "", False
        logging.info("Using %s configurable MDBList source list(s); CNC catalog is disabled for this run.", len(source_lists))
    else:
        current_update, needs_sync = check_cnc_update(session)
    logging.info("CNC last update: %s | catalog refresh needed: %s", current_update, needs_sync)
    if args.check_update:
        return 0
    if not needs_sync and not args.force:
        logging.info("CNC unchanged; reusing resolved catalog and checking watched status.")
    refresh_catalog = needs_sync or args.force
    tmdb_token, mdb_key = env("TMDB_API_TOKEN"), env("MDBLIST_API_KEY")
    watched_id = env_any("MDBLIST_WATCHED_LIST_ID", required=False)
    output_id = env_any("MDBLIST_OUTPUT_LIST_ID", "LIST_ID")
    trakt_client_id = env_any("TRAKT_CLIENT_ID", required=False)
    trakt_username = os.getenv("TRAKT_USERNAME", "").strip()
    trakt_list_slug = os.getenv("TRAKT_LIST_SLUG", "").strip()
    use_trakt = any((trakt_client_id, trakt_username, trakt_list_slug))
    if use_trakt and not all((trakt_client_id, trakt_username)):
        raise SyncError("TRAKT_CLIENT_ID and TRAKT_USERNAME must be set together.")
    if not use_trakt and watched_id:
        if watched_id == "7095":
            raise SyncError("MDBLIST_WATCHED_LIST_ID=7095 is a preset, not a MDBList list. Leave it empty to use your MDBList watchlist, or configure a real list.")
    if watched_id and watched_id == output_id:
        raise SyncError("Watched and output list IDs must be different.")
    if source_lists:
        source_ids = mdblist_source_tmdb_ids(session, mdb_key, source_lists)
        if not source_ids:
            raise SyncError("Configured MDBList source lists contain no TMDb films.")
        (REPORTS / "unresolved.json").write_text("[]\n")
        (REPORTS / "summary.json").write_text(json.dumps({"generated_at": datetime.now(UTC).isoformat(), "source_lists": source_lists, "source_count": len(source_ids), "resolved_count": len(source_ids), "unresolved_count": 0}, ensure_ascii=False, indent=2) + "\n")
    elif refresh_catalog:
        films = read_cnc(session, os.getenv("CNC_DATASET_URL") or env("CNC_DATASET_URL"))
        overrides = load_overrides()
        resolutions = [resolve_tmdb(session, tmdb_token, film, overrides) for film in films]
        unresolved = [r for r in resolutions if r.status in {"ambiguous", "unresolved"}]
        (REPORTS / "unresolved.json").write_text(json.dumps([asdict(r) for r in unresolved], ensure_ascii=False, indent=2))
        (REPORTS / "summary.json").write_text(json.dumps({"generated_at": datetime.now(UTC).isoformat(), "source_count": len(films), "resolved_count": len(films)-len(unresolved), "unresolved_count": len(unresolved)}, ensure_ascii=False, indent=2))
        # A partial source set could otherwise remove a previously valid output item.
        if unresolved:
            raise SyncError(f"{len(unresolved)} film(s) need review. See reports/unresolved.json; no MDBList update was made.")
        source_ids = {r.tmdb_id for r in resolutions if r.tmdb_id is not None}
    else:
        source_ids = load_catalog()
        if source_ids is None:
            raise SyncError("Resolved CNC catalog is missing; run once with --force.")
        (REPORTS / "unresolved.json").write_text("[]\n")
    if use_trakt:
        watched = trakt_tmdb_ids(session, trakt_client_id, trakt_username, trakt_list_slug)
    elif watched_id:
        watched = list_tmdb_ids(mdblist(session, mdb_key, "GET", f"{list_path(watched_id)}/items"))
    else:
        watched = mdblist_watched_tmdb_ids(session, mdb_key, source_ids)
    output = list_tmdb_ids(mdblist(session, mdb_key, "GET", f"{list_path(output_id)}/items"))
    desired = source_ids - watched
    additions, removals = desired - output, output - desired
    changes = {"add_tmdb_ids": sorted(additions), "remove_tmdb_ids": sorted(removals), "desired_count": len(desired), "fingerprint": hashlib.sha256(",".join(map(str, sorted(desired))).encode()).hexdigest()}
    (REPORTS / "changes.json").write_text(json.dumps(changes, indent=2))
    logging.info("Desired: %s | add: %s | remove: %s", len(desired), len(additions), len(removals))
    if not args.dry_run:
        output_list_path = f"/lists/{numeric_list_id(session, mdb_key, output_id)}"
        modify_list(session, mdb_key, output_list_path, "add", additions)
        modify_list(session, mdb_key, output_list_path, "remove", removals)
        if refresh_catalog and not source_lists:
            write_catalog(current_update, source_ids)
            write_cnc_update(current_update)
        logging.info("MDBList output list updated.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SyncError, requests.RequestException, ValueError) as exc:
        logging.error("%s", exc)
        raise SystemExit(2)
