#!/usr/bin/env python3
"""LoveMarcel → cumulative static MDBList catalogue. No deletion operations."""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
SOURCE = "https://www.lovemarcel.fr/content"
TMDB = "https://api.themoviedb.org/3"
MDB = "https://api.mdblist.com"
BUCKETS = ("movies", "shows")


class SyncError(RuntimeError):
    pass


def normalise(text):
    text = text.replace("’", "'").replace("œ", "oe").replace("Œ", "OE")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


class CatalogueParser(HTMLParser):
    """Only catalogue tiles, never promotional or search-result posters."""

    def __init__(self):
        super().__init__()
        self.items = {}
        self.next_url = None
        self.total = None
        self.active = False
        self.tile = None
        self.capture = None
        self.text = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id") == "inf-catalog-container":
            self.active = True
        if a.get("id") == "catalogue-movies-count":
            self.capture, self.text = "count", ""
        if self.active and tag == "article" and a.get("data-content-id"):
            kind = a.get("data-item-type")
            if kind not in {"program", "serie", "collection"} or not a.get("title"):
                raise SyncError("Unknown LoveMarcel tile format")
            self.tile = {"source_id": a["data-content-id"], "title": a["title"],
                         "type": {"serie": "shows", "program": "movies", "collection": "collections"}[kind], "year": None}
        if self.tile and a.get("data-metadata") == "date_aaaa":
            self.capture, self.text = "year", ""
        if self.active and "pagination__next" in a.get("class", "").split():
            self.next_url = a.get("href")

    def handle_data(self, data):
        if self.capture:
            self.text += data

    def handle_endtag(self, tag):
        if self.capture == "count" and tag == "p":
            m = re.search(r"(\d+)\s+contenu", self.text)
            if m:
                self.total = int(m[1])
            self.capture = None
        if self.capture == "year" and tag == "span":
            if self.tile and re.fullmatch(r"\s*(?:19|20)\d{2}\s*", self.text):
                self.tile["year"] = int(self.text)
            self.capture = None
        if tag == "article" and self.tile:
            self.items[self.tile["source_id"]] = self.tile
            self.tile = None


def api(session, method, url, **kwargs):
    for attempt in range(4):
        try:
            response = session.request(method, url, timeout=45, **kwargs)
        except requests.RequestException:
            # Do not include exception URLs: query strings may contain API keys.
            if attempt == 3:
                raise SyncError(f"Network failure: {urlparse(url).hostname}") from None
            time.sleep(2 ** attempt)
            continue
        if response.status_code in {429, 500, 502, 503, 504} and attempt < 3:
            delay = response.headers.get("Retry-After", "")
            time.sleep(min(int(delay), 60) if delay.isdigit() else 2 ** attempt)
            continue
        if not response.ok:
            raise SyncError(f"HTTP {response.status_code}: {urlparse(url).hostname}")
        return response
    raise SyncError("Request retries exhausted")


def fetch_catalogue(session):
    items, visited = {}, set()
    url, expected = SOURCE, None
    while url:
        if url in visited or len(visited) >= 100:
            raise SyncError("LoveMarcel pagination loop")
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "www.lovemarcel.fr" or parsed.path != "/content":
            raise SyncError("Unexpected LoveMarcel pagination URL")
        visited.add(url)
        page = CatalogueParser()
        page.feed(api(session, "GET", url).text)
        if not page.items or not (page.items.keys() - items.keys()):
            raise SyncError("Empty or repeated LoveMarcel page")
        if expected is None:
            expected = page.total
        items.update(page.items)
        if page.next_url:
            next_link = urlparse(urljoin(url, page.next_url))
            if next_link.scheme != "https" or next_link.netloc != "www.lovemarcel.fr" or next_link.path != "/content":
                raise SyncError("Unexpected LoveMarcel pagination URL")
            # The public site sometimes repeats the current page in its Next
            # link. Increment explicitly; the advertised total validates the end.
            url = SOURCE + "?" + urlencode({"o[0]": "-publication", "page": len(visited) + 1})
        else:
            url = None
    if not expected or len(items) != expected:
        raise SyncError(f"Incomplete LoveMarcel catalogue: {len(items)} / {expected}")
    return items


def positive_id(value):
    return type(value) is int and value > 0


def load_history(path):
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise SyncError("Invalid LoveMarcel history")
    for key, item in data.items():
        if not isinstance(item, dict) or item.get("source_id") != key or item.get("type") not in BUCKETS:
            raise SyncError("Invalid LoveMarcel history entry")
        if item.get("tmdb_id") is not None and not positive_id(item["tmdb_id"]):
            raise SyncError("Invalid TMDb identifier in history")
    return data


def resolve(session, token, item):
    if not item["year"]:
        return None, []  # Compilations without a year require manual review.
    kind = "tv" if item["type"] == "shows" else "movie"
    date_key = "first_air_date" if kind == "tv" else "release_date"
    title_key = "name" if kind == "tv" else "title"
    year_key = "first_air_date_year" if kind == "tv" else "year"
    params = {"query": item["title"], "language": "fr-FR", "include_adult": "true", year_key: item["year"]}
    headers = {}
    if re.fullmatch(r"[a-fA-F0-9]{32}", token):
        params["api_key"] = token
    else:
        headers["Authorization"] = f"Bearer {token}"
    payload = api(session, "GET", f"{TMDB}/search/{kind}", params=params, headers=headers).json()
    results = payload.get("results")
    if not isinstance(results, list):
        raise SyncError("Invalid TMDb search response")
    return select_match(item, payload, title_key, date_key)


