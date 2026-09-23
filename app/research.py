"""Persistent, budgeted research jobs and human-owned buyer development records."""
import json
import re
import threading
from contextlib import contextmanager
from urllib.parse import urlsplit
from flask import jsonify, request
from .db import now
from .research_io import ResearchError, capabilities, canonical_url, domain, read_page, search_web
from . import research_model as model

STATUSES = ('draft', 'running', 'needs_configuration', 'paused', 'cancelled', 'failed', 'completed')
CLASSIFICATIONS = ('potential_buyer', 'supplier', 'competitor', 'directory', 'unknown', 'irrelevant')
REVIEW_STATUSES = ('pending', 'accepted', 'excluded')
CONTACT_STATUSES = ('new', 'contacted', 'replied', 'qualified', 'unsuitable')
TECH_KEYS = ('poles', 'current_a', 'voltage_v', 'curve', 'breaking_ka')
LIMITS = {'target_count': (3, 20), 'search_limit': (5, 20), 'model_limit': (10, 40), 'page_limit': (10, 50)}

class Cancelled(Exception): pass
class BudgetReached(Exception): pass

def text(value, label, maxlen=3000, required=False):
    if not isinstance(value, str) or len(value) > maxlen or (required and not value.strip()):
        raise ValueError(f'{label}格式不正确或超过长度限制。')
    return value.strip()

def public_product(product):
    return {**{key: product.get(key, '') for key in ('id', 'name', 'name_en', 'model', 'category', 'unit')},
            'specs': {key: str(value) for key, value in product.get('specs', {}).items() if key in TECH_KEYS},
            'source_updated_at': product.get('source_updated_at', '')}

def canonical_name(name): return re.sub(r'[^\w]', '', name.casefold(), flags=re.UNICODE)

def event(task, stage, message):
    task.setdefault('events', []).append({'at': now(), 'stage': stage, 'message': message})
    task['events'] = task['events'][-300:]

def task_view(task):
    return {**{key: value for key, value in task.items() if key != 'documents'},
            'documents': [{key: value for key, value in doc.items() if key not in ('text', 'links', 'emails')} for doc in task.get('documents', [])]}

def empty_prospect(name, website, task_id, provenance):
    return {'name': name, 'website': website, 'domain': domain(website) if website else '', 'country': '', 'business_type': '',
            'classification': 'unknown', 'fit': {'level': 'unknown', 'reasons': ['尚无足够产品经营证据。']},
            'evidence': {'level': 'low', 'reasons': ['待核验企业身份及业务资料。']},
            'contactability': {'level': 'low', 'reasons': ['未确认官方联系方式。']}, 'priority': 'low',
            'why_fit': '待核验，不代表已确认买家。', 'product_ids': [], 'demand_hypothesis': '需求未知，未确认采购意向、数量或预算。',
            'unknowns': ['企业身份、采购职责与实际需求待确认。'], 'mismatches': [], 'contacts': [], 'sources': [],
            'next_action': '核验官网及产品经营范围，再决定是否联系。', 'provenance': provenance, 'task_ids': [task_id],
            'review_status': 'pending', 'review_reason': '', 'contact_status': 'new', 'contact_note': '',
            'history': [], 'needs_review': True, 'facts': {}, 'ai_processed': False}

def source_from(page, excerpt, kind='unknown'):
    return {'url': page['url'], 'title': page.get('title', ''), 'excerpt': excerpt,
            'retrieved_at': page['retrieved_at'], 'kind': kind, 'issues': page.get('issues', [])}

def apply_priority(card, preserve_manual=False):
    sufficient = (
        card['classification'] == 'potential_buyer'
        and card['fit']['level'] in ('medium', 'high')
        and card['evidence']['level'] in ('medium', 'high')
        and card['contactability']['level'] in ('medium', 'high')
    )
    suggestion = 'medium' if sufficient else 'low'
    card['suggested_priority'] = suggestion
    if not preserve_manual: card['priority'] = suggestion
    criteria = '只有同时具备潜在买家分类、中等以上产品相关度、中等以上证据及可用公开联系入口才建议中；任何一项不足或证据冲突则为低。'
    card['priority_reason'] = ('保留人工设置；' if preserve_manual else '') + '本次自动建议：' + ('中' if sufficient else '低') + '。' + criteria
    return card


