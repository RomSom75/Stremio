import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from sync_trakt_mdblist import API, SyncError, apply, normalize, plan, read_history, states


class FakeAPI:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def call(self, path, params=None, body=None, **kwargs):
        self.calls.append((path, params, body))
        return next(self.replies)


def response(rows, missing=None):
    return {'items': [{'id': k, 'watched': v} for k, v in rows], 'not_found': missing or []}, {}


class SyncTests(unittest.TestCase):
    def test_pagination_uses_server_page_count_even_with_short_page(self):
        api = FakeAPI([([{'id': 1}], {'x-pagination-page': '1', 'x-pagination-page-count': '2'}),
                       ([{'id': 2}], {'x-pagination-page': '2', 'x-pagination-page-count': '2'})])
        self.assertEqual(len(read_history(api, 'alice', 'movies', '2026-09-25T00:00:00Z')), 2)
        self.assertEqual(api.calls[1][1]['page'], 2)

    def test_missing_pagination_stops_instead_of_truncating(self):
        with self.assertRaises(SyncError):
            read_history(FakeAPI([([], {})]), 'alice', 'movies', '')

    def test_duplicate_watches_collapse_and_use_episode_not_show_id(self):
        rows = [{'episode': {'ids': {'tmdb': 55}}, 'show': {'ids': {'tmdb': 99}},
                 'watched_at': date} for date in ['2026-09-20T00:00:00Z', '2026-09-24T00:00:00Z']]
        items, skipped = normalize(rows, 'episode')
        self.assertEqual(items, [{'ids': {'tmdb': 55}, 'watched_at': '2026-09-24T00:00:00+00:00'}])
        self.assertEqual(skipped, 0)

    def test_missing_mapping_is_counted(self):
        self.assertEqual(normalize([{'movie': {'ids': {'tmdb': None}}, 'watched_at': 'bad'}], 'movie'), ([], 1))

    def test_incomplete_destination_state_stops(self):
        with self.assertRaises(SyncError):
            states(FakeAPI([response([(1, True)])]), 'movie', [1, 2])

    def test_dry_plan_does_not_write_and_keeps_only_missing(self):
        items = [{'ids': {'tmdb': n}} for n in [1, 2, 3]]
        api = FakeAPI([response([(1, True), (2, False)], [3])])
        self.assertEqual(plan(api, 'movie', items), items[1:])
        self.assertTrue(all('/sync/state/' in call[0] for call in api.calls))

    def test_second_run_writes_nothing(self):
        items = [{'ids': {'tmdb': 1}, 'watched_at': '2026-09-24T00:00:00Z'}]
        api = FakeAPI([response([(1, False)]), ({'updated': {'movies': 1}, 'not_found': {}}, {}),
                       response([(1, True)]), response([(1, True)])])
        pending = plan(api, 'movie', items)
        self.assertEqual(apply(api, 'movie', pending), 1)
        self.assertEqual(plan(api, 'movie', items), [])
        writes = [call for call in api.calls if call[0] == '/sync/watched']
        self.assertEqual(len(writes), 1)

    def test_unresolved_write_is_not_reported_successful(self):
        api = FakeAPI([({'updated': {}, 'not_found': {'episodes': [55]}}, {})])
        with self.assertRaises(SyncError):
            apply(api, 'episode', [{'ids': {'tmdb': 55}}])

    def test_unconfirmed_write_stops(self):
        api = FakeAPI([({'updated': {}}, {}), response([(1, False)])])
        with self.assertRaises(SyncError):
            apply(api, 'movie', [{'ids': {'tmdb': 1}}])

    def test_write_network_error_is_not_retried_or_leaked(self):
        api = API('MDBList', 'https://api.mdblist.com', key='secret-value')
        error = HTTPError('https://api.mdblist.com/?apikey=secret-value', 503, 'secret-value', {}, None)
        with patch('sync_trakt_mdblist.urlopen', side_effect=error) as request:
            with self.assertRaises(SyncError) as caught:
                api.call('/sync/watched', body={}, retry_safe=False)
        self.assertEqual(request.call_count, 1)
        self.assertNotIn('secret-value', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
