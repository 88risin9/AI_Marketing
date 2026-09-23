"""Exercise V1/V2 backups and the isolated buyer service together, without paid calls."""
import copy
import io
import json
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch
from app.server import create_app
from app.portal import create_buyer_app


class V2IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory()
        self.app=create_app(self.tmp.name)
        self.c=self.app.test_client()
        self.h={'X-Local-Request':'1'}

    def tearDown(self):
        self.app.extensions['research_manager'].stop()
        self.tmp.cleanup()

    def api(self,path,data=None,method='post'):
        response=getattr(self.c,method)('/api/'+path+'?workspace=demo',json=data,headers=self.h)
        self.assertEqual(response.status_code,200,response.json)
        return response.json

    def restore(self,bundle):
        return self.c.post('/api/restore?workspace=demo',headers=self.h,
            data={'file':(io.BytesIO(json.dumps(bundle).encode()),'backup.json')})

    def prepare(self):
        task=self.api('research/tasks',{'title':'Fictional bounded test','market':'United Kingdom',
            'test_market':True,'buyer_types':['distributor'],'product_ids':[1],
            'target_count':1,'search_limit':0,'model_limit':0,'page_limit':0})
        prospect=self.api(f'research/tasks/{task["id"]}/manual',{'name':'Fictional Fixture',
            'website':'https://fixture.example/','source_text':'MCBs are listed (fictional test).',
            'source_url':'https://fixture.example/catalogue'})
        self.api(f'research/prospects/{prospect["id"]}',{'review_status':'accepted',
            'review_reason':'Fictional fixture only.'},'patch')
        material=self.api(f'research/prospects/{prospect["id"]}/materials',{'mode':'manual'})
        self.api(f'research/materials/{material["id"]}',{'reviewed':True},'put')
        page=self.api('buyer-pages',{'title':'Fictional catalogue','product_ids':[1],
            'buyer_type':'Distributors','prospect_id':prospect['id']})
        page=self.api(f'buyer-pages/{page["id"]}/approve',{'confirmed_public':True,
            'expected_updated_at':page['updated_at']})
        return task,prospect,material,page

    def test_complete_v2_backup_restore_and_independent_portal_reopen(self):
        task,prospect,material,page=self.prepare()
        original=self.api('backup',method='get')
        self.assertEqual(original['schema_version'],2)
        self.api(f'buyer-pages/{page["id"]}',{'intro':'Changed and awaiting approval'},'put')
        self.api(f'research/tasks/{task["id"]}/cancel',{})
        restored=self.restore(original)
        self.assertEqual(restored.status_code,200,restored.json)
        final=self.api('backup',method='get')
        self.assertEqual(final['records'],original['records'])
        public=create_buyer_app(self.tmp.name).test_client()
        response=public.get(f'/api/public/pages/{page["token"]}')
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['page']['title'],'Fictional catalogue')
        self.assertNotIn('purchase_price',json.dumps(response.json))
        self.assertEqual(public.get('/api/research/state').status_code,404)
        self.assertEqual(self.api('research/state',method='get')['stats']['contacted'],0)
        self.assertEqual(len(self.api('state',method='get')['inquiries']),1)

    def test_reject_corrupted_public_snapshot_and_workspace_label_atomically(self):
        self.prepare()
        original=self.api('backup',method='get')
        for change in ('cost','workspace','budget'):
            bundle=copy.deepcopy(original)
            page=bundle['records']['buyer_pages'][0]
            if change=='cost':page['public_snapshot']['products'][0]['purchase_price']='SECRET'
            elif change=='workspace':
                page['draft_snapshot']['demo']=False
                page['public_snapshot']['demo']=False
                page['versions'][0]['snapshot']['demo']=False
            else:bundle['records']['research_tasks'][0]['usage']['model']=100
            response=self.restore(bundle)
            self.assertEqual(response.status_code,400,response.json)
            self.assertEqual(self.api('backup',method='get')['records'],original['records'])

    def test_running_research_prevents_restore(self):
        self.prepare()
        original=self.api('backup',method='get')
        with patch.object(self.app.extensions['research_manager'],'has_running',return_value=True):
            response=self.restore(original)
        self.assertEqual(response.status_code,400,response.json)
        self.assertIn('仍在执行',response.json['error'])
        self.assertEqual(self.api('backup',method='get')['records'],original['records'])


if __name__=='__main__':unittest.main()