def build_card(task, page, analysis=None):
    website = canonical_url(page['url'])
    card = empty_prospect(domain(website) + '（名称待核验）', website, task['id'], 'web')
    if not analysis:
        card['sources'] = [source_from(page, page['text'][:800])]
        card['unknowns'].append('已真实读取网页；模型未接入，尚未提取企业事实。')
        return apply_priority(card)
    facts = analysis['facts']; card['facts'] = {key: {**fact, 'source_url': page['url']} for key, fact in facts.items()}
    for key in ('name', 'country', 'business_type'):
        if facts[key]['value']: card[key] = facts[key]['value']
    card['classification'] = analysis['classification']; card['product_ids'] = analysis['product_ids']; card['ai_processed'] = True
    card['unknowns'] = list(dict.fromkeys(analysis['unknowns'] + ['采购意向、数量、预算及采购决策人尚未确认。', '资质、库存、销售价格及交期须人工确认。', '网页发布日期、当前经营情况和官网归属待人工复核。']))
    for key, label in (('name', '企业名称'), ('country', '所在地'), ('business_type', '经营业务'), ('product_signal', '相关产品线索')):
        if not facts[key]['value']: card['unknowns'].append(label + '缺少直接证据。')
    card['mismatches'] = analysis['mismatches']
    quotes = list(dict.fromkeys([fact['excerpt'] for fact in facts.values() if fact['excerpt']] + ([analysis['classification_excerpt']] if analysis['classification_excerpt'] else [])))
    card['sources'] = [source_from(page, quote, analysis['source_kind']) for quote in quotes] or [source_from(page, page['text'][:500])]
    classification = card['classification']
    relevant = bool(card['product_ids'] and facts['product_signal']['value'])
    if classification in ('supplier', 'competitor', 'directory', 'irrelevant'):
        card['product_ids'] = []
        card['mismatches'].append('分类判断为' + {'supplier': '供应商', 'competitor': '同类产品竞争者', 'directory': '目录平台', 'irrelevant': '明显无关对象'}[classification] + '，不作为已匹配买家。')
    elif relevant:
        card['fit'] = {'level': 'medium', 'reasons': ['经营品类线索：' + facts['product_signal']['value'], '只证明品类可能相关，未确认参数兼容或采购需求。']}
        card['why_fit'] = '网页提到“' + facts['product_signal']['value'] + '”，可进一步确认是否采购我方产品。'
        card['demand_hypothesis'] = '需求假设（未确认）：可能需要该相关品类；请先确认是否经营或采购，不能推定具体型号、数量或预算。'
    if card['mismatches']: card['fit'] = {'level': 'low', 'reasons': card['mismatches']}
    if card['country'] and card['country'].casefold() != task['market'].casefold():
        card['unknowns'].append('来源所在地与目标市场文字不一致，须确认是否覆盖目标市场。')
    enough = all(facts[key]['value'] for key in ('name', 'country', 'business_type', 'product_signal'))
    card['evidence'] = {'level': 'medium' if enough and analysis['source_kind'] == 'official' else 'low',
                        'reasons': ['逐字引文已校验；来源身份及时效仍需人工确认。', '分类为 AI 推断，不能视为确定身份。']}
    # Contact pages are observed links, never model-created addresses. Generic emails remain unverified.
    if analysis['source_kind'] == 'official':
        for link in page.get('links', []):
            if domain(link) == domain(website) and re.search(r'contact|get-in-touch|kontakt|contacto', urlsplit(link).path, re.I):
                card['contacts'].append({'type': 'contact_page', 'value': link, 'source_url': page['url'], 'verified_deliverable': False})
                break
        for email in page.get('emails', []):
            local = email.split('@')[0].lower()
            if local in ('info', 'sales', 'contact', 'enquiries', 'inquiries', 'office', 'hello', 'support'):
                card['contacts'].append({'type': 'general_email', 'value': email, 'source_url': page['url'], 'verified_deliverable': False})
                if email in page['text']: card['sources'].append(source_from(page, email, analysis['source_kind']))
    if card['contacts']: card['contactability'] = {'level': 'medium', 'reasons': ['网页公开通用邮箱/联系入口；非采购负责人，未验证可送达。']}
    return apply_priority(card)

