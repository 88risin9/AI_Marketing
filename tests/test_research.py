import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from flask import Flask
from app.db import Store
from app import research
from app import research_io as rio
from app import research_model as rm

CAPS = {'model': True, 'search': True, 'reader': True, 'missing': [], 'cost': None}
PAGE = {'url': 'https://buyer.example/about', 'title': 'Test Electrical',
        'text': 'Test Electrical is a distributor in Germany. We distribute miniature circuit breakers. Contact sales@buyer.example for information.',
        'links': ['https://buyer.example/contact'], 'emails': ['sales@buyer.example'], 'retrieved_at': '2026-09-22T10:00:00+08:00', 'issues': ['发布日期待核验'], 'kind': 'unknown'}

def analysis():
    return {'facts': {'name': {'value': 'Test Electrical', 'excerpt': 'Test Electrical is a distributor in Germany.'},
                      'country': {'value': 'Germany', 'excerpt': 'Test Electrical is a distributor in Germany.'},
                      'business_type': {'value': 'distributor', 'excerpt': 'Test Electrical is a distributor in Germany.'},
                      'product_signal': {'value': 'miniature circuit breakers', 'excerpt': 'We distribute miniature circuit breakers.'}},
            'classification': 'potential_buyer', 'classification_excerpt': 'Test Electrical is a distributor in Germany.',
            'source_kind': 'official', 'product_ids': [1], 'mismatches': [], 'unknowns': [], 'followup_queries': [], 'followup_urls': []}

