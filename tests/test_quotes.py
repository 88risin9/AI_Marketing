import unittest
from copy import deepcopy
from datetime import date,timedelta
from app.quotes import create_snapshot, customer_view, approval_errors, review_fingerprint
from app.pdf_export import pdf_bytes, html_preview

class QuotesTest(unittest.TestCase):
    def setUp(self):
        self.p={1:{'id':1,'model':'001-MCB','name_en':'Miniature circuit breaker','specs':{'poles':'2'},'unit':'pcs','purchase_price':'12.50','currency':'CNY','moq':'10','source':'SECRET_SUPPLIER_FILE','supplier':'SECRET_SUPPLIER'}}
        self.i={'analysis_reviewed':True,'selected_product_ids':[1],'analysis':{'requirements':[]},'original_text':'inquiry'}
        self.payload={'currency':'USD','lead_time':'30 days','valid_until':(date.today()+timedelta(days=30)).isoformat(),'payment_terms':'T/T','trade_terms':'FOB Shanghai','other_costs':'10','internal_note':'SECRET_INTERNAL','lines':[{'product_id':1,'quantity':'100','unit_price':'3.255','fx_rate':'0.14'}]}
    def snap(self):return create_snapshot(self.payload,self.i,self.p,{'name':'Customer'},{'name':'Seller'})
    def test_decimal_rounding_fx_and_profit(self):
        s,e=self.snap();self.assertEqual(e,[]);self.assertEqual(s['total'],'325.50');self.assertEqual(s['lines'][0]['cost_total'],'175.00');self.assertEqual(s['estimated_profit'],'140.50');self.assertEqual(s['margin_pct'],'43.16')
    def test_jpy_rounding(self):
        self.payload.update(currency='JPY',other_costs='0');self.payload['lines'][0].update(quantity='11',unit_price='3.5');s,e=self.snap();self.assertEqual(s['total'],'39')
    def test_missing_cost_no_fabricated_profit(self):
        self.payload['lines'][0]['fx_rate']='';s,e=self.snap();self.assertIsNone(s['estimated_profit']);self.assertTrue(s['cost_warnings'])
    def test_incomplete_draft_and_invalid_approval(self):
        self.payload.update(payment_terms='',currency='');s,e=self.snap();self.assertTrue(e)
    def test_unit_mismatch_blocked(self):
        self.payload['lines'][0]['unit']='boxes'
        with self.assertRaises(ValueError):self.snap()
    def test_nan_negative_and_zero_quantity_blocked(self):
        for value in ['NaN','Infinity','-1','0']:
            self.payload['lines'][0]['quantity']=value
            with self.assertRaises(ValueError):self.snap()
    def test_moq(self):
        self.payload['lines'][0]['quantity']='2';s,e=self.snap();self.assertTrue(any('起订量' in x for x in e))
    def test_customer_artifacts_allowlist_and_history(self):
        s,e=self.snap();q={'snapshot':deepcopy(s),'number':'QT-TEST','version':1,'status':'draft','created_at':'2026-09-15','demo':True}
        self.p[1]['purchase_price']='999';self.p[1]['model']='CHANGED'
        self.assertEqual(q['snapshot']['lines'][0]['model'],'001-MCB');public=customer_view(q)
        text=html_preview(public)
        self.assertNotIn('SECRET_',text);self.assertNotIn('purchase_price',str(public));self.assertNotIn('cost_total',str(public));self.assertIn('DRAFT',text)
        data=pdf_bytes(public);self.assertTrue(data.startswith(b'%PDF'))
        from pypdf import PdfReader
        from io import BytesIO
        text=''.join(p.extract_text() for p in PdfReader(BytesIO(data)).pages)
        self.assertNotIn('SECRET_',text);self.assertNotIn('175.00',text);self.assertIn('325.50',text);self.assertIn('DRAFT',text)
    def test_internal_extension_specs_excluded(self):
        self.p[1]['specs']['internal_note']='SECRET_IN_EXTENDED_SPECS'
        s,e=self.snap();q={'snapshot':s,'number':'Q','version':1,'status':'draft','created_at':'2026-09-15'}
        self.assertNotIn('SECRET_IN_EXTENDED_SPECS',html_preview(customer_view(q)))
    def test_blank_model_cannot_be_approved(self):
        self.p[1]['model']='   ';s,e=self.snap();self.assertTrue(any('型号' in x for x in e))
    def test_bad_quote_row_is_clear_validation_error(self):
        self.payload['lines']=['bad row']
        with self.assertRaises(ValueError):self.snap()
    def test_review_change_invalidates_approval(self):
        s,e=self.snap();q={'snapshot':s,'validation_errors':e,'review_fingerprint':review_fingerprint(self.i)}
        self.assertFalse(approval_errors(q,self.i));self.i['analysis_reviewed']=False;self.assertTrue(approval_errors(q,self.i))
if __name__=='__main__':unittest.main()