class ResearchManager:
    def __init__(self, stores):
        self.stores = stores; self.lock = threading.RLock(); self.threads = {}; self.closing = False
        for name in stores: self.recover(name)
    def has_running(self, workspace):
        with self.lock: return any(key[0] == workspace and thread.is_alive() for key, thread in self.threads.items())
    def recover(self, workspace):
        with self.lock:
            if self.has_running(workspace): return
            store = self.stores[workspace]
            for task in store.all('research_tasks'):
                if task.get('status') == 'running':
                    task['status'] = 'paused'; task['cancel_requested'] = False
                    event(task, 'recovery', '应用重启或恢复备份，任务已暂停；已用预算与已保存结果保留，点击继续。')
                    store.save('research_tasks', task, task['id'])
    def stop(self):
        with self.lock:
            self.closing = True
            for workspace in self.stores:
                for task in self.stores[workspace].all('research_tasks'):
                    if task['status'] == 'running':
                        task['cancel_requested'] = True; task['status'] = 'paused'
                        self.stores[workspace].save('research_tasks', task, task['id'])
    def update(self, workspace, id, change):
        with self.lock:
            store = self.stores[workspace]; task = store.get('research_tasks', id)
            change(task)
            return store.save('research_tasks', task, id)
    def create(self, workspace, data):
        if not isinstance(data, dict): raise ValueError('任务格式不正确。')
        store = self.stores[workspace]
        ids = data.get('product_ids', [])
        if not isinstance(ids, list) or not 1 <= len(ids) <= 20 or any(type(x) is not int for x in ids): raise ValueError('请选择 1 到 20 个产品。')
        task = {'title': text(data.get('title', ''), '任务名称', 160) or '买家研究任务',
                'product_ids': list(dict.fromkeys(ids)), 'market': text(data.get('market', ''), '目标市场', 120, True),
                'confirmed_conditions': text(data.get('confirmed_conditions', ''), '已确认条件'),
                'unknown_conditions': text(data.get('unknown_conditions', ''), '待确认条件'),
                'test_market': data.get('test_market', False), 'status': 'draft', 'usage': {'search': 0, 'model': 0, 'pages': 0},
                'events': [], 'gaps': [], 'queries': [], 'query_cursor': 0, 'url_queue': [], 'documents': [], 'read_failures': [],
                'prospect_ids': [], 'plan_created': False, 'cancel_requested': False, 'cost': None}
        if type(task['test_market']) is not bool: raise ValueError('测试市场标记格式不正确。')
        buyer_types = data.get('buyer_types', [])
        if not isinstance(buyer_types, list) or not 1 <= len(buyer_types) <= 10: raise ValueError('请选择或填写买家类型。')
        task['buyer_types'] = [text(value, '买家类型', 100, True) for value in buyer_types]
        for key, (default, maximum) in LIMITS.items():
            value = data.get(key, default)
            if type(value) is not int or not (1 if key == 'target_count' else 0) <= value <= maximum: raise ValueError(f'{key} 必须是上限 {maximum} 内的整数。')
            task[key] = value
        selected_products = [store.get('products', id) for id in task['product_ids']]
        if any(product.get('archived') for product in selected_products):
            raise ValueError('已归档产品仅供历史查阅，请选择在用产品创建研究任务。')
        task['product_snapshots'] = [public_product(product) for product in selected_products]
        seed_urls = data.get('seed_urls', [])
        if not isinstance(seed_urls, list) or len(seed_urls) > 20: raise ValueError('人工网址最多 20 个。')
        try: task['seed_urls'] = list(dict.fromkeys(canonical_url(url) for url in seed_urls))
        except ResearchError as exc: raise ValueError(str(exc)) from None
        task['url_queue'] = task['seed_urls'].copy()
        event(task, 'created', '任务已保存；目标市场为测试假设。' if task['test_market'] else '任务已保存；等待开始。')
        return store.save('research_tasks', task)
    def start(self, workspace, id, retry=False):
        with self.lock:
            if self.closing: raise ValueError('服务正在退出，请重启后继续。')
            key = (workspace, id)
            if key in self.threads and self.threads[key].is_alive(): return self.stores[workspace].get('research_tasks', id)
            store = self.stores[workspace]; task = store.get('research_tasks', id)
            task['status'] = 'running'; task['cancel_requested'] = False; task['last_error'] = ''
            if retry:
                for url in task.get('read_failures', []):
                    if url not in task['url_queue']: task['url_queue'].append(url)
                task['read_failures'] = []
            event(task, 'started', '继续研究；保留原有用量，调用失败也计入预算。')
            task = store.save('research_tasks', task, id)
            thread = threading.Thread(target=self._worker, args=(workspace, id), daemon=True, name=f'research-{workspace}-{id}')
            self.threads[key] = thread; thread.start()
            return task
    def cancel(self, workspace, id):
        def change(task):
            task['cancel_requested'] = True; task['status'] = 'cancelled'
            event(task, 'cancel', '已取消后续调用；正在返回的单次请求仍会保存结果与用量。')
        return self.update(workspace, id, change)
    def check(self, workspace, id):
        with self.lock:
            task = self.stores[workspace].get('research_tasks', id)
            if self.closing or task.get('cancel_requested'): raise Cancelled()
            return task
    def reserve(self, workspace, id, kind):
        with self.lock:
            task = self.check(workspace, id)
            key = {'search': 'search_limit', 'model': 'model_limit', 'pages': 'page_limit'}[kind]
            if task['usage'][kind] >= task[key]: raise BudgetReached(key)
            task['usage'][kind] += 1
            event(task, kind, f'开始第 {task["usage"][kind]} 次 {kind} 调用。')
            self.stores[workspace].save('research_tasks', task, id)
    def upsert_prospect(self, workspace, card):
        with self.lock:
            store = self.stores[workspace]
            matches = [old for old in store.all('prospects') if (card['domain'] and old.get('domain') == card['domain']) or
                       (canonical_name(card['name']) and canonical_name(old['name']) == canonical_name(card['name']))]
            if not matches: return store.save('prospects', card)
            old = matches[0]; conflict = []
            for key in ('country', 'business_type'):
                if old.get(key) and card.get(key) and old[key].casefold() != card[key].casefold():
                    conflict.append(f'{key} 不同来源表述不一致，需人工核对。')
            merged = {**old}
            if not merged.get('website') and card.get('website'):
                merged['website'] = card['website']; merged['domain'] = card['domain']
            elif old.get('domain') and card.get('domain') and old['domain'] != card['domain']:
                conflict.append('同名企业提供了不同网站域名，请人工核对是否同一家企业。')
            if card.get('provenance') == 'manual':
                for key in ('name', 'country', 'business_type', 'notes'):
                    if card.get(key): merged[key] = card[key]
                merged['provenance'] = 'manual'
                merged['manual_overrides'] = True
                merged['product_ids'] = list(dict.fromkeys(old['product_ids'] + card['product_ids']))
                merged['needs_review'] = True
            elif card.get('ai_processed'):
                merged['ai_processed'] = True
                combined = {**old.get('facts', {})}
                for key, fact in card.get('facts', {}).items():
                    if fact.get('value') and not combined.get(key, {}).get('value'): combined[key] = fact
                merged['facts'] = combined
                for key in ('name', 'country', 'business_type'):
                    if combined.get(key, {}).get('value') and not old.get('manual_overrides'): merged[key] = combined[key]['value']
                old_kind, new_kind = old['classification'], card['classification']
                if old_kind == 'unknown': merged['classification'] = new_kind
                elif new_kind != 'unknown' and new_kind != old_kind:
                    merged['classification'] = 'unknown'; conflict.append('不同网页产生不同企业分类，须人工核对买家或供应商身份。')
                merged['product_ids'] = list(dict.fromkeys(old['product_ids'] + card['product_ids']))
                signal = combined.get('product_signal', {}).get('value', '')
                merged['mismatches'] = list(dict.fromkeys(old['mismatches'] + card['mismatches']))
                if merged['classification'] in ('supplier', 'competitor', 'directory', 'irrelevant') or merged['mismatches']:
                    merged['product_ids'] = []; merged['fit'] = {'level': 'low', 'reasons': merged['mismatches'] or ['企业身份不符合潜在买家。']}
                elif signal and merged['product_ids']:
                    merged['fit'] = {'level': 'medium', 'reasons': ['经营品类线索：' + signal, '只证明品类可能相关，未确认参数兼容或采购需求。']}
                    merged['why_fit'] = '网页提到“' + signal + '”，可进一步确认是否采购我方产品。'
                    merged['demand_hypothesis'] = '需求假设（未确认）：可能需要该相关品类；采购意向、规格、数量及预算仍待确认。'
                complete = all(combined.get(key, {}).get('value') for key in ('name', 'country', 'business_type', 'product_signal'))
                official = any(source.get('kind') == 'official' for source in old['sources'] + card['sources'])
                merged['evidence'] = {'level': 'medium' if complete and official else 'low', 'reasons': ['逐字来源已校验；跨页补充资料，官网身份和时效仍须人工核验。']}
            merged['sources'] = list({(source['url'], source['excerpt']): source for source in old['sources'] + card['sources']}.values())[:60]
            merged['contacts'] = list({(contact['type'], contact['value']): contact for contact in old['contacts'] + card['contacts']}.values())[:30]
            if merged['contacts']: merged['contactability'] = {'level': 'medium', 'reasons': ['公开通用邮箱或联系页面；未验证可送达，非采购负责人。']}
            merged['task_ids'] = sorted(set(old['task_ids'] + card['task_ids']))
            resolved_labels = {label + '缺少直接证据。' for key, label in (('name', '企业名称'), ('country', '所在地'), ('business_type', '经营业务'), ('product_signal', '相关产品线索')) if merged.get('facts', {}).get(key, {}).get('value')}
            merged['unknowns'] = [item for item in dict.fromkeys(old['unknowns'] + card['unknowns'] + conflict) if item not in resolved_labels][:60]
            if conflict:
                merged['evidence'] = {'level': 'low', 'reasons': conflict}; merged['needs_review'] = True
            manually_prioritized = any(item.get('action') == 'human_feedback' and item.get('before', {}).get('priority') != item.get('after', {}).get('priority') for item in old.get('history', []))
            apply_priority(merged, preserve_manual=manually_prioritized)
            merged['history'] = old.get('history', []) + [{'at': now(), 'action': 'manual_enrichment' if card['provenance'] == 'manual' else 'deduplicated', 'note': '按企业名称或官网域名合并；人工审核结果与所有来源保留。'}]
            return store.save('prospects', merged, old['id'])

    def attach(self, workspace, id, prospect):
        def change(task):
            task['prospect_ids'] = list(dict.fromkeys(task['prospect_ids'] + [prospect['id']]))
            event(task, 'card', '已保存企业卡片：' + prospect['name'])
        self.update(workspace, id, change)
    def _worker(self, workspace, id):
        try: self.run(workspace, id)
        except Cancelled: pass
        except BudgetReached as exc:
            def change(task):
                if not task.get('cancel_requested'): task['status'] = 'completed'
                task['gaps'] = list(dict.fromkeys(task['gaps'] + [f'达到 {str(exc)} 调用上限，已停止；尚未核验的信息仍为未知。']))
                event(task, 'budget', '达到任务预算，已停止后续调用。')
            self.update(workspace, id, change)
        except (ResearchError, ValueError) as exc:
            def change(task):
                if not task.get('cancel_requested'): task['status'] = 'failed'
                task['last_error'] = str(exc)[:500]; event(task, 'failed', task['last_error'])
            self.update(workspace, id, change)
        except Exception:
            def change(task):
                if not task.get('cancel_requested'): task['status'] = 'failed'
                task['last_error'] = '研究任务发生错误，已保存结果保留；可重试或手动整理。'; event(task, 'failed', task['last_error'])
            self.update(workspace, id, change)
    def run(self, workspace, id):
        store = self.stores[workspace]
        while True:
            task = self.check(workspace, id); caps = capabilities()
            # Resume a saved page before any repeat network operation.
            pending = next((doc for doc in task['documents'] if not doc.get('analyzed')), None)
            if pending and caps['model']:
                self.reserve(workspace, id, 'model')
                analysis = model.analyze_page(task, pending)
                card = self.upsert_prospect(workspace, build_card(task, pending, analysis)); self.attach(workspace, id, card)
                def save_analysis(current):
                    for doc in current['documents']:
                        if doc['url'] == pending['url']: doc['analyzed'] = True
                    seen = {doc['url'] for doc in current['documents']} | set(current['url_queue'])
                    for url in analysis['followup_urls']:
                        if url not in seen and len(current['url_queue']) < 80: current['url_queue'].append(url); seen.add(url)
                    if analysis['classification'] in ('potential_buyer', 'unknown'):
                        for query in analysis['followup_queries']:
                            if query not in current['queries'] and len(current['queries']) < 40: current['queries'].append(query)
                self.update(workspace, id, save_analysis)
                continue
            researched_cards = [store.get('prospects', pid) for pid in task['prospect_ids']]
            sufficiently_evidenced = [card for card in researched_cards if card['classification'] not in ('unknown', 'directory', 'irrelevant') and card['evidence']['level'] in ('medium', 'high')]
            if len(sufficiently_evidenced) >= task['target_count'] and all(doc.get('analyzed') for doc in task['documents']):
                self.finish(workspace, id, 'completed', ['目标研究企业数量已达到；卡片仍需人工审核，发现企业不等于获得客户。']); return
            if task['url_queue'] and caps['reader']:
                self.reserve(workspace, id, 'pages'); url = task['url_queue'][0]
                try: page = read_page(url)
                except ResearchError as exc:
                    def failed_read(current):
                        current['url_queue'] = [u for u in current['url_queue'] if u != url]
                        current['read_failures'] = list(dict.fromkeys(current['read_failures'] + [url]))
                        current['gaps'] = list(dict.fromkeys(current['gaps'] + ['网页无法读取：' + url]))
                        event(current, 'read_failure', str(exc))
                    self.update(workspace, id, failed_read); continue
                def save_page(current):
                    current['url_queue'] = [u for u in current['url_queue'] if u not in (url, page['url'])]
                    if not any(doc['url'] == page['url'] for doc in current['documents']): current['documents'].append({**page, 'analyzed': False})
                    event(current, 'read', '已读取并保存来源：' + page['url'])
                self.update(workspace, id, save_page)
                if not caps['model']:
                    card = self.upsert_prospect(workspace, build_card(task, page)); self.attach(workspace, id, card)
                continue
            if not caps['model'] or not caps['search'] or not caps['reader']:
                self.finish(workspace, id, 'needs_configuration', caps['missing']); return
            if not task['plan_created']:
                self.reserve(workspace, id, 'model')
                feedback = [{'name': p['name'], 'review_status': p['review_status'], 'review_reason': p['review_reason'][:300]} for p in store.all('prospects') if p.get('review_reason')][:12]
                planned = model.plan(task, feedback)
                def save_plan(current):
                    current['queries'] = list(dict.fromkeys(current['queries'] + planned['queries']))
                    current['gaps'] = list(dict.fromkeys(current['gaps'] + planned['gaps'])); current['plan_created'] = True
                    event(current, 'plan', 'AI 搜索计划已保存。')
                self.update(workspace, id, save_plan); continue
            if task['query_cursor'] < len(task['queries']):
                query = task['queries'][task['query_cursor']]
                self.reserve(workspace, id, 'search'); results = search_web(query)
                def save_results(current):
                    current['query_cursor'] += 1
                    current.setdefault('search_results', []).extend(results)
                    current['search_results'] = current['search_results'][-160:]
                    seen = {doc['url'] for doc in current['documents']} | set(current['url_queue']) | set(current['read_failures'])
                    for result in results:
                        if result['url'] not in seen and len(current['url_queue']) < 80: current['url_queue'].append(result['url']); seen.add(result['url'])
                    event(current, 'search', f'已保存搜索结果：{len(results)} 条；搜索词：{query}')
                self.update(workspace, id, save_results); continue
            self.finish(workspace, id, 'completed', [f'可用计划已执行；实际研究 {len(task["prospect_ids"])} 家，目标 {task["target_count"]} 家。没有足够结果时不会凑数。']); return
    def finish(self, workspace, id, status, gaps):
        def change(task):
            if not task.get('cancel_requested'): task['status'] = status
            task['gaps'] = list(dict.fromkeys(task['gaps'] + gaps)); event(task, status, '研究已停止，结果和缺口已保存。')
        self.update(workspace, id, change)