class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.store = Store(Path(self.tmp.name) / 'live.sqlite3')
        self.store.save('products', {'name': '断路器', 'name_en': 'Miniature circuit breaker', 'model': 'M-16', 'category': 'MCB',
                                    'unit': 'pcs', 'specs': {'current_a': '16', 'private_note': 'SECRET'}, 'supplier': 'SECRET SUPPLIER', 'purchase_price': 'SECRET PRICE'})
        self.manager = research.ResearchManager({'live': self.store})
        self.task = self.manager.create('live', {'product_ids': [1], 'market': 'Germany', 'buyer_types': ['distributor'], 'test_market': True})
    def tearDown(self):
        self.manager.stop()
        for thread in self.manager.threads.values(): thread.join(3)
        self.tmp.cleanup()
    def run_task(self):
        self.manager.update('live', self.task['id'], lambda task: task.update(status='running'))
        self.manager._worker('live', self.task['id'])
        return self.store.get('research_tasks', self.task['id'])
    def test_product_context_allowlist(self):
        self.assertNotIn('SECRET', json.dumps(self.task['product_snapshots']))
    def test_invalid_limits_and_market(self):
        for payload in ({'market': ''}, {'model_limit': 41}, {'page_limit': -1}, {'search_limit': True}):
            with self.assertRaises(ValueError): self.manager.create('live', {'product_ids': [1], 'market': 'Germany', 'buyer_types': ['dealer'], **payload})
    def test_missing_config_preserves_task(self):
        with patch.object(research, 'capabilities', return_value={**CAPS, 'model': False, 'search': False, 'missing': ['模型未接入']}): result = self.run_task()
        self.assertEqual(result['status'], 'needs_configuration'); self.assertEqual(result['usage'], {'model': 0, 'search': 0, 'pages': 0})
        self.assertEqual(result['market'], 'Germany')
    def test_manual_url_reads_real_source_without_ai_claim(self):
        self.manager.update('live', self.task['id'], lambda task: task.update(url_queue=[PAGE['url']]))
        with patch.object(research, 'capabilities', return_value={**CAPS, 'model': False, 'search': False, 'missing': ['模型未接入']}), patch.object(research, 'read_page', return_value=PAGE), patch.object(rm, 'analyze_page') as model:
            result = self.run_task(); model.assert_not_called()
        card = self.store.all('prospects')[0]
        self.assertEqual(result['usage']['pages'], 1); self.assertEqual(card['provenance'], 'web'); self.assertFalse(card['ai_processed']); self.assertEqual(card['country'], '')
    def test_full_pipeline_and_contact_not_sent(self):
        with patch.object(research, 'capabilities', return_value=CAPS), patch.object(rm, 'plan', return_value={'queries': ['Germany MCB distributor'], 'gaps': []}), patch.object(research, 'search_web', return_value=[{'url': PAGE['url']}]), patch.object(research, 'read_page', return_value=PAGE), patch.object(rm, 'analyze_page', return_value=analysis()):
            result = self.run_task()
        self.assertEqual(result['status'], 'completed'); self.assertEqual(result['usage'], {'search': 1, 'model': 2, 'pages': 1})
        card = self.store.all('prospects')[0]
        self.assertEqual(card['country'], 'Germany'); self.assertEqual(card['fit']['level'], 'medium'); self.assertEqual(card['contact_status'], 'new')
        self.assertFalse(card['contacts'][0]['verified_deliverable']); self.assertEqual(research.statistics([card])['contacted'], 0)
    def test_duplicate_domain_and_name_merge_sources(self):
        card = research.build_card(self.task, PAGE, analysis()); first = self.manager.upsert_prospect('live', card)
        changed = {**card, 'website': 'https://www.buyer.example/contact', 'sources': [{**card['sources'][0], 'excerpt': 'Additional evidence'}]}
        second = self.manager.upsert_prospect('live', changed)
        self.assertEqual(first['id'], second['id']); self.assertEqual(len(self.store.all('prospects')), 1); self.assertGreater(len(second['sources']), len(first['sources']))
    def test_cross_page_enriches_missing_facts_and_match(self):
        initial = analysis(); initial['facts']['product_signal'] = {'value': '', 'excerpt': ''}; initial['product_ids'] = []
        card = self.manager.upsert_prospect('live', research.build_card(self.task, PAGE, initial))
        later = analysis(); later['facts']['country'] = {'value': '', 'excerpt': ''}; later['facts']['business_type'] = {'value': '', 'excerpt': ''}; later['classification'] = 'unknown'
        merged = self.manager.upsert_prospect('live', research.build_card(self.task, {**PAGE, 'url': 'https://buyer.example/products'}, later))
        self.assertEqual(card['id'], merged['id']); self.assertEqual(merged['country'], 'Germany'); self.assertEqual(merged['product_ids'], [1]); self.assertEqual(merged['fit']['level'], 'medium'); self.assertEqual(merged['evidence']['level'], 'medium')
    def test_manual_enriches_unknown_web_card(self):
        card = self.manager.upsert_prospect('live', research.build_card(self.task, PAGE))
        manual = research.empty_prospect('User Corrected Name', PAGE['url'], self.task['id'], 'manual')
        manual.update(country='Germany', business_type='dealer', product_ids=[1])
        merged = self.manager.upsert_prospect('live', manual)
        self.assertEqual(card['id'], merged['id']); self.assertEqual(merged['name'], 'User Corrected Name'); self.assertEqual(merged['country'], 'Germany'); self.assertEqual(merged['provenance'], 'manual'); self.assertTrue(merged['sources'])
    def test_conflicting_sources_flagged(self):
        card = research.build_card(self.task, PAGE, analysis()); self.manager.upsert_prospect('live', card)
        card['country'] = 'France'; merged = self.manager.upsert_prospect('live', card)
        self.assertEqual(merged['evidence']['level'], 'low'); self.assertTrue(any('不一致' in s for s in merged['unknowns']))
    def test_automatic_priority_requires_contact_evidence(self):
        page = {**PAGE, 'links': [], 'emails': []}
        card = research.build_card(self.task, page, analysis())
        self.assertEqual(card['fit']['level'], 'medium'); self.assertEqual(card['evidence']['level'], 'medium')
        self.assertEqual(card['contactability']['level'], 'low'); self.assertEqual(card['priority'], 'low')
        self.assertIn('公开联系入口', card['priority_reason'])
    def test_source_conflict_downgrades_automatic_priority(self):
        initial = research.build_card(self.task, PAGE, analysis())
        self.assertEqual(initial['priority'], 'medium')
        self.manager.upsert_prospect('live', initial)
        incoming = research.build_card(self.task, PAGE, analysis()); incoming['country'] = 'France'
        merged = self.manager.upsert_prospect('live', incoming)
        self.assertEqual(merged['evidence']['level'], 'low'); self.assertEqual(merged['priority'], 'low')
    def test_source_conflict_preserves_explicit_human_priority(self):
        initial = research.build_card(self.task, PAGE, analysis())
        initial['priority'] = 'high'; initial['history'] = [{'action': 'human_feedback', 'before': {'priority': 'medium'}, 'after': {'priority': 'high'}}]
        self.manager.upsert_prospect('live', initial)
        incoming = research.build_card(self.task, PAGE, analysis()); incoming['country'] = 'France'
        merged = self.manager.upsert_prospect('live', incoming)
        self.assertEqual(merged['priority'], 'high'); self.assertEqual(merged['suggested_priority'], 'low'); self.assertIn('保留人工设置', merged['priority_reason'])
    def test_competitor_and_irrelevant_never_matched(self):
        for kind in ('competitor', 'supplier', 'directory', 'irrelevant'):
            item = analysis(); item['classification'] = kind
            card = research.build_card(self.task, PAGE, item)
            self.assertEqual(card['product_ids'], []); self.assertEqual(card['fit']['level'], 'low'); self.assertEqual(card['priority'], 'low')
    def test_budget_counts_failed_search_and_retry(self):
        self.manager.update('live', self.task['id'], lambda task: task.update(plan_created=True, queries=['one'], search_limit=1))
        with patch.object(research, 'capabilities', return_value=CAPS), patch.object(research, 'search_web', side_effect=rio.ResearchError('搜索失败')) as search:
            result = self.run_task(); self.assertEqual(result['status'], 'failed'); self.assertEqual(result['usage']['search'], 1)
            result = self.run_task(); self.assertEqual(result['status'], 'completed'); self.assertEqual(search.call_count, 1)
    def test_model_failure_keeps_page_and_retry_reuses(self):
        self.manager.update('live', self.task['id'], lambda task: task.update(url_queue=[PAGE['url']]))
        with patch.object(research, 'capabilities', return_value=CAPS), patch.object(research, 'read_page', return_value=PAGE) as reader, patch.object(rm, 'analyze_page', side_effect=rio.ResearchError('模型错误')):
            result = self.run_task()
        self.assertEqual(result['status'], 'failed'); self.assertEqual(len(result['documents']), 1); self.assertEqual(result['usage']['model'], 1)
        with patch.object(research, 'capabilities', return_value=CAPS), patch.object(research, 'read_page') as reader, patch.object(rm, 'analyze_page', return_value=analysis()), patch.object(rm, 'plan', return_value={'queries': ['one'], 'gaps': []}), patch.object(research, 'search_web', return_value=[]): result = self.run_task(); reader.assert_not_called()
        self.assertEqual(len(self.store.all('prospects')), 1)
    def test_reader_failure_does_not_discard_prior_card(self):
        card = self.manager.upsert_prospect('live', research.build_card(self.task, PAGE)); self.manager.attach('live', self.task['id'], card)
        self.manager.update('live', self.task['id'], lambda task: task.update(url_queue=['https://missing.example/']))
        with patch.object(research, 'capabilities', return_value={**CAPS, 'model': False, 'search': False, 'missing': ['缺配置']}), patch.object(research, 'read_page', side_effect=rio.ResearchError('网页失败')): result = self.run_task()
        self.assertEqual(result['prospect_ids'], [card['id']]); self.assertEqual(len(self.store.all('prospects')), 1); self.assertEqual(len(result['read_failures']), 1)
    def test_cancel_inflight_saves_response_then_stops(self):
        entered = threading.Event(); release = threading.Event()
        self.manager.update('live', self.task['id'], lambda task: task.update(url_queue=[PAGE['url']]))
        def reader(url): entered.set(); release.wait(3); return PAGE
        with patch.object(research, 'capabilities', return_value=CAPS), patch.object(research, 'read_page', side_effect=reader), patch.object(rm, 'analyze_page') as model:
            self.manager.start('live', self.task['id']); self.assertTrue(entered.wait(2))
            self.manager.cancel('live', self.task['id']); release.set()
            self.manager.threads[('live', self.task['id'])].join(3); model.assert_not_called()
        result = self.store.get('research_tasks', self.task['id'])
        self.assertEqual(result['status'], 'cancelled'); self.assertEqual(len(result['documents']), 1); self.assertEqual(result['usage']['pages'], 1)
    def test_restart_pauses_running_and_preserves_budget(self):
        self.manager.update('live', self.task['id'], lambda task: task.update(status='running', usage={'search': 1, 'model': 2, 'pages': 1}))
        recovered = research.ResearchManager({'live': Store(self.store.path)})
        self.assertEqual(self.store.get('research_tasks', self.task['id'])['status'], 'paused'); self.assertEqual(self.store.get('research_tasks', self.task['id'])['usage']['model'], 2)
        recovered.stop()
    def test_backup_budget_tampering_rejected(self):
        records = self.store.export('live')['records']; research.validate_research_backup(records)
        records['research_tasks'][0]['usage']['model'] = 100
        with self.assertRaises(ValueError): research.validate_research_backup(records)
    def test_manual_material_has_no_supply_commitment(self):
        card = {**research.build_card(self.task, PAGE, analysis()), 'id': 1}
        output = research.build_material(card, self.task['product_snapshots'])
        self.assertNotIn('SECRET', json.dumps(output)); self.assertFalse(output['sent']); self.assertFalse(output['reviewed']); self.assertIn('preparing', output['body'])
    def test_material_routes_missing_model_no_charge_and_review_reset(self):
        app = Flask(__name__); manager = research.register_research(app, {'live': self.store}, lambda: 'live'); app.testing = True
        card = manager.upsert_prospect('live', research.build_card(self.task, PAGE, analysis()))
        client = app.test_client()
        with patch.object(research, 'capabilities', return_value={**CAPS, 'model': False}):
            response = client.post(f'/api/research/prospects/{card["id"]}/materials', json={'mode': 'live'})
        self.assertEqual(response.status_code, 400); self.assertEqual(self.store.get('research_tasks', self.task['id'])['usage']['model'], 0)
        response = client.post(f'/api/research/prospects/{card["id"]}/materials', json={'mode': 'manual'})
        self.assertEqual(response.status_code, 200); material = response.get_json(); self.assertEqual(material['provenance'], 'manual'); self.assertIn('miniature circuit breakers', material['body'])
        url = f'/api/research/materials/{material["id"]}'
        reviewed = client.put(url, json={'reviewed': True}).get_json(); self.assertTrue(reviewed['reviewed'])
        changed = client.put(url, json={'body': material['body'] + '\nEdited.'}).get_json(); self.assertFalse(changed['reviewed'])
        self.assertEqual(research.statistics(self.store.all('prospects'))['contacted'], 0)
        manager.stop()
    def test_manual_material_prefers_selected_human_product_quote(self):
        card = {**research.build_card(self.task, PAGE), 'id': 1}
        card['sources'] = [
            {'url': PAGE['url'], 'excerpt': 'Customer Services Sign in My Account Shopping basket', 'kind': 'unknown'},
            {'url': PAGE['url'], 'excerpt': 'B & C Type MCBs & RCBOs', 'kind': 'manual'},
        ]
        output = research.build_material(card, self.task['product_snapshots'])
        self.assertEqual(output['source_excerpt'], 'B & C Type MCBs & RCBOs'); self.assertIn('B & C Type MCBs & RCBOs', output['body']); self.assertNotIn('Sign in', output['body'])
        self.assertEqual(output['source_provenance'], 'manual'); self.assertIn('尚待核验', output['reason'])
    def test_raw_unanalysed_page_not_quoted_in_material(self):
        card = {**research.build_card(self.task, PAGE), 'id': 1}
        card['sources'] = [{'url': PAGE['url'], 'excerpt': 'Customer Services Sign in My Account Shopping basket', 'kind': 'unknown'}]
        output = research.build_material(card, self.task['product_snapshots'])
        self.assertEqual(output['source_excerpt'], ''); self.assertEqual(output['source_provenance'], 'none'); self.assertNotIn('Shopping basket', output['body'])
        self.assertIn('whether your company handles', output['body'])
    def test_manual_name_dedup_fills_previously_unknown_website(self):
        initial = research.empty_prospect('Test Electrical', '', self.task['id'], 'manual')
        original = self.manager.upsert_prospect('live', initial)
        incoming = research.empty_prospect('Test Electrical', PAGE['url'], self.task['id'], 'manual')
        merged = self.manager.upsert_prospect('live', incoming)
        self.assertEqual(merged['id'], original['id']); self.assertEqual(merged['website'], PAGE['url']); self.assertEqual(merged['domain'], 'buyer.example')
    def test_stats_denominators_and_no_data(self):
        self.assertIsNone(research.statistics([])['reply_rate'])
        card = research.build_card(self.task, PAGE); card.update(contact_status='replied', review_status='accepted')
        stats = research.statistics([card]); self.assertEqual(stats['contacted'], 1); self.assertEqual(stats['replied'], 1); self.assertEqual(stats['reply_rate'], 1)

