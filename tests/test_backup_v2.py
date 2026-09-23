"""Backward compatible backups must preserve every kind in the current database."""
import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from app.db import Store, V1_KINDS, V2_KINDS, normalize_bundle
from app.backup_validation import validate_bundle


class V2BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'live.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def test_old_backups_are_upgraded_without_requiring_new_tables(self):
        legacy = {'format': 'trade-workbench', 'schema_version': 1,
                  'workspace': 'live', 'records': {k: [] for k in V1_KINDS},
                  'settings': {}}
        original = copy.deepcopy(legacy)
        validate_bundle(legacy)
        self.store.restore(legacy, 'live', Path(self.tmp.name) / 'backups')
        self.assertEqual(legacy, original)
        exported = self.store.export('live')
        self.assertEqual(exported['schema_version'], 2)
        for kind in V2_KINDS:
            self.assertEqual(exported['records'][kind], [])

    def test_unknown_or_missing_v2_tables_rejected(self):
        current = self.store.export('live')
        del current['records']['research_tasks']
        with self.assertRaises(ValueError):
            normalize_bundle(current)
        current = self.store.export('live')
        current['records']['unknown'] = []
        with self.assertRaises(ValueError):
            normalize_bundle(current)

    def test_pre_restore_backup_and_restart_include_new_records(self):
        task = self.store.save('research_tasks', {'title': 'Test task', 'status': 'paused',
                                'usage': {'search': 1, 'model': 0, 'pages': 2}})
        snapshot = self.store.export('live')
        empty = {**snapshot, 'records': {k: [] for k in snapshot['records']}}
        name = self.store.restore(empty, 'live', Path(self.tmp.name) / 'backups')
        old = json.loads((Path(self.tmp.name) / 'backups' / name).read_text())
        self.assertEqual(old['records']['research_tasks'][0], task)
        self.store.restore(snapshot, 'live', Path(self.tmp.name) / 'backups')
        reopened = Store(self.store.path)
        self.assertEqual(reopened.get('research_tasks', task['id']), task)


if __name__ == '__main__':
    unittest.main()
