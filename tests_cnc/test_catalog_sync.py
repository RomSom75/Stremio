import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sync


class CatalogSyncTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name, path in [('ROOT', root), ('REPORTS', root / 'reports'),
                           ('STATE_FILE', root / 'state.json'), ('CATALOG_FILE', root / 'catalog.json')]:
            self.stack.enter_context(patch.object(sync, name, path))
        self.stack.enter_context(patch.dict(os.environ, {
            'MDBLIST_API_KEY': 'test-key', 'MDBLIST_OUTPUT_LIST_ID': '123',
            'TMDB_API_TOKEN': 'test-token', 'CNC_DATASET_URL': 'https://example.test/cnc.xlsx',
            # Old settings must have no effect, even when invalid.
            'MDBLIST_WATCHED_LIST_ID': '123', 'TRAKT_USERNAME': 'old-user',
            'MDBLIST_ADDITIONAL_LISTS': 'invalid-unused-setting',
        }, clear=True))
        self.stack.enter_context(patch.object(sync, 'load_dotenv'))
        self.stack.enter_context(patch.object(sys, 'argv', ['sync.py']))
        self.current = self.stack.enter_context(patch.object(sync, 'cnc_last_update', return_value='v1'))
        self.mdb = self.stack.enter_context(patch.object(sync, 'mdblist', return_value={'movies': [{'id': 2}]}))
        self.modify = self.stack.enter_context(patch.object(sync, 'modify_list'))
        self.read = self.stack.enter_context(patch.object(sync, 'read_cnc'))
        self.resolve = self.stack.enter_context(patch.object(sync, 'resolve_tmdb'))
        self.write_state(migrated=True)
        sync.write_catalog('v1', {1, 2})

    def write_state(self, migrated):
        state = {'last_update': 'v1'}
        if migrated:
            state['include_watched'] = True
        sync.STATE_FILE.write_text(json.dumps(state))

    def test_unchanged_dataset_does_not_contact_mdblist(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(sync.main(), 0)
        self.mdb.assert_not_called()
        self.modify.assert_not_called()
        self.read.assert_not_called()

    def test_migration_restores_full_catalog_once_without_reading_history(self):
        self.write_state(migrated=False)
        self.assertEqual(sync.main(), 0)
        self.assertEqual(self.mdb.call_args.args[2:], ('GET', '/lists/123/items'))
        self.assertEqual(self.mdb.call_count, 1)
        self.assertEqual(self.modify.call_args_list[0].args[-2:], ('add', {1}))
        self.assertEqual(self.modify.call_args_list[1].args[-2:], ('remove', set()))
        self.read.assert_not_called()
        self.assertTrue(json.loads(sync.STATE_FILE.read_text())['include_watched'])
        self.mdb.reset_mock()
        self.assertEqual(sync.main(), 0)
        self.mdb.assert_not_called()

    def test_new_cnc_version_updates_full_catalog_and_records_version(self):
        self.current.return_value = 'v2'
        film = sync.Film('A film', 2020, 1200000)
        self.read.return_value = [film]
        self.resolve.return_value = sync.Resolution(film, 'resolved', 3)
        self.assertEqual(sync.main(), 0)
        self.assertEqual(self.modify.call_args_list[0].args[-2:], ('add', {3}))
        self.assertEqual(self.modify.call_args_list[1].args[-2:], ('remove', {2}))
        self.assertEqual(json.loads(sync.STATE_FILE.read_text())['last_update'], 'v2')
        self.assertEqual(sync.load_catalog(), {3})

    def test_dry_run_does_not_write_or_complete_migration(self):
        self.write_state(migrated=False)
        before = sync.STATE_FILE.read_bytes()
        with patch.object(sys, 'argv', ['sync.py', '--dry-run']):
            self.assertEqual(sync.main(), 0)
        self.modify.assert_not_called()
        self.assertEqual(sync.STATE_FILE.read_bytes(), before)
        self.assertEqual(json.loads((sync.REPORTS / 'changes.json').read_text())['add_tmdb_ids'], [1])

    def test_unresolved_film_blocks_mdblist_and_state_changes(self):
        self.current.return_value = 'v2'
        film = sync.Film('Ambiguous film', 2020, 1200000)
        self.read.return_value = [film]
        self.resolve.return_value = sync.Resolution(film, 'ambiguous')
        before = sync.STATE_FILE.read_bytes()
        with self.assertRaises(sync.SyncError):
            sync.main()
        self.mdb.assert_not_called()
        self.modify.assert_not_called()
        self.assertEqual(sync.STATE_FILE.read_bytes(), before)

    def test_force_resynchronizes_even_when_cnc_unchanged(self):
        film = sync.Film('A film', 2020, 1200000)
        self.read.return_value = [film]
        self.resolve.return_value = sync.Resolution(film, 'resolved', 1)
        with patch.object(sys, 'argv', ['sync.py', '--force']):
            self.assertEqual(sync.main(), 0)
        self.read.assert_called_once()
        self.modify.assert_called()


if __name__ == '__main__':
    unittest.main()