def statistics(prospects):
    records = [p for p in prospects if p.get('provenance') != 'demo']
    researched = len(records); accepted = sum(p['review_status'] == 'accepted' for p in records)
    contacted = sum(bool(p.get('contacted_at')) or p['contact_status'] in ('contacted', 'replied', 'qualified') for p in records)
    replied = sum(bool(p.get('replied_at')) or p['contact_status'] in ('replied', 'qualified') for p in records)
    qualified = sum(bool(p.get('qualified_at')) or p['contact_status'] == 'qualified' for p in records)
    return {'researched': researched, 'accepted': accepted, 'contacted': contacted, 'replied': replied, 'qualified': qualified,
            'acceptance_rate': accepted / researched if researched else None, 'reply_rate': replied / contacted if contacted else None,
            'qualified_rate': qualified / contacted if contacted else None,
            'definitions': {'researched': '当前资料空间去重企业数（真实网页 + 人工资料；不含演示来源）', 'accepted': '当前人工认可数',
                            'contacted': '人工记录至少联系过一次的去重企业数', 'replied': '人工记录收到回复的去重企业数',
                            'qualified': '人工记录形成有效询盘的去重企业数', 'reply_rate': '回复企业数 / 实际联系企业数',
                            'qualified_rate': '有效询盘企业数 / 实际联系企业数'}, 'cost': None}

