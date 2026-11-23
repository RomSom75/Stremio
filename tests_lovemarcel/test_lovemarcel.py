import tempfile
import unittest
from pathlib import Path

from sync_lovemarcel import CatalogueParser, additions, list_item_tmdb_id, load_history, normalise, save, select_match, SyncError


class LoveMarcelTests(unittest.TestCase):
    def test_mdblist_lightweight_and_full_identifiers(self):
        for item in [{"tmdb": 123}, {"id": "m123", "tmdb": 123},
                     {"id": 123}, {"id": "m123", "ids": {"tmdb": 123}}]:
            self.assertEqual(list_item_tmdb_id(item), 123)
        for item in [{"id": "m123"}, {"tmdb": None, "id": 123}, {"tmdb": True}]:
            with self.assertRaises(SyncError):
                list_item_tmdb_id(item)

    def test_movies_shows_and_collections_are_distinct(self):
        page = CatalogueParser()
        page.feed('''<article data-content-id="advert" data-item-type="program" title="Ad"></article>
          <p id="catalogue-movies-count">3 contenus</p><div id="inf-catalog-container">
          <article data-content-id="1" data-item-type="program" title="Film &amp; doc">
          <span data-metadata="date_aaaa">2024</span></article>
          <article data-content-id="2" data-item-type="serie" title="Série">
          <span data-metadata="date_aaaa">2020</span></article>
          <article data-content-id="3" data-item-type="collection" title="Collection"></article>
          <a class="pagination__next d-none" href="/content?page=2">Suivant</a></div>''')
        self.assertEqual(set(page.items), {"1", "2", "3"})
        self.assertEqual(page.items["1"]["title"], "Film & doc")
        self.assertEqual(page.items["1"]["year"], 2024)
        self.assertEqual(page.items["2"]["type"], "shows")
        self.assertEqual(page.items["3"]["type"], "collections")
        self.assertEqual(page.total, 3)

    def test_removed_source_titles_and_watched_titles_are_preserved(self):
        history = {"old": {"type": "movies", "tmdb_id": 10},
                   "new": {"type": "movies", "tmdb_id": 20},
                   "show": {"type": "shows", "tmdb_id": 10},
                   "unknown": {"type": "movies", "tmdb_id": None}}
        # Existing contents include an unrelated movie and a watched title.
        changes = additions(history, {"movies": {10, 99}, "shows": set()})
        self.assertEqual(changes, {"movies": {20}, "shows": {10}})
        self.assertEqual(additions(history, {"movies": {20}, "shows": set()})["movies"], {10})

    def test_duplicate_tmdb_ids_are_added_once(self):
        history = {"a": {"type": "movies", "tmdb_id": 10}, "b": {"type": "movies", "tmdb_id": 10}}
        self.assertEqual(additions(history, {"movies": set(), "shows": set()})["movies"], {10})

    def test_history_round_trip_and_invalid_id_rejection(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.json"
            data = {"1": {"source_id": "1", "type": "shows", "tmdb_id": 45}}
            save(path, data)
            self.assertEqual(load_history(path), data)
            data["1"]["tmdb_id"] = True
            save(path, data)
            with self.assertRaises(SyncError):
                load_history(path)

    def test_unknown_source_type_fails(self):
        page = CatalogueParser()
        with self.assertRaises(SyncError):
            page.feed('<div id="inf-catalog-container"><article data-content-id="1" data-item-type="unknown" title="X">')

    def test_short_film_category_takes_precedence_over_serie_container(self):
        page = CatalogueParser()
        page.feed('''<div id="inf-catalog-container"><article data-content-id="1"
          data-item-type="serie" title="Court"><span data-metadata="category">Courts métrages</span>
          <span data-metadata="date_aaaa">2021</span></article></div>''')
        self.assertEqual(page.items["1"]["type"], "movies")

    def test_accents_and_punctuation(self):
        self.assertEqual(normalise("L’Âme sœur"), normalise("L'ame soeur"))

    def test_lone_wrong_title_and_wrong_year_are_unresolved(self):
        item = {"title": "Jonas", "year": 2018}
        for result in [{"id": 1, "title": "Another film", "release_date": "2018-01-01"},
                       {"id": 2, "title": "Jonas", "release_date": "2024-01-01"}]:
            self.assertIsNone(select_match(item, {"results": [result]})[0])

    def test_ambiguous_matches_need_review(self):
        item = {"title": "Jonas", "year": 2018}
        result = {"id": 1, "title": "Jonas", "release_date": "2018-01-01"}
        self.assertEqual(select_match(item, {"results": [result]})[0], 1)
        self.assertIsNone(select_match(item, {"results": [result, dict(result, id=2)]})[0])


if __name__ == "__main__":
    unittest.main()
