import unittest
from copy import deepcopy
from tempfile import TemporaryDirectory
from pathlib import Path

from app.products import validate_product, parse_import
from app.matching import match_products
from app.server import create_app
from app.backup_validation import validate_bundle


class CatalogLibraryTests(unittest.TestCase):
    def test_unknown_subcategory_is_not_invented_as_mcb(self):
        p=validate_product({'specs':{'product_family':'充电桩'}})
        self.assertEqual(p['category'],'')
        self.assertIn('分类',p['missing'])

    def test_optional_catalog_import_columns_preserve_metadata(self):
        content='型号,分类,产品大类,资料类型,目录摘要,待确认事项\nEV-1,交流充电桩,充电桩,catalog_series,7 / 11 kW,价格待确认\n'.encode('utf-8-sig')
        result=parse_import('catalog.csv',content)
        self.assertFalse(result['errors'])
        p=result['rows'][0]['product']
        self.assertEqual(p['specs']['product_family'],'充电桩')
        self.assertEqual(p['specs']['record_type'],'catalog_series')
        self.assertEqual(p['specs']['review_notes'],'价格待确认')
        self.assertEqual(p['purchase_price'],'')

    def test_unpriced_catalog_saves_and_price_can_be_added_then_cleared(self):
        with TemporaryDirectory() as tmp:
            app=create_app(Path(tmp)/'data');client=app.test_client()
            headers={'X-Local-Request':'1'}
            original={'supplier':'Catalog supplier','model':'EV-SERIES','name':'交流充电桩',
                'name_en':'AC EV charger','category':'交流充电桩','unit':'','purchase_price':'',
                'currency':'','source':'Supplier catalog, PDF page 4','updated_at':'',
                'specs':{'product_family':'充电桩','record_type':'catalog_series',
                    'source_specification':'7 / 11 / 22 kW options',
                    'review_notes':'具体配置及价格待确认'}}
            response=client.post('/api/products?workspace=demo',json=original,headers=headers)
            self.assertEqual(response.status_code,200)
            saved=response.json
            self.assertEqual(saved['purchase_price'],'')
            self.assertEqual(saved['currency'],'')
            self.assertIn('采购价',saved['missing'])
            self.assertEqual(saved['specs'],original['specs'])
            url=f'/api/products/{saved["id"]}?workspace=demo'
            # Synthetic test value, not an actual catalog price.
            edited=client.put(url,json={'purchase_price':'123.456','currency':'USD','source_updated_at':''},headers=headers)
            self.assertEqual(edited.status_code,200)
            self.assertEqual(edited.json['purchase_price'],'123.456')
            self.assertEqual(edited.json['specs'],original['specs'])
            cleared=client.put(url,json={'purchase_price':'','currency':''},headers=headers)
            self.assertEqual(cleared.json['purchase_price'],'')
            bundle=app.config['STORES']['demo'].export('demo');validate_bundle(bundle)
            self.assertEqual(bundle['records']['products'][-1]['specs'],original['specs'])
            app.extensions['research_manager'].stop()

    def test_catalog_series_never_becomes_fully_matched_single_variant(self):
        p={'id':1,'model':'MCB series','category':'MCB','unit':'pc','moq':'1',
           'source':'Catalog p4','specs':{'poles':'1P','current_a':'16','voltage_v':'230',
              'curve':'C','breaking_ka':'6','record_type':'catalog_series'}}
        analysis={'requirements':[{'field':k,'value':v,'evidence':v,'kind':'stated'}
            for k,v in {'product':'MCB','quantity':'100','unit':'pc','poles':'1P',
                        'current_a':'16','voltage_v':'230','curve':'C','breaking_ka':'6','delivery':'30 days'}.items()]}
        result=match_products(analysis,[p])['candidates'][0]
        self.assertNotEqual(result['status'],'eligible')
        self.assertTrue(any('目录系列' in note for note in result['pending']))
        self.assertFalse(result['conflicts'])

    def test_all_four_families_keep_unknown_price_distinct_from_zero(self):
        for family in ['低压电器','充电桩','中压电器','输配电']:
            p=validate_product({'category':'目录子类','purchase_price':'',
                'currency':'','specs':{'product_family':family,'record_type':'catalog_series'}})
            self.assertEqual(p['purchase_price'],'')
            self.assertEqual(p['specs']['product_family'],family)
            zero=deepcopy(p);zero['purchase_price']='0'
            self.assertEqual(validate_product(zero)['purchase_price'],'0')


if __name__=='__main__':unittest.main()