def manual_material_source(prospect):
    """Prefer a deliberately supplied short quote; never quote an unanalysed navbar."""
    sources = prospect.get('sources', [])
    for source in reversed(sources):
        excerpt = source.get('excerpt', '').strip()
        if source.get('kind') == 'manual' and source.get('url') and 0 < len(excerpt) <= 400:
            return {'excerpt': excerpt[:240], 'source_url': source['url'], 'source_provenance': 'manual',
                    'source_review_note': '人工提供片段，尚待核验；不代表已确认采购意向。'}
    fact = prospect.get('facts', {}).get('product_signal', {})
    value, excerpt, url = fact.get('value', ''), fact.get('excerpt', ''), fact.get('source_url', '')
    if value and excerpt and value in excerpt and any(source.get('url') == url and excerpt in source.get('excerpt', '') for source in sources):
        start = max(0, excerpt.index(value) - 60)
        snippet = excerpt[start:start + 240]
        return {'excerpt': snippet, 'source_url': url, 'source_provenance': 'web',
                'source_review_note': '产品线索已有逐字来源；来源身份、时效及业务适合度仍须人工复核。'}
    return {'excerpt': '', 'source_url': '', 'source_provenance': 'none',
            'source_review_note': '缺少经过选择的产品经营证据，先询问是否经营该品类。'}


