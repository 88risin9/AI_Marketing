"""Supplier imports replace fictional active products without destroying history."""
import copy
import csv
import io
import json
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.backup_validation import validate_bundle
from app.db import Store
from app.matching import match_products
from app.products import parse_import
from app.server import create_app


class ProductArchivalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = create_app(self.root / 'data')
        self.client = self.app.test_client()
        self.store = self.app.config['STORES']['demo']
        self.headers = {'X-Local-Request': '1'}
        self.supplier_product = {
            'supplier': 'Supplier from uploaded quotation', 'name': 'Contactor',
            'name_en': 'AC contactor', 'model': 'NC6-09', 'category': 'AC Contactor',
            'specs': {'current_a': '9', 'original_specification': '9 A'},
            'unit': 'pc', 'purchase_price': '24.525', 'currency': 'CNY',
            'source': 'Supplier quotation, page 1, EXW', 'updated_at': '2026-06-09',
        }

    def tearDown(self):
        self.app.extensions['research_manager'].stop()
        self.tmp.cleanup()

    def api(self, path, data=None, method='post', workspace='demo', code=200):
        response = getattr(self.client, method)(f'/api/{path}?workspace={workspace}',
                    json=data, headers=self.headers)
        self.assertEqual(response.status_code, code, response.get_data(as_text=True))
        return response.json

    def replace(self):
        return self.api('import/commit', {'rows': [self.supplier_product], 'replace_demo': True})

    def test_replacement_preserves_ids_business_history_and_complete_private_backup(self):
        # A real product in the test workspace must not be archived by replacement.
        real = self.api('products', self.supplier_product)
        # A DEMO-looking model alone is insufficient evidence of a fictional seed.
        not_fictional = self.api('products', {**self.supplier_product, 'model': 'DEMO-REAL'})
        task = self.api('research/tasks', {'product_ids': [1], 'market': 'Test market',
                    'buyer_types': ['distributor'], 'test_market': True})
        page = self.api('buyer-pages', {'title': 'Fictional local preview', 'product_ids': [1]})
        page = self.api(f'buyer-pages/{page["id"]}/approve', {'confirmed_public': True,
                    'expected_updated_at': page['updated_at']})
        quote = self.api('quotes', {'inquiry_id': 1, 'currency': 'USD', 'lines': [
                    {'product_id': 1, 'quantity': '100', 'unit_price': '2.50', 'fx_rate': '0.14'}]})
        before = self.store.export('demo')
        result = self.replace()
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['archived_count'], 3)
        self.assertGreater(result['products'][0]['id'], not_fictional['id'])
        self.assertEqual(result['products'][0]['purchase_price'], '24.525')
        self.assertEqual(result['products'][0]['source_updated_at'], '2026-06-09')
        backup = self.root / 'backups' / result['backup_file']
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        saved = json.loads(backup.read_text())
        validate_bundle(saved)
        self.assertEqual(saved['records'], before['records'])
        self.assertEqual(saved['settings'], before['settings'])
        after = self.store.export('demo')
        for kind in before['records']:
            if kind != 'products':
                self.assertEqual(after['records'][kind], before['records'][kind], kind)
        for old in before['records']['products'][:3]:
            archived = self.store.get('products', old['id'])
            self.assertTrue(archived['archived'])
            self.assertTrue(archived['archived_at'])
            self.assertEqual({k: v for k, v in archived.items()
                              if k not in ('archived', 'archived_at', 'updated_at')},
                             {k: v for k, v in old.items() if k != 'updated_at'})
        self.assertEqual(self.store.get('products', real['id']), real)
        self.assertEqual(self.store.get('products', not_fictional['id']), not_fictional)
        self.assertEqual(self.store.get('research_tasks', task['id']), before['records']['research_tasks'][0])
        self.assertEqual(self.store.get('buyer_pages', page['id'])['public_snapshot'], page['public_snapshot'])
        self.assertEqual(self.store.get('quotes', quote['id']), quote)
        state = self.api('state', method='get')
        self.assertEqual(len(state['products']), 6)
        self.assertEqual(sum(bool(p.get('archived')) for p in state['products']), 3)

    def test_regular_import_does_not_archive_and_replacing_twice_keeps_real_products(self):
        result = self.api('import/commit', {'rows': [self.supplier_product]})
        self.assertEqual(result['archived_count'], 0)
        self.assertIsNone(result['backup_file'])
        self.assertFalse(any(p.get('archived') for p in self.store.all('products')))
        first = self.replace()
        second = self.replace()
        self.assertEqual(first['archived_count'], 3)
        self.assertEqual(second['archived_count'], 0)
        self.assertFalse(self.store.get('products', result['products'][0]['id']).get('archived'))

    def test_invalid_batch_and_non_demo_replacement_leave_all_data_unchanged(self):
        before = self.store.export('demo')['records']
        self.api('import/commit', {'rows': [self.supplier_product, {'purchase_price': 'wrong'}],
                                 'replace_demo': True}, code=400)
        self.api('import/commit', {'rows': [self.supplier_product], 'replace_demo': 'yes'}, code=400)
        self.api('import/commit', {'rows': [self.supplier_product], 'replace_demo': True},
                 workspace='live', code=400)
        self.assertEqual(self.store.export('demo')['records'], before)
        self.assertEqual(self.app.config['STORES']['live'].all('products'), [])
        self.assertFalse((self.root / 'backups').exists())

    def test_failed_import_rolls_back_archival_and_keeps_pre_import_backup(self):
        before = self.store.export('demo')['records']
        original_save = Store.save_in

        def fail_new_product(store, conn, kind, data, id=None):
            if kind == 'products' and id is None:
                raise ValueError('Simulated import write failure')
            return original_save(store, conn, kind, data, id)

        with patch.object(Store, 'save_in', fail_new_product):
            self.api('import/commit', {'rows': [self.supplier_product], 'replace_demo': True}, code=400)
        self.assertEqual(self.store.export('demo')['records'], before)
        backups = list((self.root / 'backups').glob('*.json'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text())['records'], before)

    def test_backup_write_failure_prevents_replacement(self):
        before = self.store.export('demo')['records']
        with patch.object(Store, 'backup_in', side_effect=OSError('disk unavailable')):
            self.api('import/commit', {'rows': [self.supplier_product], 'replace_demo': True}, code=500)
        self.assertEqual(self.store.export('demo')['records'], before)

    def test_archived_flags_survive_product_edit_export_and_restore(self):
        self.replace()
        old = self.store.get('products', 1)
        updated = self.api('products/1', {'source': 'Updated historic source'}, method='put')
        self.assertTrue(updated['archived'])
        self.assertEqual(updated['archived_at'], old['archived_at'])
        bundle = self.store.export('demo')
        validate_bundle(bundle)
        self.store.restore(bundle, 'demo', self.root / 'backups')
        self.assertEqual(Store(self.store.path).get('products', 1), updated)
        invalid = copy.deepcopy(bundle)
        invalid['records']['products'][0]['archived'] = 'true'
        with self.assertRaises(ValueError):
            validate_bundle(invalid)

    def test_archived_products_cannot_be_selected_for_new_work(self):
        self.replace()
        result = match_products({}, self.store.all('products'))
        self.assertEqual([c['product_id'] for c in result['candidates']], [4])
        result = self.api('inquiries/1/matches', method='get')
        self.assertEqual([c['product_id'] for c in result['candidates']], [4])
        self.api('research/tasks', {'product_ids': [1], 'market': 'Test market',
                                  'buyer_types': ['distributor']}, code=400)
        self.api('buyer-pages', {'title': 'Cannot use archive', 'product_ids': [1]}, code=400)
        self.api('quotes', {'inquiry_id': 1, 'currency': 'USD', 'lines': [
            {'product_id': 1, 'quantity': '100', 'unit_price': '2.50'}]}, code=400)

    def test_supplier_model_variants_do_not_get_duplicate_warning(self):
        data = io.StringIO()
        writer = csv.writer(data)
        writer.writerow(['供应商', '型号', '分类', '额定电流(A)', '计量单位', '采购价', '采购币种'])
        writer.writerows([
            ['Example supplier', 'NC6', 'Contactor', '9', 'pc', '24.525', 'CNY'],
            ['Example supplier', 'NC6', 'Contactor', '12', 'pc', '27.000', 'CNY'],
            ['Example supplier', 'NC6', 'Contactor', '12', 'pc', '27.000', 'CNY'],
        ])
        preview = parse_import('variants.csv', data.getvalue().encode())
        self.assertEqual(preview['errors'], [])
        self.assertFalse(any(row['errors'] for row in preview['rows']))
        self.assertTrue(any('独立变体' in warning for warning in preview['rows'][1]['warnings']))
        self.assertFalse(any('重复记录' in warning for warning in preview['rows'][1]['warnings']))
        self.assertTrue(any('重复记录' in warning for warning in preview['rows'][2]['warnings']))
        self.assertEqual(preview['rows'][0]['product']['purchase_price'], '24.525')
        self.assertEqual(preview['rows'][1]['product']['purchase_price'], '27.000')


if __name__ == '__main__':
    unittest.main()
