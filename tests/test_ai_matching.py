import copy
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib import error
from app.ai import analyze, validate_analysis, demo_analysis, get_config, AIError, _post
from app.matching import scalar, grounded, match_products, multiple_groups

TEXT='Please quote 1,000 pcs MCB, 2P, 16 A, 230 V, C curve, 6 kA, delivery within 30 days.'
PRODUCT={'id':1,'category':'MCB','model':'FICTIONAL-16','specs':{'poles':'2','current_a':'16','voltage_v':'230','curve':'C','breaking_ka':'6'},'unit':'pcs','moq':'100','source':'Fictional sheet','lead_time':'20 days'}
CONFIG={'AI_PROVIDER':'openai','AI_BASE_URL':'https://api.openai.com/v1','AI_MODEL':'test-model','AI_API_KEY':'secret-test-key','AI_TIMEOUT':'15'}

def data():
    d=demo_analysis(TEXT)
    d.pop('mode')
    d['warnings']=[]
    return d

def response(output):
    return {'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(output)}]}]}

class EvidenceTests(unittest.TestCase):
    def test_stated_values_exact_and_missing_added(self):
        result=validate_analysis({'requirements':[{'field':'current_a','value':'16 A','evidence':'16 A','kind':'stated'}]},TEXT)
        self.assertIn({'field':'poles','value':'','evidence':'','kind':'missing'},result['requirements'])
        self.assertTrue(result['questions'])
    def test_fabricated_original_quote_rejected(self):
        d=data();d['requirements'][0]['evidence']='not in original'
        with self.assertRaises(ValueError):validate_analysis(d,TEXT)
    def test_genuine_quote_cannot_support_invented_value(self):
        d=data()
        for r in d['requirements']:
            if r['field']=='current_a':r.update(value='32 A',evidence=TEXT)
        with self.assertRaises(ValueError):validate_analysis(d,TEXT)
    def test_wrong_units_not_numeric_evidence(self):
        self.assertFalse(grounded('current_a','230','230 V'))
        self.assertFalse(grounded('current_a','6','6 kA'))
        self.assertFalse(grounded('breaking_ka','16','16 A'))
        self.assertTrue(grounded('current_a','16','16 A'))
        self.assertTrue(grounded('breaking_ka','6','breaking capacity 6000 A'))
    def test_ranges_and_ac_dc_cannot_be_silently_collapsed(self):
        for field,value,evidence in [('current_a','16 A','10-16 A'),('voltage_v','230 V','230 V AC'),('voltage_v','230','230/400 V'),('current_a','16','at least 16 A'),('poles','1P','1P+N')]:
            with self.subTest(evidence=evidence):self.assertFalse(grounded(field,value,evidence))
        self.assertTrue(grounded('current_a','10-16 A','10-16 A'))
        self.assertTrue(grounded('voltage_v','230 V AC','230 V AC'))
    def test_cherry_picked_excerpt_cannot_remove_qualifier(self):
        for field,value,text in [('current_a','16 A','Please quote 10-16 A MCB'),('voltage_v','230 V','Please quote 230 V AC MCB'),('poles','1P','Please quote 1P+N MCB')]:
            d={'requirements':[{'field':field,'value':value,'evidence':value,'kind':'stated'}]}
            with self.subTest(text=text),self.assertRaises(ValueError):validate_analysis(d,text)
    def test_demo_preserves_complex_expression(self):
        out=demo_analysis('100 pcs MCB 2P 10-16 A 230 V AC C curve 6 kA')
        self.assertEqual(next(r['value'] for r in out['requirements'] if r['field']=='current_a'),'10-16 A')
        self.assertEqual(next(r['value'] for r in out['requirements'] if r['field']=='voltage_v'),'230 V AC')
    def test_missing_cannot_have_invented_value(self):
        d={'requirements':[{'field':'current_a','value':'16 A','evidence':'','kind':'missing'}]}
        with self.assertRaises(ValueError):validate_analysis(d,TEXT)
    def test_unknown_demo_does_not_invent_specs(self):
        out=demo_analysis('Hello, can you send a catalog?')
        self.assertEqual(out['mode'],'demo')
        self.assertTrue(all(r['kind']=='missing' for r in out['requirements']))
        self.assertIn('演示',out['warnings'][0])
    def test_multiple_requests_warn_and_do_not_combine(self):
        text='Please quote 100 pcs MCB 16 A and 200 pcs MCB 32 A.'
        out=demo_analysis(text)
        self.assertTrue(any('多组' in w for w in out['warnings']))
        matches=match_products(out,[PRODUCT])
        self.assertNotEqual(matches['candidates'][0]['status'],'eligible')
        self.assertIn('拆分',matches['message'])
    def test_complex_expressions_are_not_collapsed(self):
        self.assertIsNone(scalar('poles','1P+N'))
        self.assertIsNone(scalar('current_a','16-32 A'))
        self.assertIsNone(scalar('voltage_v','230/400 V'))
        self.assertIsNone(scalar('voltage_v','230 V AC'))
        self.assertIsNone(scalar('breaking_ka','>=6 kA'))
        self.assertEqual(scalar('voltage_v','0.23 kV'),230)
        self.assertEqual(scalar('breaking_ka','6000 A'),6)

class MatchingTests(unittest.TestCase):
    def test_exact_parameters_reasons_and_delivery_pending(self):
        c=match_products(demo_analysis(TEXT),[PRODUCT])['candidates'][0]
        self.assertFalse(c['conflicts'])
        self.assertTrue(any('电流一致' in r for r in c['reasons']))
        self.assertEqual(c['status'],'pending')
        self.assertTrue(any('交期' in r for r in c['pending']))
    def test_same_model_does_not_override_conflict(self):
        p=copy.deepcopy(PRODUCT);p['specs']['current_a']='32'
        c=match_products(demo_analysis(TEXT),[p])['candidates'][0]
        self.assertEqual(c['status'],'conflict')
        self.assertIn('电流冲突',c['conflicts'][0])
    def test_inference_not_successfully_matched(self):
        d=demo_analysis(TEXT)
        next(r for r in d['requirements'] if r['field']=='current_a')['kind']='inferred'
        c=match_products(d,[PRODUCT])['candidates'][0]
        self.assertTrue(any('电流缺失' in x for x in c['pending']))
    def test_unit_conversion_cannot_assume_pack_size(self):
        p=dict(PRODUCT,unit='box',moq='1')
        c=match_products(demo_analysis(TEXT),[p])['candidates'][0]
        self.assertTrue(any('包装换算' in x for x in c['pending']))
        self.assertFalse(any('最小起订量' in x for x in c['reasons']))
    def test_moq_decimal_and_quantity_thousands(self):
        p=dict(PRODUCT,moq='1000.01')
        c=match_products(demo_analysis(TEXT),[p])['candidates'][0]
        self.assertEqual(c['status'],'conflict')
        self.assertTrue(any('低于' in x for x in c['conflicts']))
        self.assertEqual(scalar('quantity','1,000'),1000)
    def test_empty_and_all_conflicts_clear(self):
        self.assertIn('为空',match_products(demo_analysis(TEXT),[])['message'])
        p=copy.deepcopy(PRODUCT);p['specs']['poles']='3'
        self.assertIn('暂无合适产品',match_products(demo_analysis(TEXT),[p])['message'])
    def test_range_pending_even_with_same_model(self):
        p=copy.deepcopy(PRODUCT);p['specs']['current_a']='16-32'
        c=match_products(demo_analysis(TEXT),[p])['candidates'][0]
        self.assertEqual(c['status'],'pending')
        self.assertTrue(any('范围' in x for x in c['pending']))
    def test_ac_dc_unknown_still_rejects_known_numeric_conflict(self):
        d=demo_analysis(TEXT.replace('230 V','230 V AC'))
        p=copy.deepcopy(PRODUCT);p['specs']['voltage_v']='400'
        c=match_products(d,[p])['candidates'][0]
        self.assertEqual(c['status'],'conflict')
        self.assertTrue(any('电压数值冲突' in x for x in c['conflicts']))
        p['specs']['voltage_v']='230'
        c=match_products(d,[p])['candidates'][0]
        self.assertEqual(c['status'],'pending')
        self.assertFalse(c['conflicts'])
    def test_known_ac_dc_conflict_is_not_pending(self):
        d=demo_analysis(TEXT.replace('230 V','230 V AC'))
        p=copy.deepcopy(PRODUCT);p['specs']['voltage_v']='230 V DC'
        c=match_products(d,[p])['candidates'][0]
        self.assertEqual(c['status'],'conflict')
        self.assertTrue(any('交直流' in x for x in c['conflicts']))
    def test_explicit_other_category_cannot_be_substituted(self):
        d=demo_analysis(TEXT)
        next(r for r in d['requirements'] if r['field']=='product').update(value='MCCB',evidence='MCCB')
        c=match_products(d,[PRODUCT])['candidates'][0]
        self.assertEqual(c['status'],'conflict')
    def test_missing_information_never_success(self):
        d=demo_analysis('Please quote MCB.')
        c=match_products(d,[PRODUCT])['candidates'][0]
        self.assertEqual(c['status'],'pending')
        self.assertGreater(len(c['pending']),5)

class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict('os.environ',CONFIG,clear=True);self.env.start()
        self.file=patch('app.ai._file_config',return_value={});self.file.start()
        self.addCleanup(self.env.stop);self.addCleanup(self.file.stop)
    def test_public_configuration_has_no_key(self):
        cfg=get_config()
        self.assertTrue(cfg['configured']);self.assertNotIn('key',cfg)
        self.assertNotIn('secret-test-key',json.dumps(cfg))
    def test_missing_key_leaves_manual_available(self):
        with patch.dict('os.environ',{'AI_API_KEY':''}):
            self.assertFalse(get_config()['configured'])
            with self.assertRaisesRegex(AIError,'未接入'):analyze(TEXT)
            self.assertEqual(demo_analysis(TEXT)['mode'],'demo')
    def test_openai_payload_and_text_only(self):
        with patch('app.ai._post',return_value=response(data())) as post:
            out=analyze(TEXT)
        self.assertEqual(out['mode'],'live')
        args=post.call_args.args
        self.assertEqual(args[0],'https://api.openai.com/v1/responses')
        self.assertFalse(args[1]['store'])
        self.assertTrue(args[1]['text']['format']['strict'])
        self.assertEqual(args[1]['input'][-1]['content'],TEXT)
        self.assertNotIn('purchase_price',json.dumps(args[1]))
    def test_compatible_json_mode(self):
        body={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(data())}}]}
        with patch.dict('os.environ',{'AI_PROVIDER':'compatible','AI_BASE_URL':'https://api.deepseek.com'}),patch('app.ai._post',return_value=body) as post:
            self.assertEqual(analyze(TEXT)['mode'],'live')
        self.assertEqual(post.call_args.args[0],'https://api.deepseek.com/chat/completions')
        self.assertEqual(post.call_args.args[1]['response_format'],{'type':'json_object'})
    def test_malformed_and_incomplete_model_responses(self):
        for body in ({}, {'status':'incomplete','output':[]},response(['not an object']),response({'requirements':'bad'})):
            with self.subTest(body=body),patch('app.ai._post',return_value=body):
                with self.assertRaises(AIError):analyze(TEXT)
    def test_unsupported_evidence_failure_safe(self):
        d=data();d['requirements'][0]['value']='imagined-product'
        with patch('app.ai._post',return_value=response(d)):
            with self.assertRaisesRegex(AIError,'证据'):analyze(TEXT)
        self.assertEqual(TEXT,'Please quote 1,000 pcs MCB, 2P, 16 A, 230 V, C curve, 6 kA, delivery within 30 days.')
    def test_timeout_and_provider_error_redacted(self):
        for exc in (socket.timeout('secret-test-key'),error.URLError('secret-test-key'),error.HTTPError('https://x',401,'secret-test-key',{},None)):
            with patch('app.ai.request.build_opener') as build:
                build.return_value.open.side_effect=exc
                with self.assertRaises(AIError) as cm:_post('https://api.openai.com/v1/responses',{},'secret-test-key',10)
                self.assertNotIn('secret-test-key',str(cm.exception))
    def test_base_url_cannot_expose_key_in_query(self):
        with patch.dict('os.environ',{'AI_BASE_URL':'https://api.openai.com/v1?key=secret-test-key'}):
            cfg=get_config()
            self.assertFalse(cfg['configured']);self.assertNotIn('secret-test-key',json.dumps(cfg))
    def test_remote_http_disallowed(self):
        with patch.dict('os.environ',{'AI_PROVIDER':'compatible','AI_BASE_URL':'http://example.com'}):
            with self.assertRaisesRegex(AIError,'HTTPS'):analyze(TEXT)

if __name__=='__main__':unittest.main()