def build_material(prospect, products, selection=None):
    if selection is None:
        selection = {**manual_material_source(prospect), 'product_ids': [p['id'] for p in products], 'opening': 'category_check', 'cta': 'confirm_category'}
    else:
        cited = next((source for source in prospect.get('sources', []) if source.get('url') == selection['source_url'] and selection['excerpt'] and selection['excerpt'] in source.get('excerpt', '')), {})
        selection = {**selection, 'source_provenance': 'manual' if cited.get('kind') == 'manual' else 'web' if cited else 'none',
                     'source_review_note': 'AI 选取已有片段，仍需人工核验；不代表已确认采购意向。'}
    selected = [p for p in products if p['id'] in selection['product_ids']]
    labels = [p.get('name_en') or p.get('category') or 'electrical products' for p in selected]
    category = ', '.join(dict.fromkeys(labels))[:350] or 'low-voltage electrical products'
    excerpt = selection['excerpt']
    if excerpt:
        reason = selection['source_review_note'] + '\n联系依据（来源原文）：' + excerpt
        observation = 'The information reviewed for your company mentions: "' + excerpt + '".\n\n'
    else:
        reason = '先核实对方是否经营相关品类；企业需求尚未确认。'
        observation = ''
    next_step = 'Please confirm whether your company handles this product category.' if selection['cta'] == 'confirm_category' else 'If this category is relevant, could you share the models or specifications you are currently evaluating?'
    body = ('Dear Team,\n\n' + observation + 'We are preparing an export offering for ' + category + '. '
            'I would like to check whether this category is relevant to your business.\n\n' + next_step +
            '\n\nProduct suitability and any commercial terms would need to be reviewed against your requirements.\n\nBest regards')
    return {'prospect_id': prospect['id'], 'reason': reason, 'product_ids': selection['product_ids'],
            'subject': 'Product category enquiry: ' + category[:120], 'body': body, 'next_step': next_step,
            'source_url': selection['source_url'], 'source_excerpt': excerpt, 'source_provenance': selection['source_provenance'],
            'source_review_note': selection['source_review_note'], 'reviewed': False, 'sent': False}