def select_match(item, payload, title_key="title", date_key="release_date"):
    results = payload["results"]
    # A lone result or an identical year alone is insufficient evidence.
    matches = [x for x in results if positive_id(x.get("id"))
               and str(x.get(date_key, ""))[:4] == str(item["year"])
               and normalise(item["title"]) in {
                   normalise(x.get(title_key) or ""), normalise(x.get(f"original_{title_key}") or "")}]
    if len(matches) == 1 and payload.get("total_pages", 1) == 1:
        return matches[0]["id"], []
    return None, results


def mdb(session, key, method, path, **kwargs):
    params = dict(kwargs.pop("params", {}), apikey=key)
    return api(session, method, MDB + path, params=params, **kwargs)


def read_list(session, key, list_id):
    result = {kind: set() for kind in BUCKETS}
    # Offset is still supported by MDBList. Filter each bucket to avoid ambiguity
    # in the limit/offset behaviour of combined movie + show responses.
    for bucket, mediatype in (("movies", "movie"), ("shows", "show")):
        for offset in range(0, 100000, 1000):
            response = mdb(session, key, "GET", f"/lists/{list_id}/items", params={
                "mediatype": mediatype, "limit": 1000, "offset": offset, "extended": "ids_only"})
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get(bucket), list):
                raise SyncError("Invalid MDBList item response")
            ids = []
            for item in data[bucket]:
                if not isinstance(item, dict) or not positive_id(item.get("id")):
                    raise SyncError("Invalid MDBList identifier")
                ids.append(item["id"])
            if ids and not (set(ids) - result[bucket]):
                raise SyncError("Repeated MDBList page")
            result[bucket].update(ids)
            has_more = response.headers.get("X-Has-More", "").lower()
            if has_more == "false" or (len(ids) < 1000 and has_more != "true"):
                break
        else:
            raise SyncError("MDBList pagination limit exceeded")
    return result


def additions(history, existing):
    desired = {kind: set() for kind in BUCKETS}
    for item in history.values():
        if item.get("tmdb_id"):
            desired[item["type"]].add(item["tmdb_id"])
    return {kind: desired[kind] - existing[kind] for kind in BUCKETS}


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    temp.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply additions; default is simulation.")
    parser.add_argument("--catalogue-only", action="store_true", help="Extract public catalogue without API credentials.")
    args = parser.parse_args()
    if args.apply and args.catalogue_only:
        parser.error("--apply and --catalogue-only cannot be combined")
    load_dotenv(ROOT / ".env")
    session = requests.Session()
    session.headers["User-Agent"] = "LoveMarcel-catalogue-sync/1.0"
    reports = ROOT / "reports/lovemarcel"
    current = fetch_catalogue(session)
    save(reports / "source.json", current)
    print(f"LoveMarcel: {len(current)} titles extracted")
    if args.catalogue_only:
        return 0
    token, key = os.getenv("TMDB_API_TOKEN", "").strip(), os.getenv("MDBLIST_API_KEY", "").strip()
    if not token or not key:
        raise SyncError("TMDB_API_TOKEN and MDBLIST_API_KEY are required")
    history_path = ROOT / "lovemarcel-catalog.json"
    history = load_history(history_path)
    overrides = json.loads((ROOT / "lovemarcel-overrides.json").read_text())
    if not isinstance(overrides, dict) or not all(positive_id(v) for v in overrides.values()):
        raise SyncError("Overrides must map LoveMarcel source IDs to positive TMDb IDs")
    unresolved = []
    for source_id, item in current.items():
        if item["type"] == "collections":
            continue  # Editorial groupings have no single TMDb movie/show ID.
        old = history.get(source_id, {})
        if old and old.get("type") != item["type"]:
            raise SyncError(f"Source type changed for {source_id}; review history")
        tmdb_id = overrides.get(source_id) or old.get("tmdb_id")
        candidates = []
        if not tmdb_id:
            tmdb_id, candidates = resolve(session, token, item)
        history[source_id] = dict(item, tmdb_id=tmdb_id)
        if not tmdb_id:
            unresolved.append(dict(item, candidates=candidates))
    save(reports / "unresolved.json", unresolved)
    save(reports / "proposed-catalog.json", history)
    target = os.getenv("LOVEMARCEL_LIST_ID", "romain85/lovemarcel").strip()
    if not re.fullmatch(r"(?:[1-9]\d*|[\w-]+/[\w-]+)", target):
        raise SyncError("LOVEMARCEL_LIST_ID must be a numeric ID or username/slug")
    info = mdb(session, key, "GET", f"/lists/{target}").json()
    if isinstance(info, list) and len(info) == 1:
        info = info[0]
    if not isinstance(info, dict) or not positive_id(info.get("id")):
        raise SyncError("Cannot resolve target MDBList list")
    list_id = info["id"]
    existing = read_list(session, key, list_id)
    changes = additions(history, existing)
    save(reports / "changes.json", {**{k: sorted(v) for k, v in changes.items()},
                                   "unresolved_count": len(unresolved), "apply": args.apply})
    print(f"Additions: {len(changes['movies'])} movies, {len(changes['shows'])} shows; {len(unresolved)} unresolved")
    if not args.apply:
        return 0
    # Persist discoveries before any remote writes. A failed write can be retried
    # from the cumulative history, including titles since removed from the site.
    save(history_path, history)
    for kind in BUCKETS:
        ids = sorted(changes[kind])
        for start in range(0, len(ids), 100):
            mdb(session, key, "POST", f"/lists/{list_id}/items/add",
                json={kind: [{"tmdb": value} for value in ids[start:start + 100]]})
    if any(changes.values()):
        remaining = additions(history, read_list(session, key, list_id))
        if any(remaining.values()):
            raise SyncError("MDBList additions not fully confirmed; see reports and retry")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SyncError, ValueError, OSError) as exc:
        print(f"LoveMarcel sync failed: {exc}")
        raise SystemExit(2)
