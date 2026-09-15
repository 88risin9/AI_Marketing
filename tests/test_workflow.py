import unittest
import io
import json
from tempfile import TemporaryDirectory
from unittest.mock import patch
from datetime import date,timedelta
from app.server import create_app
from app.products import template_bytes,example_products
from app.ai import demo_analysis,AIError

TEXT='Please quote 1000 pcs MCB, 1P, 16 A, 230 V AC, C curve, 6 kA. Delivery within 30 days.'
class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.app=create_app(self.tmp.name);self.c=self.app.test_client();self.headers={'X-Local-Request':'1'};self.ws='demo'
    def tearDown(self):self.tmp.cleanup()
    def api(self,path,data=None,method='post',expected=200):
        fn=getattr(self.c,method);kwargs={'headers':self.headers}
        if data is not None:kwargs['json']=data
        r=fn('/api/'+path+'?workspace='+self.ws,**kwargs)
        self.assertEqual(r.status_code,expected,r.get_data(as_text=True));return r.json
    def prepare(self):
        c=self.api('customers',{'name':'Workflow Fixture','country':'UK','contact':'fixture@example.com','source':'Test','notes':'fictional'})
        i=self.api('inquiries',{'customer_id':c['id'],'title':'Acceptance fixture','original_text':TEXT})
        i=self.api(f'inquiries/{i["id"]}/analyze',{'mode':'demo'})
        self.assertIn('演示',str(i['analysis']['warnings']))
        m=self.api(f'inquiries/{i["id"]}/matches',method='get');self.assertEqual(m['candidates'][0]['product_id'],1)
        self.assertEqual(next(x for x in m['candidates'] if x['product_id']==2)['status'],'conflict')
        i=self.api(f'inquiries/{i["id"]}/review',{'analysis':i['analysis'],'selected_product_ids':[1],'selection_note':'Fictional test only: confirm AC specification and 30 day delivery manually.'})
        p={'inquiry_id':i['id'],'currency':'USD','lead_time':'30 days after order confirmation','valid_until':(date.today()+timedelta(days=30)).isoformat(),'payment_terms':'T/T in advance','trade_terms':'FOB Shanghai','other_costs':'20','internal_note':'DO_NOT_EXPORT_INTERNAL','lines':[{'product_id':1,'quantity':'1000','unit_price':'2.5','fx_rate':'0.14'}]}
        return i,p
    def test_full_flow_version_history_backup_restart(self):
        i,p=self.prepare();q=self.api('quotes',p);self.assertEqual(q['snapshot']['total'],'2500.00');self.assertEqual(q['snapshot']['estimated_profit'],'1290.00')
        self.assertEqual(q['status'],'draft');self.assertEqual(q['validation_errors'],[])
        draft=self.c.get(f'/api/quotes/{q["id"]}/pdf?workspace=demo');self.assertEqual(draft.status_code,200)
        q=self.api(f'quotes/{q["id"]}/approve',{});self.assertEqual(q['status'],'approved')
        pdf=self.c.get(f'/api/quotes/{q["id"]}/pdf?workspace=demo');self.assertEqual(pdf.mimetype,'application/pdf')
        from pypdf import PdfReader
        text=''.join(x.extract_text() for x in PdfReader(io.BytesIO(pdf.data)).pages)
        self.assertNotIn('DO_NOT_EXPORT_INTERNAL',text);self.assertNotIn('1190.00',text);self.assertNotIn('DRAFT',text);self.assertIn('2500.00',text)
        self.api(f'quotes/{q["id"]}/sent',{'sent':True})
        self.api(f'inquiries/{i["id"]}/followups',{'note':'Customer acknowledged quotation (fictional).','status':'跟进中','next_followup':(date.today()+timedelta(days=3)).isoformat(),'reason':''})
        p['base_quote_id']=q['id'];p['lines'][0]['unit_price']='2.75';q2=self.api('quotes',p);self.assertEqual(q2['version'],2);self.assertEqual(q2['status'],'draft')
        before_pdf=pdf.data
        self.api('products/1',{'purchase_price':'999','model':'MODIFIED-MODEL'},method='put')
        old=self.api('state',method='get')['quotes'];old=next(x for x in old if x['id']==q['id']);self.assertEqual(old['snapshot']['total'],'2500.00');self.assertEqual(old['snapshot']['lines'][0]['purchase_price'],'8.50')
        self.api(f'quotes/{q2["id"]}/approve',{},expected=400)
        backup=self.c.get('/api/backup?workspace=demo').data
        self.assertNotIn(b'AI_API_KEY',backup)
        self.api('company',{'name':'Changed name'})
        restore=self.c.post('/api/restore?workspace=demo',data={'file':(io.BytesIO(backup),'backup.json')},headers=self.headers)
        self.assertEqual(restore.status_code,200,restore.json)
        reopened=create_app(self.tmp.name).test_client().get('/api/state?workspace=demo').json
        self.assertEqual(len(reopened['quotes']),2);self.assertEqual(next(x for x in reopened['inquiries'] if x['id']==i['id'])['followups'][0]['note'],'Customer acknowledged quotation (fictional).')
        self.assertEqual(self.c.get('/api/state?workspace=live').json['inquiries'],[])
    def test_missing_information_and_conflict_cannot_be_approved(self):
        i,p=self.prepare()
        self.api(f'inquiries/{i["id"]}/review',{'analysis':i['analysis'],'selected_product_ids':[2],'selection_note':'ignore'},expected=400)
        p['payment_terms']='';q=self.api('quotes',p);self.api(f'quotes/{q["id"]}/approve',{},expected=400)
        self.api(f'quotes/{q["id"]}/sent',{'sent':True},expected=400)
    def test_ai_failure_preserves_original_and_existing_analysis(self):
        i,p=self.prepare()
        with patch('app.ai.analyze',side_effect=AIError('AI 处理超时，原始资料已保留。')):
            self.api(f'inquiries/{i["id"]}/analyze',{'mode':'live'},expected=503)
        state=self.api('state',method='get');after=next(x for x in state['inquiries'] if x['id']==i['id']);self.assertEqual(after['original_text'],TEXT);self.assertEqual(after['analysis'],i['analysis']);self.assertTrue(any(x['kind']=='ai_failure' for x in state['activities']))
    def test_import_is_atomic_and_identifiers_unchanged(self):
        rows=example_products();rows[0]['model']='0001';rows[0]['unit']=' pcs '
        result=self.api('import/commit',{'rows':rows});self.assertEqual(result['products'][0]['model'],'0001');self.assertEqual(result['products'][0]['unit'],' pcs ')
        count=len(self.api('state',method='get')['products']);rows[1]['purchase_price']='-1';self.api('import/commit',{'rows':rows},expected=400)
        self.assertEqual(len(self.api('state',method='get')['products']),count)
    def test_manual_without_key(self):
        self.ws='live';c=self.api('customers',{'name':'Manual Buyer'});i=self.api('inquiries',{'customer_id':c['id'],'original_text':'Please quote MCB.'})
        self.api(f'inquiries/{i["id"]}',{'analysis':{'requirements':[],'reply_draft':'','questions':[],'warnings':[]}},method='put')
        match=self.api(f'inquiries/{i["id"]}/matches',method='get');self.assertEqual(match['candidates'],[])
        self.api(f'inquiries/{i["id"]}/analyze',{'mode':'demo'},expected=400)
    def test_bad_backup_rejected_without_data_loss(self):
        state=self.api('state',method='get');b=json.loads(self.c.get('/api/backup?workspace=demo').data);b['records']['inquiries'][0].pop('original_text')
        r=self.c.post('/api/restore?workspace=demo',data={'file':(io.BytesIO(json.dumps(b).encode()),'bad.json')},headers=self.headers);self.assertEqual(r.status_code,400)
        self.assertEqual(self.api('state',method='get')['inquiries'],state['inquiries'])
    def test_corrupted_quote_backup_rejected(self):
        i,p=self.prepare();q=self.api('quotes',p);self.api(f'quotes/{q["id"]}/approve',{})
        original=json.loads(self.c.get('/api/backup?workspace=demo').data)
        for key,value in [('total','1.00'),('payment_terms',''),('valid_until','')]:
            bad=json.loads(json.dumps(original));bad['records']['quotes'][0]['snapshot'][key]=value
            r=self.c.post('/api/restore?workspace=demo',data={'file':(io.BytesIO(json.dumps(bad).encode()),'bad.json')},headers=self.headers)
            self.assertEqual(r.status_code,400,r.json)
            self.assertEqual(self.api('state',method='get')['quotes'][0]['snapshot']['total'],'2500.00')
    def test_cross_origin_and_header_guard(self):
        r=self.c.post('/api/company',json={'name':'x'});self.assertEqual(r.status_code,403)
        r=self.c.post('/api/company',json={'name':'x'},headers={**self.headers,'Origin':'https://evil.example'});self.assertEqual(r.status_code,403)
        r=self.c.get('/api/state',headers={'Host':'evil.example'});self.assertEqual(r.status_code,403)
if __name__=='__main__':unittest.main()