def register_research(app, stores, workspace):
    manager = ResearchManager(stores)
    def data():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict): raise ValueError('请提交 JSON 对象。')
        return payload
    def respond(record): return jsonify(record)
    @app.errorhandler(ResearchError)
    def research_error(exc): return jsonify(error=str(exc)), 400
    @app.get('/api/research/state')
    def research_state():
        store = stores[workspace()]
        prospects = store.all('prospects')
        return respond({'capabilities': capabilities(), 'tasks': [task_view(t) for t in store.all('research_tasks')],
                        'prospects': prospects, 'materials': store.all('materials'), 'stats': statistics(prospects)})
    @app.post('/api/research/tasks')
    def research_create(): return respond(task_view(manager.create(workspace(), data())))
    @app.get('/api/research/tasks/<int:id>')
    def research_task(id): return respond(task_view(stores[workspace()].get('research_tasks', id)))
    @app.post('/api/research/tasks/<int:id>/start')
    def research_start(id): return respond(task_view(manager.start(workspace(), id)))
    @app.post('/api/research/tasks/<int:id>/retry')
    def research_retry(id): return respond(task_view(manager.start(workspace(), id, True)))
    @app.post('/api/research/tasks/<int:id>/cancel')
    def research_cancel(id): return respond(task_view(manager.cancel(workspace(), id)))
    @app.post('/api/research/tasks/<int:id>/manual')
    def research_manual(id):
        ws = workspace(); store = stores[ws]; task = store.get('research_tasks', id); payload = data()
        name = text(payload.get('name', ''), '企业名称', 200, True)
        raw_url = text(payload.get('website', ''), '网站', 2000)
        website = canonical_url(raw_url) if raw_url else ''
        card = empty_prospect(name, website, id, 'manual')
        for key in ('country', 'business_type'): card[key] = text(payload.get(key, ''), key, 300)
        notes = text(payload.get('notes', ''), '备注', 3000)
        source_text = text(payload.get('source_text', ''), '来源摘录', 10000)
        source_url = text(payload.get('source_url', ''), '来源网址', 2000)
        if source_url: source_url = canonical_url(source_url)
        card['sources'] = [{'url': source_url or website, 'title': '人工提供资料', 'excerpt': source_text,
                            'retrieved_at': now(), 'kind': 'manual', 'issues': ['人工输入，未通过联网或 AI 核验。']}]
        card['notes'] = notes; card['unknowns'].append('人工提供的企业名称、所在地、业务及资料均待核验。')
        card['product_ids'] = task['product_ids']
        card = manager.upsert_prospect(ws, card); manager.attach(ws, id, card)
        return respond(card)
    @app.patch('/api/research/prospects/<int:id>')
    def research_review(id):
        with manager.lock:
            store = stores[workspace()]; card = store.get('prospects', id); payload = data()
            allowed = {'review_status', 'review_reason', 'priority', 'contact_status', 'contact_note'}
            if set(payload) - allowed: raise ValueError('包含不支持的企业编辑字段。')
            before = {key: card.get(key, '') for key in allowed}
            for key, values in (('review_status', REVIEW_STATUSES), ('priority', ('high', 'medium', 'low')), ('contact_status', CONTACT_STATUSES)):
                if key in payload:
                    if payload[key] not in values: raise ValueError('企业状态或优先级不正确。')
                    card[key] = payload[key]
            for key in ('review_reason', 'contact_note'):
                if key in payload: card[key] = text(payload[key], '人工记录', 3000)
            if card['review_status'] != before['review_status'] and not card['review_reason']: raise ValueError('接受或排除候选时请填写原因。')
            if card['contact_status'] != before['contact_status'] and not card['contact_note']: raise ValueError('变更联系结果时请填写实际情况。')
            status = card['contact_status']
            if status in ('contacted', 'replied', 'qualified'): card.setdefault('contacted_at', now())
            if status in ('replied', 'qualified'): card.setdefault('replied_at', now())
            if status == 'qualified': card.setdefault('qualified_at', now())
            card['needs_review'] = card['review_status'] == 'pending'
            card['history'].append({'at': now(), 'action': 'human_feedback', 'before': before, 'after': {key: card.get(key, '') for key in allowed}})
            return respond(store.save('prospects', card, id))
    @app.post('/api/research/prospects/<int:id>/materials')
    def research_material(id):
        ws = workspace(); store = stores[ws]; card = store.get('prospects', id); payload = data(); mode = payload.get('mode', 'manual')
        if mode not in ('live', 'manual'): raise ValueError('开发材料模式仅支持 live 或 manual。')
        if card['review_status'] == 'excluded': raise ValueError('该企业已排除；请先调整人工审核结果。')
        task = store.get('research_tasks', card['task_ids'][-1])
        products = [p for p in task['product_snapshots'] if not card['product_ids'] or p['id'] in card['product_ids']]
        if mode == 'live':
            if not capabilities()['model']: raise ResearchError('模型未接入；可使用人工辅助模板，未产生模型调用。')
            # Explicit materials action also consumes the associated task's original model budget.
            with manager.lock:
                task = store.get('research_tasks', task['id'])
                if task['usage']['model'] >= task['model_limit']: raise ValueError('关联研究任务的模型预算已用尽；可使用人工辅助模板。')
                task['usage']['model'] += 1; event(task, 'materials', '人工请求 AI 开发材料，计入模型调用上限。')
                store.save('research_tasks', task, task['id'])
            try: selection = model.material_selection(card, products)
            except ResearchError as exc:
                manager.update(ws, task['id'], lambda current: event(current, 'materials_failure', str(exc)))
                raise
        else:
            selection = None
        material = build_material(card, products, selection)
        material.update(provenance=mode, label='AI 依据来源组装的待审核草稿' if mode == 'live' else '人工辅助模板（非 AI 输出）', history=[])
        return respond(store.save('materials', material))
    @app.put('/api/research/materials/<int:id>')
    def research_material_edit(id):
        with manager.lock:
            store = stores[workspace()]; material = store.get('materials', id); payload = data()
            allowed = {'reason', 'subject', 'body', 'next_step', 'reviewed'}
            if set(payload) - allowed: raise ValueError('开发材料包含不支持的字段。')
            previous = {key: material[key] for key in allowed}
            changed = False
            for key in ('reason', 'subject', 'body', 'next_step'):
                if key in payload:
                    value = text(payload[key], '开发材料', 15000 if key == 'body' else 2000, key in ('subject', 'body'))
                    changed = changed or value != material[key]; material[key] = value
            if changed: material['reviewed'] = False
            if 'reviewed' in payload:
                if type(payload['reviewed']) is not bool: raise ValueError('审核标记不正确。')
                # A content save cannot simultaneously bless modified material.
                if payload['reviewed'] and changed: raise ValueError('请先保存修改，再单独审核开发材料。')
                material['reviewed'] = payload['reviewed']
            material['history'].append({'at': now(), 'before': previous, 'action': 'human_edit' if changed else 'human_review'})
            return respond(store.save('materials', material, id))
    return manager