class SourceValidationTests(unittest.TestCase):
    def test_citation_must_match_literal_source_value(self):
        with self.assertRaises(rio.ResearchError): rm.citation({'value': 'France', 'excerpt': 'Germany'}, 'Located in Germany')
        with self.assertRaises(rio.ResearchError): rm.citation({'value': 'France', 'excerpt': 'France'}, 'Located in Germany')
    def test_malformed_and_fake_links_rejected(self):
        item = analysis(); output = {**item['facts'], **{k: v for k, v in item.items() if k != 'facts'}}
        task = {'market': 'Germany', 'test_market': True, 'buyer_types': ['dealer'], 'confirmed_conditions': '', 'unknown_conditions': '', 'product_snapshots': [{'id': 1}]}
        output['followup_urls'] = ['https://invented.example/']
        with patch.object(rm, 'model_json', return_value=output):
            with self.assertRaises(rio.ResearchError): rm.analyze_page(task, PAGE)
    def test_public_ipv4_and_ipv6_pass(self):
        for family, address in ((socket.AF_INET, '8.8.8.8'), (socket.AF_INET6, '2001:4860:4860::8888')):
            with patch.object(socket, 'getaddrinfo', return_value=[(family, socket.SOCK_STREAM, 6, '', (address, 443))]): self.assertEqual(len(rio.public_addresses('public.example', 443)), 1)
    def test_private_dns_and_mixed_dns_blocked(self):
        for addresses in (['127.0.0.1'], ['10.1.1.1'], ['169.254.169.254'], ['::ffff:127.0.0.1'], ['8.8.8.8', '192.168.0.1']):
            with patch.object(socket, 'getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443)) for address in addresses]):
                with self.assertRaises(rio.ResearchError): rio.public_addresses('public.example', 443)
    def test_url_restrictions(self):
        for url in ('http://localhost/', 'http://127.0.0.1/', 'http://[::1]/', 'https://user:pass@example.com/', 'file:///etc/passwd', 'http://example.com:8080/', 'https://example.com\\@localhost/'):
            with self.assertRaises(rio.ResearchError): rio.canonical_url(url)
    def test_reader_extracts_bounded_text_and_links(self):
        with patch.object(rio, 'config', return_value={'reader': True, 'timeout': 15}), patch.object(rio, 'fetch_bytes', return_value=('https://public.example/', {'content-type': 'text/html'}, b'<html><title>Public</title><script>SECRET SCRIPT INSTRUCTIONS</script><p>Public Company distributes circuit breakers for electrical installers.</p><a href="/contact">Contact</a></html>')):
            page = rio.read_page('https://public.example/')
        self.assertNotIn('SECRET', page['text']); self.assertIn('https://public.example/contact', page['links'])
    def test_redirect_to_private_destination_blocked_before_second_connection(self):
        class Response:
            status = 302
            def getheader(self, name): return 'http://127.0.0.1/private'
        class Connection:
            sock = None
            def request(self, *args): pass
            def getresponse(self): return Response()
            def close(self): pass
        with patch.object(rio, 'public_addresses', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))]), patch.object(rio, 'PinnedHTTP', return_value=Connection()) as connection:
            with self.assertRaises(rio.ResearchError): rio.fetch_bytes('https://public.example/')
            self.assertEqual(connection.call_count, 1)
    def test_model_adapter_uses_v1_credentials_store_false_and_bounded_payload(self):
        cfg = {'provider': 'openai', 'base_url': 'https://api.openai.com/v1', 'model': 'test-model', 'key': 'PRIVATE-SECRET', 'timeout': 15}
        with patch.object(rm.ai, '_config', return_value=cfg), patch.object(rm.ai, '_post', return_value={}) as post, patch.object(rm.ai, '_content', return_value={'queries': ['MCB Germany'], 'gaps': []}):
            rm.model_json('test', rm.PLAN_SCHEMA, {'market': 'Germany'}, 'Plan queries.')
        payload = post.call_args.args[1]
        self.assertFalse(payload['store']); self.assertNotIn('PRIVATE-SECRET', json.dumps(payload)); self.assertTrue(payload['text']['format']['strict'])
    def test_brave_and_tavily_api_payloads(self):
        for provider, response in (('brave', {'web': {'results': [{'url': 'https://public.example/', 'title': 'Public', 'description': 'Evidence'}]}}), ('tavily', {'results': [{'url': 'https://public.example/', 'title': 'Public', 'content': 'Evidence'}]})):
            with patch.object(rio, 'config', return_value={'provider': provider, 'key': 'test-private-secret', 'timeout': 15}), patch.object(rio, 'fetch_bytes', return_value=('', {}, json.dumps(response).encode())) as fetch:
                results = rio.search_web('circuit breakers Germany')
            self.assertNotIn('test-private-secret', json.dumps(results)); self.assertEqual(results[0]['snippet'], 'Evidence')
            args = fetch.call_args
            if provider == 'tavily': self.assertEqual(json.loads(args.kwargs['body'])['search_depth'], 'basic')

if __name__ == '__main__': unittest.main()
