import io
import unittest
from contextlib import redirect_stdout
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


DATE = '2026-09-24T00:00:00Z'
OLD = '2026-09-20T00:00:00Z'
NEW = '2026-09-25T00:00:00Z'


def item(key=1, date=DATE):
    return {'ids': {'tmdb': key}, 'watched_at': date}


def response(rows, missing=None):
    return {'items': [dict(id=row[0], watched=row[1],
                           last_watched_at=row[2] if len(row) > 2 else (DATE if row[1] else None))
                      for row in rows], 'not_found': missing or []}, {}


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
        items = [item(n) for n in [1, 2, 3]]
        api = FakeAPI([response([(1, True), (2, False)], [3])])
        self.assertEqual(plan(api, 'movie', items), items[1:])
        self.assertTrue(all('/sync/state/' in call[0] for call in api.calls))

    def test_second_run_writes_nothing(self):
        items = [{'ids': {'tmdb': 1}, 'watched_at': '2026-09-24T00:00:00Z'}]
        api = FakeAPI([response([(1, False)]), response([(1, False)]), ({'updated': {'movies': 1}, 'not_found': {}}, {}),
                       response([(1, True)]), response([(1, True)])])
        pending = plan(api, 'movie', items)
        self.assertEqual(apply(api, 'movie', pending), 1)
        self.assertEqual(plan(api, 'movie', items), [])
        writes = [call for call in api.calls if call[0] == '/sync/watched']
        self.assertEqual(len(writes), 1)

    def test_unresolved_write_is_not_reported_successful(self):
        api = FakeAPI([response([], [55]), ({'updated': {}, 'not_found': {'episodes': [55]}}, {})])
        with self.assertRaises(SyncError):
            apply(api, 'episode', [item(55)])

    def test_unconfirmed_write_stops(self):
        api = FakeAPI([response([(1, False)]), ({'updated': {}}, {}), response([(1, False)])])
        with self.assertRaises(SyncError):
            apply(api, 'movie', [item()])

    def test_plan_includes_older_newer_and_missing_dates(self):
        items = [item(n) for n in [1, 2, 3, 4]]
        api = FakeAPI([response([(1, True, OLD), (2, True, NEW), (3, True, None), (4, True, DATE)])])
        self.assertEqual(plan(api, 'movie', items), items[:3])
        self.assertEqual(len(api.calls), 1)
        self.assertIn('/sync/state/', api.calls[0][0])

    def test_equivalent_timezones_and_fractional_format_do_not_write(self):
        api = FakeAPI([response([(1, True, '2026-09-24T02:00:00.000+02:00')])])
        self.assertEqual(plan(api, 'episode', [item()]), [])

    def test_invalid_destination_date_stops_before_writes(self):
        for date in ['not-a-date', '2026-09-24T00:00:00']:
            with self.subTest(date=date), self.assertRaises(SyncError):
                plan(FakeAPI([response([(1, True, date)])]), 'movie', [item()])

    def test_normalize_compares_fractional_seconds_as_instants(self):
        rows = [{'movie': {'ids': {'tmdb': 1}}, 'watched_at': date}
                for date in [DATE, '2026-09-24T00:00:00.5Z']]
        self.assertEqual(normalize(rows, 'movie')[0][0]['watched_at'], '2026-09-24T00:00:00.500000+00:00')

    def test_newer_trakt_date_updates_without_reset(self):
        for kind in ['movie', 'episode']:
            with self.subTest(kind=kind):
                api = FakeAPI([response([(1, True, OLD)]), ({'updated': {kind + 's': 1}}, {}),
                               response([(1, True, DATE)])])
                self.assertEqual(apply(api, kind, [item()]), 1)
                self.assertEqual(api.calls[1], ('/sync/watched', None, {kind + 's': [item()]}))

    def test_older_trakt_date_resets_only_affected_item_before_write(self):
        for kind in ['movie', 'episode']:
            with self.subTest(kind=kind):
                api = FakeAPI([response([(1, True, NEW), (2, True, OLD)]),
                               ({'deleted': {kind + 's': 1}}, {}), response([(1, False)]),
                               ({'updated': {kind + 's': 2}}, {}), response([(1, True), (2, True)])])
                self.assertEqual(apply(api, kind, [item(1), item(2)]), 2)
                self.assertEqual(api.calls[1], ('/sync/watched/remove', None,
                                                {kind + 's': [{'ids': {'tmdb': 1}}]}))
                self.assertEqual(api.calls[3][2], {kind + 's': [item(1), item(2)]})
                self.assertFalse(any('history' in call[0] for call in api.calls))

    def test_date_still_different_after_write_is_not_success(self):
        api = FakeAPI([response([(1, True, OLD)]), ({'updated': {'movies': 1}}, {}),
                       response([(1, True, OLD)])])
        with self.assertRaises(SyncError):
            apply(api, 'movie', [item()])

    def test_unconfirmed_date_is_reported_for_retry_in_scheduled_mode(self):
        unresolved = []
        api = FakeAPI([response([(1, True, OLD)]), ({'updated': {'movies': 1}}, {}),
                       response([(1, True, OLD)])])
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(apply(api, 'movie', [item()], strict=False, unresolved=unresolved), 0)
        self.assertIn('::warning::movie:', output.getvalue())
        self.assertEqual(unresolved, [('movie', 1)])

    def test_concurrent_sync_already_aligned_date_skips_write(self):
        api = FakeAPI([response([(1, True)])])
        self.assertEqual(apply(api, 'movie', [item()]), 1)
        self.assertEqual(len(api.calls), 1)

    def test_reset_is_verified_from_state_instead_of_counter_shape(self):
        for summary in [{'deleted': 1}, {'deleted': {'movies': 1}},
                        {'deleted': 1, 'errors': {'movies': [], 'episodes': []}}, {}]:
            with self.subTest(summary=summary):
                api = FakeAPI([response([(1, True, NEW)]), (summary, {}), response([(1, False)]),
                               ({'updated': {'movies': 1}}, {}), response([(1, True)])])
                self.assertEqual(apply(api, 'movie', [item()]), 1)
                self.assertEqual(api.calls[2][0], '/sync/state/movie/tmdb')

    def test_reset_with_alternative_counter_still_stops_if_status_unchanged(self):
        api = FakeAPI([response([(1, True, NEW)]), ({'deleted': 1}, {}), response([(1, True, NEW)])])
        with self.assertRaises(SyncError):
            apply(api, 'movie', [item()])
        self.assertFalse(any(call[0] == '/sync/watched' for call in api.calls))

    def test_empty_error_buckets_do_not_fail_confirmed_write(self):
        api = FakeAPI([response([(1, False)]),
                       ({'updated': {'movies': 1}, 'errors': {'movies': [], 'episodes': []},
                         'not_found': {'movies': [], 'episodes': []}}, {}), response([(1, True)])])
        self.assertEqual(apply(api, 'movie', [item()]), 1)

    def test_actual_errors_still_fail_even_with_confirmed_date(self):
        api = FakeAPI([response([(1, False)]),
                       ({'updated': {'movies': 1}, 'errors': {'movies': [1]}}, {}),
                       response([(1, True)])])
        with self.assertRaises(SyncError):
            apply(api, 'movie', [item()], strict=False)

    def test_unconfirmed_reset_stops_before_readding(self):
        api = FakeAPI([response([(1, True, NEW)]), ({'deleted': {'movies': 0}}, {}),
                       response([(1, True, NEW)])])
        with self.assertRaises(SyncError):
            apply(api, 'movie', [item()])
        self.assertFalse(any(call[0] == '/sync/watched' for call in api.calls))

    def test_interrupted_reset_is_restored_on_next_run(self):
        class InterruptedAPI(FakeAPI):
            def call(self, path, params=None, body=None, **kwargs):
                if path == '/sync/watched':
                    raise SyncError('Connection lost after resetting status')
                return super().call(path, params, body, **kwargs)
        api = InterruptedAPI([response([(1, True, NEW)]), ({'deleted': {'movies': 1}}, {}),
                              response([(1, False)])])
        with self.assertRaises(SyncError):
            apply(api, 'movie', [item()])
        retry = FakeAPI([response([(1, False)]), response([(1, False)]),
                         ({'updated': {'movies': 1}}, {}), response([(1, True)])])
        self.assertEqual(apply(retry, 'movie', plan(retry, 'movie', [item()])), 1)

    def test_long_quota_wait_reports_resume_time_without_leaking_credentials(self):
        api = API('MDBList', 'https://api.mdblist.com', key='secret-value')
        error = HTTPError('https://api.mdblist.com/?apikey=secret-value', 429,
                          'secret-value', {'Retry-After': '7200'}, None)
        with patch('sync_trakt_mdblist.urlopen', side_effect=error) as request, \
                patch('sync_trakt_mdblist.time.time', return_value=0), \
                patch('sync_trakt_mdblist.time.sleep') as sleep:
            with self.assertRaises(SyncError) as caught:
                api.call('/sync/state/movie/tmdb', body={'ids': [1]})
        self.assertIn('1970-01-01T02:00:00+00:00', str(caught.exception))
        self.assertIn('HTTP 429', str(caught.exception))
        self.assertNotIn('secret-value', str(caught.exception))
        request.assert_called_once()
        sleep.assert_not_called()

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