def validate_research_backup(records):
    """Validate stored job bounds before allowing restored data back into workers."""
    product_ids = {p['id'] for p in records['products']}
    task_ids = {t['id'] for t in records['research_tasks']}; prospect_ids = {p['id'] for p in records['prospects']}
    tasks_by_id = {t['id']: t for t in records['research_tasks']}
    try:
        for task in records['research_tasks']:
            if task['status'] not in STATUSES or type(task['test_market']) is not bool: raise ValueError()
            if not isinstance(task['usage'], dict) or set(task['usage']) != {'search', 'model', 'pages'}: raise ValueError()
            for key, (_, maximum) in LIMITS.items():
                if type(task[key]) is not int or not (1 if key == 'target_count' else 0) <= task[key] <= maximum: raise ValueError()
            for key, limit in (('search', 'search_limit'), ('model', 'model_limit'), ('pages', 'page_limit')):
                if type(task['usage'][key]) is not int or not 0 <= task['usage'][key] <= task[limit]: raise ValueError()
            if not isinstance(task['product_ids'], list) or not set(task['product_ids']) <= product_ids: raise ValueError()
            if not isinstance(task['prospect_ids'], list) or not set(task['prospect_ids']) <= prospect_ids: raise ValueError()
            if not isinstance(task['queries'], list) or len(task['queries']) > 40 or any(not isinstance(q, str) or len(q) > 500 for q in task['queries']): raise ValueError()
            if type(task['query_cursor']) is not int or not 0 <= task['query_cursor'] <= len(task['queries']): raise ValueError()
            if len(task['documents']) > 50 or len(task['url_queue']) > 80: raise ValueError()
            for url in task['seed_urls'] + task['url_queue'] + task['read_failures']: canonical_url(url)
            for doc in task['documents']:
                canonical_url(doc['url'])
                if not isinstance(doc['text'], str) or len(doc['text']) > 16000 or type(doc['analyzed']) is not bool: raise ValueError()
                for url in doc['links']: canonical_url(url)
            for snapshot in task['product_snapshots']:
                if set(snapshot) - {'id', 'name', 'name_en', 'model', 'category', 'unit', 'specs', 'source_updated_at'}: raise ValueError()
                if set(snapshot['specs']) - set(TECH_KEYS): raise ValueError()
        for card in records['prospects']:
            if card['provenance'] not in ('web', 'manual', 'demo') or card['classification'] not in CLASSIFICATIONS or card['review_status'] not in REVIEW_STATUSES or card['contact_status'] not in CONTACT_STATUSES: raise ValueError()
            if not isinstance(card['task_ids'], list) or not set(card['task_ids']) <= task_ids: raise ValueError()
            if card['website']: canonical_url(card['website'])
            for source in card['sources']:
                if source['url']: canonical_url(source['url'])
                if source['kind'] not in ('official', 'directory', 'unknown', 'manual') or not isinstance(source['excerpt'], str): raise ValueError()
            if card['provenance'] == 'web':
                if not card['sources']: raise ValueError()
                documents = [doc for tid in card['task_ids'] for doc in tasks_by_id[tid]['documents']]
                for source in card['sources']:
                    if source['kind'] == 'manual' or not source['excerpt'] or not any(doc['url'] == source['url'] and source['excerpt'] in doc['text'] for doc in documents): raise ValueError()
                for fact in card.get('facts', {}).values():
                    if fact.get('value') and (fact['value'] not in fact['excerpt'] or not any(doc['url'] == fact['source_url'] and fact['excerpt'] in doc['text'] for doc in documents)): raise ValueError()
            for contact in card['contacts']:
                if contact['type'] not in ('general_email', 'contact_page', 'phone') or contact['verified_deliverable'] is not False: raise ValueError()
                if contact['type'] == 'contact_page': canonical_url(contact['value'])
        for material in records['materials']:
            if material['prospect_id'] not in prospect_ids or material['provenance'] not in ('live', 'manual') or type(material['reviewed']) is not bool or material.get('sent') is not False: raise ValueError()
            for key in ('reason', 'subject', 'body', 'next_step'):
                if not isinstance(material[key], str) or len(material[key]) > 15000: raise ValueError()
    except (KeyError, TypeError, ResearchError, ValueError): raise ValueError('备份的买家研究任务、调用预算、来源或关联记录无效。') from None
