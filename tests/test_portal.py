import copy
import io
import json
import unittest
import zipfile
from tempfile import TemporaryDirectory
from unittest.mock import patch

from openpyxl import Workbook

from app.portal import (create_buyer_app, generate_page_style, parse_purchase_list,
                        validate_page_backup)
from app.server import create_app


class PortalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.app = create_app(self.tmp.name)
        self.internal = self.app.test_client()
        self.buyer_app = create_buyer_app(self.tmp.name)
        self.buyer_app.config['SUBMISSION_RATE_LIMIT'] = 100
        self.buyer = self.buyer_app.test_client()
        self.store = self.app.config['STORES']['demo']
        self.headers = {'X-Local-Request': '1'}
        p = self.store.get('products', 1)
        p['supplier'] = 'PRIVATE_SUPPLIER_SECRET'
        p['purchase_price'] = '9123.45'
        p['specs']['internal_margin'] = 'PRIVATE_MARGIN_SECRET'
        p['specs']['supply_note'] = 'PRIVATE_STOCK_NOTE'
        self.store.save('products', p, 1)

    def tearDown(self):
        manager = self.app.extensions.get('research_manager')
        if manager and hasattr(manager, 'stop'):
            manager.stop()
        self.tmp.cleanup()

    def internal_api(self, path, data=None, method='post', code=200):
        kwargs = {'headers': self.headers}
        if data is not None:
            kwargs['json'] = data
        response = getattr(self.internal, method)(f'/api/{path}?workspace=demo', **kwargs)
        self.assertEqual(response.status_code, code, response.get_data(as_text=True))
        return response.json

    def page(self, approve=True, **kwargs):
        page = self.internal_api('buyer-pages', {'title': 'MCB sourcing enquiry', 'product_ids': [1, 3],
                                  'buyer_type': 'Distributor', **kwargs})
        if approve:
            page = self.internal_api(f'buyer-pages/{page["id"]}/approve', {
                'confirmed_public': True, 'expected_updated_at': page['updated_at']})
        return page

    def form(self, **kwargs):
        return {'name': 'Buyer Test', 'company': 'Fictional Buyer Ltd', 'email': 'buyer@example.test',
                'country': 'Test market', 'model': '0001-MCB', 'quantity': '1000', 'unit': 'pcs',
                'timing': 'within 30 days',
                'requirements': 'Please quote 1000 pcs MCB, 1P, 16 A, 230 V AC, C curve, 6 kA.',
                'consent': 'yes', 'submission_id': 'test-submission-001', **kwargs}

    def submit(self, page, form=None, csrf=None, **kwargs):
        if csrf is None:
            csrf = self.buyer.get(f'/api/public/pages/{page["token"]}').json['csrf_token']
        return self.buyer.post(f'/api/public/pages/{page["token"]}/inquiries',
                               data=form or self.form(),
                               headers={'Origin': 'http://localhost', 'X-Buyer-CSRF': csrf}, **kwargs)

    def test_explicit_approval_and_stale_review_guard(self):
        page = self.page(False)
        self.assertEqual(self.buyer.get('/p/' + page['token']).status_code, 404)
        self.internal_api(f'buyer-pages/{page["id"]}/approve', {}, code=400)
        changed = self.internal_api(f'buyer-pages/{page["id"]}', {'intro': 'Updated intro'}, method='put')
        self.internal_api(f'buyer-pages/{page["id"]}/approve', {
            'confirmed_public': True, 'expected_updated_at': page['updated_at']}, code=409)
        self.assertEqual(changed['status'], 'draft')
        approved = self.internal_api(f'buyer-pages/{page["id"]}/approve', {
            'confirmed_public': True, 'expected_updated_at': changed['updated_at']})
        self.assertEqual(self.buyer.get('/p/' + approved['token']).status_code, 200)

    def test_public_read_projection_and_no_internal_routes(self):
        page = self.page()
        response = self.buyer.get(f'/api/public/pages/{page["token"]}')
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        for secret in ('supplier', 'purchase_price', 'margin', 'PRIVATE_', '9123.45', 'customer_id', 'product_ids'):
            self.assertNotIn(secret, text)
        self.assertEqual(response.json['page']['products'][1]['specs']['voltage_v'], '')
        self.assertTrue(response.json['page']['demo'])
        self.assertTrue(response.json['local_only'])
        for path in ('/api/state', '/api/customers', '/api/inquiries/1', '/api/quotes/1/pdf',
                     '/api/research/state', '/api/backup', '/data/demo.sqlite3', '/.env',
                     '/assets/app.js', '/assets/research.js', '/static/app.js',
                     f'/api/public/pages/{page["token"]}/inquiries'):
            self.assertIn(self.buyer.get(path).status_code, (404, 405), path)
        html = self.buyer.get('/p/' + page['token']).get_data(as_text=True)
        self.assertIn('LOCAL PREVIEW', html)
        for asset in ('buyer.js', 'buyer.css'):
            with self.buyer.get('/assets/' + asset) as response:
                self.assertEqual(response.status_code, 200)

    def test_submission_hands_off_to_v1_without_exposing_ids(self):
        page = self.page()
        before = len(self.store.all('inquiries'))
        form = self.form(file=(io.BytesIO(b'model,quantity,unit\n000123,1000,pcs\n'), 'requirements.csv'))
        response = self.submit(page, form)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(set(response.json), {'ok', 'message'})
        self.assertNotIn('id', response.json)
        inquiry = self.store.all('inquiries')[0]
        self.assertEqual(len(self.store.all('inquiries')), before + 1)
        self.assertIn('000123 | 1000 | pcs', inquiry['original_text'])
        self.assertIn('Quantity: 1000', inquiry['original_text'])
        self.assertEqual(inquiry['status'], '待补充')
        self.assertEqual(inquiry['source'], 'buyer_page')
        self.assertIsNone(inquiry['analysis'])
        customer = self.store.get('customers', inquiry['customer_id'])
        self.assertEqual(customer['contact'], 'buyer@example.test')
        self.assertEqual(self.app.config['STORES']['live'].all('inquiries'), [])
        # Existing inquiry extraction still works explicitly from the internal app.
        analysed = self.internal_api(f'inquiries/{inquiry["id"]}/analyze', {'mode': 'demo'})
        self.assertIsNotNone(analysed['analysis'])
        self.assertEqual(analysed['original_text'], inquiry['original_text'])
        restarted = create_buyer_app(self.tmp.name).test_client()
        self.assertEqual(restarted.get(f'/api/public/pages/{page["token"]}').json['page'], page['public_snapshot'])
        self.assertEqual(self.store.all('inquiries')[0]['original_text'], inquiry['original_text'])

    def test_repeated_submit_idempotent_and_cross_workspace_query_ignored(self):
        page = self.page()
        before = len(self.store.all('inquiries'))
        csrf = self.buyer.get(f'/api/public/pages/{page["token"]}').json['csrf_token']
        for _ in range(2):
            response = self.buyer.post(f'/api/public/pages/{page["token"]}/inquiries?workspace=live',
                data=self.form(), headers={'Origin': 'http://localhost', 'X-Buyer-CSRF': csrf})
            self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(len(self.store.all('inquiries')), before + 1)
        self.assertEqual(self.app.config['STORES']['live'].all('inquiries'), [])

    def test_seller_title_filename_and_contact_metadata_cannot_become_demand(self):
        page = self.page(title='16 A miniature circuit breakers, 230 V')
        response = self.submit(page, self.form(
            name='Contact 32 A', company='400 V Test Company', email='private-buyer@example.test',
            model='', quantity='', unit='', timing='', requirements='Please send your catalogue.',
            file=(io.BytesIO(b'Please send catalogue information.'), 'catalogue_415V.txt')))
        self.assertEqual(response.status_code, 200, response.json)
        inquiry = self.store.all('inquiries')[0]
        for seller_or_metadata in ('16 A', '230 V', '32 A', '400 V', '415V', 'private-buyer@example.test'):
            self.assertNotIn(seller_or_metadata, inquiry['original_text'])
        self.assertEqual(inquiry['attachment_name'], 'catalogue_415V.txt')
        self.assertEqual(self.store.get('customers', inquiry['customer_id'])['contact'], 'private-buyer@example.test')
        analysed = self.internal_api(f'inquiries/{inquiry["id"]}/analyze', {'mode': 'demo'})
        for requirement in analysed['analysis']['requirements']:
            if requirement['field'] in ('current_a', 'voltage_v'):
                self.assertEqual(requirement['kind'], 'missing')
                self.assertEqual(requirement['value'], '')

    def test_csrf_origin_host_and_rate_limits(self):
        page = self.page()
        url = f'/api/public/pages/{page["token"]}/inquiries'
        csrf = self.buyer.get(f'/api/public/pages/{page["token"]}').json['csrf_token']
        for headers in ({}, {'Origin': 'http://localhost'},
                        {'Origin': 'https://evil.example', 'X-Buyer-CSRF': csrf},
                        {'Origin': 'http://localhost', 'X-Buyer-CSRF': csrf, 'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(self.buyer.post(url, data=self.form(), headers=headers).status_code, 403)
        second = self.page()
        self.assertEqual(self.submit(second, csrf=csrf).status_code, 403)
        self.assertEqual(self.buyer.get('/p/' + page['token'], headers={'Host': 'evil.example'}).status_code, 403)
        self.buyer_app.config['SUBMISSION_RATE_LIMIT'] = 1
        self.assertEqual(self.submit(page).status_code, 200)
        self.assertEqual(self.submit(page, self.form(submission_id='test-submission-002')).status_code, 429)

    def test_private_data_atomic_when_storage_fails(self):
        page = self.page()
        before_customers = self.store.all('customers')
        before_inquiries = self.store.all('inquiries')
        original = type(self.store).save_in
        def fail_inquiry(store, conn, kind, data, id=None):
            if kind == 'inquiries':
                raise RuntimeError('private test detail')
            return original(store, conn, kind, data, id)
        with patch('app.portal.Store.save_in', new=fail_inquiry):
            response = self.submit(page)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn('private test detail', str(response.json))
        self.assertEqual(self.store.all('customers'), before_customers)
        self.assertEqual(self.store.all('inquiries'), before_inquiries)

    def test_snapshot_history_and_human_changes_require_reapproval(self):
        page = self.page()
        original = copy.deepcopy(page['public_snapshot'])
        product = self.store.get('products', 1)
        product['model'] = 'UPDATED_LIBRARY_MODEL'
        self.store.save('products', product, 1)
        self.assertEqual(self.buyer.get(f'/api/public/pages/{page["token"]}').json['page'], original)
        edited = self.internal_api(f'buyer-pages/{page["id"]}', {
            'title': 'Updated page', 'status': 'approved', 'public_snapshot': {'purchase_price': 'leak'}}, method='put')
        self.assertEqual(edited['status'], 'draft')
        self.assertEqual(self.buyer.get('/p/' + page['token']).status_code, 404)
        self.assertEqual(edited['versions'][0]['snapshot'], original)
        approved = self.internal_api(f'buyer-pages/{page["id"]}/approve', {
            'confirmed_public': True, 'expected_updated_at': edited['updated_at']})
        self.assertEqual(approved['approval_count'], 2)
        self.assertEqual(approved['public_snapshot']['products'][0]['model'], 'UPDATED_LIBRARY_MODEL')
        self.assertEqual(approved['versions'][0]['snapshot'], original)
        self.internal_api(f'buyer-pages/{page["id"]}/unpublish', {})
        self.assertEqual(self.buyer.get('/p/' + page['token']).status_code, 404)

    def test_backup_and_corrupt_database_fail_closed(self):
        page = self.page()
        for mutate in (lambda p: p['draft_snapshot']['products'][0].update(supplier='secret'),
                       lambda p: p['public_snapshot']['products'][0]['specs'].update(cost='secret'),
                       lambda p: p['versions'][0]['snapshot'].update(customers=['private'])):
            bad = copy.deepcopy(page)
            mutate(bad)
            with self.assertRaises(ValueError):
                validate_page_backup(bad)
        bad = self.store.get('buyer_pages', page['id'])
        bad['public_snapshot']['products'][0]['supplier'] = 'SECRET'
        self.store.save('buyer_pages', bad, bad['id'])
        self.assertEqual(self.buyer.get(f'/api/public/pages/{page["token"]}').status_code, 404)

    def test_invalid_upload_preserves_all_existing_records(self):
        page = self.page()
        before = self.store.all('inquiries')
        before_customers = self.store.all('customers')
        response = self.submit(page, self.form(file=(io.BytesIO(b'model,qty\n=1+2,4\n'), 'bad.csv')))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.all('inquiries'), before)
        self.assertEqual(self.store.all('customers'), before_customers)

    def test_missing_model_and_model_failure_never_masquerade_as_ai(self):
        count = len(self.store.all('buyer_pages'))
        with patch('app.portal.ai.get_config', return_value={'configured': False}):
            self.internal_api('buyer-pages', {'title': 'AI page', 'product_ids': [1], 'mode': 'live'}, code=503)
        self.assertEqual(len(self.store.all('buyer_pages')), count)
        with patch('app.portal.ai.get_config', return_value={'configured': True}), \
             patch('app.portal.generate_page_style', side_effect=RuntimeError('sensitive provider output')):
            response = self.internal_api('buyer-pages', {'title': 'AI page', 'product_ids': [1], 'mode': 'live'}, code=503)
        self.assertNotIn('sensitive', str(response))
        self.assertEqual(len(self.store.all('buyer_pages')), count)
        self.assertEqual(self.store.all('activities')[0]['kind'], 'ai_failure')
        self.assertEqual(self.store.all('activities')[0]['model_calls'], 1)

    def test_custom_preview_port_and_manual_generation_label(self):
        self.app.config['BUYER_PORT'] = 9010
        page = self.page(False)
        self.assertTrue(page['url'].startswith('http://127.0.0.1:9010/p/'))
        self.assertEqual(page['generation_mode'], 'manual_template')
        self.assertEqual(page['model_calls'], 0)


class PurchaseListTest(unittest.TestCase):
    def xlsx(self, cells):
        book = Workbook()
        for address, value in cells.items():
            book.active[address] = value
        output = io.BytesIO()
        book.save(output)
        return output.getvalue()

    def test_csv_txt_and_xlsx_keep_original_identifiers(self):
        self.assertIn('000001 | 50 | PCS', parse_purchase_list('list.csv', b'model,quantity,unit\n000001,50,PCS\n'))
        self.assertEqual(parse_purchase_list('list.txt', b'Please quote model 000001'), 'Please quote model 000001')
        result = parse_purchase_list('list.xlsx', self.xlsx({'A1': 'model', 'B1': 'quantity', 'A2': '000001', 'B2': 50}))
        self.assertIn('000001 | 50', result)
        self.assertLess(len(result.splitlines()), 4)

    def test_formula_and_sparse_out_of_bounds_xlsx_rejected(self):
        for cells in ({'A1': '=SUM(1,2)'}, {'A205': 'not allowed'}, {'AN2': 'not allowed'}):
            with self.assertRaises(ValueError):
                parse_purchase_list('list.xlsx', self.xlsx(cells))
        # A deliberately false dimension must not hide cells after the allowed range.
        raw = self.xlsx({'A1': 'model', 'A1000': 'outside'})
        result = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(result, 'w') as output:
            for info in source.infolist():
                data = source.read(info.filename)
                if info.filename == 'xl/worksheets/sheet1.xml':
                    data = data.replace(b'ref="A1:A1000"', b'ref="A1:A1"')
                output.writestr(info, data)
        with self.assertRaises(ValueError):
            parse_purchase_list('list.xlsx', result.getvalue())

    def test_size_archive_rows_columns_and_empty_guards(self):
        for name, raw in [('list.txt', b'a' * 16001), ('list.csv', b'a\n' * 201),
                          ('list.csv', b','.join([b'a'] * 31)), ('list.csv', b'\n'),
                          ('list.exe', b'x'), ('list.txt', b'a' * (1024 * 1024 + 1))]:
            with self.assertRaises(ValueError):
                parse_purchase_list(name, raw)
        bomb = io.BytesIO()
        with zipfile.ZipFile(bomb, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('xl/worksheets/sheet1.xml', b'x' * (9 * 1024 * 1024))
        with self.assertRaises(ValueError):
            parse_purchase_list('list.xlsx', bomb.getvalue())


class PageModelTest(unittest.TestCase):
    def snapshot(self):
        return {'title': 'Draft', 'intro': '', 'buyer_type': 'Distributor', 'approved_at': '', 'demo': True,
                'products': [{'name_en': 'Circuit breaker', 'model': 'DEMO-1', 'category': 'MCB',
                    'specs': {k: '' for k in ('poles', 'current_a', 'voltage_v', 'curve', 'breaking_ka')},
                    'unit': 'pcs', 'source_updated_at': ''}]}

    def response(self, choice):
        return {'status': 'completed', 'output': [{'type': 'message', 'content': [
                {'type': 'output_text', 'text': json.dumps(choice)}]}]}

    def test_real_adapter_path_is_structured_and_grounded(self):
        cfg = {'key': 'test-only-never-live', 'model': 'test', 'provider': 'openai',
               'base_url': 'https://api.openai.com/v1', 'timeout': 5}
        choice = {'headline': 'requirements', 'opening': 'list', 'next_step': 'share_list', 'product_order': [0]}
        with patch('app.portal.ai._config', return_value=cfg), \
             patch('app.portal.ai._post', return_value=self.response(choice)) as post:
            draft, order = generate_page_style(self.snapshot())
        self.assertEqual(order, [0])
        self.assertEqual(draft['products'], self.snapshot()['products'])
        self.assertIn('confirmed individually', draft['intro'])
        args = post.call_args.args
        self.assertTrue(args[0].endswith('/responses'))
        self.assertFalse(args[1]['store'])
        self.assertNotIn('purchase_price', json.dumps(args[1]))
        self.assertNotIn('supplier', json.dumps(args[1]))

    def test_unknown_claim_or_product_in_model_output_rejected(self):
        cfg = {'key': 'test', 'model': 'test', 'provider': 'compatible', 'base_url': 'https://example.test', 'timeout': 5}
        for choice in ({'headline': 'requirements', 'opening': 'Certified stock now', 'next_step': 'share_list', 'product_order': [0]},
                       {'headline': 'models', 'opening': 'list', 'next_step': 'share_list', 'product_order': [10]}):
            body = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(choice)}}]}
            with patch('app.portal.ai._config', return_value=cfg), patch('app.portal.ai._post', return_value=body):
                with self.assertRaises(RuntimeError):
                    generate_page_style(self.snapshot())


if __name__ == '__main__':
    unittest.main()
